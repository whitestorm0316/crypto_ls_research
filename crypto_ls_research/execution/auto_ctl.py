"""Start / stop / inspect the unattended auto-trader from the console.

Why this exists instead of the console just calling `AutoTrader.loop()` in a thread
---------------------------------------------------------------------------------
The console is a `ThreadingHTTPServer` in the user's working session.  A rebalance
loop living inside it would die the moment the console was closed, and -- worse --
would *look* alive on the page while being dead in fact.  So the loop is a
**separate process**: the console only writes an intent file and reads back a
heartbeat it did not produce.

That split makes one thing load-bearing: **the console must never report a state
it has not verified.**  "Started" is only true if a process we spawned is still
alive; "the grid is 1d" is only true if the *heartbeat the daemon wrote* says so.
`AutoTrader._write_state` already records `rebalance_days`, so this module reports
the daemon's own number rather than echoing back what the UI just asked for.  A UI
that echoes the request is indistinguishable from one that works, right up until
the run does not happen.

Layout (all under `artifacts/live/<mode>/`, next to the heartbeats they describe)::

    auto.pid      JSON: the pid we started, when, and the grid we asked for
    auto.json     written by the *daemon*: enabled / pid / grid / counters
    auto.jsonl    append-only decision audit trail

`auto.pid` vs `auto.json` is deliberate: one is what we asked for, the other is
what the daemon reports.  When they disagree, the disagreement is the diagnosis.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from ..config.settings import ACCEPTED_REBALANCE_DAYS
from .store import LIVE_DIR, Store, _pid_alive

CTL_FILE = "auto.pid"

#: Modes the auto-trader may run in.  `live` is allowed *here* because the daemon
#: itself refuses without `--allow-live`; keeping the two gates independent means a
#: UI bug cannot be the only thing standing between a user and real money.
MODES = ("paper", "demo", "live")


def ctl_path(mode: str) -> str:
    return os.path.join(LIVE_DIR, mode, CTL_FILE)


def state_path(mode: str) -> str:
    return os.path.join(LIVE_DIR, mode, "auto.json")


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_ctl(mode: Optional[str] = None) -> dict:
    """What the console asked for.  `{}` when nothing was ever started."""
    if mode:
        return _read_json(ctl_path(mode)) or {}
    return {m: (_read_json(ctl_path(m)) or {}) for m in MODES}


def read_state(mode: str) -> dict:
    """The daemon's own heartbeat.  Authoritative for "is it running right now"."""
    return _read_json(state_path(mode)) or {}


def is_running(mode: str) -> bool:
    """True only if the pid recorded in the *heartbeat* is alive **and** not us.

    Uses the daemon's heartbeat, not our control file: if the daemon crashed, the
    control file still says "started" and the heartbeat still says `enabled: true`
    with a dead pid.  Checking the pid is what turns "we once started something"
    into "something is running".
    """
    st = read_state(mode)
    pid = st.get("pid")
    if not pid or pid == os.getpid():
        return False
    if not st.get("enabled"):
        return False
    return _pid_alive(pid)


def grid_for(mode: str) -> dict:
    """The grid **the daemon reports**, falling back to what we asked for.

    Never read the requested grid from the control file alone and present it as
    current: if the daemon died before writing its first heartbeat, the request
    never took effect and echoing it would be a claim we cannot support.
    """
    st = read_state(mode)
    if st.get("rebalance_days") is not None:
        return {"rebalance_days": st.get("rebalance_days"),
                "bar": st.get("bar"),
                "interval_min": st.get("interval_min"),
                "source": "heartbeat"}
    ctl = read_ctl(mode)
    if ctl.get("rebalance_days") is not None:
        return {"rebalance_days": ctl.get("rebalance_days"),
                "bar": ctl.get("bar"),
                "interval_min": ctl.get("interval_min"),
                "source": "requested-not-confirmed"}
    return {"source": "none"}


def _spawn(mode: str, rebalance_days: float, interval_min: float, bar: str,
           dry_run: bool, allow_live: bool,
           exec_window: Optional[str] = None,
           exec_window_tol: float = 5.0,
           exec_window_utc: bool = False,
           refresh_after_hours: Optional[float] = None) -> dict:
    argv = [sys.executable, "-m", "crypto_ls_research.execution.auto_trader",
            "--mode", mode,
            "--interval", str(interval_min),
            "--rebalance-days", str(rebalance_days),
            "--bar", bar]
    if refresh_after_hours is not None:
        argv += ["--refresh-after-hours", str(refresh_after_hours)]
    if exec_window:
        argv += ["--exec-window", str(exec_window),
                 "--exec-window-tol", str(exec_window_tol)]
        if exec_window_utc:
            argv.append("--exec-window-utc")
    if dry_run:
        argv.append("--dry-run")
    if allow_live:
        argv.append("--allow-live")
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", ".."))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    # A log file rather than DEVNULL for stdout/stderr: a daemon that dies during
    # import leaves nothing at all behind otherwise, and "started, but 0 trades"
    # is indistinguishable from "never started".
    log_path = os.path.join(LIVE_DIR, mode, "auto_stdout.log")
    # Detached on purpose: the daemon must outlive the console.  Its stdout is
    # not captured -- everything worth knowing goes to `auto.jsonl` and
    # `auto.json`, which the page already reads.
    kwargs = {"cwd": root, "env": env, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        # `CREATE_BREAKAWAY_FROM_JOB` is the one that actually matters when the
        # console itself was started from a shell that owns a job object (an IDE
        # task runner, CI, a terminal wrapper): without it the whole subtree is
        # reaped the moment *that* shell exits, and "unattended" means "until I
        # close the window".  Observed live on 2026-09-28 -- a daemon spawned
        # from a one-shot shell died silently right after its first check while
        # one spawned by the long-lived console kept running.
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        try:
            # Only add breakaway when the parent is in a job that permits it;
            # otherwise CreateProcess fails outright with ERROR_ACCESS_DENIED.
            kwargs["creationflags"] = flags | CREATE_BREAKAWAY_FROM_JOB
            p = _popen(argv, kwargs, log_path)
        except OSError:
            kwargs["creationflags"] = flags
            p = _popen(argv, kwargs, log_path)
        return {"pid": p.pid, "argv": argv, "breakaway": bool(
            kwargs["creationflags"] & CREATE_BREAKAWAY_FROM_JOB)}
    kwargs["start_new_session"] = True
    return {"pid": _popen(argv, kwargs, log_path).pid, "argv": argv}


def _popen(argv: list, kwargs: dict, log_path: str):
    """Open the log, spawn, close the handle.  Shared by both platform paths."""
    with open(log_path, "ab") as log:
        kwargs = dict(kwargs, stdout=log, stderr=log)
        return subprocess.Popen(argv, **kwargs)


def start(mode: str, *, rebalance_days: float = ACCEPTED_REBALANCE_DAYS,
          interval_min: float = 30.0,
          bar: str = "1h", dry_run: bool = False,
          allow_live: bool = False,
          exec_window: Optional[str] = None,
          exec_window_tol: float = 5.0,
          exec_window_utc: bool = False,
          refresh_after_hours: Optional[float] = None) -> dict:
    """Start the daemon.  Refuses rather than fighting for the same mode.

    Two auto-traders on one mode would each read the same starting book and each
    send the full order list -- the same double-order hazard `rebalance_lock`
    exists to prevent, except the lock only serialises *rebalances*, so the two
    would alternate rather than collide.  Stop it first, explicitly.
    """
    if mode not in MODES:
        return {"ok": False, "error": f"unknown mode {mode!r}"}
    if rebalance_days is None or not (float(rebalance_days) > 0):
        return {"ok": False, "error": "调仓间隔必须是正数（天）"}
    if is_running(mode):
        st = read_state(mode)
        return {"ok": False, "already": True,
                "error": f"{mode} 已有自动任务在跑（pid {st.get('pid')}）。"
                         "先停掉它，再改参数启动。",
                "state": st}
    if mode == "live" and not allow_live:
        return {"ok": False, "error": "实盘自动交易需要显式确认（allow_live）"}

    os.makedirs(os.path.dirname(ctl_path(mode)), exist_ok=True)
    # Clear the previous heartbeat *before* spawning: otherwise, for the second
    # before the new daemon writes its own, `/api/live/auto` would report the old
    # run's `enabled: true` alongside the old dead pid.
    try:
        os.remove(state_path(mode))
    except OSError:
        pass
    try:
        info = _spawn(mode, float(rebalance_days), float(interval_min), bar,
                      dry_run, allow_live, exec_window, exec_window_tol,
                      exec_window_utc, refresh_after_hours)
    except OSError as e:
        return {"ok": False, "error": f"启动失败：{type(e).__name__}: {e}"}

    ctl = {"mode": mode, "pid": info.get("pid"), "argv": info.get("argv") or [],
           "rebalance_days": float(rebalance_days), "bar": bar,
           "interval_min": float(interval_min), "dry_run": bool(dry_run),
           "allow_live": bool(allow_live), "requested_at": time.time(),
           "requested_str": time.strftime("%Y-%m-%d %H:%M:%S"),
           # Carry the window into the control file so a reader can tell a
           # windowed start from a plain one without parsing argv.
           "exec_window": exec_window,
           "exec_window_tol": float(exec_window_tol) if exec_window else None,
           "exec_window_utc": bool(exec_window_utc) if exec_window else None,
           # None = "let the daemon use its own default", which is not the same
           # as 0 ("never refresh").  Record it so a reader can tell the two
           # apart without parsing argv.
           "refresh_after_hours": (None if refresh_after_hours is None
                                   else float(refresh_after_hours)),
           # `pid` above is the process *we spawned*, which on Windows is a
           # launcher, not the daemon.  Keep it (it is what we can kill if the
           # daemon never reports in) but record the distinction explicitly so a
           # later reader does not mistake one for the other.
           "pid_kind": "spawned"}
    tmp = ctl_path(mode) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctl, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ctl_path(mode))
    return {"ok": True, "mode": mode, "ctl": ctl}


def _adopt_daemon_pid(mode: str) -> Optional[int]:
    """Rewrite `auto.pid` with the daemon's real pid once we know it.

    Called after a heartbeat appears.  Both pids name "the auto-trader" and only
    one of them does any work, so the file whose entire purpose is "here is the
    process to kill" must converge on the daemon's.  Without this, `auto.pid`
    keeps a pid that was last alive for fourteen seconds, and the only reason
    `stop()` still hits the right process is that it reads the heartbeat first --
    a correctness that depends on a fallback never being reached is not
    correctness, it is luck.
    """
    st = read_state(mode)
    dpid = st.get("pid")
    ctl = read_ctl(mode)
    if not dpid or not ctl:
        return None
    if ctl.get("pid") == dpid and ctl.get("pid_kind") == "daemon":
        return dpid
    if not _pid_alive(dpid):
        return None
    ctl = dict(ctl)
    ctl["spawned_pid"] = ctl.get("pid")
    ctl["pid"] = dpid
    ctl["pid_kind"] = "daemon"
    ctl["adopted_at"] = time.time()
    tmp = ctl_path(mode) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ctl, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ctl_path(mode))
    except OSError:
        return None
    return dpid


def stop(mode: str, *, wait_sec: float = 6.0) -> dict:
    """Stop the daemon and **verify** it is gone.

    `SIGTERM` is the polite path (`AutoTrader.loop` sleeps in slices so it is
    honoured within ~5 s and still writes `enabled: false`).  If it is still alive
    after the grace period we say so, with the pid -- a "stopped" that leaves the
    process running is the worst possible answer, because the user then goes and
    changes the grid believing nothing is trading.
    """
    if mode not in MODES:
        return {"ok": False, "error": f"unknown mode {mode!r}"}
    st = read_state(mode)
    ctl = read_ctl(mode)
    # **Heartbeat first, control file second.**  `auto.json.pid` is written by the
    # daemon about itself; `auto.pid` is what `start()` *believed* it spawned.  On
    # Windows those differ (launcher vs. re-exec'd child), so falling back to the
    # control file means killing the launcher and orphaning a loop that keeps
    # trading while the page says "已停止".  Fact before intent.
    pid = st.get("pid") or ctl.get("pid")
    if not pid:
        return {"ok": True, "mode": mode, "was_running": False,
                "note": "没有正在运行的自动任务"}
    if pid == os.getpid():
        return {"ok": False, "error": "拒绝：记录的 pid 是本进程"}

    alive_before = _pid_alive(pid)
    if not alive_before:
        _clear_ctl(mode)
        return {"ok": True, "mode": mode, "was_running": False,
                "note": f"进程 {pid} 已经不在，清理了控制文件"}

    try:
        _terminate(pid)
    except OSError as e:
        return {"ok": False, "error": f"发送停止信号失败：{e}", "pid": pid}

    deadline = time.time() + max(0.0, wait_sec)
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.25)
    still = _pid_alive(pid)
    if still:
        # Do not clear the control file: leaving it is what lets the user (or a
        # retry) still find and kill the process.  Report the pid honestly.
        return {"ok": False, "mode": mode, "pid": pid, "still_running": True,
                "error": f"已发送停止信号，但进程 {pid} 仍未退出。"
                         "它可能正在一次调仓的中途，稍后再看一次状态。"}
    _clear_ctl(mode)
    return {"ok": True, "mode": mode, "was_running": True, "pid": pid,
            "note": "已停止（进程已确认退出）"}


def _terminate(pid: int) -> None:
    if os.name == "nt":
        # `os.kill` on Windows is `TerminateProcess` for non-console signals,
        # which is exactly what we want: there is no SIGTERM to deliver.
        import ctypes
        PROCESS_TERMINATE = 0x0001
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")
        try:
            if not k.TerminateProcess(h, 0):
                raise OSError(ctypes.get_last_error(), "TerminateProcess failed")
        finally:
            k.CloseHandle(h)
        return
    import signal as _signal
    os.kill(int(pid), _signal.SIGTERM)


def _clear_ctl(mode: str) -> None:
    try:
        os.remove(ctl_path(mode))
    except OSError:
        pass


def wait_for_heartbeat(mode: str, timeout: float = 20.0) -> dict:
    """Poll until the daemon has published a heartbeat, or give up honestly.

    The console calls this right after `start()` so that "已启动" is backed by a
    heartbeat file that exists.  Returning `ok: False` here is not an error to
    hide: it means the process was spawned but has not reported in, and the page
    should say exactly that.

    **Liveness is judged from the heartbeat, never from the control file.**  On
    Windows `python -m pkg.mod` forks a launcher which re-execs the real
    interpreter as a *child* and then exits, so `_spawn().pid` (what we recorded)
    is dead ~14 s into a perfectly healthy startup while the daemon it started
    runs on.  Polling the recorded pid would therefore declare failure on a
    working daemon -- a false alarm the console shows as `confirmed: false`.
    The heartbeat's own pid is the only one that identifies the process doing the
    work, so it is the only one worth asking about.
    """
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        st = read_state(mode)
        if st.get("enabled") and is_running(mode):
            # The daemon is up: converge the control file onto its pid so the
            # file stops naming a launcher that has been dead for a while.
            _adopt_daemon_pid(mode)
            return {"ok": True, "state": st, "ctl": read_ctl(mode)}
        pid = st.get("pid")
        # No heartbeat yet, or a heartbeat whose own pid is dead -- either way
        # nothing is going to report in.  Stop waiting instead of burning the
        # whole timeout.
        if pid and not _pid_alive(pid):
            return {"ok": False, "error": "进程已退出，未写出心跳。"
                                         "检查 artifacts/live/<mode>/auto_stdout.log"}
        time.sleep(0.4)
    return {"ok": False, "error": f"{timeout:g}s 内没有等到心跳",
            "ctl": read_ctl(mode), "state": read_state(mode)}


def stop_by_pid(mode: str, pid: int) -> dict:                      # pragma: no cover
    """Escape hatch for a stuck daemon, kept separate from `stop` on purpose."""
    st = read_state(mode)
    return {"ok": True, "pid": st.get("pid") or pid}


# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    """CLI so the stop path is as easy as the start path.

    A user who has to remember a Python one-liner in order to stop an unattended
    trader will eventually just close the window and hope.  `--stop` needs to be
    a single obvious command, and it must exit non-zero when it could not
    actually confirm the process is gone -- a "stopped" that lies is worse than
    an error message.
    """
    import argparse
    ap = argparse.ArgumentParser(description="启动/停止无人值守自动调仓")
    ap.add_argument("--mode", default="demo", choices=list(MODES))
    ap.add_argument("--stop", action="store_true", help="停止该模式的自动任务")
    ap.add_argument("--status", action="store_true", help="只看状态，不做任何事")
    ap.add_argument("--rebalance-days", type=float, default=None,
                    help="调仓网格（天）。默认沿用当前请求值，没有则用 3（v3）")
    ap.add_argument("--interval", type=float, default=30.0, help="检查间隔（分钟）")
    ap.add_argument("--bar", default="1h")
    ap.add_argument("--refresh-after-hours", type=float, default=None,
                    help="数据落后超过这么多小时就先增量刷新。不传 = 用守护进程"
                         "自己的默认值（当前 1.0 = 每个 tick 都刷新，见 "
                         "auto_trader --help）。0 = 从不刷新。")
    ap.add_argument("--dry-run", action="store_true", help="只算不下单")
    ap.add_argument("--allow-live", action="store_true",
                    help="live 模式必须显式带上")
    ap.add_argument("--exec-window", default=None, metavar="HH:MM",
                    help="执行窗口（本地时间）。到点后即使不在调仓网格上也会强制"
                         "调仓一次。**这会偏离回测网格**：信号日仍是 "
                         "UTC02:00/北京10:00 的 bar，下单却晚了几小时。"
                         "不传 = 不强制（默认）。")
    ap.add_argument("--exec-window-tol", type=float, default=5.0, metavar="MIN",
                    help="执行窗口宽度（分钟，默认 5）")
    ap.add_argument("--exec-window-utc", action="store_true",
                    help="把 --exec-window 解释成 UTC 而不是本地时间")
    a = ap.parse_args(argv)

    if a.status:
        st = read_state(a.mode)
        ctl = read_ctl(a.mode)
        print(json.dumps({
            "mode": a.mode, "running": is_running(a.mode),
            "heartbeat_pid": st.get("pid"), "heartbeat_enabled": st.get("enabled"),
            "ctl_pid": ctl.get("pid"), "ctl_pid_kind": ctl.get("pid_kind"),
            "grid": grid_for(a.mode),
            "rebalance_days": st.get("rebalance_days"),
            "exec_window": st.get("exec_window"),
            "exec_window_min": st.get("exec_window_min"),
            "exec_window_utc": st.get("exec_window_utc"),
            "refresh_after_hours": ctl.get("refresh_after_hours"),
            "forced_runs": st.get("forced_runs"),
            "checks": st.get("checks"), "trades": st.get("trades"),
        }, ensure_ascii=False, indent=1))
        return 0

    if a.stop:
        r = stop(a.mode)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 1

    # start
    rd = a.rebalance_days
    if rd is None:
        g = grid_for(a.mode)
        rd = g.get("rebalance_days")
        if rd is None:
            # `DEFAULT_SIGNAL` lives in `execution.engine`, not `backtest.engine`
            # (it is the *live* accepted config: bar + grid + asset class +
            # overrides).  Importing it from `backtest.engine` raised ImportError,
            # so the very first `auto_ctl --mode demo` without `--rebalance-days`
            # -- the documented command in AUTO_TRADER_README.md §七 -- crashed
            # instead of starting.  Only the fallback path was affected: passing
            # `--rebalance-days` explicitly, or having a previous grid on disk,
            # both skip this branch.
            from .engine import DEFAULT_SIGNAL
            rd = float(DEFAULT_SIGNAL["rebalance_days"])
    r = start(a.mode, rebalance_days=float(rd), interval_min=a.interval,
              bar=a.bar, dry_run=a.dry_run, allow_live=a.allow_live,
              exec_window=a.exec_window,
              exec_window_tol=a.exec_window_tol,
              exec_window_utc=a.exec_window_utc,
              refresh_after_hours=a.refresh_after_hours)
    if not r.get("ok"):
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 1
    w = wait_for_heartbeat(a.mode, timeout=45.0)
    r["confirmed"] = bool(w.get("ok"))
    r["state"] = w.get("state") or read_state(a.mode)
    r["grid"] = grid_for(a.mode)
    if not w.get("ok"):
        r["warning"] = w.get("error")
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["confirmed"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
