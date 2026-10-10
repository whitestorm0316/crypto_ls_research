#!/bin/bash
# ============================================================================
#  安装/查看/卸载 **实盘** 自动交易定时器（macOS launchd）
# ============================================================================
#  为什么需要双击，而不能由 AI 代装：
#    `launchctl` 不能从工具调用/沙箱里注册 launchd job —— 实测连一个 5 行的
#    最小 plist 都报 `Bootstrap failed: 5: Input/output error`。所以注册这一步
#    必须由一个**真实登录会话**里的进程来做。双击本文件即可。
#
#  这个定时器做什么：
#    每天 11:01 拉起实盘守护进程（幂等：已在跑就什么都不做）。
#    ★ 11:01 是「拉起时刻」，不是「下单时刻」。下单时刻由回测网格决定：
#      信号北京 10:00 产生 → 回测成交价取 11:00 那根 bar 的开盘 → 守护进程的
#      到期窗口实际在 11:10 打开。11:01 起来后它会自己空转到窗口开门。
#
#  安全性：实盘需要 `--allow-live`（本脚本已带）。它只开「允许下单」这道门，
#  不放宽任何限额 —— check_plan 的每个 block 仍然照常生效。
# ============================================================================
set -u

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LABEL="com.crypto-ls.auto-trader-live"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# 选解释器：判据是「能 import numpy/pandas」，不是「可执行」。
# 踩过的坑：候选表里写过 `~/.workbuddy/binaries/...`，真路径是
# `~/.workbuddy-ai/binaries/...`（少一个 -ai），于是静默回退到一个没有 numpy 的
# 解释器，报错要等到 `import numpy` 才出现，完全指不到「解释器选错了」。
PY=""
for c in \
  "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python" \
  /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import numpy, pandas' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "!! 找不到能 import numpy/pandas 的 python3。"
  echo "   试过：~/.workbuddy-ai/binaries/python/envs/default/bin/python, /opt/homebrew/bin/python3 等"
  echo "   用 PY=/path/to/python3 指定，或先装依赖。"
  exit 2
fi

echo "解释器 : $PY"
echo "项目   : $ROOT"
echo

echo "=== 1/3 写入并注册 launchd 任务 ==="
"$PY" scripts/install_scheduler.py --install --trading-mode live --allow-live --at 11:01
RC=$?
if [ $RC -ne 0 ]; then
  echo
  echo "!! 注册失败（退出码 $RC）。plist 可能已写好但没加载。"
  echo "   手动再试一次："
  echo "     launchctl bootstrap gui/$(id -u) \"$PLIST\""
  echo "   或（老写法）："
  echo "     launchctl load -w \"$PLIST\""
  exit $RC
fi

echo
echo "=== 2/3 状态 ==="
"$PY" scripts/install_scheduler.py --status --trading-mode live

echo
echo "=== 3/3 确认守护进程能被拉起（幂等，不会重复下单一本书）==="
# 同一本书下两次全量单是这套系统最贵的失败模式，所以这里只验证
# 「定时器会调用的那条命令」能跑通，**不真的**把守护进程留在后台
# ——真正的拉起交给定时器自己，或者你手动双击 START。
"$PY" - <<'PYEOF'
import json, subprocess, sys, os
# dry-run + --once：走完整条链（刷新行情 → 算信号 → 到期闸门 → 通知），
# 但 dry_run 下不可能下单。这就是「一键准备」的可验证版本。
r = subprocess.run(
    [sys.executable, "-m", "crypto_ls_research.execution.auto_trader",
     "--mode", "live", "--allow-live", "--dry-run", "--once"],
    capture_output=True, text=True)
# 取**最后一行独立的 `{`** 起的整块：日志行里也可能出现 `{`（比如刷新提示），
# 从第一个 `{` 开始解析会撞在日志上，报 "Expecting property name … char 1"。
lines = (r.stdout or "").splitlines()
start = max((i for i, l in enumerate(lines) if l.rstrip() == "{"), default=None)
tail = "\n".join(lines[start:]) if start is not None else ""
try:
    out = json.loads(tail)
    print(f"  结论        : {out.get('action')} / {out.get('reason')}")
    print(f"  信号日      : {out.get('decision_ts')}")
    print(f"  下次决策    : {out.get('next_decision_ts')}")
    print(f"  到调仓日    : {out.get('rebalance_due')}"
          f"  (已过 {out.get('bars_since_decision')}/{out.get('rebalance_bars')} 根 bar)")
    print(f"  目标毛敞口  : {out.get('target_gross')}")
    if out.get("not_due_explain"):
        print(f"  说明        : {out.get('not_due_explain')}")
    nd = out.get("notify") or {}
    print(f"  通知        : ok={nd.get('ok')} {nd.get('skipped') or nd.get('provider') or ''}")
except Exception as e:
    print("  （解析输出失败，原始尾部如下）")
    print(tail[-600:])
PYEOF

echo
echo "=== 完成 ==="
echo "每天 11:01 会拉起实盘自动交易。查看日志："
echo "  $ROOT/logs/scheduler-live.out.log"
echo "  $ROOT/artifacts/live/live/auto.json        （心跳）"
echo "  $ROOT/artifacts/live/live/auto.jsonl       （审计）"
echo
echo "卸载：$PY scripts/install_scheduler.py --uninstall --trading-mode live"
echo
read -n 1 -s -r -p "按任意键关闭…"
