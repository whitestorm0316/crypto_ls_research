"""Double-click launcher for the demo (simulated-account) auto-trader.

Why this exists instead of the `.bat` calling `auto_ctl.start()` directly
------------------------------------------------------------------------
`auto_ctl.start()` spawns a **detached** daemon and returns immediately.  A
`.bat` that did only that would show a console window that flashed and closed,
leaving the user with no evidence anything happened -- indistinguishable from a
crash on import.  So this script does three things the `.bat` cannot:

1. **Starts, then verifies.**  `wait_for_heartbeat()` is the only thing that
   turns "we spawned a process" into "a daemon is reporting in".  A start that
   is not confirmed is reported as a failure, not as success.
2. **Fails loudly before trading.**  It refuses to start if the notify config is
   unset (a silent desk is the failure mode this whole exercise guards against)
   and warns if the grid differs from the requested one.
3. **Stays in the foreground afterwards**, tailing the audit log, so the window
   is worth looking at.  Closing the window does **not** kill the daemon -- that
   is the point of the detached spawn.

The daemon it starts is mode=demo: OKX's simulated account, real matching
against live prices, no real money.  `allow_live` is never passed.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

# `PYTHONUNBUFFERED` is set by the .bat, but honour it here too so the banner
# appears immediately even when launched straight from a shell.
os.environ.setdefault("PYTHONUNBUFFERED", "1")

#: 调仓网格。**取已验收口径本身**，不在这里再写一个字面量：这个数同时被
#: `engine.DEFAULT_SIGNAL`（交易台的计划）、`webapp/spec.OPTIMAL_CLI`（控制台的
#: 「已验收最优」）和守护进程读。曾经 `DEFAULT_SIGNAL` 写 3.0、这个脚本写 1.0、
#: 守护进程命令行又传 1，三处各有一个数，结果是守护进程每天调仓、交易台却生成
#: 3 天的计划并显示「下次调仓」在 3 天后 —— 没有任何地方报错。
from crypto_ls_research.config.settings import (  # noqa: E402
    ACCEPTED_REBALANCE_DAYS as REBALANCE_DAYS)

MODE = "demo"
INTERVAL_MIN = 30.0
BAR = "1h"
#: 数据落后超过这么多小时就先增量刷新。**必须小于一根 bar。**
#:
#: `bar_age_hours` 量的是 bar 的**开盘**时间（`engine.py` 里 `newest_cached_bar`
#: 返回的是 bar 标签 = 开盘时刻），所以刷新刚做完时 `bar_age` 就已经 ≈1.17h。
#: 阈值设 1.0 的效果不是"每小时刷新"而是"每个 tick 都刷新"；设 2.0 才是每小时。
#:
#: 为什么必须小于一根 bar：到期窗口只有 **2 根 bar 宽** ——
#: `rebalance_window_open` 在 `bars_since_decision == 1` 时为真，而补了「执行
#: bar」之后这个状态横跨两个面板状态（末尾落在网格点 G、以及落在它后一根 G+1），
#: 两态是同一笔决策、互为幂等复查。面板每刷新一次最多前进 1 根 bar，所以刷新间隔
#: 必须 < 1h：到了 1h，漏掉一个 tick 就会让面板一次前进 2 根，`since` 从 0 直接
#: 跳到 2，**整个窗口被跨过去，整天不成交而日志全是 `not_due`**。
REFRESH_AFTER_HOURS = 1.0
#: 执行窗口（本地时间 HH:MM）。**留空 = 不强制，严格跟随回测网格。**
#:
#: 当前默认留空：守护进程的自然到期闸门已经按回测口径打开，不需要窗口来「挪」
#: 下单时刻。但**下单时刻并不等于回测的执行价时刻**，这点必须说清楚：
#:
#:   信号 bar    UTC 02:00 = 北京 10:00（网格锚点，`dec_idx` 决定）
#:   回测执行价  UTC 03:00 = 北京 11:00（`exec_price = next_open`）
#:   守护进程    ≈ 北京 11:10 打开窗口（实测）
#:
#: 2026-09-29 修掉了链条末端两处各自 1 小时的**人为**延迟（此前窗口 = 北京
#: 13:10–14:10）：
#:   ① `load_panels` 固定丢掉缓存里最新那根（`data/store.py` 的
#:      `inclusive="left"` off-by-one）→ 实盘路径改走 `extend_to_last=True`；
#:   ② `dec_idx` 结构上够不到面板最后一行，所以网格点自己那根记不了账 →
#:      `signal.book_with_execution_bar` 在需要时补一根「执行 bar」。
#: 两处都不改回测入口，所以 v3 的存档数字不动。
#:
#: 修完窗口 = **北京 11:10–13:10**（2 根 bar 宽：面板末尾落在网格点、以及落在它
#: 后一根，两态同解），30 分钟 tick 有 4 次机会。此前只有 1 根 bar 宽、2 次机会。
#:
#: 反过来，若把窗口设成 10:00，会**强行覆盖**到期闸门：10:00 时最新决策点
#: 就是刚产生的那根 bar，窗口每天都会照发一遍同样的目标书，把换手预算
#: 烧在无谓的重复交易上，且日志里看起来完全正常。
#:
#: 只有一种情况需要它：想让下单发生在**信号日之外**的时刻（例如 23:30），
#: 那就必须接受信号变旧 11~13.5 小时、绩效不再是 v3 口径。
#: 实测代价见 `artifacts/grid_phase/phase_sweep.csv`。
EXEC_WINDOW = os.environ.get("EXEC_WINDOW", "")
EXEC_WINDOW_TOL = float(os.environ.get("EXEC_WINDOW_TOL", "5"))


def _hr(title: str) -> None:
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


def main(argv: list) -> int:
    from crypto_ls_research.execution import auto_ctl
    from crypto_ls_research.execution.notify import Notifier
    from crypto_ls_research.execution.engine import LiveEngine

    dry_run = "--dry-run" in argv

    _hr("自动交易 · 模拟盘（demo）")
    print(f"  模式        : {MODE}（OKX 模拟盘，真实撮合，不花真钱）")
    print(f"  调仓网格    : 每 {REBALANCE_DAYS:g} 天一次")
    print(f"  检查间隔    : 每 {INTERVAL_MIN:g} 分钟")
    print(f"  刷新阈值    : 落后 {REFRESH_AFTER_HOURS:g} 小时就增量刷新"
          f"（{'每个 tick 都刷新' if REFRESH_AFTER_HOURS <= 1.0 else '约每 '
             f'{REFRESH_AFTER_HOURS:g} 小时'}）")
    print(f"  执行窗口    : "
          + (f"{EXEC_WINDOW} 本地时间（±{EXEC_WINDOW_TOL:g} 分钟）—— 偏离回测网格"
             if EXEC_WINDOW else "无（严格跟随回测网格）"))
    print(f"  dry-run     : {'是（只算不下单）' if dry_run else '否'}")
    if EXEC_WINDOW:
        print()
        print("  ⚠ 执行窗口已开启：到点会**强制**调仓一次，即信号日仍是"
              " UTC02:00（北京10:00）")
        print("    那个 bar，下单却发生在窗口时刻。这偏离回测网格，绩效不再是"
              " v3 口径。")
        print("    实测代价见 artifacts/grid_phase/phase_sweep.csv。"
              "关掉：set EXEC_WINDOW=")
    else:
        print()
        print("  下单时刻 = 信号产生后立即执行（与回测口径一致）：")
        print("    信号 bar   北京 10:00（UTC02:00）—— 回测网格锚点")
        print("    回测执行价 北京 11:00 —— 下一根 bar 的开盘价")
        print("    实际下单   ≈ 北京 11:10 —— 网格点那根 bar 一收盘，"
              "实盘路径就补一根执行 bar 把账记上")
        print("    窗口 11:10–13:10（2 根 bar 宽），30 分钟 tick 有四次机会。")
    print()

    # --- 1. notify must be live, or the desk is silent -------------------
    nd = Notifier.from_config(mode=MODE).describe()
    if nd.get("ready"):
        print(f"  [OK]   通知已启用：{nd.get('provider')}")
        print(f"         推送时机 {nd.get('notify_on')}，模式 {nd.get('modes')}")
    else:
        print("  [FAIL] 通知未配置。无人值守的盘必须能报警，先修好再启动：")
        print("         python -m crypto_ls_research.execution.notify "
              "--set-url <webhook>")
        return 2
    print()

    # --- 2. the account must be reachable --------------------------------
    print("  正在连接 OKX 模拟盘…")
    try:
        acct = LiveEngine(mode=MODE).account()
    except Exception as e:                                     # noqa: BLE001
        print(f"  [FAIL] 读不到模拟盘账户：{type(e).__name__}: {str(e)[:300]}")
        print("         检查 config/okx_creds.json 里的 demo 密钥是否有效。")
        return 3
    nav = acct.get("nav") or 0.0
    if nav <= 0:
        print(f"  [FAIL] 净值读取为 {nav}，无法交易。")
        return 3
    print(f"  [OK]   净值 {nav:,.2f} USDT · 现有持仓 "
          f"{len(acct.get('cur_sz') or {})} 个 · {acct.get('pos_mode')}")
    print()

    # --- 3. already running? --------------------------------------------
    if auto_ctl.is_running(MODE):
        st = auto_ctl.read_state(MODE)
        print(f"  [!!]   {MODE} 已有自动任务在跑（pid {st.get('pid')}）。")
        print("         先停掉它再启动，否则会出现两个循环。")
        print(f"         停止：python -m crypto_ls_research.execution.auto_ctl "
              f"--stop --mode {MODE}")
        return 4

    # --- 4. start, then CONFIRM -----------------------------------------
    print("  正在启动守护进程…")
    r = auto_ctl.start(MODE, rebalance_days=REBALANCE_DAYS,
                       interval_min=INTERVAL_MIN, bar=BAR, dry_run=dry_run,
                       exec_window=EXEC_WINDOW or None,
                       exec_window_tol=EXEC_WINDOW_TOL,
                       exec_window_utc=False,
                       refresh_after_hours=REFRESH_AFTER_HOURS)
    if not r.get("ok"):
        print(f"  [FAIL] 启动失败：{r.get('error')}")
        return 5

    w = auto_ctl.wait_for_heartbeat(MODE, timeout=45.0)
    if not w.get("ok"):
        print(f"  [FAIL] 进程起了但没写心跳：{w.get('error')}")
        print("         看 logs / artifacts/live/demo/auto_stdout.log")
        return 6

    st = auto_ctl.read_state(MODE)
    ctl = auto_ctl.read_ctl(MODE)
    got = st.get("rebalance_days")
    print(f"  [OK]   守护进程已确认：pid {st.get('pid')}")
    print(f"         控制文件 pid {ctl.get('pid')}（kind={ctl.get('pid_kind')}）")
    if got is not None and abs(float(got) - REBALANCE_DAYS) > 1e-9:
        print(f"  [!!]   网格不一致：请求 {REBALANCE_DAYS:g} 天，"
              f"守护进程报 {float(got):g} 天")
    else:
        print(f"  [OK]   调仓网格已确认：每 {float(got):g} 天"
              if got is not None else "  [!!]   心跳未报网格")
    # 窗口也必须从心跳里读回来确认，而不是回显我们请求的值：
    # 「装了窗口但没生效」是静默失败，只能在心跳里看出来。
    got_w = st.get("exec_window")
    if EXEC_WINDOW:
        if got_w:
            print(f"  [OK]   执行窗口已确认：{got_w}")
        else:
            print("  [!!]   请求了执行窗口，但守护进程心跳里没有 —— "
                  "窗口不会生效，去查 auto_stdout.log")
    else:
        print("  [--]   执行窗口：未启用（严格跟随回测网格）")
    print()
    print("  守护进程已独立运行，关掉这个窗口它不会停。")
    print("  停止方式：python -m crypto_ls_research.execution.auto_ctl "
          "--stop --mode demo")
    print()

    # --- 5. tail the audit log so the window is useful -------------------
    log = st.get("log_path") or os.path.join(
        ROOT, "artifacts", "live", MODE, "auto.jsonl")
    _hr(f"实时日志：{log}    （Ctrl+C 只关窗口，不停守护进程）")
    pos = 0
    try:
        while True:
            try:
                with open(log, encoding="utf-8") as f:
                    f.seek(pos)
                    for line in f:
                        pos = f.tell()
                        print("  " + line.rstrip()[:300])
            except OSError:
                pass
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n  （窗口已关闭；守护进程仍在运行）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
