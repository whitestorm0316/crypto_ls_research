"""Guards for *what actually sizes an order*.

Three different knobs get called "leverage" in conversation, and only one of them
moves a single order:

1. **account leverage** (`set_leverage`) -- what OKX calls leverage.  It sets how
   much *margin* an existing position consumes.  It cannot change the order size,
   because the sizer never sees it: `build_plan()` has no leverage parameter, and
   `LiveEngine.build()` does not pass one.
2. **target exposure** (scaling the signal's weights by m) -- looks like it must
   work ("$70 x 5 = $350") and provably does nothing, because the strategy's own
   turnover budget is a *fraction of NAV*: scale the target by m and the throttle
   scales by 1/m.  `test_scaling_the_target_is_cancelled_by_the_throttle` pins
   that, and it is the reason this file exists -- the invariance is surprising
   enough that someone will eventually "fix" it.
3. **the turnover throttle** (`execution.max_daily_turnover`) -- the knob that
   does move the order size, and the only one of the three that changes what
   actually gets sent.

The invariant that ties 1-3 together is that order sizes depend on
`NAV x throttle` and on nothing else in this list, which is what
`test_the_invariant_is_nav_times_throttle` asserts.

Everything here is offline: `build_plan` takes `specs` and `prices` as arguments,
so no network and no cached bars are needed.
"""
from __future__ import annotations

import inspect

import pytest

from crypto_ls_research.execution.engine import LiveEngine
from crypto_ls_research.execution.planner import build_plan
from crypto_ls_research.execution.specs import InstSpec


# One contract is worth $10 (ct_val 1.0 at price 10.0) and the exchange minimum is
# one contract, so `min_notional` is exactly $10 -- a round number makes it easy to
# see which legs the throttle can and cannot afford.
PRICE = 10.0
WEIGHTS = {"AAA-USDT-SWAP": 0.25, "BBB-USDT-SWAP": 0.25,
           "CCC-USDT-SWAP": -0.25, "DDD-USDT-SWAP": -0.25}


def _spec(inst: str) -> InstSpec:
    return InstSpec(inst_id=inst, ct_val=1.0, ct_val_ccy="BASE", lot_sz=1.0,
                    min_sz=1.0, tick_sz=0.1, state="live", max_lever=100.0,
                    settle_ccy="USDT")


def _fixture():
    specs = {i: _spec(i) for i in WEIGHTS}
    prices = {i: PRICE for i in WEIGHTS}
    return specs, prices


def _plan(weights, nav, **kw):
    specs, prices = _fixture()
    # `build_plan` returns the `Plan` itself; the `(plan, acct, px)` tuple is
    # `LiveEngine.build`'s shape, and mixing the two is a TypeError not a wrong
    # answer -- which is the good kind of mistake.
    return build_plan(weights, nav, prices=prices, specs=specs, **kw)


def _shape(plan):
    """The part of a plan a user would actually notice."""
    return (plan.n_orders,
            sorted(o.inst_id for o in plan.orders),
            sorted(o.sz for o in plan.orders),
            round(plan.order_notional, 6))


# ---------------------------------------------------------------------------
# 1. account leverage cannot reach the sizer
# ---------------------------------------------------------------------------
def test_build_plan_has_no_leverage_parameter():
    """The strongest form of "leverage does not size orders": it is not passed.

    A behavioural test ("vary set_leverage, plan is unchanged") can pass for the
    wrong reason -- e.g. if the engine silently clamps leverage to a default.  The
    signature check cannot: there is no name to pass it through.
    """
    names = list(inspect.signature(build_plan).parameters)
    offenders = [n for n in names if "lever" in n.lower() or "margin" in n.lower()]
    assert offenders == [], f"build_plan gained a leverage-ish parameter: {offenders}"


def test_the_engine_does_not_hand_leverage_to_the_sizer():
    """`LiveEngine.build()` calls `build_plan` without a leverage argument.

    Structural rather than behavioural on purpose: exercising this for real needs
    an OKX account, and the failure mode being guarded against (someone threading
    `self.set_leverage` into the sizer "for consistency") is visible in the call
    site.  Mutation: adding `leverage=self.set_leverage,` to that call turns this
    red.
    """
    src = inspect.getsource(LiveEngine.build)
    i = src.find("build_plan(")
    assert i >= 0, "LiveEngine.build no longer calls build_plan"
    depth, j = 0, i + len("build_plan")
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    call = src[i:j + 1]
    assert "leverage" not in call, (
        "LiveEngine.build now passes leverage into build_plan; account leverage "
        f"would start sizing orders:\n{call}")


# ---------------------------------------------------------------------------
# 2. scaling the target is cancelled by the throttle
# ---------------------------------------------------------------------------
def test_scaling_the_target_is_cancelled_by_the_throttle():
    """"$70 x 5 = $350" is false for the target, because the throttle is a ratio.

    With the budget held fixed, multiplying every target weight by m multiplies
    `turnover_wanted` by m, so `turnover_scale` becomes `scale/m`, and the
    executed book `m*w*scale/m` is exactly what it was.  The book that reaches
    the exchange does not change at all.
    """
    base = _plan(WEIGHTS, 1000.0, turnover_budget=0.2)
    assert base.n_orders > 0, "fixture must produce orders for this to mean anything"
    for m in (2.0, 5.0, 20.0):
        scaled = _plan({k: v * m for k, v in WEIGHTS.items()}, 1000.0,
                       turnover_budget=0.2)
        assert _shape(scaled) == _shape(base), (
            f"target x{m} changed the executed book; the throttle is no longer a "
            "NAV fraction and the 'leverage does nothing' story needs revisiting")


# ---------------------------------------------------------------------------
# 3. the invariant is NAV x throttle
# ---------------------------------------------------------------------------
def test_the_invariant_is_nav_times_throttle():
    """`$70` with throttle 1.0 lands exactly on `$350` with throttle 0.2.

    Both fix `NAV x throttle = 70`, so the per-leg dollar amount -- and therefore
    the set of legs that clear the exchange minimum -- is identical.  This is the
    only form in which "make $70 behave like $350" is true, and it buys the *first
    period's order sizes* only: the target book is still `NAV x gross`, so $70 can
    never hold a $350 book.
    """
    small = _plan(WEIGHTS, 70.0, turnover_budget=1.0)
    large = _plan(WEIGHTS, 350.0, turnover_budget=0.2)
    assert small.n_orders > 0
    assert _shape(small) == _shape(large)


# ---------------------------------------------------------------------------
# 4. the throttle is the knob that moves
# ---------------------------------------------------------------------------
def test_the_throttle_is_the_knob_that_moves_the_order_size():
    """Raising the budget grows the order; raising the target does not.

    At $70 the per-leg target is $17.50, but a 0.2 throttle only releases $3.50 of
    it -- below the $10 exchange minimum, so *no* leg is legal and the plan is
    empty.  That is the real shape of the "$70 cannot place orders" complaint, and
    it is a throttle problem before it is a capital problem.
    """
    starved = _plan(WEIGHTS, 70.0, turnover_budget=0.2)
    fed = _plan(WEIGHTS, 70.0, turnover_budget=1.0)
    assert starved.n_orders == 0, (
        "fixture no longer reproduces the starved case; the numbers below assume "
        "$3.50 per leg against a $10 minimum")
    assert fed.n_orders == len(WEIGHTS)
    assert fed.order_notional > starved.order_notional


@pytest.mark.parametrize("nav,throttle", [(70.0, 1.0), (350.0, 0.2), (700.0, 0.1)])
def test_same_nav_times_throttle_same_plan(nav, throttle):
    """The invariant is a product, so any pair with the same product agrees."""
    ref = _plan(WEIGHTS, 70.0, turnover_budget=1.0)
    assert _shape(_plan(WEIGHTS, nav, turnover_budget=throttle)) == _shape(ref)
