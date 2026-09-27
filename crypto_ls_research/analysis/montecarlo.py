"""Monte-Carlo / placebo tests.

Three families of null hypothesis, in increasing order of severity:

H1 `cross_section_permute`
    At every rebalance, permute the actual composite score across the PIT pool.
    The score *distribution* and every risk overlay are identical to the real run;
    only the cross-sectional information is destroyed.  This isolates "is the ranking
    informative?" from "is the risk engine doing the work?".

H2 `random_score`
    Replace the score with i.i.d. Gaussian noise.  Same selection mechanics, no signal.

H3 `block_shuffle` (surrogate price paths)
    Globally permute blocks of log-returns (default 1 day) and re-integrate prices,
    keeping the cross-sectional correlation structure and the amount/return pairing
    inside each block intact.  This destroys serial dependence while preserving the
    joint distribution -- i.e. it asks "would this same machinery produce this Sharpe
    on data with no momentum or flow persistence at all?"  It is the strongest test
    here, and the most expensive, so it runs on fewer iterations.

p-value = (1 + #{placebo >= real}) / (1 + n_permutations), the standard permutation
p-value which is valid without any distributional assumption.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..config.settings import BacktestConfig
from ..data.store import Panels


# ---------------------------------------------------------------------------
def permute_specs(n: int, kind: str, base_overrides: Optional[dict] = None,
                  rebalance_bars: int = 96, seed0: int = 1_000_003) -> List[dict]:
    if kind not in ("cross_section_permute", "random_score"):
        raise ValueError(kind)
    specs = []
    for i in range(n):
        kw = ({"score_permute": "cross_section"} if kind == "cross_section_permute"
              else {"selection_mode": "random_score"})
        specs.append({"label": f"{kind}#{i}", "overrides": dict(base_overrides or {}),
                      "rebalance_bars": rebalance_bars, "kwargs": kw,
                      "seed": seed0 + i, "keep_bars": True})
    return specs


# ---------------------------------------------------------------------------
def block_shuffle_panels(p: Panels, block_bars: int, rng: np.random.Generator) -> Panels:
    """Surrogate panels: global block permutation of log-returns, prices re-integrated.

    Within a block, the joint cross-section AND the amount/return pairing are preserved;
    only the *order of blocks* is randomised.  Any serial-dependence-based edge is
    therefore destroyed while the marginal distributions are unchanged.
    """
    T = len(p.index)
    nb = int(math.ceil(T / block_bars))
    bounds = [(b * block_bars, min((b + 1) * block_bars, T)) for b in range(nb)]
    perm = rng.permutation(nb)
    idx = np.concatenate([np.arange(a, b) for a, b in [bounds[i] for i in perm]])[:T]

    close = p.close.to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(np.where(close > 0, close, np.nan)), axis=0, prepend=np.nan)
    lr[0] = 0.0
    lr = np.nan_to_num(lr, nan=0.0, posinf=0.0, neginf=0.0)
    lr_sh = lr[idx]

    base = p.close.iloc[0].to_numpy(dtype="float64")
    base = np.where(np.isfinite(base) & (base > 0), base, np.nan)
    close_new = base[None, :] * np.exp(np.cumsum(lr_sh, axis=0))
    valid = np.isfinite(close)
    close_new[~valid[idx]] = np.nan            # keep the real missing-data pattern
    close_df = pd.DataFrame(close_new, index=p.index, columns=p.close.columns)

    def rescale(df: pd.DataFrame) -> pd.DataFrame:
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = (df / p.close).to_numpy(dtype="float64")[idx]
        out = close_new * ratio
        return pd.DataFrame(out, index=p.index, columns=p.close.columns)

    def rows(df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(df.to_numpy(dtype="float64")[idx], index=p.index,
                            columns=p.close.columns)

    open_df = rescale(p.open)
    high_df = rescale(p.high)
    low_df = rescale(p.low)
    amount_df = rows(p.amount)
    vol_ccy_df = rows(p.vol_ccy)
    vol_df = rows(p.vol)
    funding_df = rows(p.funding)

    # price/volume invariants must survive the shuffle
    hi = np.maximum.reduce([high_df.to_numpy(), open_df.to_numpy(), close_df.to_numpy()])
    lo = np.minimum.reduce([low_df.to_numpy(), open_df.to_numpy(), close_df.to_numpy()])
    high_df = pd.DataFrame(hi, index=p.index, columns=p.close.columns)
    low_df = pd.DataFrame(lo, index=p.index, columns=p.close.columns)

    list_dt = pd.Series(
        {c: (close_df[c].first_valid_index() if close_df[c].notna().any() else pd.NaT)
         for c in close_df.columns}, name="list_dt").dropna()
    keep = [c for c in close_df.columns if c in list_dt.index]
    return Panels(open=open_df[keep], high=high_df[keep], low=low_df[keep],
                  close=close_df[keep], vol=vol_df[keep], vol_ccy=vol_ccy_df[keep],
                  amount=amount_df[keep], funding=funding_df[keep], list_dt=list_dt[keep])


def run_block_shuffle_mc(panels: Panels, cfg: BacktestConfig, run_fn, n: int = 30,
                         block_days: float = 1.0, seed0: int = 7_000_001,
                         verbose: bool = True) -> pd.DataFrame:
    """`run_fn(panels, cfg) -> BacktestResult`; returns one row per shuffle."""
    from .metrics import compute_metrics
    block_bars = max(2, int(round(block_days * cfg.bars_per_day)))
    rows = []
    for i in range(n):
        rng = np.random.default_rng(seed0 + i)
        try:
            sp = block_shuffle_panels(panels, block_bars, rng)
            res = run_fn(sp, cfg)
            m = compute_metrics(res.bars, name=f"shuffle#{i}")
            rows.append({"iter": i, "Sharpe": m["Sharpe"], "CAGR": m["CAGR"],
                         "max_dd": m["Max Drawdown"], "ann_vol": m["Annualized Volatility"],
                         "ann_turnover": m["Annual Turnover"]})
        except Exception as e:                                        # noqa: BLE001
            rows.append({"iter": i, "error": f"{type(e).__name__}: {e}"})
        if verbose and (i + 1) % 5 == 0:
            print(f"    block-shuffle {i+1}/{n}", flush=True)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
def placebo_summary(real_value: float, placebo: Sequence[float],
                    higher_is_better: bool = True) -> Dict[str, float]:
    v = np.asarray([x for x in placebo if np.isfinite(x)], dtype="float64")
    if v.size == 0 or not np.isfinite(real_value):
        return {"n": int(v.size), "real": real_value, "p_value": np.nan,
                "percentile": np.nan, "placebo_mean": np.nan, "placebo_std": np.nan}
    if higher_is_better:
        n_extreme = int(np.sum(v >= real_value))
    else:
        n_extreme = int(np.sum(v <= real_value))
    p = (1 + n_extreme) / (1 + v.size)
    pct = float(np.mean(v < real_value)) if higher_is_better else float(np.mean(v > real_value))
    return {
        "n": int(v.size), "real": float(real_value),
        "placebo_mean": float(v.mean()), "placebo_std": float(v.std(ddof=1)) if v.size > 1 else np.nan,
        "placebo_p05": float(np.quantile(v, 0.05)), "placebo_p95": float(np.quantile(v, 0.95)),
        "percentile": pct, "p_value": float(p),
        "significant_5pct": bool(p < 0.05),
    }
