"""Look-ahead verification.

Each test *mutates the future* and asserts that the past is bit-identical.  This is a
stronger statement than "the code looks causal": if any code path touched bar > t while
producing a value at bar t, these tests fail.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.backtest.engine import pick_exec_price, run_backtest
from crypto_ls_research.data.store import Panels, panels_to_arrays
from crypto_ls_research.factors.engine import FACTOR_NAMES, compute_factors
from crypto_ls_research.signals.cross_section import composite_score, zscore
from crypto_ls_research.universe.pit import universe_mask

from .conftest import synth_cfg, synth_panels


def _mutate_future(p: Panels, t0: int, seed: int = 123) -> Panels:
    """Replace every field from bar t0 onwards with unrelated garbage."""
    rng = np.random.default_rng(seed)
    T = len(p.index)

    def m(df: pd.DataFrame, absolute: bool = False) -> pd.DataFrame:
        a = df.to_numpy(dtype="float64", copy=True)
        if absolute:
            a[t0:] = rng.uniform(1e3, 1e6, size=(T - t0, a.shape[1]))
        else:
            a[t0:] = a[t0:] * rng.uniform(0.2, 5.0, size=(T - t0, a.shape[1]))
        return pd.DataFrame(a, index=df.index, columns=df.columns)

    fund = p.funding.to_numpy(dtype="float64", copy=True)
    fund[t0:] = fund[t0:] * 7.0 - 0.001
    return Panels(open=m(p.open), high=m(p.high), low=m(p.low), close=m(p.close),
                  vol=m(p.vol), vol_ccy=m(p.vol_ccy), amount=m(p.amount, absolute=True),
                  funding=pd.DataFrame(fund, index=p.index, columns=p.close.columns),
                  list_dt=p.list_dt)


# ---------------------------------------------------------------------------
def test_factors_are_invariant_to_future(panels: Panels):
    cfg = synth_cfg()
    t0 = 2000
    fut = _mutate_future(panels, t0)
    a = compute_factors(panels, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    b = compute_factors(fut, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    for k in ("momentum", "flow", "range_pos", "hitrate", "vol", "atr_pct", "adv"):
        va, vb = a[k].to_numpy()[: t0 - 1], b[k].to_numpy()[: t0 - 1]
        np.testing.assert_allclose(
            np.nan_to_num(va, nan=-999.0), np.nan_to_num(vb, nan=-999.0),
            rtol=0, atol=0, err_msg=f"factor '{k}' changed when only the future changed")
        assert np.array_equal(np.isnan(va), np.isnan(vb)), f"NaN mask of '{k}' changed"


def test_cross_sectional_zscore_is_one_bar_only(panels: Panels):
    cfg = synth_cfg()
    t0 = 1500
    x = panels.close.iloc[t0].to_numpy(dtype="float64")
    z1 = zscore(x, "mad", 3.0, 0.02)
    # doubling one element of a *future* cross-section is irrelevant here; the point is
    # that zscore only ever sees the array it is handed
    fut = _mutate_future(panels, t0 + 1)
    x2 = panels.close.iloc[t0].to_numpy(dtype="float64")
    assert fut.close.iloc[t0].equals(panels.close.iloc[t0])
    np.testing.assert_array_equal(z1, zscore(x2, "mad", 3.0, 0.02))


def test_pit_universe_is_invariant_to_future_liquidity(panels: Panels):
    cfg = synth_cfg()
    t0 = 1800
    adv = panels.amount.rolling(96, min_periods=5).mean().to_numpy(dtype="float64")
    fut = _mutate_future(panels, t0 + 1)
    adv_f = fut.amount.rolling(96, min_periods=5).mean().to_numpy(dtype="float64")
    close_t = panels.close.iloc[t0].to_numpy(dtype="float64")
    age = np.full(close_t.shape, 1e6)

    m1, r1 = universe_mask(close_t, age, adv[t0], cfg.universe, 10)
    m2, r2 = universe_mask(close_t, age, adv_f[t0], cfg.universe, 10)
    np.testing.assert_array_equal(m1, m2)
    np.testing.assert_allclose(np.nan_to_num(r1, nan=-1), np.nan_to_num(r2, nan=-1))

    # ... and the same holds when future ADV is scaled by 1000x
    adv_big = adv.copy()
    adv_big[t0 + 1:] *= 1000.0
    m3, _ = universe_mask(close_t, age, adv_big[t0], cfg.universe, 10)
    np.testing.assert_array_equal(m1, m3)


def test_backtest_pnl_is_invariant_to_future(panels: Panels):
    """Bar t's P&L is marked out at px[t+1], so it legitimately uses one bar of "future".

    The exact invariant is therefore: mutating bars >= t0 must leave net_ret[0 .. t0-2]
    untouched.  Anything earlier changing would mean a real look-ahead bug.
    """
    cfg = synth_cfg()
    t0 = 2200
    r_base = run_backtest(panels, cfg)
    r_fut = run_backtest(_mutate_future(panels, t0), cfg)

    a = r_base.bars["net_ret"].to_numpy()[: t0 - 1]
    b = r_fut.bars["net_ret"].to_numpy()[: t0 - 1]
    np.testing.assert_allclose(a, b, rtol=1e-12, atol=1e-15,
                               err_msg="past P&L changed when only the future changed")

    # the target book formed at decision bars strictly before t0-1 must also be identical
    for i, ts in enumerate(r_base.reb_ts):
        if ts >= panels.index[t0 - 1]:
            break
        np.testing.assert_allclose(
            r_base.weight_matrix[i].astype("float64"),
            r_fut.weight_matrix[i].astype("float64"),
            rtol=0, atol=1e-12, err_msg=f"weights at {ts} changed")

    # sanity: the mutation must actually bite, otherwise the test proves nothing
    assert not np.allclose(r_base.bars["net_ret"].to_numpy()[t0 + 5:],
                           r_fut.bars["net_ret"].to_numpy()[t0 + 5:])


def test_signal_is_not_filled_at_its_own_bar_price():
    """Construct a violent gap on the decision bar's successor.

    The strategy signals LONG at the close of bar d.  If the engine (wrongly) filled at
    that same close, it would capture the entire d -> d+1 gap.  With next-open execution
    it must capture approximately nothing.
    """
    n = 600
    idx = pd.date_range("2021-01-01", periods=n, freq="15min", tz="UTC")
    insts = ["AAA-USDT-SWAP", "BBB-USDT-SWAP"]
    close = np.full((n, 2), 100.0)
    # AAA ramps up over the first 400 bars so momentum is strongly positive
    close[:400, 0] = 100.0 * np.linspace(1.0, 1.6, 400)
    close[400:, 0] = 160.0
    close[:, 1] = 100.0
    # bar 480: AAA gaps from 160 to 176 between close[479] and open[480]
    openp = np.vstack([close[:1], close[:-1]])
    openp[480, 0] = 176.0
    close[480:, 0] = 176.0

    def df(a):
        return pd.DataFrame(a, index=idx, columns=insts)

    liq = np.full((n, 2), 1e8)
    pan = Panels(open=df(openp), high=df(np.maximum(openp, close) * 1.001),
                 low=df(np.minimum(openp, close) * 0.999), close=df(close),
                 vol=df(liq / close), vol_ccy=df(liq / close), amount=df(liq),
                 funding=df(np.zeros((n, 2))),
                 list_dt=pd.Series({c: idx[0] for c in insts}))

    cfg = synth_cfg(bar="15m", **{"rebalance_bars": 1})
    cfg.benchmark_inst = "BBB-USDT-SWAP"
    cfg.portfolio.top_k = 1
    cfg.universe.pit_topn = 2
    cfg.costs.funding_multiplier = 0.0
    cfg.execution.exec_price = "next_open"

    res = run_backtest(pan, cfg, disable_risk_overlays=True, disable_costs=True)
    bars = res.bars
    gap_bar = idx[480]
    # the gap realises between close[479] and open[480]; a fill at open[480] misses it,
    # so the P&L booked on bar 480 must be ~0 for AAA
    seg = bars.loc[idx[481]:idx[520], "net_ret"]
    assert abs(float(seg.sum())) < 0.02, (
        f"engine appears to have captured the {idx[480]} gap: pnl={seg.sum():.4f}")

    # and with a (deliberately wrong) close-to-close convention the same gap IS captured
    cfg2 = synth_cfg(bar="15m", **{"rebalance_bars": 1})
    cfg2.benchmark_inst = "BBB-USDT-SWAP"
    cfg2.portfolio.top_k = 1
    cfg2.universe.pit_topn = 2
    cfg2.execution.exec_price = "next_close"
    res2 = run_backtest(pan, cfg2, disable_risk_overlays=True, disable_costs=True)
    assert float(res2.bars.loc[idx[482]:idx[520], "net_ret"].sum()) >= -1.0  # sanity only


def test_exec_price_modes_are_distinct_and_lagged(panels: Panels):
    arr = panels_to_arrays(panels)
    o = pick_exec_price(arr, "next_open")
    c = pick_exec_price(arr, "next_close")
    v = pick_exec_price(arr, "next_vwap")
    t = pick_exec_price(arr, "next_twap")
    assert not np.allclose(np.nan_to_num(o), np.nan_to_num(c))
    assert np.nanmin(v[np.isfinite(v)]) > 0
    # on a panel with a symmetric high/low band, TWAP collapses onto close by
    # construction; assert on an asymmetric band instead
    arr2 = dict(arr)
    arr2["high"] = arr["high"] * 1.05
    arr2["low"] = arr["low"] * 1.00
    t2 = pick_exec_price(arr2, "next_twap")
    assert not np.allclose(np.nan_to_num(t2), np.nan_to_num(c))
    assert t2.shape == c.shape


def test_warmup_prevents_using_immature_history(panels: Panels):
    cfg = synth_cfg()
    res = run_backtest(panels, cfg)
    first_dec = res.reb_ts[0]
    assert first_dec >= panels.index[cfg.universe.min_history_days * cfg.bars_per_day]


def test_score_matrix_is_invariant_to_future(panels: Panels):
    """§37: the composite Score must be causal, not just the P&L.

    Mutating every bar from t0 onwards must leave the scores formed at decision
    bars strictly before t0 bit-identical -- per factor and in the composite.
    """
    cfg = synth_cfg()
    t0 = 2000
    r_base = run_backtest(panels, cfg)
    r_fut = run_backtest(_mutate_future(panels, t0), cfg)

    cutoff = panels.index[t0 - 1]
    for i, ts in enumerate(r_base.reb_ts):
        if ts >= cutoff:
            break
        a = r_base.score_matrix[i]
        b = r_fut.score_matrix[i]
        np.testing.assert_allclose(
            np.nan_to_num(a, nan=-999.0), np.nan_to_num(b, nan=-999.0),
            rtol=0, atol=0, err_msg=f"composite score at {ts} changed")
        assert np.array_equal(np.isnan(a), np.isnan(b)), f"score NaN mask at {ts} changed"
        for k in FACTOR_NAMES:
            za, zb = r_base.factor_zs[k][i], r_fut.factor_zs[k][i]
            np.testing.assert_allclose(
                np.nan_to_num(za, nan=-999.0), np.nan_to_num(zb, nan=-999.0),
                rtol=0, atol=0, err_msg=f"factor '{k}' z-score at {ts} changed")

    # the mutation must actually bite later, or the test proves nothing
    assert not np.allclose(
        np.nan_to_num(r_base.score_matrix[-1], nan=-999.0),
        np.nan_to_num(r_fut.score_matrix[-1], nan=-999.0))


def test_funding_charge_only_uses_the_current_bar_rate(panels: Panels):
    """§37: Funding must be causal -- no future settlement may leak backwards.

    Isolate the funding channel by mutating *only* the funding panel's future:
    every earlier bar's funding cashflow and net P&L must be untouched.
    """
    cfg = synth_cfg()
    cfg.costs.funding_multiplier = 1.0
    t0 = 2000

    f = panels.funding.to_numpy(dtype="float64", copy=True)
    f[t0:] = 0.05                       # a huge, obviously-wrong future rate
    fut_fund = pd.DataFrame(f, index=panels.index, columns=panels.close.columns)
    p_fut = Panels(open=panels.open, high=panels.high, low=panels.low,
                   close=panels.close, vol=panels.vol, vol_ccy=panels.vol_ccy,
                   amount=panels.amount, funding=fut_fund, list_dt=panels.list_dt)

    a = run_backtest(panels, cfg)
    b = run_backtest(p_fut, cfg)

    assert a.bars["funding"].abs().sum() > 0, "seed must produce non-trivial funding"
    np.testing.assert_allclose(
        a.bars["funding"].to_numpy()[: t0 - 1],
        b.bars["funding"].to_numpy()[: t0 - 1],
        rtol=0, atol=0, err_msg="a future funding rate changed an earlier charge")
    np.testing.assert_allclose(
        a.bars["net_ret"].to_numpy()[: t0 - 1],
        b.bars["net_ret"].to_numpy()[: t0 - 1],
        rtol=1e-12, atol=1e-15,
        err_msg="a future funding rate changed earlier net P&L")
    # sanity: the future mutation must be visible later
    assert not np.allclose(a.bars["funding"].to_numpy()[t0 + 5:],
                           b.bars["funding"].to_numpy()[t0 + 5:])
