"""Point-in-Time (PIT) universe construction.

Hard requirements enforced here
-------------------------------
1. **Listing gate** -- an instrument may only be considered from its real first
   available bar onwards.  No bar before listing is ever used.
2. **Age gate** -- `age_bars >= min_history_days * bars_per_day`.
3. **Liquidity gate** -- trailing `liq_window_days` mean turnover >= `min_avg_amount_usd`.
   The average is a *rolling* mean, so no future turnover can enter it.
4. **Rank gate** -- top `pit_topn` by that trailing liquidity.

Consequence: a name that only becomes liquid in 2024 cannot influence any portfolio
formation before 2024, and the 2021 pool is built purely from 2021 information.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np

from ..config.settings import UniverseConfig


def listing_age_bars(first_valid_idx: np.ndarray, n_bars: int) -> np.ndarray:
    """bars-since-listing matrix (T, N), NaN where the instrument has not listed yet."""
    t = np.arange(n_bars, dtype="float64")[:, None]
    fv = first_valid_idx.astype("float64")[None, :]
    age = t - fv
    return np.where(np.isnan(fv) | (age < 0), np.nan, age)


def universe_mask(close_t: np.ndarray, age_bars_t: np.ndarray, adv_t: np.ndarray,
                  ucfg: UniverseConfig, min_history_bars: int,
                  extra_eligible: np.ndarray | None = None) -> Tuple[np.ndarray, np.ndarray]:
    """Eligibility mask + PIT liquidity rank (1 = most liquid) at one bar.

    Returns
    -------
    mask : bool (N,)  -- True if the name is in the tradable pool
    rank : float (N,) -- 1-based liquidity rank among eligible names, NaN otherwise
    """
    ok = (
        np.isfinite(close_t) & (close_t > 0)
        & np.isfinite(adv_t) & (adv_t >= ucfg.min_avg_amount_usd)
        & np.isfinite(age_bars_t) & (age_bars_t >= min_history_bars)
    )
    if extra_eligible is not None:
        ok &= extra_eligible
    idx = np.flatnonzero(ok)
    rank = np.full(close_t.shape, np.nan)
    if idx.size == 0:
        return ok, rank
    adv_ok = adv_t[idx]
    order = np.argsort(-adv_ok, kind="stable")          # descending liquidity
    rank[idx[order]] = np.arange(1, idx.size + 1, dtype="float64")
    topn = ucfg.pit_topn
    if topn and topn < idx.size:
        keep = idx[order[:topn]]
        mask = np.zeros(close_t.shape, dtype=bool)
        mask[keep] = True
        rank = np.where(mask, rank, np.nan)
    else:
        mask = ok
    return mask, rank


def min_history_bars(ucfg: UniverseConfig, bars_per_day: int) -> int:
    return max(1, int(round(ucfg.min_history_days * bars_per_day)))
