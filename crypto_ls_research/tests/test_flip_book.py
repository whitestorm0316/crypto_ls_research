"""Guards for `run_backtest(flip_book=...)` -- trading the book backwards.

The switch exists for one question: *is the book's return directional?*  If it is,
the mirrored book must lose, and by construction it must lose **more** than the
original gains, because the same turnover is paid twice:

    net_flip = -(net_normal + 2 * cost_normal)

Three things here are easy to get wrong and each gets an assertion:

1. **Where the flip is applied.**  Negating the *assembled target* (after
   `select_book` / `build_units`) is an exact mirror.  Negating the *score* is not:
   `_stable_book`'s incumbency buffer is path-dependent, so `score = -score` would
   move the selection too and the comparison would be confounded.  The structural
   test pins the placement.
2. **That it really is the same book.**  `gross_flip == -gross_normal` to the last
   bit, with identical turnover and identical selection sizes -- not merely
   "opposite sign on average".
3. **That "exact" has a known exception.**  `adv_cap_delta` scales each name by
   `min(1, budget / (|delta| * equity))`.  That reads the *equity level*, so it is
   not sign-symmetric: once the two arms' equity paths diverge, the cap engages on
   one side only and the mirror stops being exact.  The last test pins that
   mechanism so nobody "fixes" the residual by loosening the mirror claim.
"""
from __future__ import annotations

import inspect

import numpy as np

from crypto_ls_research.backtest.engine import run_backtest

from .conftest import synth_cfg, synth_panels

# Disable everything that reads the equity path, so nothing is left that could
# break the antisymmetry.  Any deviation under these settings is a real bug.
PURE = {"disable_risk_overlays": True, "disable_costs": True}
# `max_adv_participation` is the one remaining equity-dependent gate; push it out of
# reach to get the mathematically exact mirror.
NO_ADV_CAP = {"risk.max_adv_participation": 1e9}


def _maxdev(a, b):
    return float(np.abs(np.asarray(a) - np.asarray(b)).max())


def _run(flip: bool, cfg_ov=None, **kw):
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    cfg = synth_cfg(**{"risk.max_adv_participation": 1e9, **(cfg_ov or {})})
    return run_backtest(p, cfg, flip_book=flip, **kw)


# ---------------------------------------------------------------------------
# 1. the knob exists and defaults to off
# ---------------------------------------------------------------------------
def test_flip_book_is_an_off_by_default_keyword():
    sig = inspect.signature(run_backtest)
    assert "flip_book" in sig.parameters, "the flip switch disappeared"
    assert sig.parameters["flip_book"].default is False, (
        "flip_book must default to False -- a truthy default would silently invert "
        "every existing caller, including the live daemon")


def test_the_flip_is_applied_to_the_target_not_to_the_score():
    """Structural, and deliberately so.

    A behavioural test cannot tell the two implementations apart on a single
    rebalance -- they agree there.  They diverge only through the incumbency buffer
    over many rebalances, which is exactly the confound this pins out.  Mutation:
    moving the `base = -base` line above the `sel = select_book(...)` line, or
    replacing it with a `score` negation, turns this red.
    """
    src = inspect.getsource(run_backtest)
    i_sel = src.rfind("select_book(")
    i_flip = src.find("if flip_book:")
    assert i_sel >= 0 and i_flip >= 0, "flip_book is no longer wired into run_backtest"
    assert i_flip > i_sel, (
        "the flip is applied before the selection -- that makes it a score flip, "
        "which also moves the selected names and confounds the comparison")
    # The block runs from the guard up to the gross normalisation that follows it.
    i_end = src.find("gsum =", i_flip)
    assert i_end > i_flip, "the flip block no longer precedes the gross normalisation"
    body = src[i_flip:i_end]
    assert "base = -base" in body, (
        "the flip no longer negates the assembled target; anything else is not an "
        "exact mirror of the same book")


# ---------------------------------------------------------------------------
# 2. it is the same book, mirrored
# ---------------------------------------------------------------------------
def test_flip_book_is_an_exact_mirror_of_the_same_book():
    """Same names, same |weights|, opposite sides -- to the last bit.

    `gross_ret`, `long_ret` and `short_ret` in the ledger are *signed* P&L keyed on
    the sign of the held position, so flipping maps long onto **minus** short.
    Asserting `long_flip == +short_normal` would be the natural-looking mistake.
    """
    a = _run(False, **PURE)
    b = _run(True, **PURE)
    assert _maxdev(b.bars["gross_ret"], -a.bars["gross_ret"]) == 0.0
    assert _maxdev(b.bars["long_ret"], -a.bars["short_ret"]) == 0.0
    assert _maxdev(b.bars["short_ret"], -a.bars["long_ret"]) == 0.0
    assert _maxdev(b.bars["turnover"], a.bars["turnover"]) == 0.0
    assert _maxdev(b.bars["gross_exposure"], a.bars["gross_exposure"]) == 0.0
    # The *selection* is untouched -- the flip happens after `select_book`, which is
    # the whole point of applying it to the target instead of the score.  What
    # changes is the sign of the weight each selected name carries.
    assert [r["long"] for r in b.rebalances] == [r["long"] for r in a.rebalances]
    assert [r["short"] for r in b.rebalances] == [r["short"] for r in a.rebalances]
    for ra, rb in zip(a.rebalances, b.rebalances):
        assert rb["w_long"] == [-w for w in ra["w_long"]]
        assert rb["w_short"] == [-w for w in ra["w_short"]]
        # Same |sizes| on each side: the mirror is the same book, not a re-derived one.
        assert rb["long_gross"] == ra["long_gross"]
        assert rb["short_gross"] == ra["short_gross"]


def test_the_ledger_identity_holds_for_the_flipped_book():
    """`net_flip == -(net_normal + 2*cost_normal)` when the costs are on.

    With costs off the prediction collapses to `net_flip == -net_normal`, which is
    the cleanest form and is what this asserts (costs on adds the equity-dependent
    impact term -- see the last test).
    """
    a = _run(False, **PURE)
    b = _run(True, **PURE)
    assert _maxdev(b.bars["net_ret"], -a.bars["net_ret"]) == 0.0
    assert abs(float(a.bars["net_ret"].sum()) + float(b.bars["net_ret"].sum())) < 1e-12


# ---------------------------------------------------------------------------
# 3. the known exception, pinned
# ---------------------------------------------------------------------------
def test_the_adv_cap_is_what_breaks_the_exact_mirror():
    """A tight ADV cap must break the mirror; a loose one must not.

    `adv_cap_delta` scales each name by `min(1, budget/(|delta|*equity))`.  Because
    it reads `equity`, and the two arms compound in opposite directions, the cap
    bites harder on whichever arm has more equity.  This is a property of the
    liquidity gate, not of the mirror -- so it is asserted rather than removed.
    """
    loose = _maxdev(_run(True, **PURE).bars["gross_ret"],
                    -_run(False, **PURE).bars["gross_ret"])
    assert loose == 0.0, "with the cap out of reach the mirror must be exact"

    tight_ov = {"risk.max_adv_participation": 1e-9}
    ta = _run(False, tight_ov, **PURE)
    tb = _run(True, tight_ov, **PURE)
    tight = _maxdev(tb.bars["gross_ret"], -ta.bars["gross_ret"])
    assert tight > 0.0, (
        "a tight ADV cap no longer breaks the mirror -- if the gate stopped reading "
        "equity, the residual reported in 36b_reverse_identity.csv needs rewriting")
