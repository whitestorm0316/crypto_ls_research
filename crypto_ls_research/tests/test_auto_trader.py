"""Tests for the unattended rebalancer.

The whole point of this module is the guard the *server does not hold*: an
off-schedule rebalance is a `warn` in `check_plan`, not a `block`, so
`execute(force=False)` would send one.  If the scheduler trusted the engine to
refuse, it would rebalance every tick instead of every `rebalance_days`.

So the load-bearing assertions here are:

* not-due -> **`execute` is never called at all** (not "called and blocked");
* kill switch -> nothing else is even consulted;
* stale data -> skip, and a failed refresh does not become a trade;
* `force` is **never** passed, on any path;
* the caps come from the saved file, not from the factory defaults.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from crypto_ls_research.execution import auto_trader as at_mod
from crypto_ls_research.execution import store as store_mod
from crypto_ls_research.execution.auto_trader import AutoTrader, bar_hours
from crypto_ls_research.execution.engine import LiveEngine
from crypto_ls_research.execution.limits import LiveLimits


@pytest.fixture
def tmp_live(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


class _Target:
    def __init__(self, due: bool):
        self.weights = {}
        self.book = []
        self.pool = []
        self.decision_ts = "2026-09-28T07:00:00+00:00"
        self.next_decision_ts = "2026-10-01T07:00:00+00:00"
        self.panel_last_ts = "2026-09-28T07:00:00+00:00"
        self.bars_since_decision = 1
        self.rebalance_bars = 72
        self.rebalance_due = due
        self.gross = 1.03
        self.from_cache = True


class FakeEngine:
    """Only the members `AutoTrader` actually touches."""

    def __init__(self, *, due=True, stale=False, bar_age=0.5, stage="executed",
                 limits=None, raise_on=None, n_ok=19):
        self.limits = limits if limits is not None else LiveLimits()
        self.due, self.stale, self.bar_age = due, stale, bar_age
        self.stage, self.raise_on, self.n_ok = stage, raise_on, n_ok
        self.calls: list = []

    def data_staleness(self):
        self.calls.append("staleness")
        return {"bar": "1h", "stale": self.stale, "age_hours": 0.1,
                "bar_age_hours": self.bar_age,
                "newest_bar": "2026-09-28T07:00:00+00:00"}

    def target(self, force_signal=False):
        self.calls.append("target")
        if self.raise_on == "target":
            raise RuntimeError("signal blew up")
        return _Target(self.due)

    def execute(self, **kw):
        self.calls.append(("execute", kw))
        if self.raise_on == "execute":
            raise RuntimeError("network died mid-send")
        return {
            "stage": self.stage,
            "record": {"run_id": "demo_x", "n_orders": 19, "n_ok": self.n_ok,
                       "n_fail": 19 - self.n_ok, "turnover_frac": 0.1986,
                       "gross_target": 1.03, "gross_realised": 1.02},
            "violations": ([{"sev": "block", "key": "max_gross_notional"}]
                           if self.stage == "blocked" else
                           [{"sev": "warn", "key": "not_due"}]),
        }


def _bot(tmp_live, monkeypatch, eng=None, **kw):
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data",
                        staticmethod(lambda **k: {
                            "exit_code": 0, "tail": "",
                            "staleness_before": {"bar_age_hours": 9.0},
                            "staleness_after": {"bar_age_hours": 0.2}}))
    return AutoTrader(engine=eng if eng is not None else FakeEngine(), **kw)


# ---------------------------------------------------------------------------
# the guard the server does not hold
# ---------------------------------------------------------------------------
def test_not_due_never_reaches_execute(tmp_live, monkeypatch):
    """The load-bearing assertion.  `check_plan` only *warns* about an
    off-schedule rebalance, so if the scheduler called `execute` and relied on
    the engine to refuse, this would send 19 orders every hour."""
    eng = FakeEngine(due=False)
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "skip" and out["reason"] == "not_due"
    assert "target" in eng.calls
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in eng.calls), \
        "未到调仓日却调用了 execute —— 这正是服务端不拦的那条路"
    assert out["next_decision_ts"] and out["rebalance_due"] is False


def test_due_executes_once_and_never_passes_force(tmp_live, monkeypatch):
    """`force` only silences the off-schedule warning; on a due day there is
    nothing to silence, and passing it would mean the cap set had been relaxed."""
    eng = FakeEngine(due=True)
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    calls = [c for c in eng.calls if isinstance(c, tuple)]
    assert len(calls) == 1, f"execute 应恰好调用一次，实际 {len(calls)}"
    kw = calls[0][1]
    assert kw["force"] is False
    assert kw["dry_run"] is False
    assert kw["confirm"] is None          # paper/demo 不需要确认短语
    assert out["action"] == "traded" and out["reason"] == "executed"
    assert out["n_ok"] == 19 and out["run_id"] == "demo_x"


def test_not_due_is_respected_even_when_the_gate_is_off(tmp_live, monkeypatch):
    """`require_rebalance_due=False` means "the user wants every tick to act" --
    the scheduler must not silently keep its own opinion."""
    eng = FakeEngine(due=False, limits=LiveLimits(require_rebalance_due=False))
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "traded"


# ---------------------------------------------------------------------------
# the other guards
# ---------------------------------------------------------------------------
def test_kill_switch_stops_before_anything_else(tmp_live, monkeypatch):
    """The emergency brake must not be merely advisory: not even the signal is
    computed, because computing it is the expensive part and a raised kill
    switch means "do nothing", full stop."""
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: True)
    eng = FakeEngine(due=True)
    # Built directly, not through `_bot`: that helper patches the kill switch
    # back to False, which is exactly the thing under test here.
    out = AutoTrader(engine=eng, interval_min=60).once()
    assert out["action"] == "skip" and out["reason"] == "kill_switch"
    assert eng.calls == [], f"熔断后不该碰任何东西，实际 {eng.calls}"


def test_stale_data_skips_even_when_due(tmp_live, monkeypatch):
    """A signal computed off a stale cache is a *wrong* signal, not a late one."""
    eng = FakeEngine(due=True, stale=True, bar_age=40.0)
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "skip" and out["reason"] == "data_stale"
    assert not any(isinstance(c, tuple) for c in eng.calls)


def test_a_refresh_is_attempted_when_the_bar_is_old(tmp_live, monkeypatch):
    """The refresh must actually be attempted, and the decision re-made on the
    post-refresh staleness -- not on the pre-refresh one."""
    seen = []

    def fake_refresh(**k):
        seen.append(k)
        return {"exit_code": 0, "tail": "",
                "staleness_before": {"bar_age_hours": 9.0},
                "staleness_after": {"bar_age_hours": 0.2}}

    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data",
                        staticmethod(fake_refresh))
    eng = FakeEngine(due=True, stale=False, bar_age=9.0)
    out = AutoTrader(engine=eng, interval_min=60).once()
    assert seen and seen[0]["bar"] == "1h"
    assert "refresh" in out and out["refresh"]["exit_code"] == 0
    assert out["action"] == "traded"


def test_a_failed_refresh_does_not_turn_into_a_trade_on_stale_data(tmp_live, monkeypatch):
    """Refresh explodes -> we fall back to the staleness verdict, and a stale
    verdict still blocks.  A refresh failure must never be read as "fine"."""
    def boom(**k):
        raise OSError("no route to host")

    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data", staticmethod(boom))
    eng = FakeEngine(due=True, stale=True, bar_age=40.0)
    out = AutoTrader(engine=eng, interval_min=60).once()
    assert out["refresh"]["error"].startswith("OSError")
    assert out["action"] == "skip" and out["reason"] == "data_stale"


def test_refresh_is_skipped_when_the_bar_is_fresh(tmp_live, monkeypatch):
    """`--refresh-after-hours 0` disables refreshing entirely; and a fresh bar
    must not pay for a refresh on every tick."""
    called = []
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data",
                        staticmethod(lambda **k: called.append(1)))
    eng = FakeEngine(due=True, bar_age=0.5)
    out = AutoTrader(engine=eng, interval_min=60).once()
    assert called == [] and "refresh" not in out

    out2 = AutoTrader(engine=FakeEngine(due=True, bar_age=99.0),
                      interval_min=60, refresh_after_hours=0).once()
    assert called == [] and "refresh" not in out2


def test_unknown_bar_age_triggers_a_refresh_rather_than_assuming_fresh(tmp_live, monkeypatch):
    """`bar_age_hours is None` means the reference bar could not be read.
    "unknown" is not "fine"."""
    called = []
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data",
                        staticmethod(lambda **k: called.append(1) or {
                            "exit_code": 0, "tail": "",
                            "staleness_before": {"bar_age_hours": None},
                            "staleness_after": {"bar_age_hours": 0.2}}))
    AutoTrader(engine=FakeEngine(due=True, bar_age=None), interval_min=60).once()
    assert called == [1]


def test_a_cap_block_is_reported_as_blocked_not_as_a_quiet_skip(tmp_live, monkeypatch):
    """A `block` is the one outcome a human must look at, so it must be
    distinguishable from the routine skips in the audit trail."""
    eng = FakeEngine(due=True, stage="blocked")
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "blocked" and out["reason"] == "risk_gate"
    assert out["blocked_by"] == ["max_gross_notional"]


def test_confirm_failure_on_live_is_blocked(tmp_live, monkeypatch):
    eng = FakeEngine(due=True, stage="confirm_failed")
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "blocked" and out["reason"] == "confirm_failed"


def test_no_orders_is_a_noop_not_a_trade(tmp_live, monkeypatch):
    eng = FakeEngine(due=True, stage="no_orders")
    out = _bot(tmp_live, monkeypatch, eng, interval_min=60).once()
    assert out["action"] == "noop" and out["reason"] == "no_orders"


# ---------------------------------------------------------------------------
# the cross-process guard: the console and the bot are separate processes
# ---------------------------------------------------------------------------
def test_only_one_rebalance_at_a_time(tmp_live):
    """`_LOCK` is a `threading.Lock` -- process-local.  The console and the
    auto-trader are separate processes over the same directory; two concurrent
    executes each send the *full* order list, doubling every position."""
    with store_mod.rebalance_lock("demo"):
        with pytest.raises(store_mod.RebalanceBusy) as ei:
            with store_mod.rebalance_lock("demo"):
                pass                                  # pragma: no cover
        assert "正在调仓中" in str(ei.value)
    # released -> the next taker succeeds
    with store_mod.rebalance_lock("demo"):
        pass


def test_the_lock_is_per_mode(tmp_live):
    """A paper rebalance must not block a demo one -- different accounts."""
    with store_mod.rebalance_lock("paper"):
        with store_mod.rebalance_lock("demo"):
            pass


def test_an_abandoned_lock_is_taken_over(tmp_live):
    """A crashed holder must not wedge the desk forever.  Both signals count:
    a dead pid, and a stamp older than the TTL."""
    path = os.path.join(str(tmp_live), "demo", "rebalance.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": 999_999_999, "ts": time.time()}, f)     # dead pid
    with store_mod.rebalance_lock("demo"):
        pass

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time() - 10_000}, f)  # ancient
    with store_mod.rebalance_lock("demo"):
        pass

    with open(path, "w", encoding="utf-8") as f:
        f.write("{ half-written")                                  # debris
    with store_mod.rebalance_lock("demo"):
        pass


def test_a_live_pid_that_is_not_ours_is_still_respected(tmp_live):
    """The pid check must be about *aliveness*, not about equality with ours:
    the console is a different pid and its lock must hold.

    The pid used must be one that is **actually alive on this platform**.  This
    test used to hardcode `1`, on the reasoning that pid 1 is `init`/`launchd`
    and therefore always alive.  That is a POSIX fact, not a portable one:
    Windows has no pid 1 at all (`OpenProcess` -> `ERROR_INVALID_PARAMETER`),
    so on Windows the lock was correctly judged abandoned and the assertion
    failed for the right reason.  Picking a live pid at runtime keeps the test
    about the *aliveness* rule instead of about the number 1.
    """
    live_pid = _a_live_pid_that_is_not_ours()
    assert store_mod._pid_alive(live_pid), (
        f"前提不成立：pid {live_pid} 在本平台并非存活进程，"
        "这个测试就无法验证「别人的活锁必须被尊重」")

    path = os.path.join(str(tmp_live), "demo", "rebalance.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": live_pid, "ts": time.time()}, f)
    with pytest.raises(store_mod.RebalanceBusy):
        with store_mod.rebalance_lock("demo"):
            pass                                                # pragma: no cover


def _a_live_pid_that_is_not_ours() -> int:
    """A pid that is alive right now and is **not** `os.getpid()`.

    The parent process is the natural candidate, but a test runner may be the
    top of its own tree, so fall back to scanning for any live pid.  `4` is
    Windows' `System` process and `1` is POSIX `init`; both are checked with the
    same `_pid_alive` the lock uses, so the fallback cannot silently return a
    dead pid.
    """
    import subprocess
    candidates = []
    ppid = os.getppid()
    if ppid and ppid != os.getpid():
        candidates.append(ppid)
    candidates += [1, 4]                       # POSIX init / Windows System
    for pid in range(4, 400):                  # any live process will do
        candidates.append(pid)
    for pid in candidates:
        if pid != os.getpid() and store_mod._pid_alive(pid):
            return pid
    pytest.skip("本平台找不到一个存活的、非本进程的 pid，跳过")


def test_the_lock_is_released_when_the_body_raises(tmp_live):
    """An exception inside a rebalance must not leave the desk wedged."""
    with pytest.raises(ValueError):
        with store_mod.rebalance_lock("demo"):
            raise ValueError("boom")
    with store_mod.rebalance_lock("demo"):
        pass


def test_execute_refuses_fast_when_the_lock_is_held(tmp_live):
    """The engine itself is guarded, not just the bot -- so a human clicking
    「执行」 in the console while the bot is mid-rebalance is refused too."""
    eng = LiveEngine(mode="paper")
    with store_mod.rebalance_lock("paper"):
        with pytest.raises(store_mod.RebalanceBusy):
            # No stubbing: it must fail before computing a signal or touching
            # the network, otherwise this test would be slow and flaky.
            eng.execute(dry_run=True)


def test_flatten_takes_the_same_lock(tmp_live):
    """`flatten` racing `execute` is the worst version: the flatten closes a
    book the rebalance is simultaneously rebuilding."""
    eng = LiveEngine(mode="paper")
    with store_mod.rebalance_lock("paper"):
        with pytest.raises(store_mod.RebalanceBusy):
            eng.flatten(dry_run=True)


def test_a_busy_rebalance_is_a_skip_not_an_error(tmp_live, monkeypatch):
    """It is the other actor doing exactly this job, so it is not a failure --
    but it must be visible: "skipped for two days because a human left a tab
    executing" is a real failure mode, and only the audit trail would show it."""
    eng = FakeEngine(due=True)

    def busy(**kw):
        raise store_mod.RebalanceBusy("demo 模式正在调仓中")

    eng.execute = busy
    bot = _bot(tmp_live, monkeypatch, eng, interval_min=60)
    out = bot.once()
    assert out["action"] == "skip" and out["reason"] == "busy"
    assert "正在调仓中" in out["error"]
    st = json.loads(open(bot.state_path(), encoding="utf-8").read())
    assert st["skips"] == {"busy": 1} and st["errors"] == 0


# ---------------------------------------------------------------------------
# settings, heartbeat, loop
# ---------------------------------------------------------------------------
def test_the_scheduler_reads_the_saved_caps_not_the_factory_defaults(tmp_live):
    """Same defect class as 「保存了重启不生效」: `LiveLimits.from_dict(None)`
    returns an all-default object, so passing it would silently override the
    caps the user saved.  `run_live.py` had exactly that bug."""
    store_mod.Store("paper").save_limits({"max_gross_notional": 1234.0})
    bot = AutoTrader(mode="paper", interval_min=60, engine=None)
    assert bot.engine.limits.max_gross_notional == 1234.0
    assert bot.engine.limits.max_gross_notional != LiveLimits().max_gross_notional


def test_live_refuses_to_start_without_the_explicit_flag(tmp_live):
    """An unattended process that places real-money orders must be switched on
    by a human once, on purpose."""
    with pytest.raises(PermissionError):
        AutoTrader(mode="live", interval_min=60)
    bot = AutoTrader(mode="live", interval_min=60, allow_live=True,
                     engine=FakeEngine(due=True))
    assert bot.allow_live is True


def test_live_supplies_the_confirm_phrase_from_the_same_function(tmp_live):
    """The phrase must not be hardcoded here -- if the server rotates it, the
    scheduler has to follow, or live silently starts failing every cycle."""
    from crypto_ls_research.execution.limits import live_confirm_phrase as server_phrase
    eng = FakeEngine(due=True)
    bot = AutoTrader(mode="live", interval_min=60, allow_live=True, engine=eng)
    bot.once()
    kw = [c[1] for c in eng.calls if isinstance(c, tuple)][0]
    assert kw["confirm"] == server_phrase()
    assert isinstance(kw["confirm"], str) and kw["confirm"]


def test_the_interval_has_a_floor(tmp_live):
    """`--interval 0.01` must not become a hot loop against the exchange."""
    bot = AutoTrader(mode="paper", interval_min=0.01, engine=FakeEngine())
    assert bot.interval_min == at_mod.MIN_INTERVAL_MIN
    assert bot.interval_sec == 60.0
    assert AutoTrader(mode="paper", interval_min=60,
                      engine=FakeEngine()).interval_sec == 3600.0


def test_bar_hours_parses_the_shapes_we_use():
    assert bar_hours("1h") == 1.0
    assert bar_hours("15m") == 0.25
    assert bar_hours("5m") == pytest.approx(5 / 60)
    assert bar_hours("1d") == 24.0
    assert bar_hours("") == 1.0
    assert bar_hours("garbage") == 1.0


def test_skips_are_written_to_both_the_heartbeat_and_the_audit_trail(tmp_live, monkeypatch):
    """「一直在跑」和「第一晚就死了」从外面看是一样的 —— 所以每次决策（包括跳过）
    都必须落盘，且心跳文件要能回答"它还活着吗、最后一次干了什么"。"""
    bot = _bot(tmp_live, monkeypatch, FakeEngine(due=False), interval_min=60)
    bot.once()
    bot.once()

    st = json.loads(open(bot.state_path(), encoding="utf-8").read())
    assert st["mode"] == bot.mode and st["interval_min"] == 60
    assert st["checks"] == 2 and st["trades"] == 0
    assert st["skips"] == {"not_due": 2}
    assert st["last"]["reason"] == "not_due"
    assert st["kill_switch"] is False
    assert st["pid"] and st["started_str"]

    lines = [json.loads(x) for x in
             open(bot.log_path(), encoding="utf-8").read().splitlines() if x.strip()]
    assert len(lines) == 2 and all(x["action"] == "skip" for x in lines)


def test_an_error_in_the_loop_is_recorded_and_the_loop_keeps_going(tmp_live, monkeypatch):
    """A crash on tick 1 must not end the automation silently.  The loop records
    it and waits for the next tick -- and the tick after that still runs."""
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod.LiveEngine, "refresh_market_data",
                        staticmethod(lambda **k: {"exit_code": 0, "tail": "",
                                                  "staleness_before": {},
                                                  "staleness_after": {}}))
    bot = AutoTrader(engine=FakeEngine(due=True),
                     interval_min=at_mod.MIN_INTERVAL_MIN)
    bot._SLEEP_SLICE = 0.01
    # The floor keeps a real daemon from hot-looping, but a test cannot wait a
    # real minute per tick -- so shorten the *interval*, not the guard.
    bot.interval_sec = 0.05

    # Fail once, then behave -- and stop from inside, so the test is deterministic
    # instead of racing a wall clock.
    state = {"n": 0}
    real_target = bot.engine.target

    def flaky(force_signal=False):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first tick dies")
        stop.set()
        return real_target(force_signal=force_signal)

    bot.engine.target = flaky
    stop = threading.Event()
    t = threading.Thread(target=bot.loop, args=(stop,), daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "循环没有在 stop 后退出"

    assert bot.stats["errors"] == 1
    assert bot.stats["checks"] >= 1, "出错之后必须继续跑，而不是静默停摆"
    st = json.loads(open(bot.state_path(), encoding="utf-8").read())
    assert st["errors"] == 1 and st["enabled"] is False   # 退出时把 enabled 落回 false
    lines = [json.loads(x) for x in
             open(bot.log_path(), encoding="utf-8").read().splitlines() if x.strip()]
    assert any(x["action"] == "error" for x in lines)
