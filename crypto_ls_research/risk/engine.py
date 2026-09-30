"""Risk overlay engine.

Five independent, strictly causal overlays are applied multiplicatively to the
base book, in this order:

    target = base_weights * regime_scale * btc_vol_scale * vol_target_scale * dd_scale

plus two per-instrument constraints applied after sizing:

    * ADV participation cap       |delta notional| <= max_adv_participation * ADV_t
    * liquidation-distance filter  requiring  3*ATR% <= 1/L_in_force - mmr

`L_in_force` is `RiskConfig.leverage_in_force` -- the leverage the gate *assumes*,
which is a modelling assumption and is deliberately **not** the same field as any
policy ceiling.  See `liquidation_ok` for why that distinction is load-bearing.

Everything consumes only information available at the decision bar.
"""
from __future__ import annotations

from typing import Optional, Tuple

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
def isolated_liq_distance(lever: float, mmr: float = DEFAULT_MMR) -> float:
    """`1/L - mmr`: the adverse move that eats a leg's own isolated margin."""
    return 1.0 / max(float(lever), 1e-9) - mmr


def implied_leverage(mark_px: float, liq_px: float,
                     mmr: float = DEFAULT_MMR) -> float:
    """The leverage a position is **actually margined at**, inverted from `liqPx`.

    Inverting `isolated_liq_distance`:

        dist = 1/L - mmr   =>   L = 1 / (dist + mmr),  dist = |liqPx - markPx| / markPx

    Why this has to exist: the venue's `lever` field (on both `/account/positions`
    and `/account/leverage-info`) mirrors the *account configuration* for
    `(instId, mgnMode, posSide)` -- **not** the leverage the open position is
    margined at.  Measured 2026-09-30 on the demo venue: setting NEAR `isolated`
    long from 3 to 5 moved `lever` to "5" but left the position's `margin`
    (116.906) and `liqPx` (3.317332) **bit-identical** for >65s.  A reader who
    trusts `lever` will believe a leg was re-margined when it was not; `liqPx` is
    the only field that tells the truth.

    ⚠️ It is an **estimate**: the venue's own liquidation formula uses a
    per-instrument maintenance-margin rate (and fees) while we assume a flat
    `mmr = 0.005`.  Measured on 9 legs on 2026-09-30: `config=3` came back as
    2.98-3.30 (+/-10%), `config=100` came back as 93.6 (-6%).  Treat it as a
    detector for *gross* divergence (the 20% flag), not a precise readout.
    """
    mark = float(mark_px)
    if mark <= 0:
        raise ValueError("mark_px must be positive")
    dist = abs(float(liq_px) - mark) / mark
    return 1.0 / (dist + mmr)


def liquidation_ok(atr_pct: np.ndarray, rcfg: RiskConfig, mmr: float = DEFAULT_MMR,
                   *, leverage: Optional[float] = None) -> np.ndarray:
    """Instrument volatility screen against a **stated** margin buffer.

    True where `k * ATR% <= 1/L - mmr`, i.e. where a name's typical bar move
    leaves at least `k` multiples of headroom before the assumed liquidation
    distance.  Two things about it used to be silently wrong:

    1. **`L` was `rcfg.max_leverage`, i.e. a policy *cap* read as a fact.**  The
       gate therefore always assumed the ceiling was in force.  Measured on the
       live demo account, the gate assumed 5x -> 19.50% while `BTC-USDT-SWAP
       short isolated` sat at **100x**, 0.57% from liquidation -- off by ~33x,
       and it reported "safe".  `L` is now `rcfg.leverage_in_force` (a modelling
       assumption, named as such) and callers that know the real leverage pass
       `leverage=` explicitly.  Nothing here falls back to a ceiling.

    2. **The formula is the *isolated* one, whatever the margin mode.**  That is
       a conservative per-leg proxy: in `cross` mode there is no per-position
       liquidation price at all (the whole account equity backs every leg), so
       the honest account-level distance `(1 - mmr*G)/G` is far larger and this
       screen is *not* what keeps the book solvent.  It is kept as a
       volatility screen so that changing it does not silently move the accepted
       backtest; making it mode-aware is a separate, headline-moving change.

    The live counterpart -- comparing this assumption against each held leg's
    `implied_leverage` -- is `execution.limits.margin_headroom_violations`.
    """
    lev = float(rcfg.leverage_in_force if leverage is None else leverage)
    liq_dist = isolated_liq_distance(lev, mmr)
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
