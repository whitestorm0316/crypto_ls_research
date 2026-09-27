"""BTC/market beta estimation and neutralisation.

Method A -- `gross_matched`: long gross == short gross.  This is what most people call
"market neutral" but it is *not* beta neutral: alt betas are heterogeneous, so the
residual net beta is generally non-zero.

Method B -- `beta_neutral`: solve for the short-side gross that drives ex-ante portfolio
beta to zero.  With unit long vector u (sum 1) and unit short magnitude vector v (sum 1):

    beta_u = sum_i u_i * beta_i ,  beta_v = sum_i v_i * beta_i
    k      = beta_u / beta_v                    (clipped to beta_ratio_cap)
    g_long  = G / (1 + k),   g_short = G * k / (1 + k)

so that `G*k/(1+k) * beta_v == G/(1+k) * beta_u`.  The total gross stays exactly G.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from ..config.settings import PortfolioConfig


def rolling_beta(returns: pd.DataFrame, bench: pd.Series, window: int,
                 min_periods: int | None = None) -> pd.DataFrame:
    mp = min_periods if min_periods is not None else max(10, window // 4)
    cov = returns.rolling(window, min_periods=mp).cov(bench)
    var = bench.rolling(window, min_periods=mp).var()
    beta = cov.div(var.replace(0.0, np.nan), axis=0)
    return beta.replace([np.inf, -np.inf], np.nan)


def side_beta(beta_row: np.ndarray, idx: np.ndarray, unit_w: np.ndarray) -> float:
    if idx.size == 0 or unit_w.size == 0:
        return np.nan
    b = beta_row[idx].astype("float64")
    ok = np.isfinite(b)
    if not ok.any():
        return np.nan
    b = np.where(ok, b, np.nanmedian(b[ok]))
    return float(np.sum(unit_w * b))


def side_gross_targets(beta_u: float, beta_v: float, gross: float, cfg: PortfolioConfig,
                       mode: str) -> Tuple[float, float]:
    """Return (long_gross, short_gross) summing to `gross`."""
    if mode == "A_gross_matched" or not np.isfinite(beta_u) or not np.isfinite(beta_v) or abs(beta_v) < 1e-9:
        return gross / 2.0, gross / 2.0
    if mode != "B_beta_neutral":
        raise ValueError(f"unknown beta_neutral_mode: {mode}")
    k = beta_u / beta_v
    lo, hi = cfg.beta_ratio_cap
    k = float(np.clip(k, lo, hi))
    return gross / (1.0 + k), gross * k / (1.0 + k)
