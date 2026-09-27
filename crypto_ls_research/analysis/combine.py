"""Blend two (or more) independently-backtested books into one P&L stream.

Why this lives outside the engine
---------------------------------
The rank-IC study finds the *same* cross-section to be a trend signal at 7d and a
reversal signal at ~4h.  Those are two different bets on two different horizons,
so the clean way to test them is two separate books -- each with its own
rebalance clock -- and then ask whether the *combination* is better than either
leg alone.  Forcing both into one 3d book would average the horizons together and
destroy the very effect being measured.

Everything here is a pure function of the two return series, so it carries no new
look-ahead risk beyond what the two backtests already have:

* the blend weight is applied to a **trailing** volatility ratio, never to a
  full-sample one;
* the weight grid is reported in full, so a "peak" that only exists at one weight
  is visible as the spike it is.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from .metrics import compute_metrics

DEFAULT_WEIGHTS = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 1.0)


def align_returns(a: pd.Series, b: pd.Series) -> pd.DataFrame:
    """Inner-join two return series on timestamp (never on position)."""
    df = pd.DataFrame({"a": a, "b": b}).dropna(how="any")
    return df


def blend_ledger(net: pd.Series) -> pd.DataFrame:
    """Minimal `compute_metrics`-compatible ledger for a *blended* return stream.

    A blend of two already-costed books has no ledger of its own -- the cost,
    turnover and exposure decomposition belongs to each leg.  So:

    * `net_ret` and `equity` are real (equity is rebuilt from the blend, never
      sliced out of another book's curve);
    * `gross_ret = net_ret`, because the legs' costs are already inside the
      series and we refuse to invent a gross figure that double-counts them;
    * `fee`/`spread`/`impact`/`funding` are **0 by construction**, meaning "this
      blend adds no *incremental* cost" -- not "the strategy is costless";
    * everything that genuinely cannot be attributed at blend level (long/short
      split, turnover, all three exposure axes, participation) is **NaN**, so a
      metric depending on it returns `n/a` rather than a plausible-looking wrong
      number.  Filling zeros here would read as "long leg never wins".
    """
    v = net.to_numpy(dtype="float64")
    bars = pd.DataFrame(index=net.index)
    bars["net_ret"] = v
    bars["gross_ret"] = v
    bars["long_ret"] = np.nan
    bars["short_ret"] = np.nan
    bars["fee"] = 0.0
    bars["spread"] = 0.0
    bars["impact"] = 0.0
    bars["funding"] = 0.0
    bars["turnover"] = np.nan
    bars["gross_exposure"] = np.nan
    bars["net_exposure"] = np.nan
    bars["beta_exposure"] = np.nan
    bars["max_participation"] = np.nan
    bars["equity"] = (1.0 + v).cumprod()
    return bars


def trailing_vol_ratio(overlay: pd.Series, primary: pd.Series, window_days: float = 30.0,
                       bars_per_day: int = 24, floor: float = 0.2,
                       cap: float = 5.0) -> pd.Series:
    """Causal scalar that puts `overlay` on the same realised-vol scale as `primary`.

    Uses only bars up to and including t, so the scaling of bar t+1 cannot contain
    information from t+1.
    """
    w = max(5, int(round(window_days * bars_per_day)))
    va = overlay.rolling(w, min_periods=max(5, w // 4)).std(ddof=0)
    vb = primary.rolling(w, min_periods=max(5, w // 4)).std(ddof=0)
    ratio = (vb / va.replace(0.0, np.nan)).clip(lower=floor, upper=cap)
    return ratio.shift(1)


def blend_weight_metrics(net: pd.Series, w: float) -> Dict[str, float]:
    """`compute_metrics` for one blend weight, with the structurally-undefined
    fields forced to NaN.

    The blend has no long/short split of its own.  Left alone, the all-NaN legs
    make `(NaN > 0).mean()` evaluate to 0.0 and the report would claim the long
    leg never wins a single bar -- a fabricated statistic, not a missing one.
    """
    m = compute_metrics(blend_ledger(net), name=f"w={w}")
    for k in ("Long Win Rate (bar)", "Short Win Rate (bar)"):
        m[k] = np.nan
    return m


def combine_books(primary: pd.Series, overlay: pd.Series,
                  weights: Sequence[float] = DEFAULT_WEIGHTS,
                  risk_parity: bool = True, bars_per_day: int = 24,
                  vol_window_days: float = 30.0,
                  name_a: str = "trend", name_b: str = "reversal") -> Dict[str, object]:
    """Weighted blend table + the aligned inputs.

    `w` is the capital share of the overlay; the overlay return stream is first
    scaled by its trailing vol ratio so that `w` is a *risk* share, not a notional
    share.  A name is only added when the caller can see the whole grid, so the
    full table is returned rather than a single "best".
    """
    df = align_returns(primary, overlay)
    if len(df) < 100:
        raise ValueError(f"not enough overlapping bars to blend: {len(df)}")
    scale = trailing_vol_ratio(df["b"], df["a"], vol_window_days, bars_per_day) \
        if risk_parity else pd.Series(1.0, index=df.index)
    b_scaled = df["b"] * scale

    rows = []
    for w in weights:
        if not np.isfinite(w):
            continue
        net = (1.0 - w) * df["a"] + w * b_scaled.fillna(0.0)
        m = blend_weight_metrics(net, w)
        rows.append({
            "overlay_weight": float(w),
            "Sharpe": m["Sharpe"], "CAGR": m["CAGR"],
            "ann_vol": m["Annualized Volatility"], "max_dd": m["Max Drawdown"],
            "Sortino": m["Sortino"], "Calmar": m["Calmar"],
        })
    tbl = pd.DataFrame(rows)
    corr = float(np.corrcoef(df["a"], df["b"])[0, 1])
    return {"table": tbl, "corr": corr, "n_bars": len(df), "aligned": df,
            "b_scaled": b_scaled, "name_a": name_a, "name_b": name_b}


def blend_summary(res: Dict[str, object], baseline_label: str = "trend_only") -> pd.DataFrame:
    """Turn a `combine_books` result into a reportable table with the reference row."""
    t = res["table"].copy()
    t.insert(0, "book", [baseline_label if w == 0 else f"{res['name_a']}+{w:g}x{res['name_b']}"
                         for w in t["overlay_weight"]])
    return t


def best_weight_is_a_plateau(tbl: pd.DataFrame, metric: str = "Sharpe") -> Dict[str, object]:
    """Is the best blend weight surrounded by good neighbours?

    A single isolated peak among mediocre neighbours is the parameter-spike pattern
    the research brief warns about; a broad top is a plateau.  Returns the raw
    numbers so the judgement is auditable rather than asserted.

    **A boundary optimum is not a plateau.**  When the argmax sits at `w=0` or
    `w=1` the neighbourhood is one-sided, and a one-sided neighbourhood cannot
    tell a broad top apart from a monotone decline.  Reporting `plateau=True`
    there dresses up "reject the overlay entirely" as "a stable optimum" -- which
    is exactly what happened with every reversal blend (all five peaked at `w=0`).
    Such cases return `plateau=False` plus `at_boundary=True`.
    """
    v = tbl[metric].to_numpy(dtype="float64")
    w = tbl["overlay_weight"].to_numpy(dtype="float64")
    if not np.isfinite(v).any():
        return {"best_weight": np.nan, "peak": np.nan, "plateau": False,
                "at_boundary": False}
    i = int(np.nanargmax(v))
    at_boundary = bool(i == 0 or i == len(v) - 1)
    lo = max(0, i - 1)
    hi = min(len(v), i + 2)
    neigh = [v[j] for j in range(lo, hi) if j != i and np.isfinite(v[j])]
    med = float(np.median(neigh)) if neigh else np.nan
    peak = float(v[i])
    return {
        "best_weight": float(w[i]), "peak": peak, "neighbour_median": med,
        "peak_over_neighbour": peak / med if med and np.isfinite(med) and med > 0 else np.nan,
        "n_weights_within_10pct": int(np.sum(np.isfinite(v) & (v >= 0.9 * peak))),
        "at_boundary": at_boundary,
        "plateau": bool(not at_boundary and med and np.isfinite(med)
                        and peak <= 1.25 * med),
    }
