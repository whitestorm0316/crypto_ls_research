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
