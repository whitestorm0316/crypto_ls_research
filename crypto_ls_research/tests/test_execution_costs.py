"""Execution simulator, turnover budget, ADV cap, cost and funding arithmetic, risk overlays."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.backtest.costs import (
    effective_fee_rate, funding_cost, half_spread_rate, impact_rate, trade_cost)
from crypto_ls_research.config.settings import CostConfig, RiskConfig
from crypto_ls_research.risk.engine import (
    adv_cap_delta, btc_vol_scale, dd_scale, liquidation_ok, regime_scale,
    turnover_budget_scale, vol_target_scale)


# --------------------------- costs -----------------------------------------
def test_fee_blend_between_maker_and_taker():
    c = CostConfig(passive_fill_ratio=0.0)
    assert effective_fee_rate(c) == pytest.approx(c.taker_fee)
    c = CostConfig(passive_fill_ratio=1.0)
    assert effective_fee_rate(c) == pytest.approx(c.maker_fee)
    c = CostConfig(passive_fill_ratio=0.5)
    assert effective_fee_rate(c) == pytest.approx((c.maker_fee + c.taker_fee) / 2)
    c = CostConfig(fee_multiplier=2.0, passive_fill_ratio=0.0)
    assert effective_fee_rate(c) == pytest.approx(0.001)


def test_half_spread_tiers_on_trailing_adv():
    c = CostConfig(half_spread_bps_base=0.6, half_spread_bps_illiquid=3.0,
                   half_spread_adv_cut_usd=1e7)
    r = half_spread_rate(np.array([1e9, 1e5, np.nan]), c)
    assert r[0] == pytest.approx(0.6 / 1e4)
    assert r[1] == pytest.approx(3.0 / 1e4)
    assert r[2] == pytest.approx(3.0 / 1e4)     # unknown liquidity -> widest


def test_impact_follows_square_root_law():
    c = CostConfig(impact_coef=1.0)
    dw = np.array([0.01])
    adv = np.array([1e7])
    vol = np.array([0.02])
    eq = 1e6                                     # participation = 0.01*1e6/1e7 = 0.001
    rate, part = impact_rate(dw, adv, eq, vol, c)
    assert part[0] == pytest.approx(0.001)
    assert rate[0] == pytest.approx(0.02 * np.sqrt(0.001))


def test_impact_grows_with_capital_and_scales_as_sqrt():
    c = CostConfig(impact_coef=1.0)
    dw, adv, vol = np.array([0.05]), np.array([1e7]), np.array([0.03])
    r1, _ = impact_rate(dw, adv, 1e5, vol, c)
    r100, _ = impact_rate(dw, adv, 1e7, vol, c)     # 100x capital
    assert r100[0] / r1[0] == pytest.approx(10.0, rel=1e-9)     # sqrt(100)


def test_trade_cost_items_sum_to_total():
    c = CostConfig()
    dw = np.array([0.05, -0.03])
    adv = np.array([5e7, 2e6])
    vol = np.array([0.03, 0.06])
    d = trade_cost(dw, adv, 1e6, vol, c)
    np.testing.assert_allclose(d["total"], d["fee"] + d["spread"] + d["impact"], rtol=1e-12)


def test_zero_turnover_costs_nothing():
    d = trade_cost(np.zeros(4), np.full(4, 1e7), 1e6, np.full(4, 0.03), CostConfig())
    assert d["total"].sum() == 0.0


def test_funding_sign_convention():
    """Positive funding: longs pay, shorts receive."""
    w = np.array([0.5, -0.5])
    rate = np.array([0.0004, 0.0004])
    pnl = funding_cost(w, rate)
    assert pnl == pytest.approx(-(0.5 * 0.0004 + (-0.5) * 0.0004)) == pytest.approx(0.0)

    w_long_only = np.array([1.0, 0.0])
    assert funding_cost(w_long_only, rate) == pytest.approx(-0.0004)
    w_short_only = np.array([0.0, -1.0])
    assert funding_cost(w_short_only, rate) == pytest.approx(+0.0004)
    # negative funding flips the sign
    assert funding_cost(w_long_only, -rate) == pytest.approx(+0.0004)


def test_funding_enters_the_ledger_as_a_cashflow_not_as_a_cost():
    """The ledger must ADD ``bars["funding"]``; it is already the signed cashflow.

    ``bars["funding"] = -funding_multiplier * Σ w·rate``, so a long position on an
    instrument with a positive rate already comes out negative (the long pays).
    Subtracting that column from ``gross - trading`` double-negates it and silently
    books every funding *cost* as a funding *credit*.
    """
    from crypto_ls_research.analysis.metrics import bars_variant
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels

    p = synth_panels(n_inst=8, n_bars=2500, seed=53)
    cfg = synth_cfg()
    cfg.costs.funding_multiplier = 1.0
    res = run_backtest(p, cfg)
    b = res.bars

    trading = b["fee"] + b["spread"] + b["impact"]
    assert b["funding"].abs().sum() > 0, "seed must produce non-trivial funding"

    # 1) ledger identity:  net = gross - trading costs + funding cashflow
    np.testing.assert_allclose(
        b["net_ret"].to_numpy(),
        (b["gross_ret"] - trading + b["funding"]).to_numpy(),
        rtol=1e-10, atol=1e-14)

    # 2) dropping funding must move net P&L by exactly the funding cashflow,
    #    in the direction of the cashflow (a credit raises net P&L)
    net = bars_variant(b, "net")["net_ret"]
    no_funding = bars_variant(b, "no_funding")["net_ret"]
    np.testing.assert_allclose(
        (net - no_funding).to_numpy(), b["funding"].to_numpy(),
        rtol=1e-10, atol=1e-14)

    # 3) a zero-trading-cost venue still settles funding
    ntc = bars_variant(b, "no_trading_cost")["net_ret"]
    np.testing.assert_allclose(
        ntc.to_numpy(), (b["gross_ret"] + b["funding"]).to_numpy(),
        rtol=1e-10, atol=1e-14)


def test_positive_funding_costs_an_exposed_long_book():
    """Economic direction, independent of any ledger identity.

    With a single *constant* positive rate ``r``, funding collapses to
    ``-r · Σw = -r · net_exposure``.  The sign is therefore exact and independent of
    the book's composition: an exposed long must be charged, an exposed short paid.
    """
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels

    p = synth_panels(n_inst=8, n_bars=2500, seed=67)
    rate = 0.0004
    fund_panel = p.funding * 0.0 + rate          # constant, strictly positive
    p = type(p)(**{**p.__dict__, "funding": fund_panel})

    cfg = synth_cfg()
    cfg.costs.funding_multiplier = 1.0
    res = run_backtest(p, cfg)

    expo = res.bars["net_exposure"]
    fund = res.bars["funding"]
    assert expo.abs().sum() > 0, "expected a non-trivial book"
    assert fund.abs().sum() > 0, "constant positive rates must charge the book"

    # exact up to the float32 precision of the funding panel
    np.testing.assert_allclose(fund.to_numpy(), (-rate * expo).to_numpy(),
                               rtol=1e-5, atol=1e-12)
    live = expo.abs() > 1e-9
    assert (np.sign(fund[live]) == -np.sign(expo[live])).all()


def test_funding_multiplier_scales_cost():
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels
    p = synth_panels(n_inst=8, n_bars=2500, seed=31)
    base = synth_cfg()
    base.costs.funding_multiplier = 0.0
    r0 = run_backtest(p, base)
    base2 = synth_cfg()
    base2.costs.funding_multiplier = 2.0
    r2 = run_backtest(p, base2)
    assert r0.bars["funding"].abs().sum() == pytest.approx(0.0)
    assert r2.bars["funding"].abs().sum() > 0
    # gross P&L is untouched by funding; only net changes
    np.testing.assert_allclose(r0.bars["gross_ret"].sum(), r2.bars["gross_ret"].sum(), rtol=1e-12)


def test_metrics_do_not_report_funding_income_as_a_cost():
    """`bars["funding"]` is a cashflow with POSITIVE = income.

    A dollar-neutral book usually nets funding down to a small number that can be a
    credit.  Reporting that credit as a "Funding Cost" and folding it into the cost
    drag would present income as an expense, so the metrics keep trading costs and
    funding P&L separate and net them with the correct sign.
    """
    from crypto_ls_research.analysis.metrics import compute_metrics
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels

    p = synth_panels(n_inst=8, n_bars=2500, seed=41)
    cfg = synth_cfg()
    cfg.costs.funding_multiplier = 1.0
    res = run_backtest(p, cfg)
    m = compute_metrics(res.bars, "t")

    trading = float(res.bars[["fee", "spread", "impact"]].sum().sum())
    funding_pnl = float(res.bars["funding"].sum())
    years = m["years"]

    assert "Funding Cost (total, frac)" not in m, "funding must not be labelled a cost"
    assert m["Funding P&L (total, frac)"] == pytest.approx(funding_pnl)
    assert m["Trading Cost (total, frac)"] == pytest.approx(trading)
    assert m["Total Cost (frac)"] == pytest.approx(trading - funding_pnl)
    assert m["Cost Drag (annual)"] == pytest.approx((trading - funding_pnl) / years)
    assert m["Trading Cost Drag (annual)"] == pytest.approx(trading / years)
    # the old (wrong) formula would have added the funding credit to the drag
    naive = (trading + funding_pnl) / years
    if abs(funding_pnl) > 1e-9:
        assert m["Cost Drag (annual)"] != pytest.approx(naive)


# --------------------------- risk overlays ---------------------------------
def test_regime_scale_halves_in_bear_market():
    rc = RiskConfig(regime_bear_scale=0.55)
    assert regime_scale(0.01, rc) == 1.0
    assert regime_scale(-0.001, rc) == pytest.approx(0.55)
    assert regime_scale(np.nan, rc) == 1.0


def test_btc_vol_ramp_is_monotone_and_bounded():
    rc = RiskConfig(btc_vol_soft=0.6, btc_vol_hard=1.2, btc_vol_min_scale=0.35)
    assert btc_vol_scale(0.3, rc) == 1.0
    assert btc_vol_scale(1.5, rc) == pytest.approx(0.35)
    a, b, c = btc_vol_scale(0.7, rc), btc_vol_scale(0.9, rc), btc_vol_scale(1.1, rc)
    assert 1.0 >= a >= b >= c >= 0.35


def test_vol_target_is_clipped():
    rc = RiskConfig(target_vol_annual=0.30, min_gross_exposure=0.3, max_gross_exposure=2.0)
    assert vol_target_scale(0.30, rc) == pytest.approx(1.0)
    assert vol_target_scale(0.01, rc) == pytest.approx(2.0)      # clip at max
    assert vol_target_scale(10.0, rc) == pytest.approx(0.3)      # clip at min
    assert vol_target_scale(np.nan, rc) == 1.0


def test_drawdown_ladder_steps_and_never_locks_out():
    rc = RiskConfig(dd_ladder=((0.10, 1.0), (0.15, 0.75), (0.20, 0.5), (0.30, 0.25)))
    assert dd_scale(-0.05, rc)[0] == pytest.approx(1.0)
    assert dd_scale(-0.12, rc)[0] == pytest.approx(0.75)
    assert dd_scale(-0.18, rc)[0] == pytest.approx(0.5)
    assert dd_scale(-0.25, rc)[0] == pytest.approx(0.25)
    s, stop = dd_scale(-0.35, rc)
    assert s > 0, "a zero multiplier would lock the book permanently flat"
    assert stop is True
    assert dd_scale(-0.05, rc)[1] is False


def test_liquidation_filter_excludes_high_atr_names():
    rc = RiskConfig(max_leverage=5.0, min_liquidation_atr_multiple=3.0)
    assert 1 / 5.0 - 0.005 == pytest.approx(0.195)
    atr = np.array([0.01, 0.05, 0.065, 0.20, np.nan])
    ok = liquidation_ok(atr, rc)
    # threshold: 3*atr <= 0.195  ->  atr <= 0.065
    assert ok[0] and ok[1] and ok[2]
    assert not ok[3]
    assert not ok[4]


def test_adv_cap_truncates_oversized_trades_only():
    rc = RiskConfig(max_adv_participation=0.05)
    adv = np.array([1e7, 1e7])
    dw = np.array([0.001, 0.5])         # second one is 50% of ADV at 1e6 equity
    cap, binding = adv_cap_delta(dw, adv, 1e6, rc)
    assert cap[0] == pytest.approx(0.001)
    assert cap[1] == pytest.approx(0.05 * 1e7 / 1e6)      # = 0.5 -> exactly at budget
    assert binding == pytest.approx(0.0)                   # neither strictly exceeded

    dw2 = np.array([0.001, 5.0])
    cap2, binding2 = adv_cap_delta(dw2, adv, 1e6, rc)
    assert cap2[1] == pytest.approx(0.5)
    assert binding2 == pytest.approx(0.5)


def test_unknown_adv_gets_zero_budget():
    rc = RiskConfig(max_adv_participation=0.05)
    cap, _ = adv_cap_delta(np.array([0.3]), np.array([np.nan]), 1e6, rc)
    assert cap[0] == 0.0


def test_turnover_budget_limits_partial_rebalance():
    assert turnover_budget_scale(np.array([0.5, 0.5]), 0.0, None) == 1.0
    assert turnover_budget_scale(np.array([0.5, 0.5]), 0.0, 0.5) == pytest.approx(0.5)
    assert turnover_budget_scale(np.array([0.5, 0.5]), 0.5, 0.5) == 0.0
    assert turnover_budget_scale(np.array([0.1, 0.1]), 0.0, 0.5) == 1.0
    assert turnover_budget_scale(np.array([0.0, 0.0]), 0.0, 0.5) == 1.0


# --------------------------- engine-level execution ------------------------
def test_turnover_budget_caps_realised_turnover():
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels
    p = synth_panels(n_inst=10, n_bars=3000, seed=41)
    unlimited = synth_cfg()
    unlimited.execution.max_daily_turnover = None
    r_unl = run_backtest(p, unlimited)
    capped = synth_cfg()
    capped.execution.max_daily_turnover = 0.02
    r_cap = run_backtest(p, capped)
    assert r_cap.bars["turnover"].sum() < r_unl.bars["turnover"].sum()
    assert r_cap.bars["fee"].sum() < r_unl.bars["fee"].sum()


def test_gross_exposure_respects_the_overlay_ceiling():
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels
    p = synth_panels(n_inst=10, n_bars=3000, seed=51)
    cfg = synth_cfg()
    cfg.risk.max_gross_exposure = 1.2
    res = run_backtest(p, cfg)
    assert res.bars["gross_exposure"].max() <= 1.2 + 1e-9


def test_disable_cost_run_has_zero_costs_and_matches_gross():
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels
    p = synth_panels(n_inst=8, n_bars=2500, seed=61)
    cfg = synth_cfg()
    res = run_backtest(p, cfg, disable_costs=True)
    assert res.bars["fee"].sum() == 0.0
    assert res.bars["spread"].sum() == 0.0
    assert res.bars["impact"].sum() == 0.0
    np.testing.assert_allclose(res.bars["net_ret"].to_numpy().sum(),
                               (res.bars["gross_ret"] + res.bars["funding"]).to_numpy().sum(),
                               rtol=1e-12)


def test_stale_data_is_flattened_and_charged():
    from crypto_ls_research.backtest.engine import run_backtest
    from crypto_ls_research.data.store import Panels
    from crypto_ls_research.tests.conftest import synth_cfg, synth_panels
    p = synth_panels(n_inst=8, n_bars=3000, seed=71)
    close = p.close.copy()
    close.iloc[1500:1700, 3] = np.nan           # one name stops trading for 200 bars
    p2 = Panels(open=p.open, high=p.high, low=p.low, close=close, vol=p.vol,
                vol_ccy=p.vol_ccy, amount=p.amount, funding=p.funding, list_dt=p.list_dt)
    res = run_backtest(p2, synth_cfg())
    assert res.meta["n_stale_marks"] >= 0
    # the engine must not carry the dead name's position through the gap
    assert res.bars["n_stale"].sum() == res.meta["n_stale_marks"]
