"""Itemised transaction-cost model.

Every cost is charged per fill, at the price actually available at execution time.
There is deliberately no "round-trip cost" constant anywhere.

    cost_i(bar) = |Δw_i| * ( fee_rate + half_spread_i + impact_i )
    impact_i    = impact_coef * daily_vol_i * sqrt(participation_i)
    participation_i = |Δnotional_i| / ADV_i

`half_spread_i` is tiered on TRAILING ADV (point-in-time), never full-sample ADV.
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np

from ..config.settings import CostConfig

EPS = 1e-12


def effective_fee_rate(ccfg: CostConfig) -> float:
    """Blend of maker/taker by the passive-fill ratio.  Default = 100% taker."""
    p = float(np.clip(ccfg.passive_fill_ratio, 0.0, 1.0))
    return ccfg.fee_multiplier * ((1 - p) * ccfg.taker_fee + p * ccfg.maker_fee)


def half_spread_rate(adv_usd: np.ndarray, ccfg: CostConfig) -> np.ndarray:
    """bps -> rate.  Illiquid tier gets the wider half-spread."""
    liquid = np.isfinite(adv_usd) & (adv_usd >= ccfg.half_spread_adv_cut_usd)
    bps = np.where(liquid, ccfg.half_spread_bps_base, ccfg.half_spread_bps_illiquid)
    bps = np.where(np.isfinite(adv_usd), bps, ccfg.half_spread_bps_illiquid)
    return bps / 10_000.0


def impact_rate(delta_w: np.ndarray, adv_usd: np.ndarray, equity: float,
                daily_vol: np.ndarray, ccfg: CostConfig) -> Tuple[np.ndarray, np.ndarray]:
    """Square-root impact law.  Returns (rate, participation)."""
    notional = np.abs(delta_w) * equity
    adv = np.where(np.isfinite(adv_usd) & (adv_usd > 0), adv_usd, np.nan)
    part = notional / adv
    part = np.where(np.isfinite(part), part, 0.0)
    dv = np.nan_to_num(daily_vol, nan=0.0)
    rate = ccfg.impact_coef * dv * np.sqrt(np.maximum(part, 0.0))
    return rate, part


def trade_cost(delta_w: np.ndarray, adv_usd: np.ndarray, equity: float,
               daily_vol: np.ndarray, ccfg: CostConfig) -> Dict[str, np.ndarray]:
    fee = effective_fee_rate(ccfg)
    hs = half_spread_rate(adv_usd, ccfg)
    imp, part = impact_rate(delta_w, adv_usd, equity, daily_vol, ccfg)
    per_unit = fee + hs + imp                              # rate per unit of turnover
    gross_cost = np.abs(delta_w) * per_unit                # fraction of equity
    return {
        "total": gross_cost,
        "fee": np.abs(delta_w) * fee,
        "spread": np.abs(delta_w) * hs,
        "impact": np.abs(delta_w) * imp,
        "participation": part,
        "turnover": np.abs(delta_w),
    }


def funding_cost(weights: np.ndarray, funding_rate: np.ndarray) -> float:
    """Cashflow as a fraction of equity.

    OKX convention: when the funding rate is positive, longs pay shorts.  The rate is
    applied to notional, so the PnL contribution of a signed weight w is `-w * rate`.
    """
    ok = np.isfinite(funding_rate)
    if not ok.any():
        return 0.0
    return float(-np.sum(weights[ok] * np.nan_to_num(funding_rate[ok])))
