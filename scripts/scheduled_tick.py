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

实盘（`--trading-mode live`）
------------------------------
`live` 会把**真钱**单子下出去，所以这里必须过一次 `--allow-live`：

  * 缺 `--allow-live` 时**拒绝启动**（退出码 3），把话说明白，而不是让
    `auto_ctl.start` 回一句「实盘自动交易需要显式确认」然后静静地什么都没发生。
    这是「由人明确打开一次」的开关，不是可以靠默认值绕过去的东西。
  * `--allow-live` 只往下传到守护进程，**不改任何风控**：`check_plan` 的每个
    `block` 仍然照常生效。它开的是"允许下单"这道门，不是"放宽限额"。
  * 装到定时器里意味着**每天**都会以这个身份起来。所以安装器（
    `install_scheduler.py --trading-mode live`）要求显式指定，且用**独立的
    label**，不会把 demo 的任务覆盖掉。
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
    ap.add_argument("--allow-live", action="store_true",
                    help="`--trading-mode live` 必须显式带上：定时器会每天无人"
                         "值守地下真钱单。只开「允许下单」这道门，不放宽任何限额。")
    a = ap.parse_args(argv)

    from crypto_ls_research.execution import auto_ctl

    want_window = a.exec_window if a.mode == "window" else None
    m = a.trading_mode

    # 能力/权限检查放在**任何破坏性步骤之前**：下面的 `force_restart` 会先停掉
    # 正在跑的守护进程，如果到那时才发现没有 `--allow-live`，就会把一个健康的
    # 账本停在一半 —— 最贵的失败时刻。所以先判，再动。
    if m == "live" and not a.allow_live:
        print("[拒绝] --trading-mode live 需要同时显式给出 --allow-live。")
        print("       定时器会在无人值守时下真钱单，这个开关必须由人明确打开一次。")
        print("       只开「允许下单」这道门；check_plan 的每个 block 仍然照常生效。")
        print("       安装：python3 scripts/install_scheduler.py --install "
              "--trading-mode live --allow-live --at HH:MM")
        return 3

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
    if m == "live":
        print("[!!] 实盘模式：订单将以**真实资金**成交。熔断开关仍可在控制台拉下。")
    r = auto_ctl.start(m, rebalance_days=a.rebalance_days,
                       interval_min=INTERVAL_MIN, bar=BAR,
                       dry_run=False, allow_live=bool(a.allow_live),
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
