#!/bin/bash
# ============================================================================
#  参数中台 · 启动（macOS / Linux 双击）
# ============================================================================
#  为什么需要这个脚本，而不是在别处 `python3 webapp/server.py 8790`
#  ---------------------------------------------------------------------------
#  这不是为了"方便"，是为了让**页面上的按钮能用**。
#
#  参数中台里有一个「启动自动任务」按钮，它会 spawn 一个常驻守护进程。
#  如果 server.py 本身是从一个短命 shell（IDE 任务、工具调用、sandbox 包装）
#  里起出来的，那么它 spawn 的守护进程属于同一条进程树，会在启动期
#  （增量刷新那几十秒）被**静默回收**：日志断在半句、没有 traceback，
#  页面上却显示"已拉起进程" —— 看起来就是"按钮没用"。
#
#  双击本脚本起的中台，父进程是 Finder，**不在任何会被回收的进程树里**，
#  于是它派生的守护进程能活下来，按钮才真正可用。
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
  echo "[ERROR] 找不到 python3。装一个再试：brew install python@3.13"
  read -r -p "按回车键关闭…" _ || true
  exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8:replace
export PYTHONUNBUFFERED=1
export PYTHONPATH="$ROOT"

echo
echo "  参数中台正在启动，稍后浏览器打开 http://127.0.0.1:8790"
echo "  本窗口请保持打开（关掉窗口 = 中台停止）。"
echo "  在这个中台里点「启动自动任务」才能生效。"
echo

"$PY" "$ROOT/webapp/server.py" 8790
rc=$?

echo
echo "[exit code $rc]"
read -r -p "按回车键关闭…" _ || true
exit "$rc"
