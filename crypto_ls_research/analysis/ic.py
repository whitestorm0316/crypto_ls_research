"""Rank-IC analysis: does the score actually rank future returns in the cross-section?

    IC_h(d) = Spearman( score_d , r_{d->d+h} )

Two conventions matter and are both implemented:

* `exec` convention (default) -- forward return is measured from the *executable* price
  of bar d+1, because that is when a signal generated at the close of bar d can be
  filled.  This is the only IC that is honest about the execution delay.
* `close` convention -- forward return measured from close(d).  Reported for
  comparison only; it is NOT tradeable and overstates IC.

Because the universe at d is the PIT pool, IC is computed only over names that were
actually eligible.
"""
from __future__ import annotations

from typing import Dict, Iterable, Sequence

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResult, pick_exec_price
from ..data.store import Panels, panels_to_arrays
from ..factors.engine import FACTOR_NAMES


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 8:
        return np.nan
    x, y = a[ok], b[ok]
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() < 1e-12 or ry.std() < 1e-12:
        return np.nan
    return float(np.corrcoef(rx, ry)[0, 1])


_PANEL_FIELDS = ("open", "high", "low", "close", "vol", "vol_ccy", "amount", "funding")


def align_panels(panels: Panels, insts: Sequence[str]) -> Panels:
    """Reindex `panels` so its instrument axis matches `insts` exactly.

    A backtest result carries per-instrument matrices built from whatever the
    candle cache held at the time.  If the cache changes afterwards -- e.g. a
    download finishes mid-run and adds instruments -- a freshly loaded panel has a
    different width, and any positional use of `result.score_matrix` against it
    silently mis-aligns columns.  IC analysis therefore aligns on instrument
    IDENTITY and fails loudly when a required name has disappeared.
    """
    want = [str(i) for i in insts]
    have = list(panels.insts)
    if len(set(have)) != len(have):
        dups = sorted({c for c in have if have.count(c) > 1})
        raise ValueError(
            f"panel has duplicate instrument columns ({dups[:5]}); alignment by name is "
            f"ambiguous. Label-based selection would silently return extra columns."
        )
    missing = [i for i in want if i not in have]
    if missing:
        raise ValueError(
            f"panels are missing {len(missing)} instrument(s) required by the backtest "
            f"result, e.g. {missing[:5]}. The candle cache changed since the baseline "
            f"ran -- re-run the baseline stage before the analysis stages."
        )
    kw = {f: getattr(panels, f).loc[:, want] for f in _PANEL_FIELDS}
    # Belt and braces: label selection is only trustworthy if the width matches.
    for f, v in kw.items():
        if v.shape[1] != len(want):
            raise ValueError(f"align_panels: field '{f}' has {v.shape[1]} columns after "
                             f"selection, expected {len(want)}.")
    return Panels(list_dt=panels.list_dt.reindex(want), **kw)


def forward_returns(panels: Panels, cfg, convention: str = "exec") -> np.ndarray:
    arr = panels_to_arrays(panels)
    if convention == "exec":
        px = pick_exec_price(arr, cfg.execution.exec_price).astype("float64")
    else:
        px = arr["close"].astype("float64")
    return px


def rank_ic_over_horizons(result: BacktestResult, panels: Panels,
                          horizons_bars: Sequence[int],
                          matrix: np.ndarray | None = None,
                          convention: str = "exec") -> pd.DataFrame:
    """IC series + summary for one score/factor matrix across several horizons."""
    cfg = result.cfg
    panels = align_panels(panels, result.insts)
    px = forward_returns(panels, cfg, convention)
    T, N = px.shape
    dec_idx = np.searchsorted(panels.index, result.reb_ts)
    score = result.score_matrix if matrix is None else matrix
    if score.shape[1] != N:
        raise ValueError(f"score matrix width {score.shape[1]} != panel width {N}; "
                         f"the score was built on a different instrument set.")
    shift = 1 if convention == "exec" else 0

    rows = []
    series: Dict[int, pd.Series] = {}
    positions: Dict[int, List[int]] = {}
    for h in horizons_bars:
        ics = np.full(len(dec_idx), np.nan)
        for i, d in enumerate(dec_idx):
            a = int(d) + shift
            b = int(d) + shift + int(h)
            if b >= T or a >= T:
                continue
            fwd = px[b] / px[a] - 1.0
            fwd[~np.isfinite(fwd)] = np.nan
            ics[i] = _spearman(score[i], fwd)
        s = pd.Series(ics, index=result.reb_ts).dropna()
        series[int(h)] = s
        # Where the surviving points sit in `result.reb_ts`.  `ic_series` needs this to
        # put them back on the right timestamps: NaN arises both at the END (no room for
        # the horizon) and in the MIDDLE (early universes with fewer than 8 eligible
        # names), so "the i-th surviving value belongs to the i-th timestamp" is wrong.
        # Measured on v5_1d_all5: 152 mid-sample NaNs; the shift reached 1.66 in IC.
        positions[int(h)] = [int(i) for i in np.flatnonzero(np.isfinite(ics))]
        mu, sd = (float(s.mean()), float(s.std(ddof=1))) if len(s) > 2 else (np.nan, np.nan)
        icir = mu / sd if sd and np.isfinite(sd) and sd > 0 else np.nan
        t = icir * np.sqrt(len(s)) if np.isfinite(icir) else np.nan
        rows.append({
            "horizon_bars": int(h), "horizon_days": h / cfg.bars_per_day,
            "n": len(s), "IC_mean": mu, "IC_std": sd, "ICIR": icir,
            "t_stat": t,
            "p_value": float(2 * (1 - _norm_cdf(abs(t)))) if np.isfinite(t) else np.nan,
            "IC>0 share": float((s > 0).mean()),
        })
    out = pd.DataFrame(rows)
    # keep the IC time series for plotting, but as JSON-safe lists: pandas compares
    # `attrs` on concat and Series values raise "truth value is ambiguous".
    out.attrs["series"] = {int(h): [float(x) for x in s.to_numpy()] for h, s in series.items()}
    out.attrs["series_pos"] = {int(h): list(p) for h, p in positions.items()}
    out.attrs["series_index"] = [str(t) for t in result.reb_ts]
    return out


def ic_series(ic_tab: pd.DataFrame, horizon_bars: int) -> pd.Series:
    """The per-rebalance IC series, on its own timestamps.

    Uses the recorded positions when present so that mid-sample NaNs (early universes
    too thin to rank) do not shift every later value onto the wrong date.
    """
    s = ic_tab.attrs.get("series", {}).get(int(horizon_bars))
    if not s:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(ic_tab.attrs.get("series_index", []), utc=True)
    pos = ic_tab.attrs.get("series_pos", {}).get(int(horizon_bars))
    if pos is not None and len(pos) == len(s) and len(idx):
        return pd.Series(s, index=idx[np.asarray(pos, dtype=int)])
    return pd.Series(s, index=idx[: len(s)])


def _norm_cdf(z: float) -> float:
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def ic_by_factor(result: BacktestResult, panels: Panels, horizons_bars: Sequence[int],
                 convention: str = "exec") -> pd.DataFrame:
    frames = []
    for k in FACTOR_NAMES:
        m = result.factor_zs[k].astype("float64")
        m[~result.mask_matrix] = np.nan
        df = rank_ic_over_horizons(result, panels, horizons_bars, matrix=m,
                                   convention=convention)
        df.insert(0, "factor", k)
        frames.append(df.drop(columns=["p_value"]))
    df = rank_ic_over_horizons(result, panels, horizons_bars, convention=convention)
    df.insert(0, "factor", "SCORE")
    frames.append(df.drop(columns=["p_value"]))
    for f in frames:
        f.attrs = {}
    return pd.concat(frames, ignore_index=True)


def ic_decay(result: BacktestResult, panels: Panels, max_days: float = 10.0,
             n_points: int = 25, matrix: np.ndarray | None = None) -> pd.DataFrame:
    max_bars = max(1, int(round(max_days * result.cfg.bars_per_day)))
    hs = np.unique(np.round(np.linspace(1, max_bars, n_points)).astype(int))
    df = rank_ic_over_horizons(result, panels, list(hs), matrix=matrix)
    return df[["horizon_bars", "horizon_days", "IC_mean", "ICIR", "t_stat"]]


def factor_correlation(result: BacktestResult) -> pd.DataFrame:
    """Cross-sectional correlation between factors, averaged over rebalances.

    Answers "is Flow just another Momentum?" -- but note this is the *unconditional*
    correlation; the marginal-information question is answered by the ablation
    backtests in `attribution.py`.
    """
    ms = {k: result.factor_zs[k].astype("float64").copy() for k in FACTOR_NAMES}
    out = pd.DataFrame(index=list(FACTOR_NAMES), columns=list(FACTOR_NAMES), dtype=float)
    D = len(result.reb_ts)
    acc = {(a, b): [] for a in FACTOR_NAMES for b in FACTOR_NAMES}
    for i in range(D):
        row = {}
        for k in FACTOR_NAMES:
            v = ms[k][i]
            v[~result.mask_matrix[i]] = np.nan
            row[k] = v
        for a in FACTOR_NAMES:
            for b in FACTOR_NAMES:
                if a >= b:
                    continue
                c = _spearman(row[a], row[b])
                if np.isfinite(c):
                    acc[(a, b)].append(c)
    for a in FACTOR_NAMES:
        out.loc[a, a] = 1.0
        for b in FACTOR_NAMES:
            if a < b and acc[(a, b)]:
                m = float(np.mean(acc[(a, b)]))
                out.loc[a, b] = m
                out.loc[b, a] = m
    return out
