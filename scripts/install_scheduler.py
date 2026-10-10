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
  3. 守护进程的自然到期闸门 `rebalance_window_open`（`bars_since_decision
     == 1`）在**网格点那根 bar 收盘**时打开：实盘路径会补一根「执行 bar」
    让引擎把这一笔记上账（`signal.book_with_execution_bar`），而
    1h bar 收盘后约 10 分钟进缓存。所以窗口实际是 **北京 11:10–13:10**。

**三者并不同时对齐**：信号锚点 10:00、回测成交价 11:00、守护进程下单 ≈11:10。
最后那 10 分钟是缓存落地延迟，不是配置能改的。定时器的职责只是
"确保那一刻守护进程是在跑的" —— 它**不需要**卡在 11:05，但必须保证 11:10
之前进程已经起来，否则当天整天静默跳过（日志里只有 `not_due`，看不出异常）。

⚠️ 因此 `--refresh-after-hours` **必须小于一根 bar**：到期窗口只有 2 根 bar 宽，
漏掉一个 tick 会让面板一次前进 2 根，窗口被跳过而毫无提示。守护进程默认值
已是 1.0（每个 tick 都刷新）。

定时器模式（默认，`--mode timer`）
    在 `--at`（默认 11:05）拉起守护进程，守护进程按自己的窗口判据决定当天该
    不该调仓。**不偏离回测口径。** 11:05 早于窗口（11:10），这正是想要的：
    提前把进程拉起来，让它自己等到窗口。
    为什么不是 10:00：10:00 时那根 bar 刚收盘、还没进缓存，面板还停在上一根，
    `bars_since = 23`，窗口未开，拉起来也只能空转。

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

#: One label **per trading mode**.  Installing the live timer must not silently
#: overwrite the demo one: they are different jobs (different money, different
#: limits file), and a shared label would make the second install replace the
#: first -- leaving one of them unguarded while `--status` still looked healthy.
LABELS = {
    "demo": "com.crypto-ls.auto-trader-demo",
    "paper": "com.crypto-ls.auto-trader-paper",
    "live": "com.crypto-ls.auto-trader-live",
}
UNITS = {
    "demo": "crypto-ls-auto-trader-demo",
    "paper": "crypto-ls-auto-trader-paper",
    "live": "crypto-ls-auto-trader-live",
}

#: Default install target.  Kept as `demo` so the documented command in
#: `AUTO_TRADER_README.md` keeps meaning what it always meant.
MODE = "demo"
REBALANCE_DAYS = 1.0
INTERVAL_MIN = 30.0
BAR = "1h"

#: Back-compat module constants for the demo job (imported by tests / scripts).
LABEL = LABELS["demo"]
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
SYSTEMD_DIR = os.path.expanduser("~/.config/systemd/user")
SYSTEMD_UNIT = UNITS["demo"]


def label_for(trading_mode: str) -> str:
    return LABELS[trading_mode]


def plist_path_for(trading_mode: str) -> str:
    return os.path.expanduser(
        f"~/Library/LaunchAgents/{LABELS[trading_mode]}.plist")


def unit_name_for(trading_mode: str) -> str:
    return UNITS[trading_mode]


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
def _runner_args(hour: int, minute: int, mode: str, window_tol: float,
                 trading_mode: str, allow_live: bool) -> list:
    """The argv the scheduler invokes, for any platform.

    `--allow-live` is passed **only** when the caller asked for it *and* the mode
    is `live`.  Emitting it unconditionally would mean the flag is on by default
    in the one place that matters most (an unattended timer), which is exactly
    what the flag exists to prevent.
    """
    args = [f"--mode", mode, "--trading-mode", trading_mode,
            "--rebalance-days", "1"]
    if mode == "window":
        args += ["--exec-window", f"{hour:02d}:{minute:02d}",
                 "--exec-window-tol", str(window_tol)]
    if trading_mode == "live" and allow_live:
        args.append("--allow-live")
    return args


def plist_xml(py: str, hour: int, minute: int, mode: str, window_tol: float,
              trading_mode: str = MODE, allow_live: bool = False) -> str:
    """launchd 任务。`StartCalendarInterval` 在错过时间后会在唤醒时补跑一次。"""
    args = [py, runner_path()] + _runner_args(hour, minute, mode, window_tol,
                                             trading_mode, allow_live)
    arg_xml = "\n".join(f"    <string>{a}</string>" for a in args)
    log_dir = os.path.join(ROOT, "logs")
    return textwrap.dedent(f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key>
      <string>{label_for(trading_mode)}</string>
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
      <string>{log_dir}/scheduler-{trading_mode}.out.log</string>
      <key>StandardErrorPath</key>
      <string>{log_dir}/scheduler-{trading_mode}.err.log</string>
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


def systemd_unit(py: str, hour: int, minute: int, mode: str, window_tol: float,
                 trading_mode: str = MODE, allow_live: bool = False) -> str:
    args = [py, runner_path()] + _runner_args(hour, minute, mode, window_tol,
                                              trading_mode, allow_live)
    cmd = " ".join(args)
    return textwrap.dedent(f"""\
    [Unit]
    Description=Crypto LS {trading_mode} auto-trader daily tick
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


def systemd_timer(hour: int, minute: int, trading_mode: str = MODE) -> str:
    return textwrap.dedent(f"""\
    [Unit]
    Description=Daily {hour:02d}:{minute:02d} tick for the crypto LS {trading_mode} auto-trader

    [Timer]
    OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
    Persistent=true
    Unit={unit_name_for(trading_mode)}.service

    [Install]
    WantedBy=timers.target
    """)


# ---------------------------------------------------------------------------
def install_macos(py: str, hour: int, minute: int, mode: str, tol: float,
                  trading_mode: str = MODE, allow_live: bool = False) -> int:
    plist = plist_path_for(trading_mode)
    label = label_for(trading_mode)
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(plist, "w", encoding="utf-8") as f:
        f.write(plist_xml(py, hour, minute, mode, tol, trading_mode, allow_live))
    print(f"[OK] 写入 {plist}")
    # bootout first: loading over an existing job silently keeps the OLD plist.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[!!] launchctl bootstrap 失败：{r.stderr.strip()}")
        print("     可先用 launchctl load -w 试：")
        print(f"       launchctl load -w {plist}")
        return 1
    print(f"[OK] 已注册 launchd 任务 {label}")
    print(f"     每天 {hour:02d}:{minute:02d} 触发（休眠唤醒后会补跑一次）")
    print(f"     查看：launchctl print gui/{os.getuid()}/{label}")
    return 0


def install_linux(py: str, hour: int, minute: int, mode: str, tol: float,
                  trading_mode: str = MODE, allow_live: bool = False) -> int:
    unit = unit_name_for(trading_mode)
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    svc = os.path.join(SYSTEMD_DIR, f"{unit}.service")
    tmr = os.path.join(SYSTEMD_DIR, f"{unit}.timer")
    with open(svc, "w", encoding="utf-8") as f:
        f.write(systemd_unit(py, hour, minute, mode, tol, trading_mode, allow_live))
    with open(tmr, "w", encoding="utf-8") as f:
        f.write(systemd_timer(hour, minute, trading_mode))
    print(f"[OK] 写入 {svc}")
    print(f"[OK] 写入 {tmr}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now",
                        f"{SYSTEMD_UNIT}.timer"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[!!] systemctl 失败：{r.stderr.strip()}")
        return 1
    print(f"[OK] 已启用 systemd timer {unit}.timer")
    print(f"     每天 {hour:02d}:{minute:02d} 触发（Persistent=true，错过后补跑）")
    return 0


def uninstall(trading_mode: str = MODE) -> int:
    rc = 0
    if sys.platform == "darwin":
        label = label_for(trading_mode)
        plist = plist_path_for(trading_mode)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True)
        if os.path.exists(plist):
            os.remove(plist)
            print(f"[OK] 已删除 {plist}")
        else:
            print("（plist 本来就不存在）")
    else:
        unit = unit_name_for(trading_mode)
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        f"{unit}.timer"], capture_output=True)
        for p in (os.path.join(SYSTEMD_DIR, f"{unit}.service"),
                  os.path.join(SYSTEMD_DIR, f"{unit}.timer")):
            if os.path.exists(p):
                os.remove(p)
                print(f"[OK] 已删除 {p}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return rc


def status(trading_mode: str = MODE) -> int:
    rel = os.path.relpath(os.path.abspath(__file__), ROOT)
    if sys.platform == "darwin":
        label = label_for(trading_mode)
        r = subprocess.run(["launchctl", "print",
                            f"gui/{os.getuid()}/{label}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[--] launchd 任务未注册（{trading_mode}）")
            print(f"     安装：python3 {rel} --install --trading-mode {trading_mode}")
            if trading_mode == "live":
                print(f"           （实盘还要加 --allow-live）")
            return 1
        print(f"[OK] {trading_mode} 定时器已注册：{label}")
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.startswith(("state =", "last exit code =", "runs =",
                             "program =", "arguments =")):
                print(f"     {s}")
            elif "Hour" in s or "Minute" in s:
                print(f"     {s}")
        return 0
    unit = unit_name_for(trading_mode)
    r = subprocess.run(["systemctl", "--user", "status", f"{unit}.timer",
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
    ap.add_argument("--trading-mode", default=MODE,
                    choices=["demo", "paper", "live"],
                    help="这个定时器拉起的**交易账户**：demo（默认，模拟盘）/ "
                         "paper / live（实盘，**真钱**，必须同时给 --allow-live）。"
                         "每个模式有独立的 launchd label，装 live 不会覆盖 demo。")
    ap.add_argument("--allow-live", action="store_true",
                    help="实盘定时器必须显式带上。这是「由人明确打开一次」的开关："
                         "装进去意味着**每天**无人值守地下真钱单。"
                         "它只开「允许下单」这道门，不放宽任何限额。")
    a = ap.parse_args(argv)

    if not (a.install or a.uninstall or a.status):
        ap.print_help()
        return 2

    if a.uninstall:
        return uninstall(a.trading_mode)
    if a.status:
        return status(a.trading_mode)

    # 实盘守卫放在解析之后、**任何写盘之前**：装一个起不来的定时器比不装更糟
    # ——它每天准点失败，而 `--status` 显示"已注册"。
    if a.trading_mode == "live" and not a.allow_live:
        print("!! --trading-mode live 需要同时显式给出 --allow-live。")
        print("   装进定时器意味着每天无人值守地下真钱单，不能靠默认值打开。")
        print(f"   命令：python3 {os.path.relpath(os.path.abspath(__file__), ROOT)} "
              f"--install --trading-mode live --allow-live --at {a.at}")
        return 2

    hh, mm = a.at.split(":")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        print(f"!! --at 超出范围：{a.at}")
        return 2
    py = a.python or find_python()
    print(f"解释器 : {py}")
    print(f"项目    : {ROOT}")
    print(f"账户    : {a.trading_mode}"
          + ("   ★★★ 实盘 · 真钱 ★★★" if a.trading_mode == "live" else ""))
    print(f"label   : {label_for(a.trading_mode)}")
    print(f"触发    : 每天 {hour:02d}:{minute:02d}"
          "（拉起守护进程；**下单时刻**由回测网格决定，不由这里决定）")
    print(f"模式    : {a.mode}"
          + ("（不偏离回测口径）" if a.mode == "timer"
             else "（⚠ 偏离回测网格，先看 phase_sweep.csv）"))
    print()
    if sys.platform == "darwin":
        return install_macos(py, hour, minute, a.mode, a.exec_window_tol,
                             a.trading_mode, a.allow_live)
    if sys.platform.startswith("linux"):
        return install_linux(py, hour, minute, a.mode, a.exec_window_tol,
                             a.trading_mode, a.allow_live)
    print(f"!! 不支持的平台 {sys.platform}；Windows 请用 START_AUTO_TRADER_DEMO.bat")
    return 2


if __name__ == "__main__":
    sys.exit(main())
