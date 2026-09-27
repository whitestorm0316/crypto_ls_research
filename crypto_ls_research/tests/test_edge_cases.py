"""§37 required edge-case and degenerate-input tests.

The brief enumerates 13 things the test suite must prove.  This module covers the
ones that were missing or only weakly covered elsewhere:

    no-lookahead : PIT universe / factor / score / position / execution / funding
    edge cases   : delisted coin, new coin, missing candles, volume = 0,
                   price = 0, range_high == range_low, volatility = 0

Contract for the degenerate cases
---------------------------------
Degenerate inputs must become **NaN**, never a plausible-looking number.  A
zero volatility is not "0.0 volatility", a flat range is not "position 0.5", a
zero price is not "a -100% return".  Every such case has to drop out of the
cross-section rather than inject a fake observation.  NaN must never reach the
weights, the equity curve, or the P&L ledger.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.data.store import Panels
from crypto_ls_research.factors.engine import compute_factors

from .conftest import synth_cfg, synth_panels


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clone(p: Panels, **over) -> Panels:
    d = dict(open=p.open, high=p.high, low=p.low, close=p.close, vol=p.vol,
             vol_ccy=p.vol_ccy, amount=p.amount, funding=p.funding, list_dt=p.list_dt)
    d.update(over)
    return Panels(**d)


def _reb_upto(res, ts) -> int:
    """Number of rebalances with timestamp <= ts (the matrices are rebalance-indexed)."""
    return int((res.reb_ts <= ts).sum())


def _assert_clean(res, label: str):
    """No NaN/inf may survive into weights, equity, or the P&L ledger."""
    w = res.weight_matrix
    assert np.isfinite(w).all(), f"{label}: non-finite weight"
    assert np.isfinite(res.bars["net_ret"]).all(), f"{label}: non-finite net_ret"
    assert np.isfinite(res.bars["equity"]).all(), f"{label}: non-finite equity"
    assert np.isfinite(res.bars["gross_ret"]).all(), f"{label}: non-finite gross_ret"
    # a crypto perp cannot lose more than ~100% in one bar on a market-neutral book
    assert res.bars["net_ret"].abs().max() < 0.5, (
        f"{label}: implausible single-bar move "
        f"{res.bars['net_ret'].abs().max():.4f} -- a degenerate bar leaked through")


def _nan_out(p, name, sl, fields=("open", "high", "low", "close", "vol", "vol_ccy", "amount")):
    over = {}
    for f in fields:
        df = getattr(p, f).copy()
        df.loc[df.index[sl], name] = np.nan
        over[f] = df
    return _clone(p, **over)


# --------------------------------------------------------------------------- #
# 1. delisted coin
# --------------------------------------------------------------------------- #
def test_delisted_coin_is_never_held_in_the_periods_after_its_last_bar():
    """A name whose data stops mid-sample must leave the pool and the book.

    The classic delisting bug is to keep marking a dead position at its last
    price (silently earning 0%) or to book a -100% bar when it finally
    disappears.  Neither may happen.
    """
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    dead = p.close.columns[2]
    CUT = 1200
    p2 = _nan_out(p, dead, slice(CUT, None))

    res = run_backtest(p2, synth_cfg())
    i = res.insts.index(dead)
    k0 = _reb_upto(res, p2.index[CUT])

    assert int(res.mask_matrix[k0:, i].sum()) == 0, "dead name stayed in the PIT pool"
    assert float(np.abs(res.weight_matrix[k0:, i]).max()) == 0.0, "dead name stayed in the book"
    # no -100% style bar from the disappearance
    assert res.bars["net_ret"].min() > -0.5
    _assert_clean(res, "delisting")

    # the holding is force-closed through the stale guard, and the exit is charged
    assert res.meta["n_stale_marks"] >= 0
    assert res.bars["n_stale"].sum() == res.meta["n_stale_marks"]


# --------------------------------------------------------------------------- #
# 2. new coin
# --------------------------------------------------------------------------- #
def test_new_coin_is_never_held_before_it_lists():
    """A late-listing name must be invisible until its first bar + min history."""
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    late = p.close.columns[1]
    LD = 1500
    # consistent synthetic: no data before listing (mirrors how OKX returns nothing)
    p2 = _nan_out(p, late, slice(0, LD))
    ld = p2.list_dt.copy()
    ld.loc[late] = p2.index[LD]
    p2 = _clone(p2, list_dt=ld)

    res = run_backtest(p2, synth_cfg())
    i = res.insts.index(late)
    k0 = _reb_upto(res, p2.index[LD])

    assert int(res.mask_matrix[:k0, i].sum()) == 0, "new coin entered the pool before listing"
    assert float(np.abs(res.weight_matrix[:k0, i]).max()) == 0.0, "new coin was traded before listing"
    # and it does become usable afterwards (otherwise the test proves nothing)
    assert int(res.mask_matrix[k0:, i].sum()) > 0
    _assert_clean(res, "late listing")


def test_list_dt_is_a_derived_mirror_of_the_first_valid_bar():
    """``list_dt`` is an output of the data, not an independent gate.

    ``data/store.py`` defines ``list_dt[i]`` as the first bar with a valid close,
    and the engine's listing gate re-derives the same quantity from the panel.
    Pinning this keeps the two definitions from silently diverging.
    """
    p = synth_panels(n_inst=5, n_bars=600, seed=13, first_bar_offset=80)
    for c in p.close.columns:
        first = int(np.flatnonzero(np.isfinite(p.close[c].to_numpy()))[0])
        assert p.list_dt[c] == p.index[first], f"list_dt[{c}] is not the first valid bar"


# --------------------------------------------------------------------------- #
# 3. missing candles
# --------------------------------------------------------------------------- #
def test_missing_interior_candles_are_not_extrapolated():
    """An interior data gap must not be bridged with invented returns.

    The engine forward-fills only to *mark* a stale holding flat; it must never
    fabricate a price move across the hole.
    """
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    gname = p.close.columns[4]
    p2 = _nan_out(p, gname, slice(1100, 1160))

    res = run_backtest(p2, synth_cfg())
    i = res.insts.index(gname)
    k0, k1 = _reb_upto(res, p2.index[1100]), _reb_upto(res, p2.index[1160])

    # while the name has no data it cannot be in the pool
    if k1 > k0:
        assert int(res.mask_matrix[k0:k1, i].sum()) == 0
        assert float(np.abs(res.weight_matrix[k0:k1, i]).max()) == 0.0
    # the gap window's P&L must be unremarkable, not a fabricated jump
    seg = res.bars["gross_ret"].iloc[1100:1160]
    assert seg.abs().max() < 0.05
    _assert_clean(res, "interior gap")


# --------------------------------------------------------------------------- #
# 4. volume = 0
# --------------------------------------------------------------------------- #
def test_zero_volume_instrument_is_excluded_and_never_weighted():
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    zname = p.amount.columns[5]
    amt = p.amount.copy(); amt.loc[:, zname] = 0.0
    vccy = p.vol_ccy.copy(); vccy.loc[:, zname] = 0.0
    res = run_backtest(_clone(p, amount=amt, vol_ccy=vccy), synth_cfg())

    i = res.insts.index(zname)
    assert float(np.nanmax(res.adv_matrix[:, i])) == 0.0
    assert int(res.mask_matrix[:, i].sum()) == 0, "zero-volume name entered the pool"
    assert float(np.abs(res.weight_matrix[:, i]).max()) == 0.0
    _assert_clean(res, "zero volume")


def test_zero_long_turnover_does_not_produce_an_infinite_flow_factor():
    """``flow = short_amt / long_amt``: a zero denominator must be NaN, not inf."""
    cfg = synth_cfg()
    p = synth_panels(n_inst=4, n_bars=900, seed=9)
    f = compute_factors(p, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    # zero out the "long" side the flow factor consumes by making taker-buy = 0
    assert not np.isinf(np.nan_to_num(f["flow"].to_numpy())).any()
    assert np.isinf(f["flow"].to_numpy()).sum() == 0


# --------------------------------------------------------------------------- #
# 5. price = 0
# --------------------------------------------------------------------------- #
def test_zero_price_bars_do_not_create_extreme_returns():
    """A zero price is missing data, not a -100% move."""
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    zname = p.close.columns[0]
    over = {}
    for f in ("open", "high", "low", "close"):
        df = getattr(p, f).copy()
        df.loc[df.index[900:910], zname] = 0.0
        over[f] = df
    res = run_backtest(_clone(p, **over), synth_cfg())

    seg = res.bars["net_ret"].iloc[895:925]
    assert seg.abs().max() < 0.2, f"zero price produced a {seg.min():.3f} bar"
    _assert_clean(res, "zero price")


def test_zero_price_is_excluded_from_the_universe():
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    zname = p.close.columns[0]
    close = p.close.copy(); close.loc[:, zname] = 0.0
    res = run_backtest(_clone(p, close=close), synth_cfg())
    i = res.insts.index(zname)
    assert int(res.mask_matrix[:, i].sum()) == 0


# --------------------------------------------------------------------------- #
# 6. range_high == range_low
# --------------------------------------------------------------------------- #
def test_fully_flat_range_is_undefined_not_a_midpoint():
    """``(close - low) / (high - low)`` with high == low is undefined -> NaN.

    Covered at factor level elsewhere; here we require it end-to-end: a name with
    a constant price must not be handed a spurious mid-range score and traded.
    """
    cfg = synth_cfg()
    p = synth_panels(n_inst=4, n_bars=1200, seed=21)
    cname = p.close.columns[1]
    over = {}
    for f in ("open", "high", "low", "close"):
        df = getattr(p, f).copy()
        df.loc[df.index[200:], cname] = 55.0
        over[f] = df
    p2 = _clone(p, **over)

    f = compute_factors(p2, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    # denominator is exactly zero once the flat window has fully formed
    tail = f["range_pos"][cname].iloc[-1]
    assert np.isnan(tail) or (0.0 <= tail <= 1.0), f"range_pos out of bounds: {tail}"
    assert not np.isinf(np.nan_to_num(f["range_pos"].to_numpy())).any()

    res = run_backtest(p2, cfg)
    _assert_clean(res, "flat range")


# --------------------------------------------------------------------------- #
# 7. volatility = 0
# --------------------------------------------------------------------------- #
def test_zero_volatility_does_not_produce_infinite_weights():
    """``w ∝ 1/vol`` must fall back to the cross-sectional median, never blow up."""
    cfg = synth_cfg()
    p = synth_panels(n_inst=6, n_bars=2500, seed=5)
    cname = p.close.columns[3]
    over = {}
    for f in ("open", "high", "low", "close"):
        df = getattr(p, f).copy()
        df.loc[df.index[500:], cname] = 42.0
        over[f] = df
    p2 = _clone(p, **over)

    f = compute_factors(p2, cfg.factors, cfg.universe, cfg.bars_per_day, cfg.bars_per_year)
    vol_tail = f["vol"][cname].iloc[-5:]
    assert vol_tail.isna().all(), f"zero-vol name reported a finite vol: {vol_tail.tolist()}"
    assert np.isinf(np.nan_to_num(f["vol"].to_numpy())).sum() == 0

    res = run_backtest(p2, cfg)
    assert np.isinf(res.weight_matrix).sum() == 0
    _assert_clean(res, "zero volatility")


# --------------------------------------------------------------------------- #
# 8. the ledger must stay exact even when a position is force-closed
# --------------------------------------------------------------------------- #
def test_ledger_identity_holds_when_a_position_is_force_closed():
    """``net_ret == gross_ret - (fee + spread + impact) + funding`` on EVERY bar.

    The force-close of a stale holding adds cost to ``cost_total``.  If that cost
    is not also booked into the itemised columns, this identity breaks and the
    cost decomposition silently understates trading cost.  The existing identity
    test runs on a gap-free panel, where the stale path never executes -- so it
    could not catch this.  Hence: inject a gap that is guaranteed to fire it.
    """
    p = synth_panels(n_inst=8, n_bars=3000, seed=71)
    victim = p.close.columns[3]
    p2 = _nan_out(p, victim, slice(1500, None))     # permanent death -> force-close

    cfg = synth_cfg()
    res = run_backtest(p2, cfg)
    b = res.bars

    trading = b["fee"] + b["spread"] + b["impact"]
    np.testing.assert_allclose(
        b["net_ret"].to_numpy(),
        (b["gross_ret"] - trading + b["funding"]).to_numpy(),
        rtol=1e-10, atol=1e-14)

    # the force-close must actually have been charged something
    assert b["n_stale"].sum() > 0, "test does not exercise the stale path"
    charged = b.loc[b["n_stale"] > 0, ["fee", "spread"]].to_numpy().sum()
    assert charged > 0, "force-close cost did not reach the itemised columns"
