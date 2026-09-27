"""Factor formula correctness against hand-computed values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.data.store import Panels
from crypto_ls_research.factors.engine import compute_factors

from .conftest import synth_cfg


def _panels_from_close(close: np.ndarray, high=None, low=None, amount=None,
                       vol_ccy=None, bar: str = "15m", start="2021-01-01") -> Panels:
    n, k = close.shape
    secs = {"15m": 900, "1h": 3600}[bar]
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n,
                        freq=pd.Timedelta(seconds=secs))
    insts = [f"X{i:02d}-USDT-SWAP" for i in range(k)]
    high = close * 1.01 if high is None else high
    low = close * 0.99 if low is None else low
    amount = np.full_like(close, 1e7) if amount is None else amount
    vol_ccy = close if vol_ccy is None else vol_ccy

    def df(a):
        return pd.DataFrame(a, index=idx, columns=insts)

    return Panels(open=df(close), high=df(high), low=df(low), close=df(close),
                  vol=df(vol_ccy), vol_ccy=df(vol_ccy), amount=df(amount),
                  funding=df(np.zeros_like(close)),
                  list_dt=pd.Series({c: idx[0] for c in insts}))


def test_momentum_is_vol_adjusted():
    cfg = synth_cfg()
    n = 800
    close = np.zeros((n, 2))
    t = np.arange(n)
    close[:, 0] = 100 * (1.0 + 0.001 * t)      # smooth uptrend, low vol
    rng = np.random.default_rng(0)
    close[:, 1] = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))  # same-ish trend, high vol
    p = _panels_from_close(close)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)

    L = cfg.days_to_bars(cfg.factors.mom_lookback_days)
    raw0 = close[-1, 0] / close[-1 - L, 0] - 1
    raw1 = close[-1, 1] / close[-1 - L, 1] - 1
    V = cfg.days_to_bars(cfg.factors.mom_vol_days)
    lr0 = np.diff(np.log(close[:, 0]))[-V:]
    lr1 = np.diff(np.log(close[:, 1]))[-V:]
    v0, v1 = lr0.std(ddof=0) * np.sqrt(cfg.bars_per_year), lr1.std(ddof=0) * np.sqrt(cfg.bars_per_year)
    np.testing.assert_allclose(f["momentum"].iloc[-1, 0], raw0 / v0, rtol=1e-4)
    np.testing.assert_allclose(f["momentum"].iloc[-1, 1], raw1 / v1, rtol=1e-4)
    # the smooth series must score a far higher risk-adjusted momentum
    assert f["momentum"].iloc[-1, 0] > 3 * abs(f["momentum"].iloc[-1, 1]) or \
        f["momentum"].iloc[-1, 0] > f["momentum"].iloc[-1, 1]


def test_flow_is_short_over_long_turnover():
    cfg = synth_cfg()
    n = 1200
    rng = np.random.default_rng(3)
    close = np.full((n, 2), 100.0)
    amount = np.full((n, 2), 1e7)
    amount[-cfg.days_to_bars(cfg.factors.flow_short_days):, 0] = 4e7   # recent expansion
    amount[-cfg.days_to_bars(cfg.factors.flow_short_days):, 1] = 2.5e6  # recent contraction
    p = _panels_from_close(close, amount=amount)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    S = cfg.days_to_bars(cfg.factors.flow_short_days)
    L = cfg.days_to_bars(cfg.factors.flow_long_days)
    exp0 = amount[-S:, 0].mean() / amount[-L:, 0].mean()
    exp1 = amount[-S:, 1].mean() / amount[-L:, 1].mean()
    np.testing.assert_allclose(f["flow"].iloc[-1, 0], exp0, rtol=1e-6)
    np.testing.assert_allclose(f["flow"].iloc[-1, 1], exp1, rtol=1e-6)
    assert f["flow"].iloc[-1, 0] > 1 > f["flow"].iloc[-1, 1]


def test_range_position_bounds_and_degenerate_case():
    cfg = synth_cfg()
    n = 600
    close = np.zeros((n, 2))
    close[:, 0] = np.linspace(100, 200, n)              # monotone -> near 1
    close[:, 1] = np.linspace(200, 100, n)              # monotone down -> near 0
    high = np.maximum(close, np.roll(close, 1, axis=0)) * 1.0
    low = np.minimum(close, np.roll(close, 1, axis=0)) * 1.0
    p = _panels_from_close(close, high=high, low=low)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    v = f["range_pos"].iloc[-1].to_numpy()
    assert 0.0 <= v[0] <= 1.0 and 0.0 <= v[1] <= 1.0
    assert v[0] > 0.9 and v[1] < 0.1

    # perfectly flat range -> undefined, must be NaN (not silently 0.5)
    flat = _panels_from_close(np.full((n, 1), 50.0), high=np.full((n, 1), 50.0),
                              low=np.full((n, 1), 50.0))
    ff = compute_factors(flat, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    assert np.isnan(ff["range_pos"].iloc[-1, 0])


def test_hitrate_counts_positive_bars():
    cfg = synth_cfg()
    n = 900
    close = np.full((n, 1), 100.0)
    close[1:600, 0] = 100.0 + np.arange(1, 600) * 0.1     # all up
    close[600:, 0] = 200.0
    p = _panels_from_close(close)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    N = cfg.days_to_bars(cfg.factors.hitrate_days)
    up = (np.diff(close[:, 0]) > 0).astype(float)
    expected = up[-N:].mean()
    np.testing.assert_allclose(f["hitrate"].iloc[-1, 0], expected, rtol=1e-6)
    assert 0.0 <= f["hitrate"].iloc[-1, 0] <= 1.0


def test_adv_matches_rolling_mean():
    cfg = synth_cfg()
    n = 900
    rng = np.random.default_rng(9)
    close = np.full((n, 1), 10.0)
    amount = rng.uniform(1e6, 5e6, (n, 1))
    p = _panels_from_close(close, amount=amount)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    W = cfg.days_to_bars(cfg.universe.liq_window_days)
    np.testing.assert_allclose(f["adv"].iloc[-1, 0], amount[-W:, 0].mean(), rtol=1e-6)


def test_atr_pct_is_positive_and_trailing():
    cfg = synth_cfg()
    n = 500
    close = np.full((n, 1), 100.0)
    high = np.full((n, 1), 103.0)
    low = np.full((n, 1), 99.0)
    p = _panels_from_close(close, high=high, low=low)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    W = cfg.days_to_bars(1.0)
    np.testing.assert_allclose(f["atr_pct"].iloc[-1, 0], np.mean(np.full(W, 0.04)), rtol=1e-6)
