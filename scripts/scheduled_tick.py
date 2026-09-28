#!/usr/bin/env python3
"""定时器的一次性执行体：确保模拟盘自动交易在跑（幂等），然后退出。

由 `install_scheduler.py` 注册的 launchd / systemd 任务调用，也可以手动跑
（`--once` 语义），也可以被 Windows 的任务计划程序调用。

为什么是"确保在跑"而不是"下一单"
----------------------------------
两种职责必须分开：
  * **进程存活**是定时器的职责。笔记本休眠、系统更新重启、OOM 杀进程，
    都会让守护进程消失，而"昨天还在跑"和"三天前就死了"从外面看一模一样。
  * **是否调仓**是守护进程的职责（`not_due` 闸门 + 执行窗口）。

定时器只在守护进程**不在**时拉起它；已在跑就什么都不做。这样：
  - 到点不会产生第二个守护进程（两个循环会各自读同一本书、各下全量单）；
  - 每天到点重启一次也不是"刷新状态"，而是幂等的 no-op。

`--mode window` 时额外要求守护进程带执行窗口跑：如果当前跑着的守护进程没带
窗口，会**先停后起**（否则窗口参数永远不会生效，而日志里看不出区别）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

MODE = "demo"
REBALANCE_DAYS = 1.0
INTERVAL_MIN = 30.0
BAR = "1h"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="定时器执行体：确保自动交易在跑")
    ap.add_argument("--mode", default="timer", choices=["timer", "window"])
    ap.add_argument("--exec-window", default=None, metavar="HH:MM")
    ap.add_argument("--exec-window-tol", type=float, default=5.0)
    ap.add_argument("--trading-mode", default=MODE, choices=["demo", "paper", "live"])
    ap.add_argument("--rebalance-days", type=float, default=REBALANCE_DAYS)
    ap.add_argument("--force-restart", action="store_true",
                    help="无论是否在跑都先停后起（用于改参数后强制生效）")
    a = ap.parse_args(argv)

    from crypto_ls_research.execution import auto_ctl

    want_window = a.exec_window if a.mode == "window" else None
    m = a.trading_mode

    running = auto_ctl.is_running(m)
    st = auto_ctl.read_state(m) if running else {}
    cur_window = st.get("exec_window")

    if running and not a.force_restart:
        if want_window and not cur_window:
            # 参数变更：旧进程没带窗口，留着它就等于窗口永远不生效。
            # 必须停掉再起，否则"装了窗口但没生效"会静默持续下去。
            print(f"[!!] {m} 在跑但没带执行窗口（pid {st.get('pid')}）；"
                  f"停掉后用窗口参数重启。")
            auto_ctl.stop(m, wait_sec=10.0)
        else:
            print(f"[OK] {m} 已在运行（pid {st.get('pid')}，"
                  f"窗口 {cur_window or '无'}），本次不做事。")
            print(json.dumps({"action": "noop", "running": True,
                              "pid": st.get("pid"),
                              "exec_window": cur_window}, ensure_ascii=False))
            return 0
    elif running and a.force_restart:
        print(f"[--] --force-restart：先停掉 {m}（pid {st.get('pid')}）")
        auto_ctl.stop(m, wait_sec=10.0)

    print(f"[>>] 启动 {m}（rebalance_days={a.rebalance_days:g}，"
          f"窗口 {want_window or '无'}）")
    r = auto_ctl.start(m, rebalance_days=a.rebalance_days,
                       interval_min=INTERVAL_MIN, bar=BAR,
                       dry_run=False, allow_live=False,
                       exec_window=want_window,
                       exec_window_tol=a.exec_window_tol,
                       exec_window_utc=False)
    if not r.get("ok"):
        print(f"[FAIL] {r.get('error')}")
        return 5

    w = auto_ctl.wait_for_heartbeat(m, timeout=90.0)
    if not w.get("ok"):
        print(f"[FAIL] 起了进程但没心跳：{w.get('error')}")
        return 6
    st = auto_ctl.read_state(m)
    print(f"[OK] 守护进程已确认：pid {st.get('pid')} · "
          f"网格 {st.get('rebalance_days')} 天 · 窗口 {st.get('exec_window') or '无'}")
    print(json.dumps({"action": "started", "pid": st.get("pid"),
                      "rebalance_days": st.get("rebalance_days"),
                      "exec_window": st.get("exec_window")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
