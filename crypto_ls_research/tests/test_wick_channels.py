"""Guards for how a wick / flash spike reaches the strategy.

The question this file pins down: *if the price prints a violent intrabar spike,
what does the backtest actually see?*  The answer turned out to be counter-intuitive
enough that the first version of the experiment got it wrong, so it is worth
locking down.

A wick enters through **two structurally different paths**, and conflating them
gives a wrong answer in both directions:

1. **The return path reads `open` and nothing else.**
   `ret_exec[t] = open[t+1]/open[t] - 1` (`exec_price="next_open"`).  `high` and
   `low` never appear.  So an intrabar spike that recovers before the next open
   contributes **exactly zero** P&L -- not "approximately zero".  Asserting this
   is what the first test does, and it is the reason the stress script has to
   inject the wick *outside* every decision's lookback window to prove it.

2. **The signal path reads `high`/`low` directly.**
   `range_pos = (close - low.rolling(R).min()) / (high.rolling(R).max() - low.rolling(R).min())`
   is one of the five accepted factors, and `atr_pct = ((high-low)/close).rolling(24).mean()`
   drives the `liquidation_ok` universe filter.  So a single wide bar distorts
   `range_pos` for the next `range_days` bars, i.e. a wick is **information**, not
   just a price event.  The second and third tests pin that, so nobody "fixes" the
   wick-blindness claim by quietly breaking `range_pos`.

3. **A gap that does *not* recover is an `open` move, and its one-bar cost is
   analytic.**  `w * sum(held_i * open[t+1]/open[t])` -- note the price ratio: the
   notional is marked at the **post-shock** price.  Writing it as `w * sum(held)`
   (i.e. `w * net_exposure`) is the natural-looking mistake and is off by ~0.03%.
   The last two tests pin both the exact form and the linearity.

Measured on the real 1h panel (2021-01-01..2026-09-29, `scripts/exp_wick_stress.py`):
the wick-blindness check is `0.000e+00` on every column, the `open`-shock identity
closes to <2e-9 (float32 weight-matrix precision), and a 30%-widening wick on 0.6%
of bars moves the book on **94.7%** of bars.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from crypto_ls_research.backtest.engine import pick_exec_price, run_backtest
from crypto_ls_research.data.store import Panels
from crypto_ls_research.risk.engine import DEFAULT_MMR, liquidation_ok

from .conftest import synth_cfg, synth_panels

# Overlays off, so nothing but the book can react to the perturbation.
#
# ⚠️ This is a `run_backtest` **keyword**, not a config override.  Passing it as
# `synth_cfg(disable_risk_overlays=True)` sets an attribute on `BacktestConfig` that
# `run_backtest` never reads -- the call silently does nothing and every "overlays
# off" assertion quietly runs with the overlays ON.  `test_the_overlay_kwarg_bites`
# below exists so that failure mode cannot come back.
NO_OVERLAY = {"disable_risk_overlays": True}


# `panels_to_arrays` hands the engine **float32** prices, so any one-bar P&L
# difference carries a ~6e-8 relative rounding on `px[t+1]/px[t]`.  With gross
# exposure ~1.0 that is an absolute floor of ~1e-7 on `|measured - predicted|`.
# An earlier version of these tests used 1e-9 and passed only because the synthetic
# book happened to be *exactly* net-flat at the shock bar, which makes the common-mode
# rounding cancel; that is luck, not precision.
F32_FLOOR = 1e-7


def _rebuild(p: Panels, **fields) -> Panels:
    d = {"open": p.open, "high": p.high, "low": p.low, "close": p.close,
         "vol": p.vol, "vol_ccy": p.vol_ccy, "amount": p.amount,
         "funding": p.funding, "list_dt": p.list_dt}
    d.update(fields)
    return Panels(**d)


def _wick(p: Panels, idx, w: float) -> Panels:
    """Widen the bar: raise `high`, lower `low`.  `open`/`close` untouched."""
    hi = p.high.to_numpy("float64").copy()
    lo = p.low.to_numpy("float64").copy()
    hi[np.asarray(idx, dtype=int), :] *= (1.0 + w)
    lo[np.asarray(idx, dtype=int), :] *= (1.0 - w)
    return _rebuild(p, high=pd.DataFrame(hi, index=p.index, columns=p.close.columns),
                    low=pd.DataFrame(lo, index=p.index, columns=p.close.columns))


def _open_shock(p: Panels, t0: int, w: float) -> Panels:
    """Scale `open` from bar `t0` on.  Factors never read `open`, so the book is
    untouched; only `ret_exec` changes."""
    a = p.open.to_numpy("float64").copy()
    a[t0:, :] *= (1.0 + w)
    return _rebuild(p, open=pd.DataFrame(a, index=p.index, columns=p.close.columns))


def _maxdev(a, b) -> float:
    """Max |a-b|, with NaN treated as *equal only to NaN*.

    A plain `np.abs(a-b).max()` returns NaN as soon as either side has a NaN
    anywhere -- and `factor_zs` legitimately has NaNs (warmup, unlisted names), so
    the naive form silently turns every factor comparison into `nan > 0.0`, i.e.
    False.  Returning `inf` when the NaN patterns differ keeps the check honest:
    "both missing" is not a difference, "one missing" is.
    """
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    if a.shape != b.shape:
        return float("inf")
    na, nb = np.isnan(a), np.isnan(b)
    if not np.array_equal(na, nb):
        return float("inf")
    d = np.abs(a - b)
    d[na] = 0.0
    return float(d.max()) if d.size else 0.0


# ---------------------------------------------------------------------------
# 0. the overlays-off switch actually switches something off
# ---------------------------------------------------------------------------
def test_the_overlay_kwarg_bites():
    """`disable_risk_overlays` is a `run_backtest` keyword, not a config field.

    Every test below relies on the overlays being off, and the failure mode is
    silent in both directions: passing it to `synth_cfg` sets an attribute nobody
    reads (the overlays stay ON), and if the kwarg were ever dropped from
    `run_backtest`'s signature the call would raise -- but a *renamed* kwarg that
    lands in `**kwargs` would not.  So assert the two arms actually differ.
    """
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    on = run_backtest(p, cfg)
    off = run_backtest(p, cfg, **NO_OVERLAY)
    assert _maxdev(on.bars["gross_exposure"], off.bars["gross_exposure"]) > 0.0, (
        "disabling the risk overlays changed nothing -- the kwarg is no longer "
        "reaching the engine, and every 'overlays off' assertion here is vacuous")
    assert float(on.bars["total_scale"].min()) < 1.0 or \
        float(on.bars["total_scale"].max()) > 1.0, (
        "with the overlays on, the total scale never left 1.0 -- the overlays did "
        "not run at all, so the comparison above proves nothing")


def test_the_overlay_kwarg_bites_on_the_bars_where_it_runs():
    """`total_scale` is 0.0 on non-decision bars (it is only assigned inside the
    exec branch), so the off-arm check has to be restricted to the bars where the
    scale is actually produced -- otherwise '0.0 != 1.0' fails for a reason that has
    nothing to do with the overlays."""
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    off = run_backtest(p, cfg, **NO_OVERLAY)
    ts = off.bars["total_scale"]
    live = ts[ts != 0.0]
    assert len(live) > 0, "no bar ever produced a total_scale -- nothing to check"
    assert _maxdev(live, np.ones(len(live))) == 0.0, (
        "with the overlays off, every produced `total_scale` must be exactly 1.0")


# ---------------------------------------------------------------------------
# 1. the return path is open-only -- structurally
# ---------------------------------------------------------------------------
def test_next_open_exec_price_is_literally_the_open_array():
    arr = {"open": np.array([1.0, 2.0]), "close": np.array([9.0, 9.0])}
    assert pick_exec_price(arr, "next_open") is arr["open"], (
        "next_open stopped returning the open array itself; the whole wick-blindness "
        "argument rests on this")


def test_the_return_path_is_built_from_the_exec_price_alone():
    """Structural, because a behavioural test cannot see the difference.

    Perturbing `high`/`low` inside a decision's lookback window changes the *book*,
    so the returns change too -- for a reason that has nothing to do with the
    return formula.  Only the source text can pin which arrays `ret_exec` reads.
    Mutation: making `ret_exec` fall back to `arr["close"]`, or computing it from
    `high`/`low`, turns this red.
    """
    src = inspect.getsource(run_backtest)
    i_px = src.find("px = pick_exec_price(")
    i_ret = src.find("ret_exec = np.zeros(")
    i_fill = src.find("ret_exec[:-1] =")
    assert 0 <= i_px < i_ret < i_fill, "the exec-price / return construction moved"
    body = src[i_px:i_fill]
    assert "px_ff = _ffill_rows(px)" in body, (
        "the return series is no longer the forward-filled exec price")
    for bad in ("high", "low", "close"):
        assert bad not in body, (
            f"the return path now reads `{bad}`; a wick would change P&L directly and "
            f"every archived drawdown number would need re-deriving")


# ---------------------------------------------------------------------------
# 2. a wick outside every lookback window is invisible, bit for bit
# ---------------------------------------------------------------------------
def test_a_wick_outside_every_decision_lookback_changes_nothing():
    """Perturb `high`/`low` on the **last** bar only.

    `dec_idx = arange(warmup, T-1, R)`, so bar `T-1` is never a decision bar and no
    factor can read it.  The only thing that consumes it is
    `ret_exec[T-2] = open[T-1]/open[T-2] - 1`, which does not read high/low.  So
    every ledger column must be **bit-identical**.
    """
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    base = run_backtest(p, cfg, **NO_OVERLAY)
    wick = run_backtest(_wick(p, [len(p.index) - 1], 0.9), cfg, **NO_OVERLAY)
    for col in ("net_ret", "gross_ret", "long_ret", "short_ret",
                "gross_exposure", "turnover"):
        assert _maxdev(wick.bars[col], base.bars[col]) == 0.0, (
            f"{col} moved when only the last bar's high/low changed -- the return "
            f"path reads something it should not")


# ---------------------------------------------------------------------------
# 3. ...but a wick *does* reach the signal
# ---------------------------------------------------------------------------
def test_a_wick_inside_the_lookback_window_moves_the_signal():
    """The counterpart, and the reason the blindness test needs its own window.

    `range_pos` is built from `high.rolling(R).max()` and `low.rolling(R).min()`, so
    widening bars for `range_days` re-scales the denominator.  If this ever stops
    being true, the wick is no longer signal information and the stress script's
    channel-S numbers are stale.
    """
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    rng = np.random.default_rng(11)
    idx = rng.choice(np.arange(200, len(p.index) - 2), size=120, replace=False)
    base = run_backtest(p, cfg, **NO_OVERLAY)
    wick = run_backtest(_wick(p, idx, 0.5), cfg, **NO_OVERLAY)

    assert _maxdev(wick.factor_zs["range_pos"], base.factor_zs["range_pos"]) > 0.0, (
        "widening bars no longer changes range_pos -- either the factor stopped "
        "using high/low, or the test's injection window is wrong")
    assert _maxdev(wick.bars["gross_exposure"], base.bars["gross_exposure"]) > 0.0, (
        "range_pos changed but the book did not; channel S in exp_wick_stress.py "
        "would report a false 'signal unaffected' result")


# ---------------------------------------------------------------------------
# 4. an unrecovered gap is an open move, and its cost is analytic
# ---------------------------------------------------------------------------
def _book_at(res, t_bar: int):
    """The weight vector in force at bar `t_bar` (constant between rebalances)."""
    dec_pos = np.array([res.bars.index.get_loc(t) for t in res.reb_ts])
    k = int(np.searchsorted(dec_pos, t_bar - 1, side="right") - 1)
    assert k >= 0, "no rebalance precedes the shock bar"
    assert dec_pos[k] <= t_bar - 1, "picked a decision whose book is not yet in force"
    return np.asarray(res.weight_matrix[k], dtype="float64")


def test_an_open_shock_moves_pnl_but_leaves_the_book_alone():
    """`open` is never read by `compute_factors`, so a gap must be pure P&L.

    Two things are asserted: the book is bit-identical on every bar *before* the
    shock, and the one-bar loss equals `w * sum(held_i * open[t+1]/open[t])`.
    """
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    base = run_backtest(p, cfg, **NO_OVERLAY)
    t0 = 1500
    w = -0.10
    shk = run_backtest(_open_shock(p, t0, w), cfg, **NO_OVERLAY)

    # (a) the book cannot have moved before the shock
    assert _maxdev(shk.bars["gross_exposure"].iloc[:t0],
                   base.bars["gross_exposure"].iloc[:t0]) == 0.0, (
        "shocking `open` changed the book -- `open` has leaked into the factor stack")
    assert _maxdev(shk.bars["net_ret"].iloc[:t0 - 1],
                   base.bars["net_ret"].iloc[:t0 - 1]) == 0.0

    # (b) the one-bar cost is exact, with the post-shock price ratio
    held = _book_at(base, t0 - 1)
    o = p.open.to_numpy("float64")
    ratio = o[t0] / o[t0 - 1]
    pred = w * float(np.nansum(held * ratio))
    meas = float(shk.bars["net_ret"].iloc[t0 - 1] - base.bars["net_ret"].iloc[t0 - 1])
    assert abs(meas - pred) < F32_FLOOR, (
        f"open-shock identity broken: measured {meas!r} vs predicted {pred!r} "
        f"(residual {meas - pred:.3e})")
    # The naive form drops the price ratio.  It is only *measurably* different when
    # the book is not net-flat, so assert on the difference of the two predictions
    # rather than on `meas - naive` -- with an exactly net-flat book the ratio factor
    # cancels out of `sum(held * ratio)` and the naive form is accidentally right.
    naive = w * float(np.nansum(held))
    assert abs(pred - naive) > F32_FLOOR, (
        "`w * net_exposure` now equals the exact answer on this fixture; the "
        "price-ratio factor has gone and the derivation note in exp_wick_stress.py "
        "is stale")

    # (c) `open` is not signal information at all -- not merely "not before the
    # shock".  The selection and the *target* weights come from the factor stack and
    # the universe mask (close / age / ADV), none of which read `open`, so they must
    # be bit-identical for the WHOLE sample.  The *realised* book may still drift
    # afterwards, but only through `adv_cap_delta`, which reads `equity` -- and that
    # is exactly the known non-mirror effect, not a signal leak.
    #
    # This is the assertion that catches `range_pos` starting to read `open`: without
    # it, an `open`-shock test only inspects bars before the shock, where `open` was
    # never perturbed, and the leak sails through.
    assert [r["long"] for r in shk.rebalances] == [r["long"] for r in base.rebalances], (
        "shocking `open` changed the long selection -- `open` has leaked into the "
        "factor stack (check `range_pos`, which is built from high/low, not open)")
    assert [r["short"] for r in shk.rebalances] == [r["short"] for r in base.rebalances]
    for ra, rb in zip(base.rebalances, shk.rebalances):
        assert ra["w_long"] == rb["w_long"] and ra["w_short"] == rb["w_short"], (
            "the target weights moved under an `open` shock; they are computed before "
            "the ADV cap, so nothing equity-dependent should reach them")


def test_the_open_shock_cost_is_linear_in_the_shock_size():
    """A doubling of the gap must double the one-bar loss -- no hidden cap.

    Asserted as `d2 == 2*d1` rather than `d2/d1 == 2`.  The ratio is dominated by
    whatever absolute noise sits in `d1` (here ~1e-9, the float32 precision of
    `weight_matrix`), which at `d1 ~ 1.8e-4` shows up as a 6e-6 wobble in the ratio
    and would fail a tight ratio test for no good reason.  The difference form has a
    noise floor of the same ~1e-9 and is stable.
    """
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg()
    base = run_backtest(p, cfg, **NO_OVERLAY)
    t0 = 1500
    d1 = float(run_backtest(_open_shock(p, t0, -0.25), cfg, **NO_OVERLAY)
               .bars["net_ret"].iloc[t0 - 1] - base.bars["net_ret"].iloc[t0 - 1])
    d2 = float(run_backtest(_open_shock(p, t0, -0.50), cfg, **NO_OVERLAY)
               .bars["net_ret"].iloc[t0 - 1] - base.bars["net_ret"].iloc[t0 - 1])
    assert abs(d1) > 1e-6, (
        f"a 25% gap on the whole cross-section moved P&L by only {d1:.3e}; either the "
        f"book is net-flat at that bar or the shock did not land")
    assert abs(d2 - 2.0 * d1) < 4.0 * F32_FLOOR, (
        f"loss is not linear in the shock: d1={d1!r}, d2={d2!r}, "
        f"d2-2*d1={d2 - 2.0 * d1:.3e}")


# ---------------------------------------------------------------------------
# 5. the liquidation filter's calibration
# ---------------------------------------------------------------------------
def test_liquidation_filter_threshold_is_three_atr_at_five_x():
    """`3 * ATR% <= 1/L_in_force - mmr` -> ATR% <= 6.5% at 5x.

    This is the only wick-specific *pre-trade* defence in the system, so its
    calibration is pinned -- including the direction, which is easy to state
    backwards: **raising** the leverage ceiling *tightens* the filter, because it
    shrinks the liquidation distance `1/L - mmr`.
    """
    rc5 = synth_cfg().risk
    rc5.leverage_in_force, rc5.min_liquidation_atr_multiple = 5.0, 3.0
    thr5 = (1.0 / rc5.leverage_in_force - DEFAULT_MMR) / rc5.min_liquidation_atr_multiple
    assert abs(thr5 - 0.065) < 1e-12, f"threshold moved: {thr5}"

    atr = np.array([0.03, thr5 - 1e-9, thr5, thr5 + 1e-9, 0.12, np.nan])
    assert list(liquidation_ok(atr, rc5)) == [True, True, True, False, False, False], (
        "the liquidation-distance filter is off by one at the boundary, or it no "
        "longer treats an unknown ATR as untradeable")

    rc20 = synth_cfg().risk
    rc20.leverage_in_force, rc20.min_liquidation_atr_multiple = 20.0, 3.0
    thr20 = (1.0 / rc20.leverage_in_force - DEFAULT_MMR) / rc20.min_liquidation_atr_multiple
    assert thr20 < thr5, "raising the leverage ceiling must tighten the filter"
    assert bool(liquidation_ok(np.array([0.07]), rc20)[0]) is False, (
        "a 7% ATR name passed at 20x, where the liquidation distance is only 4.5%")

    rc3 = synth_cfg().risk
    rc3.leverage_in_force, rc3.min_liquidation_atr_multiple = 3.0, 3.0
    thr3 = (1.0 / rc3.leverage_in_force - DEFAULT_MMR) / rc3.min_liquidation_atr_multiple
    assert thr3 > thr5, "lowering the leverage ceiling must loosen the filter"
    assert bool(liquidation_ok(np.array([0.07]), rc3)[0]) is True, (
        "a 7% ATR name was rejected at 3x, where the liquidation distance is 32.8%")
