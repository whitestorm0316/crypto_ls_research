"""The side of every order leg, for every position transition.

Why this file exists
--------------------
`_orders_for_instrument` decides, for each instrument, the one or two legs that
move `cur_sz` to `tgt_sz`.  In `long_short_mode` an order carries **two**
direction fields and they are not redundant:

* `posSide` -- which book the order acts on (`long` / `short`),
* `side`    -- the direction (`buy` / `sell`).

So increasing a short is a **sell**, and reducing a short is a **buy**.  The code
used to derive the side from `abs(tgt_sz) - abs(cur_sz)`, which throws the sign
away and returns `buy` for *both* "increase a long" and "increase a short" --
i.e. every short leg went the wrong way.

That defect is silent in the worst way: the exchange accepts the order, nothing
raises, the plan looks reasonable, and the position simply moves in the opposite
direction.  Measured on the demo account before the fix: **7 longs totalling
$9,431 against 5 shorts totalling $817**, a book that is **92% long** while the
strategy targets **47.6%** long -- the "market-neutral" strategy was not being
executed as market-neutral at all, and the low gross exposure had been attributed
to the turnover ramp alone.

The matrix below is the whole contract, so a future edit to any branch has to
face all eight transitions rather than the two the author happened to think of.
"""
from __future__ import annotations

import pytest

from crypto_ls_research.execution.planner import _orders_for_instrument

#: (label, cur_sz, tgt_sz, expected [(side, posSide, action), ...])
CASES = [
    # --- net_mode: one signed position, so `side` is just the delta's sign ---
    ("net open long", 0, 100, "net_mode", [("buy", "net", "increase")]),
    ("net open short", 0, -100, "net_mode", [("sell", "net", "increase")]),
    ("net increase long", 100, 200, "net_mode", [("buy", "net", "increase")]),
    ("net reduce long", 200, 100, "net_mode", [("sell", "net", "reduce")]),
    ("net increase short", -100, -200, "net_mode", [("sell", "net", "increase")]),
    ("net reduce short", -200, -100, "net_mode", [("buy", "net", "reduce")]),
    ("net close long", 100, 0, "net_mode", [("sell", "net", "close")]),
    ("net close short", -100, 0, "net_mode", [("buy", "net", "close")]),

    # --- long_short_mode: posSide names the book, side is the direction -----
    ("ls open long", 0, 100, "long_short_mode", [("buy", "long", "open")]),
    ("ls open short", 0, -100, "long_short_mode", [("sell", "short", "open")]),
    ("ls increase long", 100, 200, "long_short_mode", [("buy", "long", "increase")]),
    ("ls reduce long", 200, 100, "long_short_mode", [("sell", "long", "reduce")]),
    # the two that used to be inverted
    ("ls increase short", -100, -200, "long_short_mode", [("sell", "short", "increase")]),
    ("ls reduce short", -200, -100, "long_short_mode", [("buy", "short", "reduce")]),
    ("ls close long", 100, 0, "long_short_mode", [("sell", "long", "close")]),
    ("ls close short", -100, 0, "long_short_mode", [("buy", "short", "close")]),
    # a flip is two legs, and the old book must be closed first
    ("ls flip long->short", 100, -100, "long_short_mode",
     [("sell", "long", "flip_close"), ("sell", "short", "flip_open")]),
    ("ls flip short->long", -100, 100, "long_short_mode",
     [("buy", "short", "flip_close"), ("buy", "long", "flip_open")]),
]


@pytest.mark.parametrize("label,cur,tgt,mode,expected", CASES,
                         ids=[c[0] for c in CASES])
def test_order_legs_have_the_right_side(label, cur, tgt, mode, expected):
    legs = _orders_for_instrument("X-USDT-SWAP", cur, tgt, mode,
                                  price=100.0, spec=None,
                                  ord_type="market", limit_buffer_bps=15.0)
    got = [(l["side"], l["pos_side"], l["action"]) for l in legs]
    assert got == expected, f"{label}: 期望 {expected}，实际 {got}"


@pytest.mark.parametrize("label,cur,tgt,mode,expected", CASES,
                         ids=[c[0] for c in CASES])
def test_every_leg_moves_the_position_towards_the_target(label, cur, tgt, mode,
                                                         expected):
    """The property, not the table: applying the legs must land on the target.

    A sign error can still produce a plausible-looking table, so this walks the
    position forward the way the fill does -- `buy` adds, `sell` subtracts, with
    the sign carried by `posSide` in long/short mode -- and checks the endpoint.
    """
    legs = _orders_for_instrument("X-USDT-SWAP", cur, tgt, mode,
                                  price=100.0, spec=None,
                                  ord_type="market", limit_buffer_bps=15.0)
    if mode == "net_mode":
        pos = cur
        for l in legs:
            pos += l["sz"] if l["side"] == "buy" else -l["sz"]
    else:
        books = {"long": max(cur, 0.0), "short": min(cur, 0.0)}
        for l in legs:
            signed = l["sz"] if l["side"] == "buy" else -l["sz"]
            books[l["pos_side"]] += signed
        pos = books["long"] + books["short"]
    assert abs(pos - tgt) < 1e-9, (
        f"{label}: 照这些腿成交后仓位是 {pos}，目标 {tgt}")
