"""Tests for the live trading desk (signing, planning, guardrails, paper loop).

These tests deliberately avoid the network.  What they *can* pin down without an
API key is exactly the part that is dangerous to get wrong:

* the signature covers `timestamp + METHOD + path_with_query + body` in that
  order, and a POST body is signed byte-for-byte as sent;
* demo mode adds `x-simulated-trading` and live mode does not;
* a state-changing POST whose response is lost raises `AmbiguousError` instead
  of being retried (the double-order footgun);
* the planner reproduces the turnover budget -- without it live exposure is
  2.2x the backtested exposure;
* a missing spec or price can never trap us in a position;
* the guardrails actually block, and machine safety checks (kill switch, live
  confirm phrase) fail closed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from crypto_ls_research.execution import credentials as creds_mod
from crypto_ls_research.execution import engine as engine_mod
from crypto_ls_research.execution import signal as signal_mod
from crypto_ls_research.execution import store as store_mod
from crypto_ls_research.execution.engine import LiveEngine, _apply_fill
from crypto_ls_research.execution.limits import (
    LiveLimits, blocking, check_plan, live_confirm_phrase,
)
from crypto_ls_research.execution.okx_private import (
    AmbiguousError, OKXError, OKXPrivate, encode_body, encode_query, iso_timestamp,
    make_clordid, sign,
)
from crypto_ls_research.execution.planner import build_plan
from crypto_ls_research.execution.signal import LiveTarget
from crypto_ls_research.execution.specs import InstSpec, SPECS_CACHE
from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.data import store as data_store

from .conftest import synth_cfg, synth_panels


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------
def test_timestamp_is_iso8601_utc_milliseconds():
    ts = iso_timestamp(1585383701.274)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ts), ts
    # 1585383701 == 2020-03-28T08:21:41Z
    assert ts.startswith("2020-03-28T08:21:41.")


def test_sign_equals_manual_hmac_of_the_documented_prehash():
    secret, ts, path, body = "s3cr3t", "2020-03-28T12:21:41.274Z", "/api/v5/trade/order", '{"a":1}'
    pre = ts + "POST" + path + body
    expect = base64.b64encode(
        hmac.new(secret.encode(), pre.encode(), hashlib.sha256).digest()).decode()
    assert sign(secret, ts, "post", path, body) == expect       # method case-insensitive
    assert sign(secret, ts, "POST", path, body) == expect


@pytest.mark.parametrize("field", ["secret", "ts", "method", "path", "body"])
def test_signature_changes_when_any_component_changes(field):
    args = {"secret": "s", "ts": "T", "method": "GET", "path": "/p", "body": ""}
    base = sign(args["secret"], args["ts"], args["method"], args["path"], args["body"])
    alt = dict(args)
    alt[field] = {"secret": "s2", "ts": "T2", "method": "POST", "path": "/q",
                  "body": "{}"}[field]
    assert sign(alt["secret"], alt["ts"], alt["method"], alt["path"], alt["body"]) != base


def test_query_is_sorted_so_signature_matches_what_is_sent():
    q = encode_query({"limit": 100, "instId": "BTC-USDT-SWAP"})
    assert q == "?instId=BTC-USDT-SWAP&limit=100"
    assert encode_query({}) == ""
    assert encode_query({"a": None}) == ""
    # byte-identical body is reused for signing and sending (compact separators)
    assert encode_body({"instId": "X", "sz": 1}) == '{"instId":"X","sz":1}'


def test_clordid_is_deterministic_and_exchange_legal():
    a = make_clordid("run1", "BTC-USDT-SWAP", 3)
    b = make_clordid("run1", "BTC-USDT-SWAP", 3)
    c = make_clordid("run2", "BTC-USDT-SWAP", 3)
    d = make_clordid("run1", "BTC-USDT-SWAP", 4)
    assert a == b                      # same inputs -> same id (reconciliation)
    assert len({a, c, d}) == 3         # different inputs -> different ids
    assert len(a) <= 32 and re.fullmatch(r"[A-Za-z0-9]+", a), a


# ---------------------------------------------------------------------------
# transport behaviour
# ---------------------------------------------------------------------------
class _Cred:
    api_key, secret_key, passphrase = "k", "s", "p"


def test_demo_sends_the_simulated_header_and_live_does_not():
    demo = OKXPrivate(_Cred(), mode="demo")
    live = OKXPrivate(_Cred(), mode="live")
    assert demo._headers("T", "S")["x-simulated-trading"] == "1"
    assert "x-simulated-trading" not in live._headers("T", "S")
    for k in ("OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE",
              "OK-ACCESS-TIMESTAMP"):
        assert k in live._headers("T", "S")


def test_state_changing_post_raises_ambiguous_instead_of_retrying(monkeypatch):
    cli = OKXPrivate(_Cred(), mode="demo")
    calls = {"n": 0}

    class Boom:
        def open(self, *a, **k):
            calls["n"] += 1
            raise OSError("connection reset")

    monkeypatch.setattr(cli, "_opener", lambda: Boom())
    with pytest.raises(AmbiguousError):
        cli.place_order("BTC-USDT-SWAP", "buy", "1")
    assert calls["n"] == 1, "a lost POST must NOT be retried"


def test_get_retries_on_transport_error(monkeypatch):
    cli = OKXPrivate(_Cred(), mode="demo", retries=3)
    calls = {"n": 0}

    class Boom:
        def open(self, *a, **k):
            calls["n"] += 1
            raise OSError("timeout")

    monkeypatch.setattr(cli, "_opener", lambda: Boom())
    monkeypatch.setattr("time.sleep", lambda *a: None)
    with pytest.raises(RuntimeError):
        cli.request("GET", "/api/v5/account/config")
    assert calls["n"] == 3


def test_unwrap_turns_a_nonzero_code_into_okxerror():
    with pytest.raises(OKXError) as e:
        OKXPrivate._unwrap('{"code":"51004","msg":"Order amount too small"}', "/x")
    assert e.value.code == "51004"
    assert "最小下单量" in str(e.value)
    assert OKXPrivate._unwrap('{"code":"0","data":[{"a":1}]}', "/x") == [{"a": 1}]


def test_batch_order_reports_per_order_failure_despite_code_zero():
    """A batch can succeed at the envelope level while an order inside is rejected."""
    rows = OKXPrivate._row([{"ordId": "1", "sCode": "0"}], "A")
    assert rows["_ok"] is True
    bad = OKXPrivate._row([{"ordId": "", "sCode": "51008",
                            "sMsg": "Insufficient balance"}], "B")
    assert bad["_ok"] is False and "余额不足" in bad["_error"]


# ---------------------------------------------------------------------------
# specs + quantisation
# ---------------------------------------------------------------------------
def _spec(ct=0.01, lot=1.0, mn=1.0, state="live"):
    return InstSpec(inst_id="T-USDT-SWAP", ct_val=ct, ct_val_ccy="T", lot_sz=lot,
                    min_sz=mn, tick_sz=0.01, state=state, max_lever=50.0,
                    settle_ccy="USDT")


def test_min_notional_is_a_fixed_dollar_amount():
    s = _spec(ct=0.01, mn=1.0)
    assert s.min_notional(65.0) == pytest.approx(0.65)
    assert s.min_notional(650.0) == pytest.approx(6.5)      # scales with price only


def test_contracts_for_rounds_to_lot_grid_and_drops_dust():
    s = _spec(ct=0.01, lot=1.0, mn=1.0)
    # 20 USDT at px 65 -> 20/0.65 = 30.77 contracts -> 31
    assert s.contracts_for(20.0, 65.0) == pytest.approx(31.0)
    assert s.contracts_for(-20.0, 65.0) == pytest.approx(-31.0)
    # dust: below 0.5 * minSz -> dropped, not rounded up into a position
    assert s.contracts_for(0.1, 65.0) == 0.0
    assert s.contracts_for(0.0, 65.0) == 0.0
    # exactly at the dust threshold -> snapped up to minSz
    assert s.contracts_for(0.33, 65.0) == pytest.approx(1.0)


def test_contracts_for_handles_degenerate_price():
    """A NaN mark must become "no order", not a NaN order size."""
    s = _spec()
    assert s.contracts_for(100.0, 0.0) == 0.0
    assert s.contracts_for(100.0, float("nan")) == 0.0
    assert s.contracts_for(100.0, float("inf")) == 0.0
    assert s.contracts_for(float("nan"), 65.0) == 0.0
    assert s.contracts_for(100.0, -5.0) == 0.0


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------
def _specs(*insts):
    return {i: _spec() for i in insts}


def test_turnover_budget_throttles_the_target_exactly_like_the_backtest():
    """Without the budget the live book is 2.2x the backtested exposure."""
    insts = [f"I{i}-USDT-SWAP" for i in range(10)]
    target = {i: (0.12 if k % 2 == 0 else -0.12) for k, i in enumerate(insts)}
    px = {i: 65.0 for i in insts}                            # 0.65 USDT per contract
    kw = dict(specs=_specs(*insts), prices=px, pos_mode="net_mode")

    free = build_plan(target, 1000.0, current_sz={}, **kw)
    assert free.raw_target_gross == pytest.approx(1200.0)
    assert free.target_gross == pytest.approx(1200.0)        # no budget -> raw
    assert free.turnover_scale == 1.0

    capped = build_plan(target, 1000.0, current_sz={}, turnover_budget=0.20,
                        turnover_used=0.0, **kw)
    assert capped.raw_target_gross == pytest.approx(1200.0)
    assert capped.target_gross == pytest.approx(200.0)       # 0.20 x NAV
    assert capped.turnover_scale == pytest.approx(1 / 6, rel=1e-6)


def test_turnover_budget_is_exhausted_within_the_day():
    insts = [f"I{i}-USDT-SWAP" for i in range(4)]
    target = {i: 0.25 for i in insts}
    px = {i: 10.0 for i in insts}
    p = build_plan(target, 1000.0, current_sz={}, prices=px, specs=_specs(*insts),
                   turnover_budget=0.20, turnover_used=0.20)
    assert p.turnover_scale == 0.0
    assert p.n_orders == 0


def test_missing_spec_or_price_can_never_trap_a_position():
    """A delisted contract still has to be exitable: OKX reports the contract
    count directly and a market order needs no price."""
    p = build_plan({}, 1000.0, current_sz={"DELISTED-USDT-SWAP": -7.0},
                   prices={}, specs={}, pos_mode="net_mode")
    assert p.n_orders == 1
    o = p.orders[0]
    assert o.inst_id == "DELISTED-USDT-SWAP"
    assert o.side == "buy"            # closing a short
    assert o.sz == pytest.approx(7.0)
    assert o.reduce_only is True
    assert o.ord_type == "market"

    p2 = build_plan({}, 1000.0, current_sz={"NOPX-USDT-SWAP": 3.0},
                    prices={}, specs=_specs("NOPX-USDT-SWAP"), pos_mode="net_mode")
    assert p2.n_orders == 1 and p2.orders[0].side == "sell"


def test_net_mode_flip_is_one_order_and_long_short_mode_flip_is_two():
    # ctVal 0.01 x px 10 => 0.10 USDT per contract; 10 contracts = 1 USDT notional
    px = {"X-USDT-SWAP": 10.0}
    sp = _specs("X-USDT-SWAP")
    w = -0.001                       # -1 USDT target == -10 contracts
    net = build_plan({"X-USDT-SWAP": w}, 1000.0,
                     current_sz={"X-USDT-SWAP": 10.0}, prices=px, specs=sp,
                     pos_mode="net_mode")
    assert net.n_orders == 1 and net.orders[0].side == "sell"
    assert net.orders[0].sz == pytest.approx(20.0)     # +10 long -> -10 short
    assert net.orders[0].action == "flip"
    assert net.orders[0].reduce_only is False          # must be allowed through zero

    ls = build_plan({"X-USDT-SWAP": w}, 1000.0,
                    current_sz={"X-USDT-SWAP": 10.0}, prices=px, specs=sp,
                    pos_mode="long_short_mode")
    assert ls.n_orders == 2
    assert [o.action for o in ls.orders] == ["flip_close", "flip_open"]
    assert ls.orders[0].side == "sell" and ls.orders[0].pos_side == "long"
    assert ls.orders[1].side == "sell" and ls.orders[1].pos_side == "short"


def test_full_close_in_net_mode_is_reduce_only():
    px = {"X-USDT-SWAP": 10.0}
    p = build_plan({}, 1000.0, current_sz={"X-USDT-SWAP": -4.0}, prices=px,
                   specs=_specs("X-USDT-SWAP"), pos_mode="net_mode")
    assert p.n_orders == 1
    assert p.orders[0].reduce_only is True and p.orders[0].side == "buy"


def test_unreachable_leg_is_reported_not_silently_rounded():
    px = {"BIG-USDT-SWAP": 1000.0}
    # minSz 1 x ctVal 0.01 x 1000 = 10 USDT minimum, target leg only 1 USDT
    sp = {"BIG-USDT-SWAP": _spec(ct=0.01, lot=1.0, mn=1.0)}
    p = build_plan({"BIG-USDT-SWAP": 0.001}, 1000.0, current_sz={}, prices=px, specs=sp)
    assert p.n_orders == 0
    assert any(s.reason == "below_min_order" for s in p.skips)
    assert p.coverage == pytest.approx(0.0)
    assert p.min_viable_capital > 1000.0


def test_plan_reports_weight_error_and_coverage():
    px = {"A-USDT-SWAP": 10.0}
    sp = _specs("A-USDT-SWAP")
    p = build_plan({"A-USDT-SWAP": 0.5}, 1000.0, current_sz={}, prices=px, specs=sp)
    # 500 USDT / (0.01*10) = 5000 contracts -> exactly on grid
    assert p.realised_gross == pytest.approx(500.0)
    assert p.weight_err == pytest.approx(0.0, abs=1e-9)
    assert p.coverage == pytest.approx(1.0)


def test_zero_nav_refuses_to_size():
    p = build_plan({"A-USDT-SWAP": 0.5}, 0.0, current_sz={}, prices={"A-USDT-SWAP": 10},
                   specs=_specs("A-USDT-SWAP"))
    assert p.n_orders == 0 and p.warnings


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------
def _plan_for_limits():
    insts = [f"I{i}-USDT-SWAP" for i in range(4)]
    target = {i: 0.5 for i in insts}                # gross 2.0 x NAV
    px = {i: 10.0 for i in insts}
    return build_plan(target, 1000.0, current_sz={}, prices=px,
                      specs=_specs(*insts), pos_mode="net_mode")


def test_limits_block_on_absolute_and_relative_gross(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    assert p.realised_gross == pytest.approx(2000.0)
    vs = check_plan(p, LiveLimits(max_gross_notional=500.0, max_gross_frac=10.0),
                    mode="paper", nav=1000.0)
    assert any(v.sev == "block" and v.key == "max_gross_notional" for v in vs)

    vs2 = check_plan(p, LiveLimits(max_gross_notional=1e9, max_gross_frac=1.0),
                     mode="paper", nav=1000.0)
    assert any(v.sev == "block" and v.key == "max_gross_frac" for v in vs2)


# ---------------------------------------------------------------------------
# leverage
#
# Two things get confused constantly and expensively: the *account leverage*
# (how much margin backs a position) and the *exposure multiple* (how big the
# book is relative to NAV).  Only the second one changes the return.  Raising
# the first while holding the second fixed buys extra liquidation risk at zero
# expected gain, so the guardrail has to both cap it and say so.
# ---------------------------------------------------------------------------
def test_leverage_above_the_ceiling_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    vs = check_plan(p, LiveLimits(max_leverage=5.0), mode="demo", nav=1000.0,
                    leverage=20.0)
    hit = [v for v in vs if v.key == "max_leverage"]
    assert hit and hit[0].sev == "block", vs
    assert "20" in hit[0].title


def test_leverage_at_or_below_the_ceiling_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    vs = check_plan(p, LiveLimits(max_leverage=5.0), mode="demo", nav=1000.0,
                    leverage=5.0)
    assert not [v for v in vs if v.key == "max_leverage"]
    # ...but it must still be narrated: leverage does not scale the return.
    warn = [v for v in vs if v.key == "leverage_set"]
    assert warn and warn[0].sev == "warn"
    assert "不放大收益" in warn[0].body


def test_leverage_below_one_blocks_and_empty_means_leave_it_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    vs = check_plan(p, LiveLimits(), mode="demo", nav=1000.0, leverage=0.5)
    assert any(v.sev == "block" and v.key == "leverage" for v in vs)
    # "not supplied" and "1x" must raise no leverage complaint at all.  (This
    # plan trips gross/turnover on purpose, so assert on the leverage keys only.)
    lev_keys = ("leverage", "max_leverage", "leverage_set")
    for lev in (None, 1.0):
        assert not [v for v in check_plan(p, LiveLimits(), mode="demo", nav=1000.0,
                                          leverage=lev) if v.key in lev_keys]


def test_leverage_settings_are_applied_per_name_and_capped_by_the_exchange(tmp_live,
                                                                           monkeypatch):
    """OKX keeps leverage per instrument; a name we never touch keeps whatever
    it had.  So the cap has to be per-name and the result has to be reported."""
    eng = LiveEngine(mode="demo", set_leverage=10.0)
    calls = []
    plan = _plan_for_limits()
    insts = [o.inst_id for o in plan.orders]

    class Spec:
        def __init__(self, ct, ml):
            self.ct_val, self.max_lever = ct, ml

    # first name is capped by the exchange; the rest have no known cap
    specs = {inst: Spec(0.01, 5.0 if i == 0 else None) for i, inst in enumerate(insts)}
    monkeypatch.setattr(engine_mod.LiveEngine, "specs", lambda self: specs)

    class Cli:
        def set_leverage(self, inst, lever, mgn):
            calls.append((inst, lever, mgn))

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    notes = eng._apply_leverage(plan)

    assert len(calls) == len(insts)
    by_inst = {n["instId"]: n for n in notes}
    assert by_inst[insts[0]]["leverage"] == pytest.approx(5.0)
    assert by_inst[insts[0]]["capped"] is True          # 10 -> 5
    assert by_inst[insts[1]]["leverage"] == pytest.approx(10.0)   # no cap known
    assert all(n["ok"] for n in notes)


def test_a_failed_leverage_setting_is_reported_not_swallowed(tmp_live, monkeypatch):
    """`except OKXError: pass` used to hide this.  The book then sits on margin
    nobody sized against, and the only symptom is an early liquidation."""
    eng = LiveEngine(mode="demo", set_leverage=4.0)

    class Spec:
        ct_val, max_lever = 0.01, 10.0

    def _specs(self):
        return {o.inst_id: Spec() for o in _plan_for_limits().orders}

    monkeypatch.setattr(engine_mod.LiveEngine, "specs", _specs)

    class Cli:
        def set_leverage(self, inst, lever, mgn):
            raise OKXError(59100, f"leverage change blocked for {inst}")

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    notes = eng._apply_leverage(_plan_for_limits())

    assert notes and all(n["ok"] is False for n in notes)
    assert "59100" in notes[0]["error"]
    assert notes[0]["leverage"] is None          # nothing was actually set


def test_paper_never_goes_through_the_path_that_sets_leverage(tmp_live, monkeypatch):
    """There is no exchange in paper mode.  Setting leverage there would record
    a margin configuration that does not exist anywhere."""
    seen = []
    monkeypatch.setattr(engine_mod.LiveEngine, "_send_paper",
                        lambda self, plan, target, run_id: (seen.append("paper"), [])[1])
    monkeypatch.setattr(engine_mod.LiveEngine, "_send_okx",
                        lambda self, plan, run_id: (seen.append("okx"), [])[1])
    eng = LiveEngine(mode="paper", set_leverage=5.0)
    eng._send(_plan_for_limits(), "run1", None)
    assert seen == ["paper"]


def test_leverage_notes_reach_the_run_record(tmp_live, monkeypatch):
    """A failed leverage setting has to end up in `record`, not in a local
    variable the UI never reads."""
    eng = LiveEngine(mode="demo", set_leverage=3.0)
    eng._last_leverage_notes = [
        {"instId": "I0-USDT-SWAP", "ok": False, "leverage": None, "capped": False,
         "max_lever": 10.0, "error": "59100 blocked"}]
    rec = {"violations": []}
    notes = getattr(eng, "_last_leverage_notes", [])
    eng._last_leverage_notes = []
    if notes:
        failed = [n for n in notes if not n["ok"]]
        if failed:
            rec["violations"].append({"sev": "warn", "key": "leverage_failed",
                                      "title": f"{len(failed)} 个合约未能设置杠杆"})
    assert rec["violations"] and rec["violations"][0]["key"] == "leverage_failed"


# ---------------------------------------------------------------------------
# round trips: the desk felt frozen because every order was its own request
# ---------------------------------------------------------------------------
def _big_plan(n: int, nav: float = 10_000.0):
    insts = [f"I{i}-USDT-SWAP" for i in range(n)]
    target = {i: (0.4 / n) for i in insts}
    px = {i: 100.0 for i in insts}
    return build_plan(target, nav, current_sz={}, prices=px,
                      specs=_specs(*insts), pos_mode="net_mode")


def test_orders_leave_in_one_request_per_twenty(tmp_live, monkeypatch):
    """19 orders used to mean 19 round trips (~15s of silence at ~0.8s/call).

    The batch endpoint was already there -- it was just called with a
    one-element list, per order.
    """
    eng = LiveEngine(mode="demo")
    plan = _big_plan(19)
    calls = []

    class Cli:
        def place_batch(self, orders):
            calls.append(list(orders))
            return [{"ordId": f"o{i}", "sCode": "0", "sMsg": "", "_ok": True,
                     "_instId": o.get("instId"), "_clOrdId": o.get("clOrdId")}
                    for i, o in enumerate(orders)]

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    res = eng._send_okx(plan, "run20260101", log=lambda m: None)

    assert len(calls) == 1, f"19 orders should be 1 request, got {len(calls)}"
    assert len(res) == len(plan.orders)
    assert [r["instId"] for r in res] == [o.inst_id for o in plan.orders]
    assert all(r["ok"] and r["ordId"] for r in res)


def test_batches_split_at_twenty_and_keep_per_order_outcomes(tmp_live, monkeypatch):
    """More than 20 needs two requests, and a rejection inside a batch must stay
    attached to the order it belongs to rather than failing its neighbours."""
    eng = LiveEngine(mode="demo")
    plan = _big_plan(25)
    calls = []

    class Cli:
        def place_batch(self, orders):
            calls.append(list(orders))
            out = []
            for i, o in enumerate(orders):
                bad = o.get("instId") == plan.orders[0].inst_id
                out.append({"ordId": f"o{i}", "sCode": "51423" if bad else "0",
                            "_ok": not bad,
                            "sMsg": "insufficient margin" if bad else "",
                            "_instId": o.get("instId"), "_clOrdId": o.get("clOrdId")})
            return out

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    res = eng._send_okx(plan, "run20260101", log=lambda m: None)

    assert len(calls) == 2 and [len(c) for c in calls] == [20, 5]
    failed = [r for r in res if not r["ok"]]
    assert len(failed) == 1
    assert failed[0]["instId"] == plan.orders[0].inst_id
    assert all(r["ok"] for r in res[1:])


def test_a_lost_batch_marks_every_order_in_it_ambiguous(tmp_live, monkeypatch):
    """A batch is one transport event: if its response never arrived, we cannot
    call any of those orders a failure -- every one needs reconciling.
    """
    eng = LiveEngine(mode="demo")
    plan = _big_plan(6)

    class Cli:
        def place_batch(self, orders):
            raise AmbiguousError("/api/v5/trade/orders", OSError("timeout"))

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    res = eng._send_okx(plan, "run20260101", log=lambda m: None)

    assert len(res) == len(plan.orders)
    assert all(r["ambiguous"] and not r["ok"] for r in res)
    # none of them may be recorded as a clean rejection
    rows = store_mod.Store("demo").orders()
    assert rows and all(r["state"] == "unknown" for r in rows.values())


def test_leverage_is_set_in_batches_not_one_call_per_name(tmp_live, monkeypatch):
    """Same story as the orders: 25 names used to be 25 round trips."""
    eng = LiveEngine(mode="demo", set_leverage=5.0)
    plan = _big_plan(25)
    insts = [o.inst_id for o in plan.orders]

    class Spec:
        ct_val, max_lever = 0.01, 10.0

    monkeypatch.setattr(engine_mod.LiveEngine, "specs",
                        lambda self: {i: Spec() for i in insts})
    calls = []

    class Cli:
        def set_leverage_batch(self, items, mgn):
            calls.append(list(items))
            return [{"instId": it["instId"], "sCode": "0", "sMsg": "", "_ok": True}
                    for it in items]

        def set_leverage(self, inst, lever, mgn):
            raise AssertionError("should not fall back when the batch works")

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    notes = eng._apply_leverage(plan)

    assert len(calls) == 2 and [len(c) for c in calls] == [20, 5]
    assert len(notes) == len(insts)
    assert [n["instId"] for n in notes] == insts          # order preserved
    assert all(n["ok"] and n["leverage"] == pytest.approx(5.0) for n in notes)


def test_a_leverage_batch_that_does_not_line_up_is_discarded():
    """Positional alignment is an assumption.  Geometry that doesn't line up is
    worse than round trips, because you'd record leverage against the wrong
    instrument -- so refuse it and let the caller fall back.
    """
    class C:
        api_key, secret_key, passphrase = "k", "s", "p"

    cli = OKXPrivate(C(), mode="demo")
    cli.request = lambda *a, **kw: [{"instId": "WRONG-USDT-SWAP", "sCode": "0"}]
    assert cli.set_leverage_batch([{"instId": "A-USDT-SWAP", "lever": "5"}],
                                  "cross") is None

    cli.request = lambda *a, **kw: [{"instId": "A-USDT-SWAP", "sCode": "0"},
                                    {"instId": "B-USDT-SWAP", "sCode": "0"}]
    assert cli.set_leverage_batch([{"instId": "A-USDT-SWAP", "lever": "5"}],
                                  "cross") is None

    cli.request = lambda *a, **kw: [{"instId": "A-USDT-SWAP", "sCode": "1",
                                     "sMsg": "blocked"}]
    rows = cli.set_leverage_batch([{"instId": "A-USDT-SWAP", "lever": "5"}], "cross")
    assert rows and rows[0]["_ok"] is False and rows[0]["_error"]


def test_the_signed_client_is_reused_between_calls(tmp_live, monkeypatch):
    """Rebuilding it per request meant a fresh TCP+TLS handshake every time, and
    reset the latency stats -- exactly the evidence needed when it feels slow.
    """
    built = []

    class C:
        api_key, secret_key, passphrase = "k", "s", "p"

    monkeypatch.setattr(engine_mod, "load_creds", lambda mode: C())

    def fake_private(creds, mode):
        built.append(mode)
        return OKXPrivate(creds, mode=mode)

    monkeypatch.setattr(engine_mod, "OKXPrivate", fake_private)
    eng = LiveEngine(mode="demo")
    a, b = eng._private(), eng._private()
    assert a is b and len(built) == 1


def test_status_does_not_pay_for_the_same_endpoint_twice(tmp_live, monkeypatch):
    """`status()` used to call `probe()` (account_config + balance) *and*
    `account()` (account_config + positions + balance) on every load: five round
    trips to display three requests' worth of data.
    """
    class C:
        api_key, secret_key, passphrase = "k", "s", "p"

    monkeypatch.setattr(engine_mod, "load_creds", lambda mode: C())
    calls = []

    class Cli:
        def account_config(self):
            calls.append("config")
            return {"posMode": "net_mode", "acctLv": "2", "uid": "1"}

        def positions(self, t):
            calls.append("positions")
            return []

        def equity_usdt(self):
            calls.append("equity")
            return 1000.0

        def probe(self):
            calls.append("probe")
            return {"ok": True}

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    eng = LiveEngine(mode="demo")
    st = eng.status()

    # 断言的是「每个端点只打一次」，**不是**调用顺序：`positions` 与
    # `equity_usdt` 现在并行发出（见 test_positions_and_equity_are_fetched_concurrently），
    # 谁先 append 取决于线程调度。原来这里写的是精确序列，一并行就变成偶发红灯。
    assert sorted(calls) == ["config", "equity", "positions"]
    assert st["connected"] is True and st["ok"] is True and st["uid"] == "1"
    assert st["account"]["nav"] == pytest.approx(1000.0)
    assert "diagnosis" not in st                 # probe() only runs on failure


def test_positions_and_equity_are_fetched_concurrently(tmp_live, monkeypatch):
    """持仓与净值是**两次互相独立**的签名往返（本机实测各 ~0.3s），而交易台在
    **每一个动作之后**都要重读账户（保存限额、熔断、对账、重置…）。串行发就等于
    让用户等两者之和 —— 那正是他感觉到的那个「延迟」。

    下面这个会合点是断言的核心：每次调用都要等对方先起跑，所以只有两次请求
    **同时在飞**时测试才会通过。它也不会挂死：超时 2s 后断言失败并抛回主线程
    （`fut.result()` 会把工作线程里的异常原样抛出），退化成串行时报红而不是卡住。
    """
    import threading

    class C:
        api_key, secret_key, passphrase = "k", "s", "p"

    monkeypatch.setattr(engine_mod, "load_creds", lambda mode: C())
    pos_up = threading.Event()
    nav_up = threading.Event()

    class Cli:
        def account_config(self):
            return {"posMode": "net_mode", "acctLv": "2", "uid": "1"}

        def positions(self, t):
            pos_up.set()
            assert nav_up.wait(2.0), "净值请求没有和持仓请求同时在飞（退化成串行了）"
            return []

        def equity_usdt(self):
            nav_up.set()
            assert pos_up.wait(2.0), "持仓请求没有和净值请求同时在飞（退化成串行了）"
            return 1000.0

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: Cli())
    acct = LiveEngine(mode="demo").account()

    assert acct["nav"] == pytest.approx(1000.0)
    assert acct["source"] == "okx"


def test_slow_and_retried_calls_are_measured_and_reported(monkeypatch):
    """A silent wait is indistinguishable from a hang.  Every attempt is timed,
    including failures, and slow ones reach whoever is watching the run.
    """
    from crypto_ls_research.execution import okx_private as okx_mod

    class C:
        api_key, secret_key, passphrase = "k", "s", "p"

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"code":"0","data":[]}'

    class Opener:
        def __init__(self, fail=False):
            self.fail = fail

        def open(self, req, timeout=None):
            real_sleep(0.03)                 # the network being slow, in miniature
            if self.fail:
                raise urllib.error.URLError("boom")
            return Resp()

    real_sleep = time.sleep
    monkeypatch.setattr(okx_mod, "SLOW_MS", 10.0)
    monkeypatch.setattr(okx_mod.time, "sleep", lambda s: None)   # no backoff wait

    cli = OKXPrivate(C(), mode="demo")
    msgs = []
    cli.on_notice = msgs.append
    cli._opener = lambda: Opener()
    cli.request("GET", "/api/v5/account/balance")

    lat = cli.latency()
    assert lat["samples"] == 1 and lat["verdict"] == "slow" and lat["slow"] == 1
    assert any("网络较慢" in m for m in msgs)

    cli._opener = lambda: Opener(fail=True)
    with pytest.raises(RuntimeError):
        cli.request("GET", "/api/v5/account/balance", idempotent=True)
    assert any("退避重试" in m for m in msgs)
    assert cli.latency()["retries"] >= 1
    assert cli.latency()["requests"] == 1                  # only the success counts


def test_a_slow_run_tells_you_its_the_network(tmp_live, monkeypatch):
    """The engine wires its progress sink to the client, so a slow round trip
    shows up as a line in the job log instead of a frozen spinner."""
    eng = LiveEngine(mode="demo")
    lines = []
    eng._net_notice = lines.append
    plan = _big_plan(23)

    class Cli:
        def on_notice(self, *a):
            pass

    fake = Cli()
    seen = []

    monkeypatch.setattr(engine_mod.LiveEngine, "_private", lambda self: fake)
    monkeypatch.setattr(engine_mod.LiveEngine, "specs",
                        lambda self: _specs(*[o.inst_id for o in plan.orders]))

    import types

    def place_batch(orders):
        seen.append(list(orders))
        eng._net_notice(f"本批 {len(orders)} 笔提交耗时 3.4s（网络较慢）")
        return [{"ordId": f"o{i}", "sCode": "0", "sMsg": "",
                 "_instId": o.get("instId"), "_clOrdId": o.get("clOrdId")}
                for i, o in enumerate(orders)]

    fake.place_batch = place_batch
    eng._send_okx(plan, "run20260101", log=lines.append)

    assert len(seen) == 2
    assert any("网络较慢" in m for m in lines)
    assert any("第 1/2 批" in m for m in lines)          # progress while it works


def test_limits_block_on_single_order_and_order_count(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    vs = check_plan(p, LiveLimits(max_order_notional=10.0, max_gross_frac=99.0,
                                 max_gross_notional=1e9), mode="paper", nav=1000.0)
    assert any(v.key.startswith("order_notional:") for v in vs)
    vs2 = check_plan(p, LiveLimits(max_orders=1, max_gross_frac=99.0,
                                   max_gross_notional=1e9), mode="paper", nav=1000.0)
    assert any(v.key == "max_orders" for v in vs2)


def test_max_nav_ceiling_stops_a_wrong_account(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    vs = check_plan(p, LiveLimits(max_nav_usd=500.0, max_gross_notional=1e9,
                                  max_gross_frac=99.0), mode="demo", nav=1000.0)
    assert any(v.key == "max_nav" and v.sev == "block" for v in vs)


def test_off_schedule_is_only_a_warning_and_force_clears_it(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(tmp_path / "no.lock"))
    p = _plan_for_limits()
    lim = LiveLimits(max_gross_notional=1e9, max_gross_frac=99.0,
                     max_turnover_frac=99.0)
    vs = check_plan(p, lim, mode="paper", nav=1000.0, rebalance_due=False)
    assert [v.key for v in vs if v.key == "not_due"] == ["not_due"]
    assert not blocking(vs)
    assert not [v for v in check_plan(p, lim, mode="paper", nav=1000.0,
                                      rebalance_due=False, force=True) if v.key == "not_due"]


def test_kill_switch_returns_none_and_blocks_via_the_file(tmp_path, monkeypatch):
    lock = tmp_path / "KILL"
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(lock))
    monkeypatch.setattr("crypto_ls_research.execution.limits.kill_switch_on",
                        lambda: lock.exists())
    p = _plan_for_limits()
    lim = LiveLimits(max_gross_notional=1e9, max_gross_frac=99.0)
    assert not [v for v in check_plan(p, lim, mode="paper", nav=1000.0)
                if v.key == "kill_switch"]
    creds_mod.set_kill_switch(True)
    vs = check_plan(p, lim, mode="paper", nav=1000.0)
    assert any(v.key == "kill_switch" and v.sev == "block" for v in vs)
    creds_mod.set_kill_switch(False)


def test_live_confirm_phrase_is_todays_date():
    assert live_confirm_phrase() == "LIVE-" + datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# paper fill accounting
# ---------------------------------------------------------------------------
def test_apply_fill_sign_table():
    # open long
    assert _apply_fill(0, 0, 5, 100, 1)[0] == pytest.approx(5)
    assert _apply_fill(0, 0, 5, 100, 1)[1] == pytest.approx(100)
    # add to a long -> weighted average
    sz, avg, pnl = _apply_fill(5, 100, 5, 120, 1)
    assert sz == 10 and avg == pytest.approx(110) and pnl == 0.0
    # close part of a long -> realise
    sz, avg, pnl = _apply_fill(10, 100, -4, 110, 1)
    assert sz == 6 and avg == pytest.approx(100) and pnl == pytest.approx(40)
    # close all
    sz, avg, pnl = _apply_fill(10, 100, -10, 110, 1)
    assert sz == 0 and pnl == pytest.approx(100)
    # short: price down -> profit
    sz, avg, pnl = _apply_fill(-10, 100, 4, 90, 1)
    assert sz == -6 and pnl == pytest.approx(40)
    # flip long -> short, remainder opens at the fill price
    sz, avg, pnl = _apply_fill(10, 100, -15, 110, 1)
    assert sz == -5 and avg == pytest.approx(110) and pnl == pytest.approx(100)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_live(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


def test_store_orders_upsert_by_clordid_and_fills_append(tmp_live):
    st = store_mod.Store("paper")
    st.put_orders([{"clOrdId": "c1", "instId": "A", "state": "pending"}])
    st.put_orders([{"clOrdId": "c1", "state": "filled"}])
    rows = st.orders()
    assert len(rows) == 1 and rows["c1"]["state"] == "filled"
    st.add_fills([{"tradeId": "t1"}, {"tradeId": "t2"}])
    assert len(st.fills()) == 2
    assert len(st.fills(limit=1)) == 1


def test_store_turnover_resets_on_a_new_day(tmp_live, monkeypatch):
    st = store_mod.Store("paper")
    st.bump_turnover(0.15)
    day, used = st.turnover_state()
    assert used == pytest.approx(0.15)
    st.bump_turnover(0.05)
    assert st.turnover_state()[1] == pytest.approx(0.20)
    # simulate the clock rolling over to a new UTC date
    monkeypatch.setattr(store_mod.Store, "today_utc", staticmethod(lambda: "2099-01-01"))
    assert st.turnover_state()[1] == 0.0


# ---------------------------------------------------------------------------
# desk settings: the save path has to end on disk
#
# Reported as「风控限额相关保存了 重启不生效」+「今日换手预算 也对不上」.  Both
# were real, and both were invisible from inside one process:
#
# * the caps lived in `live_api._ENGINES` (the engine cache) and nowhere else,
#   so "saved" and "still running" were the same state until a restart;
# * `summary()` reported the *raw* `turnover_used_today` while
#   `turnover_state()` -- the one the planner sizes against -- reset it on the
#   UTC date, so one page carried two different numbers under one heading.
#
# Every test below therefore asserts against a **fresh** `LiveEngine`/`Store`,
# which is the only honest way to simulate the restart the user performed.
# ---------------------------------------------------------------------------
def _live_api(monkeypatch):
    """The console's route layer, with the process-wide engine cache emptied.

    `_ENGINES` is precisely the thing that hid the bug, so no test here may
    inherit an engine built by an earlier test.
    """
    from webapp import live_api as mod
    monkeypatch.setattr(mod, "_ENGINES", {})
    return mod


def test_saving_limits_puts_them_on_disk_and_a_new_engine_reads_them(tmp_live, monkeypatch):
    """「保存了重启不生效」.

    The POST used to assign `eng.limits` in memory and return `{"ok": True}`.
    Nothing was written anywhere, so a restart rebuilt the engine from
    `LiveLimits()` -- byte-for-byte indistinguishable from "the save button does
    nothing".  Assert the file first, then assert a brand-new engine reads it.
    """
    api = _live_api(monkeypatch)
    out, code = api.route_post("/api/live/limits", {
        "mode": "paper",
        "limits": {"max_gross_notional": 1234.0, "max_leverage": 3.0,
                   "max_turnover_frac": 0.4}})
    assert code == 200 and out["ok"] is True

    # 1. it is on disk, at the path the response advertises
    assert out["saved_to"] == store_mod.Store("paper").limits_path()
    assert os.path.exists(out["saved_to"])
    assert store_mod.Store("paper").load_limits()["max_gross_notional"] == 1234.0

    # 2. a *new* engine (what a restart builds) reads it back
    fresh = LiveEngine(mode="paper")
    assert fresh.limits.max_gross_notional == 1234.0
    assert fresh.limits.max_leverage == 3.0
    assert fresh.limits.max_turnover_frac == 0.4

    # 3. and the file is genuinely the source, not a coincidence with a default:
    #    a saved field differs from the factory value, an unsaved one matches it.
    assert fresh.limits.max_gross_notional != LiveLimits().max_gross_notional
    assert fresh.limits.max_orders == LiveLimits().max_orders


def test_never_saved_limits_stay_none_and_fall_back_to_defaults(tmp_live, monkeypatch):
    """`None` must stay distinguishable from `{}`.

    "never configured" falls back to the factory caps; "the user cleared every
    cap" must not.  Collapsing the two would make the very first `LiveEngine`
    adopt an all-zero limit set -- i.e. refuse every trade, for no stated reason.
    """
    api = _live_api(monkeypatch)
    assert store_mod.Store("paper").load_limits() is None

    meta, _ = api.route_get("/api/live/meta", {})
    assert meta["limits_saved"]["paper"] is None
    assert LiveEngine(mode="paper").limits.to_dict() == LiveLimits().to_dict()

    # An explicitly emptied payload is *not* "never saved": it round-trips as an
    # all-default object, which is what `from_dict({})` means.
    api.route_post("/api/live/limits", {"mode": "paper", "limits": {}})
    assert store_mod.Store("paper").load_limits() is not None


def test_limits_are_saved_per_mode_not_shared(tmp_live, monkeypatch):
    """Absolute caps are account-specific: a $1k paper book and a $54k demo
    account cannot share a `max_gross_notional`.  One file per mode, or saving
    on the desk silently re-arms the other mode's guardrail."""
    api = _live_api(monkeypatch)
    api.route_post("/api/live/limits", {"mode": "paper",
                                       "limits": {"max_gross_notional": 111.0}})
    api.route_post("/api/live/limits", {"mode": "demo",
                                       "limits": {"max_gross_notional": 222.0}})
    assert store_mod.Store("paper").load_limits()["max_gross_notional"] == 111.0
    assert store_mod.Store("demo").load_limits()["max_gross_notional"] == 222.0
    assert LiveEngine(mode="paper").limits.max_gross_notional == 111.0
    assert LiveEngine(mode="demo").limits.max_gross_notional == 222.0
    # live was never touched -> still the factory value, still "never saved"
    assert store_mod.Store("live").load_limits() is None


def test_limits_post_rejects_an_unknown_mode(tmp_live, monkeypatch):
    """A typo'd mode must not silently write to `paper`'s file."""
    api = _live_api(monkeypatch)
    out, code = api.route_post("/api/live/limits", {"mode": "nope", "limits": {}})
    assert code == 400 and "error" in out
    assert store_mod.Store("paper").load_limits() is None


def test_an_explicit_limits_argument_still_beats_the_saved_file(tmp_live, monkeypatch):
    """`LiveEngine(limits=...)` is the caller saying "use these now".  The saved
    caps are a *fallback* for construction, not an override of an explicit
    argument -- otherwise a programmatic run could never tighten a cap."""
    store_mod.Store("paper").save_limits({"max_gross_notional": 999.0})
    assert LiveEngine(mode="paper").limits.max_gross_notional == 999.0
    explicit = LiveEngine(mode="paper", limits=LiveLimits(max_gross_notional=7.0))
    assert explicit.limits.max_gross_notional == 7.0


def test_a_new_utc_day_zeroes_used_turnover_and_status_agrees(tmp_live, monkeypatch):
    """「今日换手预算 也对不上」.

    `summary()` used to report the *raw* `turnover_used_today` while
    `turnover_state()` -- the figure the planner sizes against -- reset it on the
    UTC date.  The account panel read one, the planner used the other: two
    numbers, one heading, and no way to tell which was right.

    They now share `_turnover_from`, so the assertion is that the two are *the
    same value*, plus that the discarded figure is disclosed rather than
    silently dropped.
    """
    st = store_mod.Store("paper")
    st.write_state({"mode": "paper", "turnover_day": "2000-01-01",
                    "turnover_used_today": 0.19})
    s = st.summary()
    assert s["turnover_used_today"] == 0.0                  # reset, not 0.19
    assert s["turnover_day"] == store_mod.Store.today_utc()
    assert s["turnover_stale"] is True                      # ...and disclosed
    assert st.turnover_state()[1] == s["turnover_used_today"]

    monkeypatch.setattr(engine_mod, "newest_cached_bar", lambda bar="1h": None)
    stt = LiveEngine(mode="paper").status()
    assert stt["turnover_used_today"] == s["turnover_used_today"]
    assert stt["store"]["turnover_used_today"] == stt["turnover_used_today"]


def test_todays_turnover_is_not_flagged_stale(tmp_live, monkeypatch):
    """The staleness flag has to be able to be *false*, or it says nothing.

    Covers both legitimate non-stale shapes: a state file stamped today, and one
    that has never recorded a day at all (fresh ledger).
    """
    st = store_mod.Store("paper")
    st.write_state({"mode": "paper", "turnover_day": store_mod.Store.today_utc(),
                    "turnover_used_today": 0.07})
    s = st.summary()
    assert s["turnover_used_today"] == pytest.approx(0.07)
    assert s["turnover_stale"] is False

    store_mod.Store("demo").write_state({"mode": "demo"})   # no day recorded
    d = store_mod.Store("demo").summary()
    assert d["turnover_used_today"] == 0.0 and d["turnover_stale"] is False


def test_store_survives_a_truncated_file(tmp_live):
    st = store_mod.Store("demo")
    st.add_run({"run_id": "r1"})
    with open(st.path("runs.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"run_id": "half')          # a crash mid-append
    rows = st.runs()
    assert [r["run_id"] for r in rows] == ["r1"]


def test_disarming_the_kill_switch_never_claims_a_state_it_did_not_reach(tmp_path, monkeypatch):
    """Disarming must never *report* success it did not achieve.

    This host installs a Python `sitecustomize.py` safe-delete guard which aborts
    with `SystemExit` -- a `BaseException` -- once too many files have been
    deleted in a session.  `set_kill_switch(False)` used to catch only `OSError`,
    so the abort escaped and killed the request.  Swallowing it silently would be
    worse: the console would say "熔断已解除" while every order stayed blocked.

    So `set_kill_switch` re-reads the lock file and returns the state that is
    actually in effect.  Whether a removal is *permitted* is an environment
    property (and the normal path is covered by the kill-switch plan test above),
    so this test asserts the contract: the refused case is reported truthfully.
    """
    lock = tmp_path / "KILL"
    monkeypatch.setattr(creds_mod, "LOCK_FILE", str(lock))

    # arming is a write, and must be reflected in the reported state
    assert creds_mod.set_kill_switch(True) is True
    assert creds_mod.kill_switch_on() is True

    def refused(p):
        raise SystemExit(1)                     # exactly what the host guard raises

    monkeypatch.setattr(creds_mod.os, "remove", refused)
    assert creds_mod.set_kill_switch(False) is True      # still armed, truthfully
    assert creds_mod.kill_switch_on() is True


# ---------------------------------------------------------------------------
# reset must archive, never delete
# ---------------------------------------------------------------------------
def test_reset_archives_the_ledger_instead_of_deleting_it(tmp_live, monkeypatch):
    """A trading ledger is evidence; "reset the paper account" must not shred it.

    This also pins the failure mode that made the button unusable: this machine
    installs a `sitecustomize.py` safe-delete guard that refuses bulk recursive
    deletion and **aborts with `SystemExit`** (a `BaseException`, so the HTTP
    handler's `except Exception` could not catch it).  The request was dropped
    with no response and the page looked dead.  Renaming files never triggers it,
    and no deletion is needed here at all (`keep_archives` >= the count).
    """
    st = store_mod.Store("paper")
    st.write_state({"mode": "paper", "cash": 1234.0, "positions": {"X": 1}})
    st.add_run({"run_id": "keepme"})
    st.set_order({"clOrdId": "c1", "instId": "X", "state": "filled"})
    with open(st.path("fills.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "instId": "X"}\n')
    assert len(store_mod.Store("paper").orders()) == 1

    dest = store_mod.reset_mode("paper")
    assert dest and os.path.isdir(dest)
    # ledger is empty again
    assert store_mod.Store("paper").read_state() == {}
    assert store_mod.Store("paper").orders() == {}
    assert store_mod.Store("paper").runs() == []
    # ...but nothing was destroyed: the old rows are readable from the archive
    assert sorted(os.listdir(dest)) == ["fills.jsonl", "orders.json", "runs.jsonl",
                                        "state.json"]
    with open(os.path.join(dest, "runs.jsonl"), encoding="utf-8") as f:
        assert "keepme" in f.read()
    assert store_mod.archives("paper") == [os.path.basename(dest)]

    # an empty ledger has nothing to rotate, so no empty snapshot is created
    assert store_mod.reset_mode("paper") is None
    assert len(store_mod.archives("paper")) == 1

    # a later reset with content gets its own snapshot, even within the same second
    st.write_state({"mode": "paper", "second": True})
    dest2 = store_mod.reset_mode("paper")
    assert dest2 and dest2 != dest
    assert len(store_mod.archives("paper")) == 2


def test_reset_never_touches_a_file_it_does_not_own(tmp_live):
    """`os.listdir`-driven cleanup would delete a user's notes.  Ours is an
    explicit allow-list, so an unlisted file survives a reset."""
    st = store_mod.Store("demo")
    st.write_state({"mode": "demo", "cash": 1.0})
    stray = st.path("MY_NOTES.txt")
    with open(stray, "w", encoding="utf-8") as f:
        f.write("do not delete me")
    store_mod.reset_mode("demo")
    assert os.path.isfile(stray)


def test_reset_prunes_old_archives_without_a_recursive_delete(tmp_live, monkeypatch):
    """Pruning is bounded and file-by-file, and its failure is never fatal.

    The `os.remove` call is replaced by a counter so this test does not depend on
    the host *allowing* deletions -- and so it can assert the two properties that
    matter: only individual files are removed (never `rmtree`), and an abort
    during housekeeping does not break the reset that triggered it.
    """
    removed = []
    monkeypatch.setattr(store_mod.os, "remove", lambda p: removed.append(p))

    st = store_mod.Store("paper")
    last = None
    for i in range(4):
        st.write_state({"mode": "paper", "n": i})
        st.add_run({"run_id": f"r{i}"})
        last = store_mod.reset_mode("paper", keep_archives=2)
        assert last and os.path.isdir(last)          # reset always succeeds
    # pruning happened, and it only ever targeted individual ledger files ...
    assert len(removed) >= 4
    assert all(os.path.basename(p) in store_mod.LEDGER_FILES for p in removed)
    assert all(os.sep + "archive" + os.sep in p for p in removed)
    # ... never the newest snapshot (which `keep_archives=2` must preserve)
    assert not any(os.path.basename(last) in p for p in removed)

    # and a hard abort mid-prune must still leave a working reset
    def boom(p):
        raise SystemExit(1)
    monkeypatch.setattr(store_mod.os, "remove", boom)
    st.write_state({"mode": "paper", "after_boom": True})
    assert store_mod.reset_mode("paper", keep_archives=1) is not None
    assert store_mod.Store("paper").read_state() == {}


# ---------------------------------------------------------------------------
# paper engine end to end (no network)
# ---------------------------------------------------------------------------
def _minimal_target(weights, adv=None):
    return LiveTarget(weights=weights, book=[], pool=[
        {"instId": i, "adv": (adv or {}).get(i, 5e7), "score": 1.0, "price": 10.0,
         "selected": True} for i in weights],
        decision_ts="2026-09-23T02:00:00+00:00",
        next_decision_ts="2026-09-26T02:00:00+00:00",
        panel_last_ts="2026-09-26T00:00:00+00:00", bars_since_decision=72,
        rebalance_bars=72, rebalance_due=True, gross=sum(abs(v) for v in weights.values()))


def test_paper_execute_updates_cash_positions_and_turnover(tmp_live, monkeypatch):
    px = {"A-USDT-SWAP": 10.0, "B-USDT-SWAP": 20.0}
    monkeypatch.setitem(__import__("crypto_ls_research.execution.specs",
                                  fromlist=["SPECS_CACHE"]).SPECS_CACHE, None, None)
    eng = LiveEngine(mode="paper", limits=LiveLimits(
        max_gross_notional=1e9, max_gross_frac=99.0, max_turnover_frac=99.0,
        min_nav_usd=0.0, require_rebalance_due=False))
    monkeypatch.setattr(eng, "specs", lambda: {i: _spec() for i in px})
    monkeypatch.setattr(eng, "prices", lambda insts=None, force=False: dict(px))
    eng.store.write_state({"mode": "paper", "cash": 1000.0, "nav0": 1000.0,
                           "positions": {}, "pos_mode": "net_mode"})

    target = _minimal_target({"A-USDT-SWAP": 0.10, "B-USDT-SWAP": -0.10})
    plan, acct, _ = eng.build(target, use_budget=False)
    assert plan.n_orders == 2 and acct["nav"] == pytest.approx(1000.0)

    res = eng._send_paper(plan, target, "run1")
    assert all(r["ok"] for r in res)
    st = eng.store.read_state()
    assert set(st["positions"]) == {"A-USDT-SWAP", "B-USDT-SWAP"}
    assert st["positions"]["A-USDT-SWAP"]["sz"] > 0
    assert st["positions"]["B-USDT-SWAP"]["sz"] < 0
    assert st["cash"] < 1000.0                      # costs were charged
    assert len(eng.store.fills()) == 2
    assert len(eng.store.orders()) == 2

    # NAV now reflects the entry cost, not the notional
    acct2 = eng.account()
    assert 995.0 < acct2["nav"] < 1000.0


# ---------------------------------------------------------------------------
# turnover budget: charged for what went out, not for what we hoped to send
# ---------------------------------------------------------------------------
def _budgeted_plan(monkeypatch, budget=0.20):
    """A two-name paper plan that actually has a turnover budget attached."""
    px = {"A-USDT-SWAP": 10.0, "B-USDT-SWAP": 20.0}
    eng = LiveEngine(mode="paper", limits=LiveLimits(
        max_gross_notional=1e9, max_gross_frac=99.0, max_turnover_frac=99.0,
        min_nav_usd=0.0, require_rebalance_due=False))
    monkeypatch.setattr(eng, "specs", lambda: {i: _spec() for i in px})
    monkeypatch.setattr(eng, "prices", lambda insts=None, force=False: dict(px))
    eng.store.write_state({"mode": "paper", "cash": 1000.0, "nav0": 1000.0,
                           "positions": {}, "pos_mode": "net_mode"})
    target = _minimal_target({"A-USDT-SWAP": 0.10, "B-USDT-SWAP": -0.10})
    plan, acct, _ = eng.build(target, turnover_budget=budget)
    assert plan.turnover_budget == pytest.approx(budget)
    assert plan.n_orders == 2 and plan.turnover_frac > 0
    return eng, target, plan


def _turnover_record(plan, results, nav, run_id="runT"):
    """A run record shaped exactly like the one `execute()` persists."""
    return {
        "run_id": run_id, "mode": "paper", "ts": time.time(),
        "action": "rebalance", "dry_run": False, "nav": nav,
        "n_orders": plan.n_orders,
        "n_ok": sum(1 for r in results if r.get("ok")),
        "n_fail": sum(1 for r in results if not r.get("ok")),
        "order_notional": plan.order_notional,
        "turnover_frac": plan.turnover_frac,
        "turnover_charged": LiveEngine._charged_turnover(plan, results, nav),
        "violations": [], "results": results, "errors": [], "elapsed": 1.0,
    }


def test_an_all_rejected_rebalance_does_not_burn_the_daily_budget(tmp_live, monkeypatch):
    """The bug this exists to prevent.

    `_after_execute` used to bump by `plan.turnover_frac` unconditionally, so a
    rebalance whose every order was *rejected* still ate the whole day's budget.
    One failed demo run left `turnover_used_today = 0.19994` of 0.20; the next
    plan came out scaled by ~5e-5 -- orders of a few cents -- and nothing in the
    UI explained why.  Rejected orders moved nothing.
    """
    eng, target, plan = _budgeted_plan(monkeypatch)
    rows = [{"instId": o.inst_id, "ok": False, "notional": o.delta_notional,
             "error": "51008 余额不足"} for o in plan.orders]
    rec = _turnover_record(plan, rows, 1000.0)
    assert rec["turnover_charged"] == 0.0
    eng._after_execute(target, {"nav": 1000.0}, plan, rec)
    assert eng.store.turnover_state()[1] == pytest.approx(0.0), \
        "被拒的订单没有成交，不该占用换手预算"


def test_accepted_orders_charge_exactly_the_planned_turnover(tmp_live, monkeypatch):
    """All accepted -> the charge equals the plan (paper always fills)."""
    eng, target, plan = _budgeted_plan(monkeypatch)
    rows = [{"instId": o.inst_id, "ok": True, "notional": o.delta_notional}
            for o in plan.orders]
    rec = _turnover_record(plan, rows, 1000.0)
    assert rec["turnover_charged"] == pytest.approx(plan.turnover_frac)
    eng._after_execute(target, {"nav": 1000.0}, plan, rec)
    assert eng.store.turnover_state()[1] == pytest.approx(plan.turnover_frac)


def test_a_partly_rejected_rebalance_charges_only_what_went_out(tmp_live, monkeypatch):
    eng, target, plan = _budgeted_plan(monkeypatch)
    rows = [{"instId": o.inst_id, "ok": (i == 0), "notional": o.delta_notional}
            for i, o in enumerate(plan.orders)]
    rec = _turnover_record(plan, rows, 1000.0)
    want = sum(r["notional"] for r in rows if r["ok"]) / 1000.0
    assert rec["turnover_charged"] == pytest.approx(want)
    assert 0.0 < rec["turnover_charged"] < plan.turnover_frac
    eng._after_execute(target, {"nav": 1000.0}, plan, rec)
    assert eng.store.turnover_state()[1] == pytest.approx(want)


def test_a_lost_batch_is_charged_in_full(tmp_live, monkeypatch):
    """An ambiguous batch may well have gone through.

    Charging nothing would let the same budget be spent twice before
    reconciliation, so for a risk limit the safe side is "assume it happened".
    """
    eng, target, plan = _budgeted_plan(monkeypatch)
    rows = [{"instId": o.inst_id, "ok": False, "ambiguous": True,
             "notional": o.delta_notional, "error": "响应丢失，需对账"}
            for o in plan.orders]
    rec = _turnover_record(plan, rows, 1000.0)
    assert rec["turnover_charged"] == pytest.approx(plan.turnover_frac)
    eng._after_execute(target, {"nav": 1000.0}, plan, rec)
    assert eng.store.turnover_state()[1] == pytest.approx(plan.turnover_frac)


def test_no_budget_and_no_nav_charge_nothing():
    """Guards on the pure helper: no budget, or an unusable NAV."""
    p = build_plan({"A-USDT-SWAP": 0.10}, 1000.0, current_sz={},
                   prices={"A-USDT-SWAP": 10.0}, specs=_specs("A-USDT-SWAP"),
                   turnover_budget=None)
    rows = [{"instId": "A-USDT-SWAP", "ok": True, "notional": 100.0}]
    assert LiveEngine._charged_turnover(p, rows, 1000.0) == 0.0
    assert LiveEngine._charged_turnover(p, rows, 0.0) == 0.0
    assert LiveEngine._charged_turnover(p, [], 1000.0) == 0.0


def test_paper_flatten_returns_to_flat(tmp_live, monkeypatch):
    px = {"A-USDT-SWAP": 10.0}
    eng = LiveEngine(mode="paper", limits=LiveLimits(
        max_gross_notional=1e9, max_gross_frac=99.0, max_turnover_frac=99.0,
        min_nav_usd=0.0, require_rebalance_due=False))
    monkeypatch.setattr(eng, "specs", lambda: {i: _spec() for i in px})
    monkeypatch.setattr(eng, "prices", lambda insts=None, force=False: dict(px))
    eng.store.write_state({"mode": "paper", "cash": 1000.0, "nav0": 1000.0,
                           "positions": {}, "pos_mode": "net_mode"})
    target = _minimal_target({"A-USDT-SWAP": 0.20})
    plan, _, _ = eng.build(target, use_budget=False)
    eng._send_paper(plan, target, "run1")
    assert eng.store.read_state()["positions"]

    out = eng.flatten(dry_run=True)
    assert out["stage"] == "dry_run"
    assert out["plan"]["n_orders"] == 1 and out["plan"]["orders"][0]["side"] == "sell"
    # the real exit is not throttled by the turnover budget
    assert out["plan"]["turnover_budget"] is None


def test_data_staleness_flags_an_old_cache(tmp_live, monkeypatch):
    """The *mtime* leg: did the refresh actually run?

    Hermetic on purpose -- the reference-instrument probe is stubbed out, so a
    real BTC parquet in the developer's cache cannot decide the outcome of this
    test (that leak is exactly how "assert stale is False" used to pass on a
    machine with fresh data and fail on a clean one).
    """
    monkeypatch.setattr(engine_mod, "newest_cached_bar", lambda bar="1h": None)
    eng = LiveEngine(mode="paper")
    monkeypatch.setattr(os.path, "getmtime", lambda p: 1_600_000_000.0)  # 2020
    monkeypatch.setattr(os, "listdir", lambda p: ["x.parquet"])
    st = eng.data_staleness()
    assert st["bar"] == "1h" and st["age_hours"] > 12 and st["stale"] is True
    assert st["newest_bar"] is None and st["bar_age_hours"] is None

    monkeypatch.setattr(os.path, "getmtime", lambda p: time.time())
    assert eng.data_staleness()["stale"] is False

    def boom(p):
        raise OSError("no cache")

    monkeypatch.setattr(os, "listdir", boom)
    st2 = eng.data_staleness()
    assert st2["age_hours"] is None and st2["stale"] is False


def test_staleness_gates_on_bar_age_even_when_the_mtime_is_fresh(tmp_live, monkeypatch):
    """The *bar* leg, and why both legs exist.

    A cache whose file mtime is one second old can still hold a bar that is two
    days old: a download can rewrite files without the newest bar advancing
    (partial tail, a symbol that stopped trading, a clock skew).  Judging
    freshness by mtime alone would call that "fresh" and trade on it.
    """
    monkeypatch.setattr(os.path, "getmtime", lambda p: __import__("time").time())
    monkeypatch.setattr(os.path, "getmtime", lambda p: time.time())
    monkeypatch.setattr(os, "listdir", lambda p: ["x.parquet"])
    now = pd.Timestamp.now(tz="UTC")
    old_bar = (now - pd.Timedelta(hours=49)).isoformat()
    fresh_bar = (now - pd.Timedelta(minutes=30)).isoformat()
    monkeypatch.setattr(engine_mod, "newest_cached_bar",
                        lambda bar="1h", inst="BTC-USDT-SWAP": old_bar)

    st = engine_mod.staleness("1h")
    assert st["age_hours"] < 1                     # mtime leg says "just wrote it"
    assert st["bar_age_hours"] > 48                # bar leg says "two days old"
    assert st["stale"] is True                     # -> stale wins

    monkeypatch.setattr(engine_mod, "newest_cached_bar",
                        lambda bar="1h", inst="BTC-USDT-SWAP": fresh_bar)
    st2 = engine_mod.staleness("1h")
    assert st2["stale"] is False and st2["bar_age_hours"] < 1


def test_newest_cached_bar_uses_the_reference_instrument_not_a_pool_max(tmp_path, monkeypatch):
    """`max()` over the pool is a lie: one updated symbol makes 160 stale ones
    look current.  The probe must answer for a fixed reference instrument."""
    monkeypatch.setattr(engine_mod, "_NEWEST_CACHE", {})
    cdir = tmp_path / "candles" / "1h"
    cdir.mkdir(parents=True)
    idx = pd.date_range("2026-09-20", periods=3, freq="1h", tz="UTC")
    old = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    old.to_parquet(cdir / "BTC-USDT-SWAP.parquet")
    newer = pd.date_range("2026-09-26", periods=3, freq="1h", tz="UTC")
    pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=newer).to_parquet(
        cdir / "SLOW-USDT-SWAP.parquet")
    monkeypatch.setattr(engine_mod, "CACHE", str(tmp_path))

    got = engine_mod.newest_cached_bar("1h")
    assert got.startswith("2026-09-20")            # BTC's bar, not the pool max
    assert not got.startswith("2026-09-26")


# ---------------------------------------------------------------------------
# incremental market-data refresh (the "refresh quotes" button)
# ---------------------------------------------------------------------------
def test_refresh_market_data_asks_for_tomorrow_and_stays_incremental(tmp_live, monkeypatch):
    """Two regressions in one assertion set, both observed for real.

    1. `--end` is an *exclusive midnight* bound (`ts <= ms(end)`), so passing
       today's date stops at today's 00:00 bar -- which is the bar the cache
       already holds.  The refresh then reports `uptodate: 161, req=0` while
       still missing every bar since midnight (measured: 32 h stale, 0 requests).
       It must ask for tomorrow.
    2. `--refresh` re-downloads the whole history for all 161 symbols (~27k
       requests, ~40 min).  A button that blocks for 40 minutes is not a button.
    """
    seen = {}

    class _R:
        returncode = 0
        stdout = "[1h] done in 0s  {'app': 161}  req=161\n"
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    monkeypatch.setattr(engine_mod, "newest_cached_bar", lambda bar="1h",
                        inst="BTC-USDT-SWAP": None)
    monkeypatch.setattr("subprocess.run", fake_run)

    out = LiveEngine.refresh_market_data(bar="1h", progress=lambda m: None)
    cmd = seen["cmd"]
    end = cmd[cmd.index("--end") + 1]
    tomorrow = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
    assert end == tomorrow, f"--end must be tomorrow, got {end}"
    for flag in ("--incremental", "--only-candles", "--reuse-pool"):
        assert flag in cmd, f"missing {flag}"
    assert "--refresh" not in cmd
    assert out["exit_code"] == 0 and "staleness_after" in out


# ---------------------------------------------------------------------------
# incremental market-data refresh, downloader level
# ---------------------------------------------------------------------------
class _FakeOKX:
    """Applies the task function inline; records how far back each symbol asked.

    `download_candles` is what decides the request window, so the fake only has
    to reproduce `pmap`'s contract: `res[inst] = task(inst)`.
    """

    def __init__(self, rows_by_inst):
        self.rows_by_inst = rows_by_inst
        self.windows = {}
        self.stats = {"req": 0, "err": 0}

    def pmap(self, fn, insts, desc=""):
        return {i: fn(i) for i in insts}


def _candle_rows(start_iso, n, step_ms=3600_000, px=10.0):
    t0 = int(pd.Timestamp(start_iso, tz="UTC").timestamp() * 1000)
    return [[str(t0 + k * step_ms), str(px), str(px), str(px), str(px),
             "1", "1", "1", "1"] for k in range(n)]


@pytest.fixture()
def dl(tmp_path, monkeypatch):
    import crypto_ls_research.data.download as dl_mod
    monkeypatch.setattr(dl_mod, "CACHE", str(tmp_path))
    dl_mod.ensure_dirs()
    monkeypatch.setattr(dl_mod, "ms", lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000))
    return dl_mod


def test_incremental_refresh_only_appends_the_tail(dl, monkeypatch):
    """The whole point: a quote refresh must not re-download five years.

    We assert on the *request window* the downloader chose, because that is what
    separates a 15-second button from a 40-minute one (~27k requests).
    """
    path = os.path.join(dl.CACHE, "candles", "1h", "A-USDT-SWAP.parquet")
    idx = pd.date_range("2026-09-01", "2026-09-20 00:00", freq="1h", tz="UTC")
    pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0,
                  "vol_ccy": 1.0, "amount": 1.0}, index=idx).to_parquet(path)
    n_before = len(pd.read_parquet(path))

    cli = _FakeOKX({})
    asked = {}

    def fake_dl_one(c, inst, bar, start_ms, end_ms):
        asked[inst] = (start_ms, end_ms)
        rows = _candle_rows("2026-09-19 18:00", 200)     # overlaps the cache by 6 bars
        cli.stats["req"] += 1
        return rows

    monkeypatch.setattr(dl, "_dl_one", fake_dl_one)
    dl.download_candles(cli, ["A-USDT-SWAP"], "1h", "2021-01-01", "2026-09-26",
                        incremental=True)

    start_ms, end_ms = asked["A-USDT-SWAP"]
    full_start = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp() * 1000)
    assert start_ms > full_start + 1_000_000_000        # nowhere near 2021
    assert start_ms == int(pd.Timestamp("2026-09-19 18:00", tz="UTC").timestamp() * 1000)
    assert cli.stats["req"] == 1                        # one request, not a history

    after = pd.read_parquet(path)
    assert len(after) > n_before
    assert after.index.is_monotonic_increasing and not after.index.duplicated().any()
    assert after.index[0] == idx[0]                     # history preserved
    assert after.index[-1] > idx[-1]


def test_incremental_refresh_leaves_an_uptodate_symbol_alone(dl, monkeypatch):
    here = pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.date_range(end=here, periods=10, freq="1h", tz="UTC")
    pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0,
                  "vol_ccy": 1.0, "amount": 1.0}, index=idx).to_parquet(
        os.path.join(dl.CACHE, "candles", "1h", "B-USDT-SWAP.parquet"))
    cli = _FakeOKX({})
    monkeypatch.setattr(dl, "_dl_one",
                        lambda *a: pytest.fail("must not re-request an up-to-date symbol"))
    dl.download_candles(cli, ["B-USDT-SWAP"], "1h", "2021-01-01",
                        here.strftime("%Y-%m-%d"), incremental=True)


def test_a_stale_cache_without_incremental_still_refetches_everything(dl, monkeypatch):
    """Documents the default (non-incremental) behaviour, i.e. why the button
    must not use it: a stale cache falls through to a full history request."""
    idx = pd.date_range("2026-09-01", periods=10, freq="1h", tz="UTC")
    pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0,
                  "vol_ccy": 1.0, "amount": 1.0}, index=idx).to_parquet(
        os.path.join(dl.CACHE, "candles", "1h", "C-USDT-SWAP.parquet"))
    asked = {}

    def fake_dl_one(c, inst, bar, start_ms, end_ms):
        asked[inst] = start_ms
        return _candle_rows("2026-09-25 00:00", 20)

    monkeypatch.setattr(dl, "_dl_one", fake_dl_one)
    dl.download_candles(_FakeOKX({}), ["C-USDT-SWAP"], "1h", "2021-01-01",
                        "2026-09-26", incremental=False)
    assert asked["C-USDT-SWAP"] == int(pd.Timestamp("2021-01-01", tz="UTC").timestamp() * 1000)


def test_merge_append_keeps_the_fresher_row_on_the_seam(dl):
    """OKX can revise the newest bars, so the freshly downloaded row must win."""
    path = os.path.join(dl.CACHE, "candles", "1h", "D-USDT-SWAP.parquet")
    idx = pd.date_range("2026-09-20", periods=3, freq="1h", tz="UTC")
    pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0,
                  "vol_ccy": 1.0, "amount": 1.0}, index=idx).to_parquet(path)
    fresh = pd.DataFrame({"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "vol": 9.0,
                          "vol_ccy": 9.0, "amount": 9.0},
                         index=[idx[2], idx[2] + pd.Timedelta(hours=1)])
    out = dl._merge_append(path, fresh)
    assert len(out) == 4
    assert float(out.loc[idx[2], "close"]) == 9.0          # revised bar replaced
    assert float(out.loc[idx[0], "close"]) == 1.0          # untouched history


def test_paper_book_keeps_the_fill_price_and_never_the_intended_book(tmp_live, monkeypatch):
    """Two accounting bugs, both of which made the on-screen numbers fiction.

    1. `_after_execute` used to write `positions: plan_target_positions(plan)` --
       the *intended* book, in a different schema (`{"sz","px"}`, no `avg_px`).
       That replaced the paper book written by `_send_paper`, so the next
       `_paper_account` computed `upl = (mark - 0) * sz * ct`: unrealised P&L
       became the book's **net notional**.  On a dollar-neutral book that is
       near zero by luck, which is why it survived unnoticed.
    2. Position notional must carry `ctVal` (`|sz| * ct * markPx`).  Without it
       BTC-USDT-SWAP (ctVal 0.01) reads 100x too large, and the desk displayed
       "gross exposure 1.00x NAV" for a 0.20x book.
    """
    px = {"A-USDT-SWAP": 10.0}
    monkeypatch.setitem(SPECS_CACHE, None, None)
    eng = LiveEngine(mode="paper", limits=LiveLimits(
        max_gross_notional=1e9, max_gross_frac=99.0, max_turnover_frac=99.0,
        min_nav_usd=0.0, require_rebalance_due=False))
    monkeypatch.setattr(eng, "specs", lambda: {"A-USDT-SWAP": _spec(ct=0.01, mn=0.1)})
    monkeypatch.setattr(eng, "prices", lambda insts=None, force=False: dict(px))
    eng.store.write_state({"mode": "paper", "cash": 1000.0, "nav0": 1000.0,
                           "positions": {}, "pos_mode": "net_mode"})

    target = _minimal_target({"A-USDT-SWAP": 0.20})         # 200 USDT gross
    plan, acct, _ = eng.build(target, use_budget=False)
    eng._send_paper(plan, target, "run1")

    book = eng.store.read_state()["positions"]
    sz = book["A-USDT-SWAP"]["sz"]
    assert book["A-USDT-SWAP"]["avg_px"] == pytest.approx(10.0)   # the FILL price
    assert abs(sz) * 0.01 * 10.0 == pytest.approx(200.0, rel=0.02)

    # the intention must land in its own field, never overwrite the book
    rec = {"run_id": "run1", "ts": time.time()}
    eng._after_execute(target, acct, plan, rec)
    st = eng.store.read_state()
    assert set(st["positions"]["A-USDT-SWAP"]) == {"sz", "avg_px"}
    # 2000 contracts x ctVal 0.01 x 10 USDT = 200 USDT of notional
    assert st["target_positions"]["A-USDT-SWAP"]["sz"] == pytest.approx(2000.0)

    # with mark == fill price there is no P&L left to explain
    a2 = eng.account()
    assert a2["unrealised"] == pytest.approx(0.0, abs=1e-9)
    assert a2["nav"] == pytest.approx(a2["cash"], abs=1e-9)
    # notional is ctVal-aware and is what the console must display
    assert a2["positions"][0]["notional"] == pytest.approx(200.0, rel=0.02)


def test_book_from_account_normalises_the_display_list():
    """`account()` returns a display list; the local book is a dict of
    `{sz, avg_px}`.  Assigning one to the other is how `avg_px` disappeared."""
    acct = {"positions": [
        {"instId": "A", "pos": -3.0, "avgPx": 12.5, "markPx": 13.0, "notional": 4.0},
        {"instId": "B", "pos": 0.0, "avgPx": None, "markPx": 1.0},
        {"pos": 9.0},                                   # no instId -> skipped
    ]}
    assert engine_mod._book_from_account(acct) == {
        "A": {"sz": -3.0, "avg_px": 12.5}, "B": {"sz": 0.0, "avg_px": 0.0}}
    assert engine_mod._book_from_account({}) == {}


# ---------------------------------------------------------------------------
# the execution window is an edge, not a level
#
# `due` used to be `bars_since_decision >= R`, which is true only when the
# panel's last bar IS a grid point.  But a grid point cannot be booked while it
# is the panel's last bar (exec price is `next_open`), so at that instant the
# newest booked rebalance was still the PREVIOUS grid point -- and the desk
# traded a book one whole rebalance period stale.  Nothing raised.  The
# positions were simply a day (or three) old.
# ---------------------------------------------------------------------------
def test_the_window_is_open_on_exactly_one_bar_past_a_grid_point():
    """`== 1`, not `>= R`.  This is the regression guard for the stale book."""
    assert signal_mod.rebalance_window_open(1) is True


def test_the_old_level_trigger_is_not_the_window():
    """`bars_since_decision == R` means the panel is sitting ON the grid point.

    That is one bar *before* the window: the grid point's own rebalance is not
    bookable yet, so `target` is still the previous period's book.  Firing here
    is exactly the bug.

    The live path no longer reports this state at all -- it appends the execution
    bar instead, so a panel ending on a grid point reports `since == 1` (see
    `test_the_appended_execution_bar_books_what_the_next_bar_would_have`).  The
    rule stays pinned anyway: it is the one-line change that would reintroduce a
    book a full period stale, and nothing else would go red.
    """
    for R in (24, 72):                                   # 1-day and 3-day grids
        assert signal_mod.rebalance_window_open(R) is False, R


def test_the_window_is_not_open_everywhere_else_either():
    """Guard the other direction: a level trigger would rebalance every tick."""
    for since in (0, 2, 3, 23, 25, 71, 73):
        assert signal_mod.rebalance_window_open(since) is False, since


# ---------------------------------------------------------------------------
# the execution bar
#
# `run_backtest` books a rebalance at bar `t` from the decision taken at `t - 1`,
# so the panel's last row is the bar the desk is TRADING AT -- not merely the last
# bar whose OHLC is known.  `dec_idx = arange(warmup + off, T - 1, R)` therefore
# excludes the panel's last row, and a panel that stops on a grid point cannot book
# that grid point at all: `rebalances[-1]` is still the PREVIOUS one.
#
# Waiting for the next bar to *close* cost a whole hour at 1h bars even though the
# only field the engine reads from it -- the fill price `open[T-1]` -- is known the
# instant the bar opens.  Measured 2026-09-29 on the real cache (R=24, 1-day grid):
# the panel stopped at 09-29T02:00Z, the newest cached candle was 03:00Z, and the
# desk could not trade until 北京 13:19 -- two hours after bar G closed at 11:00.
# ---------------------------------------------------------------------------
def _cut(p, n: int):
    """The first `n` rows of a panel, i.e. a panel that ends at `p.index[n - 1]`."""
    import dataclasses
    return dataclasses.replace(
        p, **{f: getattr(p, f).iloc[:n]
              for f in ("open", "high", "low", "close", "vol", "vol_ccy",
                        "amount", "funding")})


def test_the_grid_point_check_fires_on_exactly_one_bar_of_the_period():
    """One bar too eager and the panel is pushed PAST the grid point, so
    `bars_since_decision` becomes 2 and the desk never trades at all; one bar too
    shy and it trades the previous period's book.  Both are silent."""
    R = 24
    g = pd.Timestamp("2026-09-29T02:00:00+00:00")            # the grid point
    booked = g - R * pd.Timedelta(hours=1)                   # what the engine recorded

    assert signal_mod.needs_execution_bar(g, booked, R, "1h") is True
    for k in (-1, 1, 2, 23, 25):
        assert signal_mod.needs_execution_bar(
            g + k * pd.Timedelta(hours=1), booked, R, "1h") is False, k
    # a 1-bar grid gets the same treatment: the appended bar is never a decision bar
    assert signal_mod.needs_execution_bar(g, g - pd.Timedelta(hours=1), 1, "1h") is True


def _grid_point_index(p, cfg) -> int:
    """A grid point comfortably inside the panel, derived from the engine itself."""
    a = int(run_backtest(p, cfg).meta["warmup_bars"])
    return a + int(cfg.rebalance_bars) * 27


def test_the_panel_cannot_book_the_grid_point_it_ends_on():
    """The regression: without the execution bar the desk is a period stale."""
    p = synth_panels(n_inst=8, n_bars=3000, bar="15m")
    cfg = synth_cfg("15m")
    i = _grid_point_index(p, cfg)
    p_on = _cut(p, i + 1)                       # panel ends ON the grid point

    last = run_backtest(p_on, cfg).rebalances[-1]

    assert pd.Timestamp(last["ts"]) == p.index[i - int(cfg.rebalance_bars)], \
        "面板末尾那个调仓点被记账了 —— 前提变了，下面那条等值测试就不再是同一个问题"
    assert pd.Timestamp(last["ts"]) < p_on.index[-1]


def test_the_appended_execution_bar_books_what_the_next_bar_would_have():
    """The equality the live path depends on, measured on a real backtest.

    Appending a copy of the last closed bar must give the SAME book as waiting for
    the next bar to arrive -- same legs, same weights, same `exec_ts`.  If this ever
    drifts, the desk trades something the backtest never booked.
    """
    p = synth_panels(n_inst=8, n_bars=3000, bar="15m")
    cfg = synth_cfg("15m")
    i = _grid_point_index(p, cfg)

    appended = run_backtest(
        signal_mod._append_execution_bar(_cut(p, i + 1), "15m"), cfg).rebalances[-1]
    natural = run_backtest(_cut(p, i + 2), cfg).rebalances[-1]

    assert pd.Timestamp(appended["ts"]) == p.index[i]
    assert pd.Timestamp(appended["exec_ts"]) == p.index[i + 1]
    assert appended["exec_ts"] == natural["exec_ts"]
    for k in ("long", "short", "w_long", "w_short", "exposure", "scale",
              "long_gross", "short_gross"):
        assert appended[k] == natural[k], f"{k} 与「等下一根 bar」的结果不一致"


def test_the_call_site_actually_appends_when_the_panel_ends_on_a_grid_point():
    """The append has to happen in the live path, not merely be available.

    `_append_execution_bar` is correct in isolation; the failure this guards is the
    call site declining to use it, which leaves the desk a full period stale with
    nothing in the log to show for it.  And the reverse: appending one bar late
    pushes the panel PAST the grid point, `bars_since_decision` reads 2, and the
    desk never trades at all.
    """
    p = synth_panels(n_inst=8, n_bars=3000, bar="15m")
    cfg = synth_cfg("15m")
    i = _grid_point_index(p, cfg)

    p_on, res_on, appended = signal_mod.book_with_execution_bar(_cut(p, i + 1), cfg, "15m")
    assert appended is True, "面板末尾就在调仓点上，却没有补执行 bar"
    assert len(p_on.index) == i + 2
    assert pd.Timestamp(res_on.rebalances[-1]["ts"]) == p.index[i]
    assert pd.Timestamp(res_on.rebalances[-1]["exec_ts"]) == p.index[i + 1]

    p_past, res_past, appended_past = signal_mod.book_with_execution_bar(
        _cut(p, i + 2), cfg, "15m")
    assert appended_past is False, "面板已经走过调仓点，再补一根就把它推过头了"
    assert len(p_past.index) == i + 2
    assert pd.Timestamp(res_past.rebalances[-1]["ts"]) == p.index[i]


def test_the_execution_bar_carries_only_what_the_last_closed_bar_knew():
    """No look-ahead: the appended row is a copy, so it adds no information."""
    p = synth_panels(n_inst=6, n_bars=500, bar="1h")
    q = signal_mod._append_execution_bar(p, "1h")

    assert len(q.index) == len(p.index) + 1
    assert q.index[-1] == p.index[-1] + pd.Timedelta(hours=1)
    assert list(q.insts) == list(p.insts)
    assert q.list_dt.equals(p.list_dt)
    for f in ("open", "high", "low", "close", "vol", "vol_ccy", "amount", "funding"):
        pd.testing.assert_series_equal(getattr(q, f).iloc[-1],
                                       getattr(p, f).iloc[-1], check_names=False)


def test_extend_to_last_reaches_the_newest_cached_bar(tmp_path):
    """The default stops one bar short of the cache; `extend_to_last` reaches it.

    `pd.date_range(..., inclusive="left")` treats `end` as exclusive, so the rebuild
    that is supposed to "extend to the last available bar" always stopped one bar
    short whenever the newest candle sat past `end` -- the normal case for a live
    caller passing today's date.  The default keeps that behaviour because every
    archived v3 number was produced with it; the live path opts out.
    """
    cdir = tmp_path / "candles" / "1h"
    cdir.mkdir(parents=True)
    idx = pd.date_range("2026-09-28 00:00", periods=30, freq="1h", tz="UTC")
    px = pd.Series(range(100, 130), index=idx, dtype="float64")
    for inst in ("A-USDT-SWAP", "B-USDT-SWAP"):
        pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                      "vol": 1.0, "vol_ccy": 1.0, "amount": 1e6}
                     ).to_parquet(cdir / f"{inst}.parquet")

    end = idx[-3].strftime("%Y-%m-%d %H:%M")        # a bound BEHIND the newest bar
    short = data_store.load_panels("1h", "2026-09-28", end, cache=str(tmp_path))
    full = data_store.load_panels("1h", "2026-09-28", end, cache=str(tmp_path),
                                  extend_to_last=True)

    assert short.index[-1] == idx[-2], "默认行为变了（存档结果依赖它）"
    assert full.index[-1] == idx[-1], "extend_to_last 没取到最新那根"
    assert len(full.index) == len(short.index) + 1


# ---------------------------------------------------------------------------
# signal cache round trip
#
# The worst bug this desk has had was not a wrong number -- it was a *second
# code path*.  `compute_live_target` returns the cached payload on a hit and
# re-derives from the panel on a miss, so a payload that cannot be rebuilt makes
# the first plan work and every plan after it crash.  Whether the desk can
# re-derive its target must not depend on how recently it already did.
# ---------------------------------------------------------------------------
def _a_cached_target() -> LiveTarget:
    return LiveTarget(
        weights={"A-USDT-SWAP": 0.2, "B-USDT-SWAP": -0.2},
        book=[{"instId": "A-USDT-SWAP", "w": 0.2}],
        pool=[{"instId": "A-USDT-SWAP", "score": 1.5}],
        decision_ts="2026-09-26T00:00:00Z",
        next_decision_ts="2026-09-29T00:00:00Z",
        panel_last_ts="2026-09-26T23:00:00Z",
        bars_since_decision=23, rebalance_bars=72, rebalance_due=False,
        gross=0.4,
        diagnostics={"scale": 0.1804}, config={"bar": "1h"},
        generated_at=1_700_000_000.0)


def test_signal_survives_its_own_cache_payload_with_the_cache_stamp_on_it(tmp_live,
                                                                          monkeypatch):
    """`_write_cache` stamps `_cached_at`; `from_dict` must tolerate it."""
    monkeypatch.setattr(signal_mod, "SIGNAL_DIR", str(tmp_live / "live"))
    monkeypatch.setattr(signal_mod, "SIGNAL_CACHE", str(tmp_live / "live" / "sig.json"))
    t = _a_cached_target()
    signal_mod._write_cache("k", t.as_dict())

    row = signal_mod._read_cache("k", ttl=3600.0)
    assert row is not None
    assert "_cached_at" in row                        # the stamp really is there
    back = LiveTarget.from_dict(row)
    assert back.decision_ts == t.decision_ts
    assert back.weights == t.weights
    assert back.diagnostics == t.diagnostics
    assert back.gross == pytest.approx(t.gross)
    assert back.from_cache is False                   # caller decides, not the file


def test_a_cache_hit_reaches_the_return_a_miss_would_have(monkeypatch):
    """Drive the real call site: a hit must return, not raise.

    Only the cheap path is exercised -- that is the point, a hit must not fall
    through to the backtest.
    """
    row = _a_cached_target().as_dict()
    row["_cached_at"] = time.time()
    row["_schema"] = 3                                # a future key must not break old readers
    monkeypatch.setattr(signal_mod, "_read_cache", lambda key, ttl: row)
    monkeypatch.setattr(signal_mod, "resolve_insts",
                        lambda *a, **k: pytest.fail("a cache hit must not rebuild the pool"))

    t = signal_mod.compute_live_target(bar="1h", rebalance_days=3.0, use_cache=True)
    assert t.from_cache is True
    assert t.decision_ts == "2026-09-26T00:00:00Z"
    assert t.rebalance_due is False


# ---------------------------------------------------------------------------
# the cache key must notice a code change
#
# `_cache_key` used to be built from data + params only.  A fix to the signal
# path therefore stayed invisible for up to `ttl` (30 min) and the daemon kept
# serving a target -- including a `rebalance_due=False` -- computed by the
# PREVIOUS version.  That symptom is the worst kind: the restart looks like it
# did not take effect, and the desk silently skips the very window it was just
# fixed to catch.  `_code_signature()` is the guard.
# ---------------------------------------------------------------------------
def _key(**over) -> str:
    base = dict(bar="1h", rebalance_days=1.0, asset_class="crypto",
                start="2021-01-01", end="2026-01-01", overrides={},
                data_sig="D", code_sig="C")
    base.update(over)
    return signal_mod._cache_key(**base)


def test_the_code_signature_is_part_of_the_cache_identity():
    """A different code signature must give a different key -- and a same one must not."""
    assert _key(code_sig="C1") != _key(code_sig="C2")
    assert _key(code_sig="C1") == _key(code_sig="C1")      # not accidentally unique
    assert _key(data_sig="D2") != _key()                   # the older half still works


def test_the_code_fingerprint_comes_from_the_files_not_from_a_constant():
    """A constant would make the guard silently absent while looking present."""
    sig = signal_mod._code_signature()
    assert "missing" not in sig, sig
    parts = sig.split("|")
    assert len(parts) == 2, parts
    for part in parts:
        mtime, sep, size = part.partition(":")
        assert sep == ":" and mtime.isdigit() and size.isdigit(), part
    # the first component is this module: compare it against the real stat, so a
    # helper that returned a plausible-looking literal would still fail.
    st = os.stat(signal_mod.__file__)
    assert parts[0] == f"{int(st.st_mtime)}:{st.st_size}"


class _KeySeen(Exception):
    """Raised by the `_read_cache` spy so the call site stops before the rebuild."""


def test_a_code_change_reaches_the_cache_key_through_the_call_site(monkeypatch):
    """`_code_signature()` must be *in the key the call site builds*.

    Testing `_cache_key` alone would not catch the interesting mistake: the helper
    can be perfect and still be dropped from the call.  So drive the real entry
    point and read the key it actually asked the cache for.
    """
    seen: list = []

    def spy(key, ttl):
        seen.append(key)
        raise _KeySeen

    monkeypatch.setattr(signal_mod, "_read_cache", spy)
    for sig in ("code-v1", "code-v2"):
        monkeypatch.setattr(signal_mod, "_code_signature", lambda s=sig: s)
        with pytest.raises(_KeySeen):
            signal_mod.compute_live_target(bar="1h", rebalance_days=1.0,
                                           use_cache=True, end="2026-01-01")
    assert len(seen) == 2, seen
    assert seen[0] != seen[1], "代码变了却拿到同一个 cache key → 会继续吃旧 payload"


def test_a_cached_payload_missing_a_required_field_fails_with_a_useful_message():
    """Fail loud and legibly.  `LiveTarget(**d)` used to raise a bare TypeError
    naming a private cache key, which pointed the reader at the wrong module."""
    row = _a_cached_target().as_dict()
    row["_cached_at"] = time.time()
    del row["weights"]
    with pytest.raises(ValueError) as ei:
        LiveTarget.from_dict(row)
    assert "weights" in str(ei.value)
    assert "cached target" in str(ei.value)


def test_a_stale_or_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path, monkeypatch):
    p = tmp_path / "sig.json"
    monkeypatch.setattr(signal_mod, "SIGNAL_CACHE", str(p))
    monkeypatch.setattr(signal_mod, "SIGNAL_DIR", str(tmp_path))
    assert signal_mod._read_cache("k", ttl=3600.0) is None       # missing file
    p.write_text("{not json", encoding="utf-8")
    assert signal_mod._read_cache("k", ttl=3600.0) is None       # truncated write
    # `_write_cache` always stamps "now" (correctly -- it is the *write* time), so
    # an expired entry has to be forged in the file rather than inserted by hand.
    p.write_text(json.dumps({"k": {"_cached_at": time.time() - 10_000}}), encoding="utf-8")
    assert signal_mod._read_cache("k", ttl=60.0) is None         # expired
    assert signal_mod._read_cache("k", ttl=-1) is not None       # ttl<0 disables expiry
    # and a write really did land, readable straight back through the cache
    signal_mod._write_cache("k2", _a_cached_target().as_dict())
    assert signal_mod._read_cache("k2", ttl=3600.0)["decision_ts"] == "2026-09-26T00:00:00Z"


# ---------------------------------------------------------------------------
# credential hygiene
# ---------------------------------------------------------------------------
def test_credentials_are_masked_and_never_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(creds_mod, "CREDS_FILE", str(tmp_path / "okx_creds.json"))
    for env in ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE",
                "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        monkeypatch.delenv(env, raising=False)
    creds_mod.save_creds("demo", "ABCDEFGH12345678", "supersecretvalue", "pass1234")
    st = creds_mod.creds_status()
    assert st["demo"]["configured"] is True
    assert st["demo"]["key_hint"] == "ABCD…5678"
    blob = json.dumps(st, ensure_ascii=False)
    assert "supersecretvalue" not in blob and "pass1234" not in blob
    assert st["live"]["configured"] is False
    assert st["paper"]["configured"] is False          # paper can never hold a key

    c = creds_mod.load_creds("demo")
    assert "supersecretvalue" not in repr(c) and "supersecretvalue" not in str(c)


def test_env_var_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(creds_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(creds_mod, "CREDS_FILE", str(tmp_path / "okx_creds.json"))
    creds_mod.save_creds("live", "FILEKEY123456789", "s", "p")
    monkeypatch.setenv("OKX_API_KEY", "ENVKEY1234567890")
    monkeypatch.setenv("OKX_SECRET_KEY", "s2")
    monkeypatch.setenv("OKX_PASSPHRASE", "p2")
    c = creds_mod.load_creds("live")
    assert c.source == "env" and c.api_key.startswith("ENVKEY")
    assert creds_mod.creds_status()["live"]["env_override"] is True


# ---------------------------------------------------------------------------
# a batch is answered twice: once for the request, once per order
# ---------------------------------------------------------------------------
def _okx_with_envelope(monkeypatch, envelope: dict):
    """An `OKXPrivate` whose only stubbed layer is the socket.

    `request()` is redirected through the real `_unwrap`, so what is under test
    is the production envelope -> rows contract rather than a paraphrase of it.
    `__new__` skips `__init__` because none of this needs credentials.
    """
    cli = OKXPrivate.__new__(OKXPrivate)
    raw = json.dumps(envelope)
    monkeypatch.setattr(cli, "request",
                        lambda method, path, params=None, body=None, **kw:
                        OKXPrivate._unwrap(raw, path))
    return cli


def test_a_partly_successful_batch_keeps_every_per_order_verdict(monkeypatch):
    """`code: "2"` is a healthy batch, not a failure.

    OKX answers batch endpoints at two levels: the envelope `code` describes the
    *request* ("Bulk operation partially successful"), each row's `sCode`
    describes *that order*.  `_unwrap` raised on any non-zero envelope, so
    `place_batch`'s row loop never ran.

    What that cost, measured on a real demo rebalance: 19 orders sent, 11 of
    them filled, and the ledger recorded `n_ok = 0` with the identical error
    `OKX 错误 2: Bulk operation partially successful` on every row.  OKX does not
    keep rejected orders, so once the response was discarded the per-order
    reasons were unrecoverable -- not from `orders-pending`, not from
    `orders-history`.  The diagnosis was destroyed at the moment it was created.
    """
    orders = [{"instId": "A-USDT-SWAP", "clOrdId": "c1", "sz": "1"},
              {"instId": "B-USDT-SWAP", "clOrdId": "c2", "sz": "2"},
              {"instId": "C-USDT-SWAP", "clOrdId": "c3", "sz": "3"}]
    cli = _okx_with_envelope(monkeypatch, {
        "code": "2", "msg": "Bulk operation partially successful",
        "data": [
            {"clOrdId": "c1", "ordId": "11", "sCode": "0", "sMsg": ""},
            {"clOrdId": "c2", "ordId": "", "sCode": "51001",
             "sMsg": "Instrument ID does not exist"},
            {"clOrdId": "c3", "ordId": "13", "sCode": "0", "sMsg": ""},
        ]})
    rows = cli.place_batch(orders)
    assert [r["_ok"] for r in rows] == [True, False, True]
    assert rows[1]["_error"] and "51001" in rows[1]["_error"]
    assert rows[0]["_instId"] == "A-USDT-SWAP" and rows[0]["_clOrdId"] == "c1"
    assert rows[0]["ordId"] == "11"


def test_the_engine_records_mixed_outcomes_from_one_partial_batch(tmp_live, monkeypatch):
    """End to end through `_send_okx`: 1 accepted, 1 rejected, with its own reason."""
    eng, target, plan = _budgeted_plan(monkeypatch)
    # `_send_okx` mints its own clOrdIds; pin them so the envelope can echo them.
    monkeypatch.setattr(engine_mod, "make_clordid",
                        lambda prefix, inst, seq: f"c{seq}")
    envelope = {"code": "2", "msg": "Bulk operation partially successful",
                "data": [{"clOrdId": "c0", "ordId": "o0", "sCode": "0", "sMsg": ""},
                         {"clOrdId": "c1", "ordId": "", "sCode": "51001",
                          "sMsg": "Instrument ID does not exist"}]}
    cli = _okx_with_envelope(monkeypatch, envelope)
    monkeypatch.setattr(eng, "_private", lambda: cli)
    monkeypatch.setattr(eng, "_apply_leverage", lambda p: [])

    res = eng._send_okx(plan, "demo_20260928_150854_862ed8")
    assert [r["ok"] for r in res] == [True, False]
    assert res[0]["error"] is None and res[0]["ordId"] == "o0"
    assert "51001" in res[1]["error"]
    assert res[1]["error"] != res[0]["error"]
    assert eng.store.orders()["c1"]["state"] == "rejected"
    assert eng.store.orders()["c0"]["state"] == "pending"


def test_an_envelope_error_without_rows_still_raises(monkeypatch):
    """The fix must not turn a real failure into a silent success.

    Auth and parameter failures carry no per-row verdicts, so there is nothing
    to hand back and they must keep raising.  So must the transient codes the
    retry loop in `request()` depends on.
    """
    cases = [
        ("50111", []),                       # bad key
        ("51000", []),                       # bad parameter
        ("50013", None),                     # system busy -> retried upstream
        ("50011", [{"sCode": "0"}]),         # rate limit: rows exist, code is not per-row
        ("51001", [{"instId": "X"}]),        # a row, but no sCode -> not a verdict list
    ]
    for code, data in cases:
        cli = _okx_with_envelope(monkeypatch, {"code": code, "msg": "boom", "data": data})
        with pytest.raises(OKXError) as ei:
            cli.place_batch([{"instId": "A-USDT-SWAP", "clOrdId": "c1", "sz": "1"}])
        assert ei.value.code == code


def test_a_row_verdict_is_bound_by_clordid_not_by_position(monkeypatch):
    """Rows are matched to requests by the echoed `clOrdId`.

    Trusting array position would attribute a rejection to a neighbouring
    instrument -- a wrong answer that looks exactly like a right one.
    """
    orders = [{"instId": "A-USDT-SWAP", "clOrdId": "c1", "sz": "1"},
              {"instId": "B-USDT-SWAP", "clOrdId": "c2", "sz": "2"}]
    cli = _okx_with_envelope(monkeypatch, {
        "code": "2", "msg": "partial",
        "data": [
            {"clOrdId": "c2", "sCode": "51001", "sMsg": "no such instrument"},
            {"clOrdId": "c1", "sCode": "0", "sMsg": ""},
        ]})
    by_cid = {r["_clOrdId"]: r for r in cli.place_batch(orders)}
    assert by_cid["c1"]["_instId"] == "A-USDT-SWAP" and by_cid["c1"]["_ok"] is True
    assert by_cid["c2"]["_instId"] == "B-USDT-SWAP" and by_cid["c2"]["_ok"] is False


def test_clordid_prefix_distinguishes_runs_within_the_same_month():
    """The prefix used to be mode + year + month.

    `demo_20260928_150854_862ed8`.replace("_", "")[:10] == "demo202609", so every
    rebalance that month minted byte-identical client order ids for the same
    (instrument, index).  Two runs can no longer be told apart when reconciling,
    and `order_by_clordid` answers about the wrong one.
    """
    # Imported here, not at module scope: the helper does not exist before the
    # fix, and a module-level import would turn every test in this file into a
    # collection error instead of failing this one.
    from crypto_ls_research.execution.engine import _clordid_prefix
    a, b = "demo_20260928_150854_862ed8", "demo_20260928_150902_1f4c77"
    assert _clordid_prefix(a) != _clordid_prefix(b)
    assert _clordid_prefix(a) == _clordid_prefix(a)        # deterministic
    assert _clordid_prefix("demo_20261001_000000_862ed8") != _clordid_prefix(a)
    assert make_clordid(_clordid_prefix(a), "ARB-USDT-SWAP", 0) != \
           make_clordid(_clordid_prefix(b), "ARB-USDT-SWAP", 0)
    cid = make_clordid(_clordid_prefix(a), "USELESS-USDT-SWAP", 18)
    assert len(cid) <= 32 and cid.isalnum()               # still legal on OKX


# ---------------------------------------------------------------------------
# the venue's instrument universe is not the spec cache's
# ---------------------------------------------------------------------------
def _venue_limits():
    return LiveLimits(max_gross_notional=1e9, max_gross_frac=99.0,
                      max_turnover_frac=99.0, min_nav_usd=0.0,
                      require_rebalance_due=False)


def test_check_plan_warns_about_legs_this_venue_does_not_list():
    """7 of the 8 rejected orders in one demo run were instruments demo lacks.

    The contract-spec cache is built from public *live* data, so the planner
    sizes legs the demo exchange has never heard of.  Asking once up front turns
    a batch of unexplained rejections into a plan-time warning.
    """
    p = build_plan({"A-USDT-SWAP": 0.10, "B-USDT-SWAP": -0.10}, 1000.0,
                   current_sz={}, prices={"A-USDT-SWAP": 10.0, "B-USDT-SWAP": 10.0},
                   specs=_specs("A-USDT-SWAP", "B-USDT-SWAP"))
    assert p.n_orders == 2
    vs = check_plan(p, _venue_limits(), mode="demo", nav=1000.0,
                    venue_insts={"A-USDT-SWAP"})
    got = [v for v in vs if v.key == "venue_missing"]
    assert len(got) == 1
    assert got[0].sev == "warn", "模拟盘缺腿是可预期的，不该拦下整个调仓"
    assert "B-USDT-SWAP" in got[0].body and "A-USDT-SWAP" not in got[0].body
    assert not blocking(vs)


def test_check_plan_is_silent_when_the_venue_list_is_unknown():
    """No credentials, or the endpoint is down -> skip, never guess."""
    p = build_plan({"A-USDT-SWAP": 0.10}, 1000.0, current_sz={},
                   prices={"A-USDT-SWAP": 10.0}, specs=_specs("A-USDT-SWAP"))
    assert not [v for v in check_plan(p, _venue_limits(), mode="demo", nav=1000.0,
                                      venue_insts=None) if v.key == "venue_missing"]
    # An empty-but-known list is a different statement from "unknown".
    assert [v for v in check_plan(p, _venue_limits(), mode="demo", nav=1000.0,
                                  venue_insts=set()) if v.key == "venue_missing"]


def test_venue_instruments_is_best_effort_and_cached(tmp_live, monkeypatch):
    """A failing endpoint must never block a rebalance."""
    eng = LiveEngine(mode="demo", limits=_venue_limits())
    calls = []

    class Boom:
        def request(self, *a, **kw):
            calls.append(a)
            raise RuntimeError("network down")

    monkeypatch.setattr(eng, "_private", lambda: Boom())
    assert eng.venue_instruments() is None
    assert eng.venue_instruments() is None
    assert len(calls) == 1, "失败也要缓存，别把公共接口打爆"

    class Fine:
        def request(self, *a, **kw):
            calls.append(a)
            return [{"instId": "A-USDT-SWAP", "state": "live"},
                    {"instId": "DEAD-USDT-SWAP", "state": "suspend"}]

    monkeypatch.setattr(eng, "_private", lambda: Fine())
    eng._venue = (0.0, None)
    assert eng.venue_instruments() == {"A-USDT-SWAP"}, "非 live 状态的合约不算可交易"


# ---------------------------------------------------------------------------
# realised exposure: the book that happened, not the book that was wanted
# ---------------------------------------------------------------------------
def test_realised_gross_counts_only_the_legs_that_went_out(tmp_live, monkeypatch):
    """A rejected leg leaves its old position standing; it cannot be booked at target.

    One demo rebalance recorded 10,842 USDT of "realised" gross from 19 orders
    of which 11 filled -- the intended book, presented as the actual one -- and
    that figure then flowed into `add_equity(...)` and the desk's exposure cell.
    """
    eng, target, plan = _budgeted_plan(monkeypatch)
    acct = {"nav": 1000.0, "cur_sz": {}, "positions": []}
    assert plan.realised_gross > 0

    all_ok = [{"instId": o.inst_id, "ok": True} for o in plan.orders]
    g_all, n_all = LiveEngine._realised_book(plan, all_ok, acct)
    assert g_all == pytest.approx(plan.realised_gross)
    assert n_all == pytest.approx(plan.realised_net)

    # One leg from flat is rejected -> that leg stays flat, so it is not booked.
    half = [{"instId": o.inst_id, "ok": (i == 0)} for i, o in enumerate(plan.orders)]
    g_half, _ = LiveEngine._realised_book(plan, half, acct)
    assert 0.0 < g_half < g_all

    # Nothing went out -> nothing changed: the pre-trade book, which was flat.
    none_ok = [{"instId": o.inst_id, "ok": False} for o in plan.orders]
    assert LiveEngine._realised_book(plan, none_ok, acct) == (0.0, 0.0)


def test_a_rejected_close_keeps_the_position_it_failed_to_shut(tmp_live, monkeypatch):
    """The other direction: a *close* that was rejected still holds risk."""
    px = {"A-USDT-SWAP": 10.0}
    eng = LiveEngine(mode="paper", limits=_venue_limits())
    monkeypatch.setattr(eng, "specs", lambda: {i: _spec() for i in px})
    monkeypatch.setattr(eng, "prices", lambda insts=None, force=False: dict(px))
    # start long 1000 contracts (100 USDT), target flat
    acct_in = {"mode": "paper", "nav": 1000.0, "cur_sz": {"A-USDT-SWAP": 1000.0},
               "marks": dict(px), "pos_mode": "net_mode",
               "positions": [{"instId": "A-USDT-SWAP", "pos": 1000.0,
                              "notional": 100.0}]}
    plan, acct, _ = eng.build(_minimal_target({}), acct=acct_in, use_budget=False)
    assert plan.n_orders == 1 and plan.orders[0].tgt_sz == 0.0
    rejected = [{"instId": "A-USDT-SWAP", "ok": False}]
    assert LiveEngine._realised_book(plan, rejected, acct)[0] == pytest.approx(100.0)
    accepted = [{"instId": "A-USDT-SWAP", "ok": True}]
    assert LiveEngine._realised_book(plan, accepted, acct)[0] == pytest.approx(0.0)
