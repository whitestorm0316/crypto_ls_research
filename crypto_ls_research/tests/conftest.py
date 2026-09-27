"""Shared fixtures: fast synthetic panels with a known ground-truth structure."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import BacktestConfig, default_config
from crypto_ls_research.data.store import Panels

FIELDS = ("open", "high", "low", "close", "vol", "vol_ccy", "amount")


def synth_panels(n_inst: int = 12, n_bars: int = 3000, bar: str = "15m",
                 seed: int = 7, start: str = "2021-01-01",
                 beta_lo: float = 0.6, beta_hi: float = 1.4,
                 liq_lo: float = 5e6, liq_hi: float = 5e8,
                 first_bar_offset: int = 0) -> Panels:
    rng = np.random.default_rng(seed)
    secs = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400}[bar]
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n_bars, freq=pd.Timedelta(seconds=secs))
    bpd = int(86400 / secs)

    mkt = rng.normal(0.00002, 0.004, n_bars)          # common market factor
    betas = rng.uniform(beta_lo, beta_hi, n_inst)
    drift = rng.normal(0.000005, 0.00008, n_inst)     # persistent cross-sectional drift
    idio = rng.normal(0, 0.006, (n_bars, n_inst))
    logret = drift[None, :] + betas[None, :] * mkt[:, None] + idio
    close = 100.0 * np.exp(np.cumsum(logret, axis=0))

    noise = rng.uniform(0.001, 0.01, (n_bars, n_inst))
    high = close * (1 + noise)
    low = close * (1 - noise)
    openp = np.vstack([close[:1], close[:-1]]) * (1 + rng.normal(0, 0.001, (n_bars, n_inst)))
    liq = rng.uniform(liq_lo, liq_hi, n_inst)[None, :] * np.exp(
        rng.normal(0, 0.5, (n_bars, n_inst)))
    vol_ccy = liq / close
    vol = vol_ccy
    funding = rng.normal(0.0001, 0.0002, (n_bars, n_inst))

    insts = [f"C{i:02d}-USDT-SWAP" for i in range(n_inst)]
    idx = idx.set_names("ts")

    def df(a):
        return pd.DataFrame(a, index=idx, columns=insts)

    if first_bar_offset:
        for j in range(n_inst):
            k = (j * first_bar_offset) % max(1, n_bars // 2)
            for f in (openp, high, low, close, vol, vol_ccy, liq):
                f[:k, j] = np.nan
            funding[:k, j] = 0.0

    list_dt = pd.Series({c: idx[np.argmax(np.isfinite(close[:, j]))]
                         for j, c in enumerate(insts)}, name="list_dt")
    return Panels(open=df(openp), high=df(high), low=df(low), close=df(close),
                  vol=df(vol), vol_ccy=df(vol_ccy), amount=df(liq), funding=df(funding),
                  list_dt=list_dt)


def synth_cfg(bar: str = "15m", **ov) -> BacktestConfig:
    cfg = default_config(bar=bar, rebalance_bars=96)
    cfg.benchmark_inst = "C00-USDT-SWAP"
    # shrink windows so a 3000-bar synthetic panel is usable
    cfg.universe.min_history_days = 2
    cfg.universe.liq_window_days = 1
    cfg.universe.min_avg_amount_usd = 1e3
    cfg.universe.pit_topn = 8
    cfg.factors.mom_lookback_days = 0.5
    cfg.factors.mom_vol_days = 0.25
    cfg.factors.flow_short_days = 0.25
    cfg.factors.flow_long_days = 3.0
    cfg.factors.range_days = 0.75
    cfg.factors.hitrate_days = 0.5
    cfg.portfolio.beta_lookback_days = 1.0
    cfg.risk.vol_est_window_days = 1.0
    cfg.risk.regime_mom_days = 1.0
    cfg.risk.btc_vol_window_days = 0.5
    cfg.portfolio.top_k = 3
    for k, v in ov.items():
        parts = k.split(".")
        t = cfg
        for p in parts[:-1]:
            t = getattr(t, p)
        setattr(t, parts[-1], v)
    return cfg


@pytest.fixture(scope="session")
def panels() -> Panels:
    return synth_panels()


@pytest.fixture(scope="session")
def panels_late_listing() -> Panels:
    return synth_panels(n_inst=10, n_bars=4000, seed=11, first_bar_offset=250)
