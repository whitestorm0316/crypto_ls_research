#!/bin/bash
# ============================================================================
#  自动交易 · 模拟盘（demo） · macOS / Linux 双击启动
# ============================================================================
#  双击即可运行（macOS Finder 里 .command 文件默认可双击）。
#  首次双击若提示「无法打开，因为它来自身份不明的开发者」：
#     右键 → 打开 → 仍要打开。  或执行一次：
#     chmod +x START_AUTO_TRADER_DEMO.command
#
#  与 Windows 的 START_AUTO_TRADER_DEMO.bat 等价，配置一字不差：
#     模式 demo · 调仓间隔 1 天 · 检查间隔 30 分钟 · 执行窗口 关（跟随回测口径）
#
#  下单时刻 = 信号产生后立即执行：
#     调仓网格锚在北京 10:00 产生信号，回测执行价取下一根 bar 开盘＝北京 11:00，
#     守护进程的自然到期闸门也在那时打开。三者天然对齐，无需执行窗口。
#  想让下单挪到别的时刻（如 23:30）：EXEC_WINDOW=23:30 ./START_AUTO_TRADER_DEMO.command
#  代价是信号变旧 11~13.5 小时、绩效不再等同 v3 回测。
# ============================================================================
set -u

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

# 手动双击时 PATH 很干净，找不到 brew/python。补齐常见位置。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PY="${PY:-}"
if [ -z "$PY" ]; then
  for cand in \
      "$ROOT/.venv/bin/python3" \
      "$ROOT/venv/bin/python3" \
      "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" \
      "$(command -v python3 2>/dev/null || true)" \
      "/opt/homebrew/bin/python3" \
      "/usr/local/bin/python3" \
      "/usr/bin/python3" ; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi

if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "[ERROR] 找不到 python3。装一个再试："
  echo "        brew install python@3.13"
  echo "        或者显式指定： PY=/path/to/python3 ./START_AUTO_TRADER_DEMO.command"
  echo
  read -r -p "按回车键关闭…" _ || true
  exit 1
fi

# 中文/emoji 输出在 macOS Terminal 默认 UTF-8，无需 chcp；显式设一遍防止
# 从某些非 UTF-8 环境（如旧版 crontab、ssh 带 LC_ALL=C）启动时乱码。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8:replace
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT"

echo "使用解释器: $PY"
echo

# 参数透传：./START_AUTO_TRADER_DEMO.command --dry-run
"$PY" "$ROOT/scripts/auto_demo.py" "$@"
rc=$?

echo
echo "[exit code $rc]"
# 双击启动的窗口关掉就没了，不加这句看不到报错。
read -r -p "按回车键关闭…" _ || true
exit "$rc"
