"""Risk overlay engine.

Five independent, strictly causal overlays are applied multiplicatively to the
base book, in this order:

    target = base_weights * regime_scale * btc_vol_scale * vol_target_scale * dd_scale

plus two per-instrument constraints applied after sizing:

    * ADV participation cap       |delta notional| <= max_adv_participation * ADV_t
    * liquidation-distance filter  requiring  3*ATR% <= 1/max_leverage - mmr

Everything consumes only information available at the decision bar.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from ..config.settings import RiskConfig

DEFAULT_MMR = 0.005      # maintenance-margin rate proxy (0.5%)


# ---------------------------------------------------------------------------
# market regime
# ---------------------------------------------------------------------------
def btc_regime_series(btc_close: pd.Series, rcfg: RiskConfig, bars_per_day: int,
                      bars_per_year: float) -> Tuple[pd.Series, pd.Series]:
    """Trailing BTC momentum and annualised realised vol.  Both are backward-looking."""
    mom_bars = max(2, int(round(rcfg.regime_mom_days * bars_per_day)))
    vol_bars = max(2, int(round(rcfg.btc_vol_window_days * bars_per_day)))
    mom = btc_close / btc_close.shift(mom_bars) - 1.0
    lr = np.log(btc_close.where(btc_close > 0)).diff()
    vol = lr.rolling(vol_bars, min_periods=max(5, vol_bars // 4)).std(ddof=0) * np.sqrt(bars_per_year)
    return mom, vol


def regime_scale(btc_mom: float, rcfg: RiskConfig) -> float:
    if not rcfg.regime_enabled:
        return 1.0
    if not np.isfinite(btc_mom):
        return 1.0
    return rcfg.regime_bear_scale if btc_mom < 0 else 1.0


def btc_vol_scale(btc_vol: float, rcfg: RiskConfig) -> float:
    """Linear de-risking ramp between `btc_vol_soft` and `btc_vol_hard`."""
    if not rcfg.btc_vol_cap_enabled or not np.isfinite(btc_vol):
        return 1.0
    if btc_vol <= rcfg.btc_vol_soft:
        return 1.0
    if btc_vol >= rcfg.btc_vol_hard:
        return rcfg.btc_vol_min_scale
    frac = (btc_vol - rcfg.btc_vol_soft) / (rcfg.btc_vol_hard - rcfg.btc_vol_soft)
    return float(1.0 - frac * (1.0 - rcfg.btc_vol_min_scale))


# ---------------------------------------------------------------------------
# volatility targeting
# ---------------------------------------------------------------------------
def vol_target_scale(realized_annual_vol: float, rcfg: RiskConfig) -> float:
    if not np.isfinite(realized_annual_vol) or realized_annual_vol <= 1e-6:
        return 1.0
    raw = rcfg.target_vol_annual / realized_annual_vol
    return float(np.clip(raw, rcfg.min_gross_exposure, rcfg.max_gross_exposure))


# ---------------------------------------------------------------------------
# drawdown ladder
# ---------------------------------------------------------------------------
def dd_scale(drawdown: float, rcfg: RiskConfig, stop_new_entries_above: float = 0.20
             ) -> Tuple[float, bool]:
    """Return (multiplier, stop_new_entries).

    `drawdown` is a negative number (equity / running_peak - 1).  A hard floor is kept
    on the multiplier so that the book cannot be locked permanently flat at the trough
    (a classic, value-destroying bug in naive drawdown stops).
    """
    dd = abs(min(drawdown, 0.0))
    scale = 1.0
    for thresh, mult in rcfg.dd_ladder:
        if dd <= thresh:
            scale = mult
            break
    else:
        scale = rcfg.dd_ladder[-1][1]
    return float(scale), bool(dd > stop_new_entries_above)


# ---------------------------------------------------------------------------
# per-instrument constraints
# ---------------------------------------------------------------------------
def liquidation_ok(atr_pct: np.ndarray, rcfg: RiskConfig, mmr: float = DEFAULT_MMR) -> np.ndarray:
    """True where the instrument can be held at `max_leverage` with >= k*ATR of headroom."""
    liq_dist = 1.0 / max(rcfg.max_leverage, 1e-9) - mmr
    need = rcfg.min_liquidation_atr_multiple * np.nan_to_num(atr_pct, nan=np.inf)
    return np.isfinite(atr_pct) & (need <= liq_dist)


def adv_cap_delta(delta_w: np.ndarray, adv_usd: np.ndarray, equity: float,
                  rcfg: RiskConfig) -> Tuple[np.ndarray, float]:
    """Scale down a weight-delta vector so no single name breaches the ADV cap.

    Returns (capped_delta, binding_fraction).  Names whose ADV is unknown get a zero
    budget -- unknown liquidity is treated as no liquidity.
    """
    notional = np.abs(delta_w) * equity
    budget = np.where(np.isfinite(adv_usd), adv_usd, 0.0) * rcfg.max_adv_participation
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where(notional > 0, np.minimum(1.0, budget / np.maximum(notional, 1e-12)), 1.0)
    scale = np.where(notional > 0, np.nan_to_num(scale, nan=0.0), 1.0)
    capped = delta_w * scale
    binding = float(np.mean(scale < 0.999)) if scale.size else 0.0
    return capped, binding


def turnover_budget_scale(delta_w: np.ndarray, used_today: float, budget: float) -> float:
    """Proportional partial-rebalance scale in [0, 1]."""
    if budget is None or not np.isfinite(budget) or budget <= 0:
        return 1.0
    remaining = budget - used_today
    if remaining <= 0:
        return 0.0
    want = float(np.abs(delta_w).sum())
    if want <= 0:
        return 1.0
    return float(min(1.0, remaining / want))
