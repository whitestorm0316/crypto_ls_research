#!/bin/bash
# ============================================================================
#  自动交易 · 停止全部模式（demo / paper / live） · macOS / Linux
# ============================================================================
#  双击即可。等价于 Windows 的 STOP_AUTO_TRADER.bat。
#  退出码：0 = 全部确认已停；1 = 至少一个模式没能确认进程已消失。
# ============================================================================
set -u

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
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

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8:replace
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT"

rc_all=0
for M in demo paper live; do
  echo "--- $M ---"
  "$PY" -m crypto_ls_research.execution.auto_ctl --stop --mode "$M"
  # 0 = 确认已停（含「本来就没在跑」）；非 0 = 进程仍在，必须让人看见。
  r=$?
  [ "$r" -ne 0 ] && rc_all=1
  echo
done

if [ "$rc_all" -eq 0 ]; then
  echo "[OK] 所有模式已确认停止。"
else
  echo "[!!] 有模式未能确认停止，请查看上面的输出。"
fi

read -r -p "按回车键关闭…" _ || true
exit "$rc_all"
