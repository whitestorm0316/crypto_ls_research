#!/usr/bin/env python3
"""每天定时拉起（或唤醒）模拟盘自动交易 —— macOS launchd / Linux systemd 安装器。

为什么是"拉起 + 唤醒"而不是"到点下一单"
----------------------------------------
下单时刻由**回测口径**决定，不是由这个定时器决定。链条是：

  1. 调仓网格锚死在 **UTC 02:00 = 北京 10:00** 产生信号（`dec_idx =
     arange(warmup, T-1, R)`，warmup 由因子回看窗口决定，面板起点
     2021-01-01 00:00 UTC）。v3 的 689 个调仓点实测**全部**落在此刻，
     没有任何配置项能改它。
  2. 回测的执行价取**下一根 bar 的开盘价**（`exec_price=next_open`）
     ＝ 北京 11:00。
  3. 守护进程的自然到期闸门 `due = bars_since_decision >= R` 也恰好在
     面板确认那根 bar 之后打开 —— 同样是北京 11:00。

**三者天然对齐**，所以"信号产生后立即下单"＝让守护进程按自己的判据跑，
不要加执行窗口。定时器的职责只是"确保那一刻守护进程是在跑的"。

定时器模式（默认，`--mode timer`）
    在 `--at`（默认 11:05，即北京 11:00 之后第一个 5 分钟点）拉起守护进程，
    守护进程按自己的网格判据决定当天该不该调仓。**不偏离回测口径。**
    为什么不是 10:00：10:00 时那根 bar 刚收盘、面板还没确认，`bars_since
    = 0`，`due` 仍为 False，拉起来也只能空转等到 11:00。

执行窗口模式（`--mode window`）
    到点由守护进程的 `--exec-window` 强行放行一次（覆盖 `due` 闸门）。
    **这会偏离回测网格**：信号日不变，下单却晚了若干小时。绩效不再是 v3
    口径，参见 `artifacts/grid_phase/phase_sweep.csv`。
    ⚠️ 别把窗口设成 10:00 —— 那时最新决策点就是刚产生的 bar，窗口会每天
    照发一遍同样的目标书，把换手预算烧在重复交易上，而日志里看不出异常。

安装
----
    python3 scripts/install_scheduler.py --install                    # 11:05 定时器
    python3 scripts/install_scheduler.py --install --at 11:05
    python3 scripts/install_scheduler.py --install --mode window --at 23:30
    python3 scripts/install_scheduler.py --status
    python3 scripts/install_scheduler.py --uninstall

macOS 用 launchd 而不是 cron，原因：
  1. cron 在笔记本休眠时**直接跳过**任务，醒来也不会补；launchd 会在唤醒后
     尽快补跑一次（`StartCalendarInterval` 的语义）。
  2. macOS 自 Catalina 起 cron 需要额外的「完全磁盘访问」授权才能可靠运行，
     launchd 不需要。
  3. launchd 的任务归属用户的 GUI 会话，能拿到与双击脚本一致的环境。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LABEL = "com.crypto-ls.auto-trader-demo"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
SYSTEMD_DIR = os.path.expanduser("~/.config/systemd/user")
SYSTEMD_UNIT = "crypto-ls-auto-trader-demo"

MODE = "demo"
REBALANCE_DAYS = 1.0
INTERVAL_MIN = 30.0
BAR = "1h"


def find_python() -> str:
    for c in (
        sys.executable,
        os.path.join(ROOT, ".venv", "bin", "python3"),
        os.path.join(ROOT, "venv", "bin", "python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    raise SystemExit("找不到 python3；用 --python /path/to/python3 指定")


def runner_path() -> str:
    """The one-shot entry point the scheduler invokes."""
    return os.path.join(ROOT, "scripts", "scheduled_tick.py")


# ---------------------------------------------------------------------------
def plist_xml(py: str, hour: int, minute: int, mode: str, window_tol: float) -> str:
    """launchd 任务。`StartCalendarInterval` 在错过时间后会在唤醒时补跑一次。"""
    args = [py, runner_path(), "--mode", mode]
    if mode == "window":
        args += ["--exec-window", f"{hour:02d}:{minute:02d}",
                 "--exec-window-tol", str(window_tol)]
    arg_xml = "\n".join(f"    <string>{a}</string>" for a in args)
    log_dir = os.path.join(ROOT, "logs")
    return textwrap.dedent(f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key>
      <string>{LABEL}</string>
      <key>ProgramArguments</key>
      <array>
    {arg_xml}
      </array>
      <key>WorkingDirectory</key>
      <string>{ROOT}</string>
      <key>StartCalendarInterval</key>
      <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
      </dict>
      <key>StandardOutPath</key>
      <string>{log_dir}/scheduler.out.log</string>
      <key>StandardErrorPath</key>
      <string>{log_dir}/scheduler.err.log</string>
      <key>EnvironmentVariables</key>
      <dict>
        <key>PYTHONPATH</key><string>{ROOT}</string>
        <key>PYTHONUTF8</key><string>1</string>
        <key>PYTHONIOENCODING</key><string>utf-8:replace</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
      </dict>
      <key>RunAtLoad</key><false/>
      <key>ProcessType</key><string>Background</string>
    </dict>
    </plist>
    """)


def systemd_unit(py: str, hour: int, minute: int, mode: str, window_tol: float) -> str:
    args = [py, runner_path(), "--mode", mode]
    if mode == "window":
        args += ["--exec-window", f"{hour:02d}:{minute:02d}",
                 "--exec-window-tol", str(window_tol)]
    cmd = " ".join(args)
    return textwrap.dedent(f"""\
    [Unit]
    Description=Crypto LS demo auto-trader daily tick
    After=network-online.target

    [Service]
    Type=oneshot
    WorkingDirectory={ROOT}
    Environment=PYTHONPATH={ROOT}
    Environment=PYTHONUTF8=1
    Environment=PYTHONIOENCODING=utf-8:replace
    Environment=PYTHONUNBUFFERED=1
    ExecStart={cmd}
    """)


def systemd_timer(hour: int, minute: int) -> str:
    return textwrap.dedent(f"""\
    [Unit]
    Description=Daily 23:30 tick for the crypto LS demo auto-trader

    [Timer]
    OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
    Persistent=true
    Unit={SYSTEMD_UNIT}.service

    [Install]
    WantedBy=timers.target
    """)


# ---------------------------------------------------------------------------
def install_macos(py: str, hour: int, minute: int, mode: str, tol: float) -> int:
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(PLIST, "w", encoding="utf-8") as f:
        f.write(plist_xml(py, hour, minute, mode, tol))
    print(f"[OK] 写入 {PLIST}")
    # bootout first: loading over an existing job silently keeps the OLD plist.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[!!] launchctl bootstrap 失败：{r.stderr.strip()}")
        print("     可先用 launchctl load -w 试：")
        print(f"       launchctl load -w {PLIST}")
        return 1
    print(f"[OK] 已注册 launchd 任务 {LABEL}")
    print(f"     每天 {hour:02d}:{minute:02d} 触发（休眠唤醒后会补跑一次）")
    print(f"     查看：launchctl print gui/{os.getuid()}/{LABEL}")
    return 0


def install_linux(py: str, hour: int, minute: int, mode: str, tol: float) -> int:
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    svc = os.path.join(SYSTEMD_DIR, f"{SYSTEMD_UNIT}.service")
    tmr = os.path.join(SYSTEMD_DIR, f"{SYSTEMD_UNIT}.timer")
    with open(svc, "w", encoding="utf-8") as f:
        f.write(systemd_unit(py, hour, minute, mode, tol))
    with open(tmr, "w", encoding="utf-8") as f:
        f.write(systemd_timer(hour, minute))
    print(f"[OK] 写入 {svc}")
    print(f"[OK] 写入 {tmr}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now",
                        f"{SYSTEMD_UNIT}.timer"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[!!] systemctl 失败：{r.stderr.strip()}")
        return 1
    print(f"[OK] 已启用 systemd timer {SYSTEMD_UNIT}.timer")
    print(f"     每天 {hour:02d}:{minute:02d} 触发（Persistent=true，错过后补跑）")
    return 0


def uninstall() -> int:
    rc = 0
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True)
        if os.path.exists(PLIST):
            os.remove(PLIST)
            print(f"[OK] 已删除 {PLIST}")
        else:
            print("（plist 本来就不存在）")
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        f"{SYSTEMD_UNIT}.timer"], capture_output=True)
        for p in (os.path.join(SYSTEMD_DIR, f"{SYSTEMD_UNIT}.service"),
                  os.path.join(SYSTEMD_DIR, f"{SYSTEMD_UNIT}.timer")):
            if os.path.exists(p):
                os.remove(p)
                print(f"[OK] 已删除 {p}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return rc


def status() -> int:
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "print",
                            f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("[--] launchd 任务未注册")
            print(f"     安装：python3 {os.path.relpath(os.path.abspath(__file__), ROOT)} --install")
            return 1
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.startswith(("state =", "last exit code =", "runs =",
                             "program =", "arguments =")):
                print(f"     {s}")
            elif "Hour" in s or "Minute" in s:
                print(f"     {s}")
        return 0
    r = subprocess.run(["systemctl", "--user", "status", f"{SYSTEMD_UNIT}.timer",
                        "--no-pager"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    return 0 if r.returncode == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每天定时拉起模拟盘自动交易（launchd/systemd）")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--at", default="11:05", metavar="HH:MM",
                    help="本地时间，默认 11:05（北京 11:00 之后第一个 5 分钟点，"
                         "即回测执行价所在 bar 确认之后）")
    ap.add_argument("--mode", default="timer", choices=["timer", "window"],
                    help="timer=只在到点拉起守护进程（不偏离回测口径）；"
                         "window=到点强制调仓一次（偏离回测网格，见文件头说明）")
    ap.add_argument("--exec-window-tol", type=float, default=5.0,
                    help="window 模式下窗口宽度（分钟）")
    ap.add_argument("--python", default=None, help="显式指定 python3 路径")
    a = ap.parse_args(argv)

    if not (a.install or a.uninstall or a.status):
        ap.print_help()
        return 2

    if a.uninstall:
        return uninstall()
    if a.status:
        return status()

    hh, mm = a.at.split(":")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        print(f"!! --at 超出范围：{a.at}")
        return 2
    py = a.python or find_python()
    print(f"解释器 : {py}")
    print(f"项目    : {ROOT}")
    print(f"触发    : 每天 {hour:02d}:{minute:02d}")
    print(f"模式    : {a.mode}"
          + ("（不偏离回测口径）" if a.mode == "timer"
             else "（⚠ 偏离回测网格，先看 phase_sweep.csv）"))
    print()
    if sys.platform == "darwin":
        return install_macos(py, hour, minute, a.mode, a.exec_window_tol)
    if sys.platform.startswith("linux"):
        return install_linux(py, hour, minute, a.mode, a.exec_window_tol)
    print(f"!! 不支持的平台 {sys.platform}；Windows 请用 START_AUTO_TRADER_DEMO.bat")
    return 2


if __name__ == "__main__":
    sys.exit(main())
