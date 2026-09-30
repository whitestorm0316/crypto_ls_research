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

# 逐个候选**实际试导入 numpy/pandas**，而不是"能执行就选"。
# 踩过的坑：候选表里写的是 `~/.workbuddy/binaries/...`，而真正的托管路径是
# `~/.workbuddy-ai/binaries/...`（少一个 `-ai`）。于是它一路回退到
# `/opt/homebrew/bin/python3` —— 那个解释器**没有 numpy**，脚本要跑到
# `import numpy` 才炸，报错完全指不到"解释器选错了"这件事。
# 判据从"可执行"改成"能 import 依赖"：选错会当场说出来，而不是晚一步炸。
PY="${PY:-}"
PY_TRIED=""
# 显式指定的 PY 也要过同一道检查：否则 `PY=/opt/homebrew/bin/python3` 会原样重现
# 同一个坑（依赖缺失要等到 `import numpy` 才炸，报错指不到"解释器选错了"）。
if [ -n "$PY" ]; then
  if ! "$PY" -c 'import numpy, pandas' >/dev/null 2>&1; then
    PY_TRIED="          ${PY}（显式指定，但缺 numpy/pandas）\n"
    PY=""
  fi
fi
if [ -z "$PY" ]; then
  for cand in \
      "$ROOT/.venv/bin/python3" \
      "$ROOT/venv/bin/python3" \
      "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python3" \
      "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" \
      "/usr/bin/python3" \
      "$(command -v python3 2>/dev/null || true)" \
      "/opt/homebrew/bin/python3" \
      "/usr/local/bin/python3" ; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import numpy, pandas' >/dev/null 2>&1; then PY="$cand"; break; fi
    PY_TRIED="${PY_TRIED}          $cand\n"
  done
fi

if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "[ERROR] 找不到能用的 python3（需要 numpy + pandas）。"
  if [ -n "$PY_TRIED" ]; then
    echo "        这些找到了、但依赖不全："
    printf "%b" "$PY_TRIED"
  fi
  echo
  echo "        装依赖（用项目自带的托管解释器）："
  echo "          \"$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python3\" -m pip install numpy pandas"
  echo "        或显式指定： PY=/path/to/python3 ./$(basename "$0")"
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
