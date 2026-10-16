#!/usr/bin/env bash
# ============================================================================
# 在新 Linux 机器上初始化本项目的行情数据
# ============================================================================
#
# 为什么需要这个脚本
# ------------------
# 仓库里 `.gitignore` 忽略了 **所有数据**（`data_cache*/`、`artifacts/**`、
# `logs/`、`config/*creds*.json` …），所以新机器上 `git clone` 之后**什么都
# 没有**：没有 K 线、没有候选池、没有分类、没有凭据。下面每一步都对应一个
# "漏了它就跑不起来 / 跑出错误结论"的具体后果。
#
# 用法：
#   ./scripts/init_linux.sh              # 完整初始化（约 1-2 小时网络时间）
#   ./scripts/init_linux.sh --quick      # 只 1h（最快能跑研究/回测）
#   ./scripts/init_linux.sh --check      # 只体检，不下载
#
# 幂等：可以反复跑。已有的缓存不会被覆盖（除非显式 --refresh）。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PY:-python3}"

# 代理：三个客户端（okx/binance/okx_private）都从**这一个变量**读。默认 7897
# 是本机那台的端口；换机器必须显式给，否则表现为"网络不通"（实为代理端口错）。
# 允许 `OKX_PROXY=` 显式置空 ⇒ 直连（服务器上常需要）。
export OKX_PROXY="${OKX_PROXY-http://127.0.0.1:7897}"

QUICK=0
CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
warn() { printf '\033[33m  ⚠️  %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  ✓  %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
step "0/6  体检：Python 版本与依赖"
# ---------------------------------------------------------------------------
# 需要 3.10+（用了 `X | None` 一类的语法）。实测 3.11/3.12/3.13 均可。
"$PY" - <<'PYCHK'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"需要 Python ≥ 3.10，当前 {sys.version.split()[0]}")
print(f"  Python {sys.version.split()[0]}  OK")
PYCHK

# 依赖是**纯 numpy/pandas 一系**，没有交易所 SDK（走原生 HTTP）。
# ⚠️ 不在这里自动 pip install：新机器可能有系统包管理器 / venv / conda，
#    猜错了会污染环境。缺失的包在这里**报出来**，由你选装法。
"$PY" - <<'PYCHK'
import importlib.util          # ⚠️ `import importlib` 之后 `importlib.util` 不一定在
need = {"numpy": "核心数值", "pandas": "核心数据", "scipy": "统计",
        "matplotlib": "画图（研究用）", "seaborn": "画图（研究用）",
        "pytest": "测试（开发用）"}
missing = [f"{m}（{why}）" for m, why in need.items()
           if importlib.util.find_spec(m) is None]
if missing:
    print("  ★ 缺这些包：")
    for m in missing:
        print("     -", m)
    print("\n  装法（任选）：")
    print("     python3 -m venv .venv && . .venv/bin/activate")
    print("     pip install numpy pandas scipy matplotlib seaborn pytest")
    raise SystemExit(1)
print("  依赖齐全 OK")
PYCHK

# ---------------------------------------------------------------------------
step "0b/6  代理体检（**先查再下**，否则会跑到第 2 步才 stack trace）"
# ---------------------------------------------------------------------------
# 🔴 三个客户端（`okx_client` / `binance_client` / `okx_private`）都从**同一个**
#    环境变量 `OKX_PROXY` 读代理，**默认 `http://127.0.0.1:7897`**。
#    换一台机器、代理端口不同（常见 7890）⇒ 到达交易所前先被本机代理拒掉，
#    报的是 `URLError: [Errno 111] Connection refused` 或 `[Errno 61]` ——
#    那个错**看不出是代理问题**，只像是"网络不通"。
#
# ⚠️ `DEFAULT_PROXY` 是**模块导入时**读一次的，所以必须在**启动子进程之前**
#    把环境变量设好；进程起来之后再 export 没用。
PROBE="$("$PY" - "$OKX_PROXY" <<'PYCHK' 2>/dev/null || true
import socket, sys, urllib.parse
raw = (sys.argv[1] if len(sys.argv) > 1 else "") or ""
u = urllib.parse.urlparse(raw if "://" in raw else f"http://{raw}")
if not u.hostname:
    print("noproxy"); raise SystemExit(0)
try:
    s = socket.create_connection((u.hostname, u.port or 7890), timeout=3); s.close()
    print(f"ok {u.hostname}:{u.port}")
except Exception as e:
    print(f"dead {u.hostname}:{u.port} {type(e).__name__}")
PYCHK
)"
case "$PROBE" in
  ok*)    ok "代理可达：${PROBE#ok }" ;;
  noproxy)
    warn "OKX_PROXY 为空 ⇒ 走**直连**。如果这台机器直连不通 OKX，下载会失败"
    echo "     要挂代理：export OKX_PROXY=http://127.0.0.1:7890" ;;
  *)
    echo
    echo "  ★ 代理连不上：${PROBE#dead }"
    echo "    三个客户端都读同一个 \`OKX_PROXY\`（默认 127.0.0.1:7897），"
    echo "    但你这台机器的代理端口不是这个 —— 这就是那个"
    echo "    \`URLError: [Errno 111] Connection refused\` 的成因。"
    echo
    echo "    改法（**不用改代码**，先 export 再跑）："
    echo "      export OKX_PROXY=http://127.0.0.1:7890      # 换成你的端口"
    echo "      ./scripts/init_linux.sh"
    echo
    echo "    或者直接直连（服务器上通常走不通交易所，但先试这个）："
    echo "      OKX_PROXY= ./scripts/init_linux.sh"
    echo
    if [ "$CHECK_ONLY" != "1" ]; then
      echo "    先确认端口对不对：ss -ltnp | grep -E '789[0-9]|1080'"
      exit 1
    fi
    ;;
esac

# ---------------------------------------------------------------------------
step "1/6  凭据（不碰仓库，只在本地 config/）"
# ---------------------------------------------------------------------------
mkdir -p config
if [ -f config/okx_creds.json ]; then
  ok "config/okx_creds.json 已存在"
else
  warn "config/okx_creds.json 不存在 —— 研究/回测**不需要**它，只有实盘下单才要"
  echo "     从模板复制后填真实值："
  echo "       cp config/okx_creds.example.json config/okx_creds.json && \$EDITOR config/okx_creds.json"
  echo "     ⚠️ 模拟盘必须用**模拟盘专用 Key**（实盘 Key 会返回 50111）"
fi
if [ -f config/notify.json ]; then
  ok "config/notify.json 已存在"
else
  warn "config/notify.json 不存在 —— 调仓通知不会发（不影响交易）"
  echo "       cp config/notify.example.json config/notify.json && \$EDITOR config/notify.json"
fi
if [ -f config/auth.json ]; then
  ok "config/auth.json 已存在（控制台登录已配置）"
else
  warn "config/auth.json 不存在 —— 控制台首次打开时会引导你建账号（这是正常的）"
  echo "     ⚠️ 口令哈希 + TOTP 密钥在这文件里，**别复制、别提交**；"
  echo "        新机器上重新建一个账号更干净（旧机器的 TOTP 也不会跟着过来）。"
fi

# ---------------------------------------------------------------------------
step "2/6  下载 K 线（这一步最慢）"
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo "  （--check：跳过）"
else
  BARS="1h 15m 5m"
  [ "$QUICK" = "1" ] && BARS="1h"
  # ⚠️ `--end` 必须**显式给明天的 UTC 日期**：
  #    默认值是硬编码的 `2026-09-26`，而它是**排他上界**。照默认跑会比今天
  #    少二十来天的 bar，而且不报错 —— 后续回测静静地在旧数据上出结论。
  END="$(date -u -d tomorrow +%Y-%m-%d 2>/dev/null || date -u -v+1d +%Y-%m-%d)"
  echo "  bars=$BARS   end=${END}（明天 UTC，排他上界）"
  echo "  规模参考（160 标的全量）：1h≈2.7万请求 / 15m≈1.3万 / 5m≈0.35万 / funding≈1万"
  echo "  首次全量约 1–2 小时；中断了直接重跑（已下的会跳过）。"
  echo
  "$PY" -m crypto_ls_research.data.download \
        --bars $BARS --end "$END" --rate 11 --workers 12
fi

# ---------------------------------------------------------------------------
step "3/6  资产分类快照（研究/风控要用）"
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo "  （--check：跳过）"
elif [ -f data_cache/meta/inst_category.json ]; then
  ok "已存在 data_cache/meta/inst_category.json"
else
  # 从 instruments 快照建分类。⚠️ 这一步依赖上一步产出的 meta/instruments.parquet。
  "$PY" -m crypto_ls_research.data.asset_class --build || \
    warn "asset_class --build 失败 —— 检查上一步是否真的产出了 meta/instruments.parquet"
fi

# ---------------------------------------------------------------------------
step "4/6  资金费序列（拼接 OKX + Binance）"
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo "  （--check：跳过）"
else
  echo "  ⚠️ OKX 只留约 3 个月历史 ⇒ 走的是**拼接序列**（OKX 近期 + Binance 远期）。"
  echo "     这一步**不能少**：资金费是策略的成本项，缺了它回测的净收益是虚的。"
  "$PY" -m crypto_ls_research.data.funding_build --rate 8 --workers 8 || \
    warn "funding_build 失败 —— 若只想跑'毛收益'研究可以暂时跳过；但**别把缺资金费的结果当净收益**"
fi

# ---------------------------------------------------------------------------
step "5/6  体检：缓存完整性"
# ---------------------------------------------------------------------------
"$PY" - <<'PYCHK'
import json, os, sys
import pandas as pd
sys.path.insert(0, ".")
from crypto_ls_research.config.settings import CACHE_DIR
print(f"  CACHE_DIR = {CACHE_DIR}")
if not os.path.isdir(CACHE_DIR):
    sys.exit("  ★ 缓存目录不存在 —— 第 2 步没成功")
meta = os.path.join(CACHE_DIR, "meta")
poolf = os.path.join(meta, "candidate_pool.json")
if not os.path.exists(poolf):
    sys.exit("  ★ 没有 candidate_pool.json —— 第 2 步没成功")
pool = json.load(open(poolf))
print(f"  候选池 full = {len(pool['full'])} 只")

worst = None
for bar in ("1h", "15m", "5m"):
    d = os.path.join(CACHE_DIR, "candles", bar)
    if not os.path.isdir(d):
        print(f"  candles/{bar:4s}  —— 未下载（--quick 或该 bar 不需要）")
        continue
    fs = [f for f in os.listdir(d) if f.endswith(".parquet")]
    if not fs:
        print(f"  candles/{bar:4s}  —— 空目录")
        continue
    # 抽一个文件看最新 bar，判断数据新鲜度
    df = pd.read_parquet(os.path.join(d, sorted(fs)[0]))
    last = df.index[-1]
    # ⚠️ 用 `Timestamp.now("UTC")` 而不是 `utcnow()` —— 后者在新版 pandas 上
    #    发 deprecation 警告（警告本身无害，但会淹没体检输出里的真问题）。
    now = pd.Timestamp.now("UTC")
    if last.tz is None:
        last = last.tz_localize("UTC")
    lag = (now - last).total_seconds() / 3600
    print(f"  candles/{bar:4s}  {len(fs):4d} 个文件  最新 bar={last}  落后 {lag:.1f} h")
    if worst is None or lag > worst[1]:
        worst = (bar, lag)
fund = os.path.join(CACHE_DIR, "funding")
nf = len([f for f in os.listdir(fund)]) if os.path.isdir(fund) else 0
print(f"  funding/      {nf} 个文件")
if os.path.exists(os.path.join(meta, "inst_category.json")):
    print("  meta/inst_category.json  OK")
else:
    print("  ★ meta/inst_category.json 缺 —— 第 3 步没成功")
print()
if worst and worst[1] > 30:
    print(f"  ⚠️  {worst[0]} 落后 {worst[1]:.1f} 小时。"
          f"注意：**日常**刷新用 `python scripts/refresh_data_once.py`，"
          f"不用重跑本脚本的全量下载。")
else:
    print("  ✓ 数据看起来是齐的")
PYCHK

# ---------------------------------------------------------------------------
step "5b/6  重建控制台页头要读的基线产物"
# ---------------------------------------------------------------------------
# 🔴 不跑这一步，控制台页头会显示
#    「基线数据缺失（未找到已验收配置的产物）」
# —— 因为 `artifacts/` 被 gitignore，新机器上那份产物不存在。
#
# 好消息：页头只读 `artifacts/<TAG>/tables/01_headline_metrics.json`，
# 而它是 `run.research` 的 **base 阶段**产出的 ⇒ 只跑一个阶段、**约 10 秒**。
# ⚠️ 但花的这 10 秒值得 —— 否则你要点开页面才知道少东西。
if [ "$CHECK_ONLY" = "1" ]; then
  echo "  （--check：跳过）"
else
  # 存在性判断交给 Python（它才知道 OPTIMAL_TAG 是什么，别在 bash 里再抄一遍）
  HAS_HEAD="$("$PY" - <<'PYCHK' 2>/dev/null || true
import os, sys
sys.path.insert(0, "webapp")
from spec import OPTIMAL_TAG
print("yes" if os.path.isfile(
    os.path.join("artifacts", OPTIMAL_TAG, "tables", "01_headline_metrics.json")
) else "no")
PYCHK
)"
  if [ "$HAS_HEAD" = "yes" ]; then
    ok "基线产物已存在（页头有数字）"
  else
    echo "  页头那份 JSON 不在盘上 —— 用 base 阶段重建（约 10 秒）"
    echo "  ⚠️ 它产出的数字会带**今天的**区间与候选池，与历史记录里的"
    echo "     1.9372 **不会完全相同**（新上市合约会进池、区间往后延）——"
    echo "     这是时点差异，不是配置错。脚本会把这条再提醒一次。"
    "$PY" scripts/rebuild_optimal_baseline.py || \
      warn "重建失败 —— 控制台页头仍会显示「基线数据缺失」，其余功能不受影响"
  fi
fi

# ---------------------------------------------------------------------------
step "6/6  跑一次冒烟（可选）"
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  echo "  （--check：跳过）"
else
  echo "  用现有缓存跑一遍回测，确认管线通（不拉网、可复现）："
  echo "    python -m crypto_ls_research.run.research --help    # 看有哪些子命令"
  echo "    pytest crypto_ls_research/tests -q                  # 全量测试"
  echo
  echo "  说明：**研究回测读的是 data_cache/ 的 parquet，不会再拉网**。"
  echo "  这是刻意的 —— 每次跑前拉网 = 每次在**不同的**历史上下结论，基线就不可比了。"
fi

step "完成"
cat <<'EOF'
  日常维护（在已初始化的机器上）：

    # 增量刷新行情（约 15 秒，只补尾巴）
    python scripts/refresh_data_once.py 1h

    # 控制台（需登录；走隧道时另见 scripts/START_CONSOLE_NGROK.sh）
    python -m webapp.server 8790

  ⚠️ 三个最容易踩的坑：
    1. `--end` 别用默认值（硬编码 2026-09-26，会静静少二十来天）
    2. 换机器重建的**候选池**是从"今天的实时挂单"建的 ⇒ 与本机那份不同，
       跨机器的历史结论**不可直接比**。要可比就得连 `meta/` 一起拷。
    3. `CRYPTO_CACHE_DIR` 可以指向别处（默认 `<repo>/data_cache`）；一旦设了，
       download 和 research 必须指同一个，否则"下的和读的不是一份"。
EOF
