"""实盘定时器：**「默认关闭」必须是可证的，不能只是写在注释里。**

背景
----
`scripts/install_scheduler.py` + `scripts/scheduled_tick.py` 是一对：安装器写一个
launchd/systemd 任务，每天到点调用 tick，tick 再幂等地把守护进程拉起来。

这两个脚本原本写死 `trading_mode="demo"`，而且 tick 里硬编码
`allow_live=False`。也就是说**实盘自动交易根本起不来** —— 这不是"忘了开"，
是代码里没有那条路。补上这条路的时候有三个陷阱，每个都能静默地毁掉一整户账：

1. **默认值反了。** `--allow-live` 如果默认 True（或由 `--trading-mode live`
   隐含），那么"实盘需要显式确认"这句话在**最需要它的地方**（无人值守的定时器）
   恰好不成立。所以断言的是*缺参数时拒绝*，而且**拒绝时不许写盘**。
2. **label 冲突。** 装 live 定时器如果把 demo 的 label 覆盖掉，`--status`
   仍然显示"已注册"，但被覆盖掉的那个任务从此不再运行 —— 一个静默的失守。
   所以每个交易模式必须有独立的 label。
3. **拒绝点太晚。** 如果 `--allow-live` 的检查写在 `stop()` 之后，一个健康的
   账本会被停在一半才发现缺参数。检查必须在任何破坏性步骤之前。

本文件把这三条钉成断言。断言用**性质**（是否含某参数 / 两个 label 是否不同 /
是否写了文件），不比对整段 XML —— XML 里的注释和排版会变，性质不会。
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
SCRIPTS = os.path.join(ROOT, "scripts")


def _load(name: str, filename: str):
    """Import a standalone script by path (they are not part of the package)."""
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sched():
    return _load("_sched_under_test", "install_scheduler.py")


@pytest.fixture(scope="module")
def tick_src() -> str:
    with open(os.path.join(SCRIPTS, "scheduled_tick.py"), encoding="utf-8") as f:
        return f.read()


def _prog_args(plist_xml: str) -> list:
    """The `<ProgramArguments>` strings, in order."""
    block = plist_xml.split("<key>ProgramArguments</key>", 1)[1]
    block = block.split("</array>", 1)[0]
    return re.findall(r"<string>(.*?)</string>", block)


# ---------------------------------------------------------------------------
# 1. 默认关闭：缺 --allow-live 必须拒绝，且拒绝发生在写盘之前
# ---------------------------------------------------------------------------
def test_installer_refuses_live_without_allow_live(sched, tmp_path, monkeypatch):
    """`--trading-mode live` 缺 `--allow-live` ⇒ 退出码 2，且**一个文件都不写**。

    只断言退出码是不够的：一个先写 plist 再返回 2 的实现同样会"通过"，
    而它已经把一个实盘任务留在盘上了。所以同时钉住"没写文件"。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    rc = sched.main(["--install", "--trading-mode", "live", "--at", "11:01"])
    assert rc == 2, f"实盘缺 --allow-live 应拒绝，实际退出码 {rc}"
    written = list((tmp_path / "Library" / "LaunchAgents").glob("*"))
    assert written == [], f"拒绝时不该写任何 plist，却写了：{written}"


def test_installer_accepts_live_with_allow_live(sched, tmp_path, monkeypatch):
    """反向断言：给了 `--allow-live` 就**不再**拒绝。

    没有这一条，上面那条在"函数永远返回 2"时也是绿的 —— 那就成了假守卫。
    这里只走到"参数检查通过"就够了，所以把真正落盘的那一步换掉。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    called = {}

    def fake_install(py, hour, minute, mode, tol, trading_mode, allow_live):
        called.update(hour=hour, minute=minute, trading_mode=trading_mode,
                      allow_live=allow_live)
        return 0

    monkeypatch.setattr(sched, "install_macos", fake_install, raising=True)
    monkeypatch.setattr(sched, "install_linux", fake_install, raising=True)
    monkeypatch.setattr(sched.sys, "platform", "darwin", raising=True)
    rc = sched.main(["--install", "--trading-mode", "live", "--allow-live",
                     "--at", "11:01"])
    assert rc == 0
    assert called.get("trading_mode") == "live"
    assert called.get("allow_live") is True
    assert (called.get("hour"), called.get("minute")) == (11, 1)


# ---------------------------------------------------------------------------
# 2. label 隔离：装 live 不许覆盖 demo
# ---------------------------------------------------------------------------
def test_every_trading_mode_has_its_own_label(sched):
    """两个模式共用一个 label ⇒ 后装的把先装的顶掉，而 `--status` 看不出来。"""
    labels = {m: sched.label_for(m) for m in ("demo", "paper", "live")}
    assert len(set(labels.values())) == len(labels), f"label 撞车：{labels}"
    for mode, label in labels.items():
        assert mode in label, f"{mode} 的 label 里应能看出模式：{label!r}"
    units = {m: sched.unit_name_for(m) for m in ("demo", "paper", "live")}
    assert len(set(units.values())) == len(units), f"unit 撞车：{units}"


def test_demo_plist_is_unchanged_by_the_new_parameter(sched):
    """老行为必须原样保留：demo 任务里**不能**出现 `--allow-live`。

    这是「默认值不许被顺手翻转」的那一条 —— 加参数最容易发生的事就是把新开关
    的默认值填成"开"。
    """
    xml = sched.plist_xml("/usr/bin/python3", 11, 5, "timer", 5.0,
                          trading_mode="demo", allow_live=False)
    args = _prog_args(xml)
    assert "--allow-live" not in args
    assert "--trading-mode" in args
    assert args[args.index("--trading-mode") + 1] == "demo"
    assert sched.label_for("demo") in xml


def test_live_plist_carries_the_optin_and_the_mode(sched):
    xml = sched.plist_xml("/usr/bin/python3", 11, 1, "timer", 5.0,
                          trading_mode="live", allow_live=True)
    args = _prog_args(xml)
    assert "--allow-live" in args, "实盘 plist 必须带 --allow-live"
    assert args[args.index("--trading-mode") + 1] == "live"
    assert sched.label_for("live") in xml
    assert sched.label_for("demo") not in xml, "实盘 plist 不许挂 demo 的 label"
    # 到点时刻要真写进 StartCalendarInterval，不然它每天在别的时间起来
    assert "<key>Hour</key><integer>11</integer>" in xml
    assert "<key>Minute</key><integer>1</integer>" in xml


# ---------------------------------------------------------------------------
# 3. tick：拒绝点必须在任何破坏性步骤之前
# ---------------------------------------------------------------------------
def test_tick_refuses_live_without_allow_live_and_exits_3(tick_src):
    """真跑一次：缺 `--allow-live` 时退出码 3，且**不 import auto_ctl**。

    用真的子进程跑，因为"拒绝"这件事的价值全在于**它确实没往下走**。
    """
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "scheduled_tick.py"),
         "--trading-mode", "live"],
        capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 3, f"应退出 3，实际 {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout + p.stderr
    assert "--allow-live" in out, "拒绝信息里必须写出正确的用法"


def test_tick_guard_precedes_stop(tick_src):
    """`--allow-live` 的检查必须出现在**第一次 `auto_ctl` 调用之前**。

    否则 `--force-restart` 会先把一个健康的账本停掉，然后才发现参数不合法 ——
    最贵的失败时刻。这条断言用源码顺序钉住它（行为测试做不到：跑到那一步
    需要真凭据）。
    """
    guard = tick_src.index('if m == "live" and not a.allow_live')
    # 注意不能拿 `auto_ctl.` 的第一个出现当"第一次调用"：`from ... import auto_ctl`
    # 这行 import 本身就含这个子串，于是它永远排在守卫前面，断言恒假。
    # 用**调用**的形状（`auto_ctl.is_running(`）才有意义。
    first_call = tick_src.index("auto_ctl.is_running(")
    assert guard < first_call, (
        "实盘守卫必须在第一次 auto_ctl 调用之前 —— 否则会先停掉守护进程才拒绝")
    # 停进程那个动作尤其要在后面，单独钉一次（它是不可逆的）
    assert guard < tick_src.index("auto_ctl.stop("), (
        "实盘守卫必须在 auto_ctl.stop() 之前 —— 否则会把健康账本停在一半")


def test_tick_passes_allow_live_through_not_a_literal(tick_src):
    """`allow_live=False` 写死正是"实盘永远起不来"的那一行。"""
    assert "allow_live=False" not in tick_src, (
        "`allow_live=False` 是硬编码的：这会让实盘定时器永远起不来")
    assert "allow_live=bool(a.allow_live)" in tick_src, (
        "应把 --allow-live 透传给 auto_ctl.start")
