#!/bin/bash
# ============================================================================
#  参数中台 · macOS 一键初始化（双击运行）
# ============================================================================
#  为什么需要这个文件，而不是让你去跑 `scripts/init_linux.sh`
#  ---------------------------------------------------------------------------
#  那个脚本其实是**跨平台**的（纯 bash，没有 apt/systemctl，日期用
#  `date -u -d tomorrow || date -u -v+1d` 两路兜底），逻辑也是对的 —— 它甚至
#  在注释里专门警告了 `--end` 硬编码这个坑。
#
#  但它的名字叫 `init_linux`。在 Mac 上没人会想到去双击一个 linux 脚本，
#  于是新 clone 之后最常见的路径是：直接双击 `START_CONSOLE.command` → 起服务
#  → 点「运行回测」→ `FileNotFoundError: inst_category.json`。
#
#  所以这里只做三件事：挑一个**真能 import numpy/pandas** 的解释器、问一句
#  「快速还是全量」、然后把活儿交给 init_linux.sh（**不复制它的逻辑**：
#  同一条规则写两遍，两遍就会不一致 —— 本仓库为此付过代价）。
# ============================================================================
set -u

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8:replace
export PYTHONUNBUFFERED=1

# 代理：三个交易所客户端都从这一个变量读。本机默认 7897（clash/verge 常用端口）。
# 需要直连就 `OKX_PROXY= ./scripts/init_macos.command`。
export OKX_PROXY="${OKX_PROXY-http://127.0.0.1:7897}"

# ---------------------------------------------------------------------------
# 挑解释器：判据是「能 import numpy + pandas」，不是「可执行」
# ---------------------------------------------------------------------------
# 踩过的坑：候选表里写 `~/.workbuddy/binaries/...`（少一个 `-ai`），于是一路
# 回退到没装 numpy 的 /opt/homebrew/bin/python3，报错要到 `import numpy` 才炸，
# 完全指不到「解释器选错了」。
PY="${PY:-}"
if [ -n "$PY" ] && ! "$PY" -c 'import numpy, pandas' >/dev/null 2>&1; then
  echo "[WARN] 你指定的 PY=$PY 缺 numpy/pandas，忽略它，重新挑。"
  PY=""
fi
if [ -z "$PY" ]; then
  for cand in \
      "$ROOT/.venv/bin/python3" \
      "$ROOT/venv/bin/python3" \
      "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python3" \
      "$(command -v python3 2>/dev/null || true)" \
      "/opt/homebrew/bin/python3" \
      "/usr/local/bin/python3" \
      "/usr/bin/python3" ; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import numpy, pandas' >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi

if [ -z "$PY" ]; then
  echo
  echo "[ERROR] 找不到能用的 python3（需要 numpy + pandas）。"
  echo "        先建一个："
  echo "          python3 -m venv .venv && . .venv/bin/activate"
  echo "          pip install numpy pandas scipy matplotlib seaborn pytest"
  echo
  read -r -p "按回车键关闭…" _ || true
  exit 1
fi

echo
echo "  解释器：$PY"
echo "  项目根：$ROOT"
echo "  代理  ：${OKX_PROXY:-（直连）}"
echo
echo "  选择初始化范围："
echo "    1) 快速（只下 1h，约 40–60 分钟）—— 够跑研究/回测/控制台"
echo "    2) 全量（1h + 15m + 5m，约 1.5–2.5 小时）—— 还要跑多周期实验才需要"
echo "    3) 只体检（不下载）"
echo
read -r -p "  输入 1 / 2 / 3（回车默认 1）： " choice || true
case "${choice:-1}" in
  2) ARGS="" ;;
  3) ARGS="--check" ;;
  *) ARGS="--quick" ;;
esac

echo
echo "  即将执行： PY=$PY scripts/init_linux.sh ${ARGS:-(全量)}"
echo "  （中断了直接重跑，已下载的会跳过）"
echo
sleep 1

PY="$PY" bash "$ROOT/scripts/init_linux.sh" $ARGS
rc=$?

echo
if [ "$rc" = "0" ]; then
  echo "  ✓ 初始化完成。接下来双击 START_CONSOLE.command 起控制台。"
  echo "    首次打开页面会让你创建账号（口令 + 二重认证）。"
else
  echo "  [exit code $rc] 初始化没走完。上面最后几行就是原因；"
  echo "  数据是增量下载的，修好后重跑本脚本即可（已下的会跳过）。"
fi
echo
read -r -p "按回车键关闭…" _ || true
exit "$rc"
