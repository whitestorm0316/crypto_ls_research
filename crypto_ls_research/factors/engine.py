"""Factor engine.  Every factor is a strictly *trailing* rolling statistic.

Look-ahead safety argument
--------------------------
Each factor is built exclusively from `rolling(...)`, `shift(positive)`, `expanding` or
element-wise arithmetic on the raw panel.  No `shift(-k)`, no centred window, no
full-sample statistic.  Therefore factor value at bar t depends only on bars <= t.

`tests/test_no_lookahead.py` proves this empirically by mutating future bars and
asserting the factor panel up to t is bit-identical.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from ..config.settings import FactorConfig, UniverseConfig
from ..data.store import Panels

EPS = 1e-12


def _ann_vol(logret: pd.DataFrame, window: int, bars_per_year: float) -> pd.DataFrame:
    v = logret.rolling(window, min_periods=max(5, window // 4)).std(ddof=0)
    return v * np.sqrt(bars_per_year)


def compute_factors(p: Panels, fcfg: FactorConfig, ucfg: UniverseConfig,
                    bars_per_day: int, bars_per_year: float) -> Dict[str, pd.DataFrame]:
    """Return raw (un-standardised) factor panels plus risk primitives."""
    close, high, low = p.close, p.high, p.low
    amount = p.amount

    # ---- risk primitives ---------------------------------------------------
    logret = np.log(close.where(close > 0)).diff()
    vol = _ann_vol(logret, max(2, int(round(fcfg.mom_vol_days * bars_per_day))), bars_per_year)
    vol = vol.replace(0.0, np.nan)

    atr_bars = max(2, int(round(1 * bars_per_day)))
    atr_pct = ((high - low) / close).rolling(atr_bars, min_periods=2).mean()

    adv_bars = max(2, int(round(ucfg.liq_window_days * bars_per_day)))
    adv = amount.rolling(adv_bars, min_periods=max(2, adv_bars // 4)).mean()

    # ---- Factor 1: volatility-adjusted momentum ---------------------------
    L = max(2, int(round(fcfg.mom_lookback_days * bars_per_day)))
    mom_raw = close / close.shift(L) - 1.0
    momentum = mom_raw / vol                      # risk-adjusted trend

    # ---- Factor 2: liquidity flow (short-horizon turnover vs long-horizon) --
    S = max(2, int(round(fcfg.flow_short_days * bars_per_day)))
    Ll = max(S + 1, int(round(fcfg.flow_long_days * bars_per_day)))
    short_amt = amount.rolling(S, min_periods=max(2, S // 4)).mean()
    long_amt = amount.rolling(Ll, min_periods=max(2, Ll // 4)).mean()
    flow = (short_amt / long_amt.replace(0.0, np.nan))
    flow = flow.replace([np.inf, -np.inf], np.nan)

    # ---- Factor 3: range position -----------------------------------------
    R = max(2, int(round(fcfg.range_days * bars_per_day)))
    r_high = high.rolling(R, min_periods=max(2, R // 4)).max()
    r_low = low.rolling(R, min_periods=max(2, R // 4)).min()
    denom = (r_high - r_low)
    # degenerate flat range -> undefined, not 0.5
    valid = denom > EPS * r_high.replace(0.0, np.nan).where(r_high.notna(), 1.0)
    range_pos = ((close - r_low) / denom.where(valid)).clip(lower=0.0, upper=1.0)

    # ---- Factor 4: hit rate ------------------------------------------------
    N = max(2, int(round(fcfg.hitrate_days * bars_per_day)))
    up = (close.diff() > 0).astype("float64")
    up = up.where(close.notna())
    hitrate = up.rolling(N, min_periods=max(2, N // 4)).mean()

    # ---- Factor 5: short-horizon reversal ----------------------------------
    # Minus the risk-adjusted return over a short window.  The rank-IC study finds
    # every trend factor significantly *negative* at the 4h horizon and positive at
    # 7d, i.e. the same cross-section is a reversal signal over hours.  Stating that
    # as its own factor makes the effect testable (standalone fast book, or blended
    # into the slow book) instead of leaving it buried in the noise of a 3d book.
    # Trailing only: `close.shift(RS)` with RS >= 2, so no future bar can enter.
    RS = max(2, int(round(fcfg.rev_short_days * bars_per_day)))
    rev_raw = close / close.shift(RS) - 1.0
    rev_short = -rev_raw / vol

    return {
        "momentum": momentum.astype("float32"),
        "flow": flow.astype("float32"),
        "range_pos": range_pos.astype("float32"),
        "hitrate": hitrate.astype("float32"),
        "rev_short": rev_short.astype("float32"),
        "vol": vol.astype("float32"),
        "atr_pct": atr_pct.astype("float32"),
        "adv": adv.astype("float32"),
    }


FACTOR_NAMES = ("momentum", "flow", "range_pos", "hitrate", "rev_short")
