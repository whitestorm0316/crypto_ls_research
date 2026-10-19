"""后端内置调度器：**「开关关着就绝不下单」+「同一天只跑一次」必须可证。**

背景
----
这个调度器活在控制台进程里（控制台双重 fork + setsid，父进程 launchd，能长期
活着），替代了原来那个独立的守护进程 —— 后者从 Web 按钮派生时会被调用方的
进程树回收，是 README §一·补 那一整节在讲的老毛病。

四条必须成立的规矩，每条对应一种具体的伤害：

1. **关着 ⇒ 零动作。** 线程仍在跑（便宜），但执行前先查开关；关着就什么都不做。
   断言的是 **runner 一次都没被调用** —— 只断言"结果里写了 auto_disabled"
   不够：一个"先下单再把结论改成跳过"的实现同样会给出那个字段。
2. **同一天只执行一次。** 调度器每秒醒一次；不记账的话，11:00 之后的每一秒
   都会下一单。这是本模块最贵的失败模式。
3. **失败不写幂等账。** 一次网络抖动不该等于当天不交易（要能重试），
   但**成功提交之后**必须立刻记账（重复提交 = 双倍下单）。
4. **重启自恢复。** 改代码重启控制台之后，开关仍然生效。否则"重启一次就
   再也不自动了"，而页面上开关还显示「已开启」—— 最坏的一种。
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webapp import auto_schedule                                  # noqa: E402


def _mk(tmp_path, *, enabled=True, at=None, runner=None):
    """一个指向临时目录的调度器。

    `at` 默认取**一分钟前** —— 落在窗口内（`at <= now <= at+10min`），
    所以"到点"为真，而不必依赖某个特定墙钟时刻。用固定的 "00:00" 会在
    加上"错过不补"的窗口判定之后**落在窗口外**（实测被 5 条用例抓到）。
    """
    state = tmp_path / "auto_schedule.json"
    calls = []

    def default_runner(mode):
        calls.append(mode)
        return {"ok": True}

    s = auto_schedule.Scheduler(
        "live", runner or default_runner,
        state_path=lambda: str(state),
        is_enabled=lambda: enabled,
        at=at or time.strftime("%H:%M", time.localtime(time.time() - 60)),
        tick=0.01,
        log=lambda m: None)
    return s, calls, state


# ---------------------------------------------------------------------------
# 1. 开关关着 ⇒ runner 一次都不被调用
# ---------------------------------------------------------------------------
def test_disabled_never_calls_the_runner(tmp_path):
    """**断言的是 runner 没被调用**，不是"结论里说跳过了"。

    只查结论字段的写法会放过一个"先执行再改写结论"的实现 —— 而那个实现
    在真实账户上就是把单下出去了。
    """
    s, calls, _ = _mk(tmp_path, enabled=False)
    s._maybe_run()
    s._maybe_run()
    assert calls == [], f"开关关着却调用了 runner：{calls}"
    assert s.last.get("skipped") == "auto_disabled"


def test_enabled_does_call_the_runner(tmp_path):
    """反向断言：开着就该执行。没有这一条，上面那条在"永远不执行"时也是绿的。"""
    s, calls, _ = _mk(tmp_path, enabled=True)
    s._maybe_run()
    assert calls == ["live"], "开关开着却没有执行 —— 开关没被读到"


# ---------------------------------------------------------------------------
# 2. 同一天只执行一次（本模块最贵的失败模式）
# ---------------------------------------------------------------------------
def test_runs_at_most_once_per_day(tmp_path):
    """每秒醒一次，若不记账就是每秒下一单。"""
    s, calls, state = _mk(tmp_path, enabled=True)
    for _ in range(5):
        s._maybe_run()
    assert len(calls) == 1, f"同一天执行了 {len(calls)} 次 —— 会重复下单"
    assert json.loads(state.read_text())["last_run_day"] == auto_schedule._today()


def test_idempotence_account_is_per_day_not_per_process(tmp_path):
    """跨进程也要幂等：控制台重启后，同一天不该再下一单。"""
    s1, calls1, state = _mk(tmp_path, enabled=True)
    s1._maybe_run()
    assert len(calls1) == 1
    # 新建一个调度器（等价于重启），指向同一个账本
    s2, calls2, _ = _mk(tmp_path, enabled=True)
    s2._maybe_run()
    assert calls2 == [], "重启后又跑了一次 —— 幂等账没有跨进程生效"


# ---------------------------------------------------------------------------
# 3. 没到点不执行；失败要能重试
# ---------------------------------------------------------------------------
def test_does_not_run_before_the_time(tmp_path):
    """`at` 设在未来 ⇒ 不该执行。"""
    later = time.strftime("%H:%M", time.localtime(time.time() + 3600))
    s, calls, _ = _mk(tmp_path, enabled=True, at=later)
    for _ in range(3):
        s._maybe_run()
    assert calls == [], f"还没到点就执行了（at={later}）"


def test_missed_window_is_not_backfilled(tmp_path):
    """⚠️ **错过就等下一个窗口，不在下午替早上补作业。**

    这条是实测逼出来的：第一版只判 `now >= at`，于是把控制台在 **15:25**
    重启起来的那一刻它立刻跑了一次（`runs.jsonl` 里那条 `15:25:31 n=0`）。
    那一次恰好没下单（目标已与持仓一致），但机制是错的 —— 任何下午的重启都会
    拿当天已经过期的信号下单。断言用一个**明确在过去**的 `at`。
    """
    past = time.strftime("%H:%M", time.localtime(time.time() - 4 * 3600))
    s, calls, _ = _mk(tmp_path, enabled=True, at=past)
    for _ in range(3):
        s._maybe_run()
    assert calls == [], (
        f"at={past} 早已过去 4 小时，却补跑了一次 —— 用的是过期信号")


def test_inside_the_window_it_runs(tmp_path):
    """反向断言：**窗口内**必须执行。

    没有这一条，上面那条在"永远不执行"时也是绿的 —— 那就从"补作业"变成
    "根本不干活"，而后者更糟（用户以为开好了）。
    """
    just_now = time.strftime("%H:%M", time.localtime(time.time() - 60))
    s, calls, _ = _mk(tmp_path, enabled=True, at=just_now)
    s._maybe_run()
    assert calls == ["live"], "窗口内（1 分钟前到点）却没有执行"


def test_an_unparseable_time_never_fires(tmp_path):
    """`at` 解析不了 ⇒ 不执行（fail closed），而不是当成 00:00 全天候。"""
    s, calls, _ = _mk(tmp_path, enabled=True, at="not-a-time")
    s._maybe_run()
    assert calls == [], "at 解析失败却执行了"


def test_a_failure_does_not_burn_the_day(tmp_path):
    """执行抛错时**不写幂等账** —— 否则一次网络抖动 = 当天不交易。"""
    def boom(mode):
        raise RuntimeError("网络抖了一下")

    s, _, state = _mk(tmp_path, enabled=True, runner=boom)
    s._maybe_run()
    assert not state.exists(), "失败却写了幂等账 —— 当天不会再有第二次机会"
    assert s.last.get("ok") is False
    # 恢复后同一窗口内应该还能重试成功
    s._runner = lambda mode: {"ok": True}
    s._maybe_run()
    assert json.loads(state.read_text())["last_run_day"] == auto_schedule._today()


def test_success_writes_the_account_immediately(tmp_path):
    """成功提交后**立刻**记账 —— 记账晚一步就等于允许双倍下单。"""
    s, calls, state = _mk(tmp_path, enabled=True)
    s._maybe_run()
    assert state.exists(), "成功执行却没有落盘幂等账"
    d = json.loads(state.read_text())
    assert d["last_run_day"] == auto_schedule._today()
    assert "last_ts" in d


# ---------------------------------------------------------------------------
# 4. 线程生命期 + 自恢复
# ---------------------------------------------------------------------------
def test_thread_starts_and_stops(tmp_path):
    s, calls, _ = _mk(tmp_path, enabled=False, at="23:59:59")
    try:
        s.start()
        for _ in range(100):
            if s.is_alive():
                break
            time.sleep(0.01)
        assert s.is_alive(), "线程没起来"
        # start 幂等：重复调用不该起第二个线程
        s.start()
        assert s.is_alive()
    finally:
        s.stop()
    assert not s.is_alive(), "stop() 之后线程还活着"


def test_registry_recovers_enabled_accounts(tmp_path, monkeypatch):
    """启动时自恢复：开关打开的账户要自动把调度器建起来。

    没有这一步，"改代码重启一次"就等于"以后再也不自动了"，而页面上开关
    仍然显示「已开启」—— 用户没有任何线索。
    """
    import crypto_ls_research.execution.store as store_mod
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))

    from webapp import live_api
    monkeypatch.setattr(live_api, "_REGISTRY", auto_schedule.Registry())

    store_mod.Store("live").save_auto_config({"enabled": True})
    store_mod.Store("demo").save_auto_config({"enabled": False})

    started = live_api.start_enabled_schedulers()
    assert "live" in started, f"开启的账户没被恢复：{started}"
    assert "demo" not in started, "关闭的账户不该被恢复"

    got = live_api._REGISTRY.get("live")
    assert got is not None and got.is_alive()
    got.stop()
    # demo 压根不该被建
    assert live_api._REGISTRY.get("demo") is None


def test_scheduler_calls_the_same_path_as_the_button(tmp_path, monkeypatch):
    """调度器必须走**和网页按钮同一条提交路径**，不能自己调 engine.execute()。

    理由不是整洁：走同一个作业队列才能让网页与定时器互相串行化。各自直接
    execute 会变成"两个 actor 各读一遍同一本书、各发全量单"—— 而
    `rebalance_lock` 只串行化单次调仓，两个循环会交替插入而不是碰撞。
    """
    import crypto_ls_research.execution.store as store_mod
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))

    from webapp import live_api
    submitted = []
    monkeypatch.setattr(live_api, "_submit",
                        lambda kind, mode, runner, label="", net_of=None:
                        (submitted.append((kind, mode)), {"id": "j1"})[1])
    monkeypatch.setattr(live_api, "_wait_job", lambda jid, timeout=0: {"status": "done"})

    out = live_api.auto_runner("demo")
    kinds = [k for k, _ in submitted]
    assert kinds == ["refresh", "auto"], (
        f"调度器没有走两次提交（刷新 → 下单）：{kinds}")
    assert out == {"id": "j1"}


def test_a_scope_systemexit_does_not_kill_the_scheduler(tmp_path):
    """🔴 `resolve_insts()` 的 fail-loud 是 `SystemExit`，`except Exception` 接不住。

    于是控制台的调度线程会**死掉**，而 `threading` 把线程里的 `SystemExit`
    静默吞掉 ⇒ 界面上只有「调度器不在运行」，没有任何原因。
    同时幂等账也不能写 —— 重建分类快照后，同一个窗口还应该能重试成功。

    触发场景：全量下载把新上市合约拉进了缓存，而 `inst_category.json` 没重建。
    """
    def boom(mode):
        raise SystemExit(
            "asset-class scope 'crypto' cannot classify 1 cached "
            "instrument(s): ['CT-USDT-SWAP']")

    s, _, state = _mk(tmp_path, enabled=True, runner=boom)
    s._maybe_run()
    assert s.last.get("ok") is False, "SystemExit 没被这一轮接住 —— 线程会死"
    assert "宇宙分类失败" in (s.last.get("error") or ""), (
        f"错误信息没指认根因：{s.last.get('error')!r}")
    assert not state.exists(), "失败却写了幂等账 —— 当天不会再有第二次机会"
    # 恢复后同一窗口内仍可成功：说明"死"的只是这一轮，不是这个调度器
    s._runner = lambda mode: {"ok": True}
    s._maybe_run()
    assert json.loads(state.read_text())["last_run_day"] == auto_schedule._today()
