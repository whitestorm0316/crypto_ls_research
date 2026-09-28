"""Tests for the console-side start/stop of the unattended rebalancer.

What is actually load-bearing here
---------------------------------
The console cannot see the daemon's process tree; all it has is two files it did
not write (`auto.json` from the daemon, `auto.pid` from itself) plus the OS pid
table.  So every claim this module makes is a *join* of those, and the tests are
about the joins that can lie:

* **"running" must be verified.**  It is not "we once spawned something": it is
  "the pid in the heartbeat is alive right now".  A crashed daemon leaves
  `enabled: true` behind forever, and reporting that as running means the user
  goes on to change parameters believing nothing is trading.
* **The grid must be the daemon's number, not our request.**  Echoing back what
  the UI asked for is indistinguishable from a working system until the night
  nothing happens.  When the daemon has not written a heartbeat, the answer must
  be `requested-not-confirmed`, not a bare number.
* **Stop must verify.**  A "stopped" that leaves the process alive is the worst
  possible answer, so `still_running` is reported rather than swallowed.
* **Two traders on one mode is a double-order hazard.**  `rebalance_lock` only
  serialises *rebalances*; two loops would alternate rather than collide, so
  `start` must refuse rather than join.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from crypto_ls_research.execution import auto_ctl
from crypto_ls_research.execution import store as store_mod


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Point `LIVE_DIR` at a temp dir so nothing touches the real artifacts."""
    d = tmp_path / "live"
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(d))
    monkeypatch.setattr(auto_ctl, "LIVE_DIR", str(d))
    for m in auto_ctl.MODES:
        os.makedirs(os.path.join(str(d), m), exist_ok=True)
    return d


def _write_heartbeat(root, mode: str, **over) -> dict:
    st = {"mode": mode, "enabled": True, "pid": os.getpid(),
          "interval_min": 30.0, "bar": "1h", "rebalance_days": 1.0,
          "started": time.time(), "checks": 3, "trades": 1, "skips": {},
          "errors": 0, "last": {}, "kill_switch": False}
    st.update(over)
    with open(os.path.join(str(root), mode, "auto.json"), "w", encoding="utf-8") as f:
        json.dump(st, f)
    return st


def _write_ctl(root, mode: str, **over) -> dict:
    c = {"mode": mode, "pid": 4321, "rebalance_days": 1.0, "bar": "1h",
         "interval_min": 30.0, "requested_at": time.time()}
    c.update(over)
    with open(os.path.join(str(root), mode, "auto.pid"), "w", encoding="utf-8") as f:
        json.dump(c, f)
    return c


# -- is_running: 必须验证，不能凭"启动过"就报在跑 ---------------------------------

def test_no_heartbeat_means_not_running(live):
    assert auto_ctl.is_running("paper") is False


def test_heartbeat_with_our_own_pid_is_not_a_running_daemon(live):
    """`pid == os.getpid()` would be the console itself, never the daemon.

    This is the "we are looking at our own reflection" bug: the console writing a
    heartbeat and then reading it as proof that a background job is alive.
    """
    _write_heartbeat(live, "paper", pid=os.getpid())
    assert auto_ctl.is_running("paper") is False


def test_heartbeat_with_enabled_false_is_not_running(live):
    """A daemon that shut down cleanly leaves `enabled: false` -- respect it."""
    _write_heartbeat(live, "paper", pid=4, enabled=False)
    assert auto_ctl.is_running("paper") is False


def test_dead_pid_in_heartbeat_is_not_running(live):
    """The crash case: `enabled: true` and a pid that no longer exists."""
    _write_heartbeat(live, "paper", pid=999_999)
    assert auto_ctl.is_running("paper") is False


def test_alive_pid_in_heartbeat_is_running(live):
    """A live pid that is not ours, with `enabled: true`, IS running."""
    live_pid = os.getppid() or 4
    if live_pid == os.getpid() or not store_mod._pid_alive(live_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")
    _write_heartbeat(live, "paper", pid=live_pid)
    assert auto_ctl.is_running("paper") is True


# -- grid_for: 报守护进程写的数，不是我们请求的数 --------------------------------

def test_grid_reports_the_heartbeat_not_the_request(live):
    """The daemon says 1d while an older control file says 3d: heartbeat wins.

    This is the whole reason `grid_for` exists.  If the request won, the page
    would show the grid the user *typed* even when the running process is on a
    different one -- a difference that is invisible until a missed rebalance.
    """
    _write_ctl(live, "paper", rebalance_days=3.0)
    _write_heartbeat(live, "paper", pid=os.getppid() or 4, rebalance_days=1.0)
    g = auto_ctl.grid_for("paper")
    assert g["rebalance_days"] == 1.0
    assert g["source"] == "heartbeat"


def test_grid_is_flagged_unconfirmed_when_only_requested(live):
    """Nothing but a control file -> the number is a request, and must say so."""
    _write_ctl(live, "paper", rebalance_days=1.0)
    g = auto_ctl.grid_for("paper")
    assert g["rebalance_days"] == 1.0
    assert g["source"] == "requested-not-confirmed"


def test_grid_is_none_when_never_started(live):
    assert auto_ctl.grid_for("paper")["source"] == "none"
    assert "rebalance_days" not in auto_ctl.grid_for("paper")


# -- start: 拒绝，而不是抢同一个 mode ---------------------------------------------

def test_start_refuses_when_already_running(live, monkeypatch):
    """Two loops on one mode would alternate rebalances, each sending the full
    order list from the same starting book -- the double-order hazard."""
    called = []
    monkeypatch.setattr(auto_ctl, "_spawn", lambda *a, **k: called.append(a) or {"pid": 1})
    _write_heartbeat(live, "paper", pid=os.getppid() or 4)

    r = auto_ctl.start("paper", rebalance_days=1.0)
    assert r["ok"] is False
    assert r["already"] is True
    assert called == [], "已有实例在跑时绝不能再次 spawn"


def test_start_rejects_nonpositive_grid(live):
    r = auto_ctl.start("paper", rebalance_days=0)
    assert r["ok"] is False and "正数" in r["error"]
    r = auto_ctl.start("paper", rebalance_days=-1)
    assert r["ok"] is False


def test_start_rejects_bad_mode(live):
    assert auto_ctl.start("nope")["ok"] is False


def test_start_live_requires_explicit_allow(live):
    """The daemon has its own `--allow-live` gate; this is the second, independent
    one, so a UI bug alone can never be what starts real-money trading."""
    r = auto_ctl.start("live", rebalance_days=1.0, allow_live=False)
    assert r["ok"] is False and "实盘" in r["error"]


def test_start_writes_ctl_and_clears_stale_heartbeat(live, monkeypatch):
    """A previous run's heartbeat must be cleared before spawning.

    Otherwise, for the second before the new daemon writes its own, the page
    reports the *old* run's `enabled: true` with the old (dead) pid -- i.e. it
    shows a running job that is not this one.
    """
    _write_heartbeat(live, "paper", pid=999_999)     # leftovers from a crash
    monkeypatch.setattr(auto_ctl, "_spawn",
                        lambda *a, **k: {"pid": 4321, "argv": ["x"]})
    r = auto_ctl.start("paper", rebalance_days=1.0)
    assert r["ok"] is True
    assert auto_ctl.read_state("paper") == {}, "陈旧心跳必须在 spawn 前清掉"
    ctl = auto_ctl.read_ctl("paper")
    assert ctl["pid"] == 4321 and ctl["rebalance_days"] == 1.0


# -- stop: 必须读回验证 ----------------------------------------------------------

def test_stop_with_nothing_running_is_a_clean_noop(live):
    r = auto_ctl.stop("paper")
    assert r["ok"] is True and r["was_running"] is False


def test_stop_refuses_our_own_pid(live):
    _write_heartbeat(live, "paper", pid=os.getpid())
    r = auto_ctl.stop("paper")
    assert r["ok"] is False and "本进程" in r["error"]


def test_stop_of_a_dead_pid_cleans_up(live):
    _write_heartbeat(live, "paper", pid=999_999)
    _write_ctl(live, "paper", pid=999_999)
    r = auto_ctl.stop("paper")
    assert r["ok"] is True and r["was_running"] is False
    assert auto_ctl.read_ctl("paper") == {}, "确认已死就该清掉控制文件"


def test_stop_reports_still_running_rather_than_lying(live, monkeypatch):
    """The important failure: signal sent, process alive.

    Cleared control file + a cheerful "已停止" is exactly how a user ends up
    reconfiguring the grid while the old loop is still trading.
    """
    live_pid = os.getppid() or 4
    if live_pid == os.getpid() or not store_mod._pid_alive(live_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")
    _write_heartbeat(live, "paper", pid=live_pid)
    _write_ctl(live, "paper", pid=live_pid)
    monkeypatch.setattr(auto_ctl, "_terminate", lambda pid: None)   # 信号无效
    r = auto_ctl.stop("paper", wait_sec=0.3)
    assert r["ok"] is False and r["still_running"] is True
    assert r["pid"] == live_pid
    assert auto_ctl.read_ctl("paper") != {}, \
        "没停下来就不能清控制文件，否则人再也找不到这个进程"


# -- wait_for_heartbeat ---------------------------------------------------------

def test_wait_for_heartbeat_does_not_mistake_a_dead_launcher_for_a_dead_daemon(
        live, monkeypatch):
    """On Windows `python -m pkg.mod` forks a launcher that re-execs the real
    interpreter as a *child*; the launcher exits immediately afterwards.

    So `_spawn().pid` is the launcher, `auto.json.pid` is the daemon.  If
    `wait_for_heartbeat` polls the control file's pid it sees "the process is
    gone" ~14 s into a perfectly healthy startup and reports a false failure --
    which the console surfaces as `confirmed: false` on a daemon that is running
    fine.  Regression: observed live on 2026-09-28 (ctl pid 21344 dead, daemon
    7308 alive, heartbeat healthy)."""
    dead_launcher = 999_999          # exits right after spawning, like the launcher
    assert not store_mod._pid_alive(dead_launcher)
    daemon_pid = os.getppid() or 4   # any live pid that is not ours
    if daemon_pid == os.getpid() or not store_mod._pid_alive(daemon_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")

    def fake_spawn(mode, *a, **k):
        _write_heartbeat(live, mode, pid=daemon_pid, rebalance_days=1.0)
        return {"pid": dead_launcher, "argv": ["x"]}

    monkeypatch.setattr(auto_ctl, "_spawn", fake_spawn)
    auto_ctl.start("paper", rebalance_days=1.0)
    w = auto_ctl.wait_for_heartbeat("paper", timeout=3.0)
    assert w["ok"] is True, (
        "心跳已由守护进程写出，就不能因为**启动器**退出了而报失败："
        f"{w.get('error')}")


def test_wait_for_heartbeat_times_out_honestly(live, monkeypatch, tmp_path):
    monkeypatch.setattr(auto_ctl, "_spawn", lambda *a, **k: {"pid": os.getpid()})
    r = auto_ctl.start("paper", rebalance_days=1.0)
    assert r["ok"] is True
    w = auto_ctl.wait_for_heartbeat("paper", timeout=0.5)
    # 进程就是本进程 -> `is_running` 为假（不能把自己当守护进程），因此必然 unconfirmed
    assert w["ok"] is False


def test_wait_for_heartbeat_succeeds_when_heartbeat_appears(live, monkeypatch):
    live_pid = os.getppid() or 4
    if live_pid == os.getpid() or not store_mod._pid_alive(live_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")

    real_spawn = auto_ctl._spawn

    def spawn_then_write(*a, **k):
        _write_heartbeat(live, "paper", pid=live_pid, rebalance_days=1.0)
        return {"pid": live_pid, "argv": ["x"]}

    monkeypatch.setattr(auto_ctl, "_spawn", spawn_then_write)
    auto_ctl.start("paper", rebalance_days=1.0)
    w = auto_ctl.wait_for_heartbeat("paper", timeout=2.0)
    assert w["ok"] is True
    assert w["state"]["rebalance_days"] == 1.0


def test_control_file_converges_on_the_daemon_pid(live, monkeypatch):
    """`auto.pid` 的用途是「要杀哪个进程」，所以它必须收敛到真正干活的 pid。

    启动瞬间记下的是启动器 pid（Windows 上十几秒后就死了）。心跳一出现就把它
    改写成守护进程的 pid，否则这个文件永远在指一个已经消失的进程。
    """
    real_pid = os.getppid() or 4
    if real_pid == os.getpid() or not store_mod._pid_alive(real_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")
    dead_launcher = 999_999

    def spawn_then_write(*a, **k):
        _write_heartbeat(live, "paper", pid=real_pid, rebalance_days=1.0)
        return {"pid": dead_launcher, "argv": ["x"]}

    monkeypatch.setattr(auto_ctl, "_spawn", spawn_then_write)
    auto_ctl.start("paper", rebalance_days=1.0)
    assert auto_ctl.read_ctl("paper")["pid"] == dead_launcher, "启动时先记启动器"

    w = auto_ctl.wait_for_heartbeat("paper", timeout=2.0)
    assert w["ok"] is True
    ctl = auto_ctl.read_ctl("paper")
    assert ctl["pid"] == real_pid, "心跳出现后必须改写成守护进程 pid"
    assert ctl["pid_kind"] == "daemon"
    assert ctl["spawned_pid"] == dead_launcher, "原始 pid 要留痕，不能悄悄丢"


def test_adopt_refuses_a_dead_heartbeat_pid(live):
    """不能把一个已死的 pid 抄进控制文件——那等于伪造一个可杀的目标。"""
    _write_ctl(live, "paper", pid=4321)
    _write_heartbeat(live, "paper", pid=999_999)
    assert auto_ctl._adopt_daemon_pid("paper") is None
    assert auto_ctl.read_ctl("paper")["pid"] == 4321, "改写失败时保持原样"


def test_stop_prefers_the_heartbeat_pid_over_the_control_file(live, monkeypatch):
    """控制文件写的是「我们启动了谁」，心跳写的是「谁在跑」。杀后者。

    反过来就会杀掉已经消失的启动器，把一个还在交易的循环留成孤儿，而页面上
    显示「已停止」。
    """
    daemon_pid = os.getppid() or 4
    if daemon_pid == os.getpid() or not store_mod._pid_alive(daemon_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")
    _write_ctl(live, "paper", pid=999_999)          # 已死的启动器
    _write_heartbeat(live, "paper", pid=daemon_pid)

    killed = []
    monkeypatch.setattr(auto_ctl, "_terminate", lambda pid: killed.append(pid))
    r = auto_ctl.stop("paper", wait_sec=0.3)
    assert killed == [daemon_pid], f"必须杀心跳里的 pid，实际杀了 {killed}"
    assert r["pid"] == daemon_pid


# -- CLI：停止路径必须和启动路径一样简单 ----------------------------------------

def test_cli_status_reports_not_running_for_a_dead_heartbeat_pid(live, capsys):
    """心跳里留着 `enabled: true` 和死 pid —— 这正是崩溃后的状态。

    它必须报 `running: false`。报 true 用户就会以为盘在跑，转身去干别的。
    """
    _write_heartbeat(live, "demo", pid=999_999)
    rc = auto_ctl.main(["--status", "--mode", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"running": false' in out
    assert '"heartbeat_enabled": true' in out, "原始字段要照实显示，不能藏"


def test_cli_stop_on_nothing_exits_zero(live, capsys):
    rc = auto_ctl.main(["--stop", "--mode", "demo"])
    assert rc == 0, "没有东西可停不是错误"
    assert '"was_running": false' in capsys.readouterr().out


def test_cli_stop_exits_nonzero_when_the_process_survives(live, monkeypatch, capsys):
    """停机失败必须是非零退出码。

    否则脚本里 `&&` 链会继续往下走，用户以为停了。
    """
    live_pid = os.getppid() or 4
    if live_pid == os.getpid() or not store_mod._pid_alive(live_pid):
        pytest.skip("本平台找不到一个存活的、非本进程的 pid")
    _write_heartbeat(live, "demo", pid=live_pid)
    monkeypatch.setattr(auto_ctl, "_terminate", lambda pid: None)
    rc = auto_ctl.main(["--stop", "--mode", "demo"])
    assert rc == 1
    assert '"still_running": true' in capsys.readouterr().out


def test_cli_rejects_an_unknown_mode(live):
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        auto_ctl.main(["--status", "--mode", "nope"])


# -- _pid_alive 的平台差异 -------------------------------------------------------
# 这一条是本次修掉的真实缺陷：`os.kill(pid, 0)` 在 Windows 上不是 POSIX 语义，
# 用 `OpenProcess` 才能区分「不存在」与「存在但不是我们的」。

def test_pid_alive_rejects_nonsense(live):
    for bad in (0, -1, None, "abc", ""):
        assert store_mod._pid_alive(bad) is False


def test_pid_alive_accepts_our_own_process(live):
    assert store_mod._pid_alive(os.getpid()) is True


def test_pid_alive_rejects_a_pid_that_cannot_exist(live):
    assert store_mod._pid_alive(999_999) is False


def test_pid_alive_accepts_the_system_process(live):
    """`4` is Windows' `System`; on POSIX it is some kernel thread.

    Platform-aware on purpose: the *rule* being tested is "an alive pid we do not
    own counts as alive", and the old `os.kill(pid, 0)` spelling got that wrong on
    Windows (it reported alive processes as dead, so another user's lock was
    treated as debris and stolen).
    """
    if os.name == "nt":
        assert store_mod._pid_alive(4) is True
    else:
        pytest.skip("POSIX 上 pid 4 不保证存在")
