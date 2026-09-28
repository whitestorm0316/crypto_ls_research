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
  echo "[ERROR] 找不到 python3。"
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
