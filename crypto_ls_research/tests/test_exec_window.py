"""执行窗口（`--exec-window`）的测试。

这个功能唯一的风险是「静默偏离」：窗口一旦生效，下单时刻就不再是回测网格
上的那个点，而策略绩效也不再是 v3 口径。所以这里守的不是"窗口能不能用"，
而是三条**不变量**：

1. **默认不开。** 不传 `--exec-window` 时，`not_due` 必须照旧拦截。
   整个项目的历史结论都建立在"锚点 UTC02:00/北京10:00"上，一次误开就会让
   那些结论与实盘脱钩，而日志里看不出区别。
2. **开了只能在窗口内生效，且必须留下痕迹。** `forced_by` /
   `signal_age_hours` 必须写进决策记录——否则"周三为什么调仓了"这个问题
   只能在事后对着 P&L 猜。
3. **限频。** 至多每个 `rebalance_days` 强制一次。窗口宽度设错（比如手滑
   填了 60 分钟）而 tick 是 30 分钟时，不能一晚上把 20%/日的换手预算烧光。
"""
from __future__ import annotations

import re
import time

import pytest

from crypto_ls_research.execution.auto_trader import (
    AutoTrader, in_exec_window, parse_hhmm)


# -- parse_hhmm ------------------------------------------------------------
def test_parse_hhmm_basic():
    assert parse_hhmm("00:00") == 0
    assert parse_hhmm("23:30") == 23 * 60 + 30 == 1410
    assert parse_hhmm("09:05") == 9 * 60 + 5


@pytest.mark.parametrize("bad", ["", None, "2330", "23:30:00", "24:00",
                                 "23:60", "-1:00", "ab:cd", "23:3a"])
def test_parse_hhmm_rejects_garbage(bad):
    """必须抛错，不能兜底成 00:00。

    A typo that silently became midnight would move every future order by
    nine hours and look exactly like a strategy change.
    """
    with pytest.raises(ValueError):
        parse_hhmm(bad)


# -- in_exec_window --------------------------------------------------------
def test_in_exec_window_disabled_by_default():
    assert in_exec_window(None, now=time.time()) is False


def test_in_exec_window_uses_local_wall_clock():
    """23:30 窗口应当在本地 23:30 打开——这是操作员说的那个时刻。"""
    import datetime as _dt
    base = _dt.datetime(2026, 9, 28, 23, 30, 0).timestamp()
    assert in_exec_window(23 * 60 + 30, now=base, tol_min=5) is True
    assert in_exec_window(23 * 60 + 30, now=base + 4 * 60, tol_min=5) is True
    assert in_exec_window(23 * 60 + 30, now=base + 6 * 60, tol_min=5) is False
    assert in_exec_window(23 * 60 + 30, now=base - 60, tol_min=5) is False


def test_in_exec_window_handles_midnight_wrap():
    """23:58 + 5 分钟会跨天；跨天窗口必须仍然判对。"""
    import datetime as _dt
    lo = 23 * 60 + 58
    assert in_exec_window(lo, now=_dt.datetime(2026, 9, 28, 23, 59).timestamp(),
                          tol_min=5) is True
    assert in_exec_window(lo, now=_dt.datetime(2026, 9, 29, 0, 1).timestamp(),
                          tol_min=5) is True
    assert in_exec_window(lo, now=_dt.datetime(2026, 9, 29, 0, 4).timestamp(),
                          tol_min=5) is False


# -- AutoTrader wiring -----------------------------------------------------
def _trader(**kw):
    """构造一个不碰网络、不碰磁盘的 AutoTrader。

    `engine` / `store` / `notifier` 都被替换成哑对象：这里要测的是窗口判据
    本身，不是下单链路（那条链路由 test_auto_trader.py 覆盖）。
    """
    class _Limits:
        require_rebalance_due = True

    class _Engine:
        limits = _Limits()

    t = AutoTrader.__new__(AutoTrader)
    t.mode = "demo"
    t.bar = "1h"
    t.rebalance_days = 1.0
    t.interval_min = 30.0
    t.dry_run = True
    t.exec_window_min = None
    t.exec_window_tol_min = 5.0
    t.exec_window_utc = False
    t._last_forced_ts = 0.0
    t._forced_count = 0
    t._log_fn = None
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def test_window_disabled_never_opens():
    t = _trader()
    assert t._exec_window_open() is False


def test_window_label_states_the_timezone():
    """标签必须带**ASCII** 时区偏移。

    裸 "23:30" 在机器时区与操作员不同时是歧义的；而 `time.strftime("%Z")`
    在中文 Windows 上返回 `中国标准时间`，会经 argv / JSON / 日志三处传播，
    能把 GBK 控制台打成 UnicodeEncodeError。所以必须是 UTC±HH:MM。
    """
    t = _trader(exec_window_min=1410)
    lab = t._window_label()
    assert lab is not None
    assert lab.startswith("23:30")
    assert "UTC" in lab
    lab.encode("ascii")          # 中文时区名会在这里炸
    assert re.search(r"UTC[+-]\d{2}:\d{2}", lab)


def test_window_rate_limited_to_one_force_per_rebalance_period(monkeypatch):
    """限频：24h 内第二次强制必须被拒。

    没有这条，窗口宽度填错 + 30 分钟 tick 会一夜之间把整个换手预算烧光。
    """
    t = _trader(exec_window_min=1410, rebalance_days=1.0)
    # 强制"当前在窗口内"
    monkeypatch.setattr("crypto_ls_research.execution.auto_trader."
                        "in_exec_window", lambda *a, **k: True)
    assert t._exec_window_open() is True
    t._last_forced_ts = time.time()          # 记下刚才那次
    assert t._exec_window_open() is False, "同一周期内不应再次强制"

    # 满一个周期后允许
    t._last_forced_ts = time.time() - 25 * 3600
    assert t._exec_window_open() is True


def test_window_utc_mode_interprets_hhmm_as_utc(monkeypatch):
    """--exec-window-utc 走的是 UTC 钟，不是本地钟。"""
    t = _trader(exec_window_min=15 * 60 + 30, exec_window_utc=True)
    import datetime as _dt
    good = _dt.datetime(2026, 9, 28, 15, 31, tzinfo=_dt.timezone.utc).timestamp()
    bad = _dt.datetime(2026, 9, 28, 3, 0, tzinfo=_dt.timezone.utc).timestamp()
    monkeypatch.setattr(time, "time", lambda: good)
    assert t._exec_window_open() is True
    monkeypatch.setattr(time, "time", lambda: bad)
    assert t._exec_window_open() is False


# -- the CLI plumbing must actually reach the daemon ----------------------
def test_spawn_forwards_the_exec_window():
    """`auto_ctl._spawn` 必须把窗口写进 argv，否则窗口只存在于控制文件里。"""
    from crypto_ls_research.execution import auto_ctl
    captured = {}

    class _P:
        pid = 4242

    def fake_popen(argv, kwargs, log_path):
        captured["argv"] = argv
        return _P()

    import crypto_ls_research.execution.auto_ctl as m
    orig = m._popen
    m._popen = fake_popen
    try:
        m._spawn("demo", 1.0, 30.0, "1h", True, False,
                 exec_window="23:30", exec_window_tol=7.0, exec_window_utc=False)
    finally:
        m._popen = orig
    argv = captured["argv"]
    assert "--exec-window" in argv
    assert argv[argv.index("--exec-window") + 1] == "23:30"
    assert argv[argv.index("--exec-window-tol") + 1] == "7.0"


def test_spawn_omits_the_window_when_not_requested():
    """不传窗口时 argv 里不能出现这个开关。

    默认必须是「与回测口径一致」；多一个默认生效的开关就会让所有既有结论
    与实盘脱钩。
    """
    captured = {}

    class _P:
        pid = 4242

    import crypto_ls_research.execution.auto_ctl as m

    def fake_popen(argv, kwargs, log_path):
        captured["argv"] = argv
        return _P()

    orig = m._popen
    m._popen = fake_popen
    try:
        m._spawn("demo", 1.0, 30.0, "1h", True, False)
    finally:
        m._popen = orig
    assert "--exec-window" not in captured["argv"]


# -- the launcher's *default* must be the backtest-consistent one ----------
def test_launcher_defaults_to_no_exec_window():
    """`scripts/auto_demo.py` 的默认必须是"无窗口"。

    这是"信号产生后立即下单"的唯一实现方式：调仓网格在北京 10:00 产生信号，
    回测执行价取下一根 bar 开盘＝北京 11:00，守护进程的 `due` 闸门也恰好在
    那时打开 —— 三者天然对齐，**不需要窗口**。

    反过来，窗口一旦默认打开（尤其是设成 10:00），会**强行覆盖** `due`：
    10:00 时最新决策点就是刚产生的那根 bar，窗口每天都会照发一遍同样的目标书，
    把换手预算烧在无谓的重复交易上，而日志里看起来完全正常。
    """
    import importlib.util
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.join(root, "scripts", "auto_demo.py")
    spec = importlib.util.spec_from_file_location("_auto_demo_probe", path)
    mod = importlib.util.module_from_spec(spec)
    # 不能让它执行 main()，模块级只有常量与环境读取
    spec.loader.exec_module(mod)

    # 环境里没设 EXEC_WINDOW 时，默认必须是空 -> 等价于"不强制"
    assert mod.EXEC_WINDOW == "", (
        f"启动器默认执行窗口应为空（跟随回测口径），实际 {mod.EXEC_WINDOW!r}。"
        "若非空，10:00 会每天重复下单同一目标书。")
    assert (mod.EXEC_WINDOW or None) is None
