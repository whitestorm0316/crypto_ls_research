"""宇宙补齐：把「宇宙体检」报出的缺口真正修掉的执行器。

与 `universe_drift`（只读）不同，本模块**有副作用**，只应被一个显式动作触发
（控制台的「补齐宇宙」按钮 / CLI）。它按顺序跑四步，任何一步失败即停：

1. `data.download` —— **重建**候选池（**不带** `--reuse-pool`）+ 增量下载。
   增量模式对新合约是正确的：盘上没文件的 symbol 会从池起点拉全史，老 symbol
   只补尾巴（`download_candles`/`download_funding` 的 `_last_ts()==None` 路径）。
2. `data.asset_class --build` —— 重建分类快照（缺分类是 resolve_insts 的
   fail-loud 陷阱）。
3. `specs.refresh_specs()` —— 刷新规格快照（缺规格 ⇒ planner 按 no_spec 跳过）。
4. `data.funding_build` —— 重建拼接资金费率（`funding_hyb/` 是 live 面板默认
   读取的目录；新合约没有它，funding 因子就是 NaN、进不了池）。

完成后前后各跑一次 `drift_report(force=True)`，让用户直接看到缺口收敛。

宇宙变了 = 策略变了：本模块**故意不**重建基线产物、不重算信号——那是人的
验收决定（数字要重新记录），不是一键自动化的范围。结束的 progress 行会把
下一步命令打印出来。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

BARS = ("1h", "15m", "5m")          # 数据流水线的三档 bar（与 init_linux.sh 一致）
DOWNLOAD_TIMEOUT = 2 * 3600.0       # 新合约多、网络差时可能很慢；给足
STEP_TIMEOUT = 1800.0


STREAM_MIN_GAP = 1.5        # 秒：两条流式行之间的最小间隔（防止刷屏）
STREAM_MAX_LINES = 120      # 每个子进程最多转发这么多行（之后只发心跳）
HEARTBEAT_EVERY = 30.0      # 秒：无输出时的心跳，证明还活着


def _default_runner(argv: List[str], timeout: float,
                    progress: Callable[[str], None]) -> Tuple[int, str]:
    """跑一个子进程，**边跑边把输出转发给 progress**，返回 (exit_code, 尾部若干行)。

    🔴 这里曾经是 `subprocess.run(capture_output=True)`：子进程的 stdout 被
    **整段缓冲**，直到它结束才一次性返回。而 ① 下载（十几分钟）与 ④ 重建
    funding（十几分钟）恰恰是最长的两步 ⇒ 界面上只有一行「…」，和卡死长得
    一模一样（用户实测：「几十分钟还没好」）。**没有输出的长任务必须能自证
    还活着**：这里改成 Popen + 逐行转发 + 30 秒心跳。

    环境处理与 `engine.refresh_market_data` 相同：清掉会劫持 127.0.0.1 的
    HTTP(S)_PROXY（沙箱遗留物），保留显式的 `OKX_PROXY`（三个客户端都读它）。
    `PYTHONUNBUFFERED=1` 是必须的：否则子进程的 print 仍会攒满 8KB 才吐出来，
    逐行转发就退化成「最后一次性倒出来」。
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8:replace",
           "PYTHONUNBUFFERED": "1"}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)

    t0 = time.time()
    tail: List[str] = []
    state = {"last": 0.0, "n": 0}

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=env, bufsize=1)

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:                    # 行缓冲：子进程一吐就拿到
            line = raw.rstrip("\n").strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]                          # 只留尾部，供失败时回报
            now = time.time()
            if state["n"] >= STREAM_MAX_LINES:
                continue
            if now - state["last"] < STREAM_MIN_GAP:
                continue
            state["last"] = now
            state["n"] += 1
            progress("  " + line[-180:])

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    deadline = t0 + timeout
    beat = time.time()
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if time.time() > deadline:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(argv, timeout)
        if time.time() - beat >= HEARTBEAT_EVERY:
            beat = time.time()
            progress(f"  … 运行中 {int(time.time() - t0)}s（尚未结束）")
        time.sleep(0.5)
    th.join(timeout=5)

    out = "\n".join(tail[-8:])
    if rc != 0:
        out += f"\n[exit {rc}] 见上方日志"
    return rc, out


def _steps(bars: Tuple[str, ...], end: str) -> List[dict]:
    """步骤表。`--reuse-pool` 的缺席是**刻意的**，也是测试要钉住的重点。"""
    return [
        {"label": "① 重建候选池并增量下载 K 线 + OKX 资金费率",
         "argv": [sys.executable, "-m", "crypto_ls_research.data.download",
                  "--bars", *bars, "--incremental", "--end", end],
         "hint": "新合约拉全史，通常 10–30 分钟（取决于新合约数量与网络）",
         "timeout": DOWNLOAD_TIMEOUT},
        {"label": "② 重建合约分类快照",
         "argv": [sys.executable, "-m", "crypto_ls_research.data.asset_class",
                  "--build"],
         "hint": "通常几秒",
         "timeout": STEP_TIMEOUT},
        {"label": "③ 刷新合约规格快照",
         "fn": "specs",
         "hint": "通常几秒"},
        {"label": "④ 重建拼接资金费率（funding_hyb）",
         "argv": [sys.executable, "-m", "crypto_ls_research.data.funding_build",
                  "--end", end],
         "hint": ("全量重建：要拉全部合约的 Binance funding 历史，"
                  "通常 5–20 分钟，且产物在**最后一次性**写出"),
         "timeout": STEP_TIMEOUT},
    ]


def upgrade_universe(progress: Optional[Callable[[str], None]] = None, *,
                     bars: Tuple[str, ...] = BARS, end: Optional[str] = None,
                     runner: Optional[Callable[..., Tuple[int, str]]] = None,
                     specs_fn: Optional[Callable[[], int]] = None,
                     drift_fn: Optional[Callable[..., dict]] = None) -> dict:
    """跑完整套补齐。**返回值永不缺 ok 键**；失败即停并写明卡在哪一步。

    注入缝（测试不碰真网络/真子进程）：`runner`（子进程替身）、`specs_fn`
    （refresh_specs 替身）、`drift_fn`（体检替身）。
    """
    p = progress or (lambda m: None)
    run_proc = runner or _default_runner
    do_specs = specs_fn or _default_specs_fn
    drift = drift_fn or _default_drift_fn
    end = end or time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))

    out: Dict = {"ok": False, "steps": [], "before": None, "after": None,
                 "error": None}

    p("体检（补齐前）…")
    out["before"] = drift(force=True)
    if out["before"].get("ok"):
        p(f"  {out['before']['summary']}")

    for st in _steps(bars, end):
        p(st["label"] + " …")
        if st.get("hint"):
            p("  " + st["hint"])       # 先说清「大概多久」，否则长步骤像卡死
        rec: Dict = {"label": st["label"], "exit_code": None, "tail": ""}
        if st.get("fn") == "specs":
            try:
                n = do_specs()
                rec["exit_code"] = 0
                rec["tail"] = f"refresh_specs: {n} instruments"
                p(f"  ✓ 规格 {n} 只")
            except Exception as e:                          # noqa: BLE001
                rec["exit_code"] = -1
                rec["tail"] = f"{type(e).__name__}: {e}"
        else:
            try:
                rc, tail = run_proc(st["argv"], st["timeout"], p)
                rec["exit_code"] = rc
                rec["tail"] = tail
            except subprocess.TimeoutExpired:
                rec["exit_code"] = -1
                rec["tail"] = f"超时（>{st['timeout']:.0f}s）"
            except Exception as e:                          # noqa: BLE001
                rec["exit_code"] = -1
                rec["tail"] = f"{type(e).__name__}: {e}"
        for line in rec["tail"].splitlines()[-4:]:
            p("  " + line)
        out["steps"].append(rec)
        if rec["exit_code"] != 0:
            out["error"] = (f"步骤失败：{st['label']}（exit={rec['exit_code']}）。"
                            f"修好后重跑本动作即可，已完成的步骤会增量跳过。")
            p("✗ " + out["error"])
            return out

    p("体检（补齐后）…")
    out["after"] = drift(force=True)
    if out["after"].get("ok"):
        p(f"  {out['after']['summary']}")
        gaps = (out["after"]["n_new"] + out["after"]["n_dead"]
                + out["after"]["n_unclassified"] + out["after"]["n_missing_specs"])
        out["ok"] = True
        if gaps == 0:
            p("✓ 宇宙缺口已清零。下一步：重算信号（「生成下单计划」或一键准备）。")
        else:
            p(f"⚠ 仍有 {gaps} 条缺口（新合约需满 30 天历史才会进 PIT 池，属正常）。"
              f"下一步：重算信号，并重新验收数字（宇宙变了 = 策略变了）。")
    else:
        # 补齐成功但复查失败（网络抖动）：动作本身算成功，复查下次轮询会刷新。
        out["ok"] = True
        p(f"⚠ 补齐已完成，但复查体检失败：{out['after'].get('error')}")

    out["next"] = ("数字要重新验收：python scripts/rebuild_optimal_baseline.py --force "
                   "（或在报告页重跑验收），并把新数字记进 FINDINGS.md。")
    p("提醒：" + out["next"])
    return out


def _default_specs_fn() -> int:
    from .specs import refresh_specs
    return refresh_specs()


def _default_drift_fn(**kw) -> dict:
    from .universe_drift import drift_report
    return drift_report(**kw)


def main() -> None:                                            # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(
        description="宇宙补齐：重建池 + 分类/规格快照 + funding（有副作用）")
    ap.add_argument("--bars", nargs="*", default=list(BARS))
    ap.add_argument("--end", default=None, help="exclusive, YYYY-MM-DD")
    a = ap.parse_args()
    r = upgrade_universe(progress=print, bars=tuple(a.bars), end=a.end)
    import json as _json
    print(_json.dumps({k: r[k] for k in ("ok", "error")}, ensure_ascii=False))


if __name__ == "__main__":                                     # pragma: no cover
    main()
