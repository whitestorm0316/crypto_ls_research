# 在新 Linux 机器上部署

> 这份文档只讲**从 git clone 到能跑回测/控制台**要做的全部事情。
> 每个"为什么"都对应一个新机器上真实会踩的坑，不是泛泛的清单。

## 一句话

```bash
git clone <repo> && cd crypto_ls_research
python3 -m venv .venv && . .venv/bin/activate
pip install numpy pandas scipy matplotlib seaborn pytest
./scripts/init_linux.sh
```

---

## 为什么不能只 clone 就跑

`.gitignore` **忽略了所有数据与凭据**：

| 被忽略的 | 大小（本机） | 新机器上 |
|---|---|---|
| `data_cache*/` | 349M + 188M | K 线、候选池、分类快照**全没有** |
| `artifacts/**` | 1.1G | 回测产物、账本没有（可复算） |
| `config/*creds*.json` | 小 | 下单凭据没有（研究不需要） |
| `config/auth.json` | 小 | 控制台登录没配（首次打开会引导） |
| `logs/` | — | 日志没有 |

所以：**行情数据必须自己下**。这是本页的主体。

---

## 1) 环境

```bash
python3 --version        # 需要 ≥ 3.10（用了 `X | None` 语法）
```

依赖只有 6 个，**没有交易所 SDK**（走原生 HTTP 手写的客户端）：

```bash
pip install numpy pandas scipy matplotlib seaborn pytest
```

> 研究/回测其实只需要 **numpy + pandas**；`scipy` 统计检验要用，
> `matplotlib`/`seaborn` 出图要用，`pytest` 跑测试要用。

`scripts/init_linux.sh` 会替你检查版本与依赖，缺了会明确报出来，
**不会**自作主张 pip install（新机器上 venv / conda / 系统包管理器都可能有，
猜错了会污染环境）。

### 🔴 代理：换机器必看

三个客户端（`okx_client` / `binance_client` / `okx_private`）都从**同一个**环境变量
读代理：

```bash
export OKX_PROXY=http://127.0.0.1:7890      # 换成你的端口
```

**默认是 `http://127.0.0.1:7897`** —— 那是开发机的端口。换台机器代理端口不同
（常见 **7890**）就会在到达交易所**之前**被本机代理拒掉：

```
RuntimeError: GET /api/v5/public/instruments failed after 4:
  URLError: <urlopen error [Errno 111] Connection refused>
```

⚠️ 这个错**看不出是代理问题**，只像"网络不通"。脚本里有一步**代理体检**会
在下载前就报出来，并告诉你怎么改。想直连（服务器上常需要）：

```bash
OKX_PROXY= ./scripts/init_linux.sh
```

> `DEFAULT_PROXY` 是**模块导入时**读一次的，所以必须先 `export` 再启动进程 ——
> 进程起来之后再设没用。

---

## 2) 凭据（研究可跳过）

```bash
cp config/okx_creds.example.json config/okx_creds.json   # 实盘下单才需要
cp config/notify.example.json    config/notify.json      # 调仓通知才需要
```

- **跑研究/回测完全不需要凭据。**
- ⚠️ 模拟盘必须用**模拟盘专用 Key**（用实盘 Key 会返回 `50111 Invalid OK-ACCESS-KEY`）。
- ⚠️ 这两个文件已在 `.gitignore`，**别提交、别从别的机器拷过来**。

控制台登录（`config/auth.json`）**不建议拷**：里面是口令哈希 + TOTP 密钥，
而且旧机器的验证器 App 条目也不会跟着过来。新机器上第一次打开
`http://127.0.0.1:8790` 会直接进"首次设置"，重新建一个更干净。

---

## 2b) 配飞书通知（调仓时推手机）

⚠️ `config/notify.json` **被 gitignore**（里面有 webhook URL —— 拿到它谁都能往你群里发消息），
所以每台机器都要配一次。

### 用内置 CLI（推荐，比手改 JSON 稳）

```bash
# 一行搞定：写 URL + 自动 enabled:true + 立刻发一条测试消息
python -m crypto_ls_research.execution.notify \
  --set-url 'https://open.feishu.cn/open-apis/bot/v2/hook/你的hook'

# 看当前状态（URL 自动打码）
python -m crypto_ls_research.execution.notify --show

# 单发一条测试消息
python -m crypto_ls_research.execution.notify --test
```

成功的输出长这样：

```json
{"ok": true, "status": 200, "provider": "feishu",
 "resp": "{\"StatusCode\":0,\"StatusMessage\":\"success\",...}"}
```

`ok: true` 且飞书返回 `success` ⇒ 群里应该已经收到一条「✅ 测试消息」。

### webhook 从哪来

飞书群 → 右上角 **设置** → **群机器人** → **添加机器人** → **自定义机器人** →
复制 Webhook 地址（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`）。

### 🔴 安全设置必须和配置对上

飞书机器人有三种安全设置，**选错了消息会被拒**：

| 飞书侧设置 | 配置侧要做什么 | 不对上的后果 |
|---|---|---|
| **签名校验** | `--set-secret '<密钥>'` | 不带签名 ⇒ 被拒 |
| **自定义关键词** | `--set-keyword '<那个词>'` | 消息里没这个词 ⇒ **`code 19024`** |
| **IP 白名单** | 什么都不用填（但要填服务器出口 IP） | IP 不在名单 ⇒ 被拒 |

> ⚠️ 「签名校验」和「自定义关键词 」都不填，而飞书侧却开了——
> 是最常见的失败原因。`--set-secret` / `--set-keyword` 会写完**立刻自检**，
> 所以一跑就知道对不对。

### 只推哪些、哪些模式推

`config/notify.json` 里两个数组（CLI 不改这两个，要手编）：

- **`notify_on`**：`traded`（调仓完成）/ `blocked`（风控拦截）/ `error`（出错）。
  **`not_due`（非调仓日）刻意不推** —— 它每天都发生，推了就没人看了。
- **`modes`**：默认 `["demo","live"]`。**`paper` 不在里面** —— 纸面账不是真钱，默认不推。

### ⚠️ 改了配置**不用重启控制台**

`Notifier.from_config()` 每次发送时重新读盘，所以改完立即生效
（有测试守着这条：`test_auto_notify.py`）。

## 3) 下载行情数据 ← 主体

```bash
./scripts/init_linux.sh              # 完整：1h + 15m + 5m + 分类 + 资金费
./scripts/init_linux.sh --quick      # 只 1h（最快能跑研究）
./scripts/init_linux.sh --check      # 只体检，不下载
```

脚本是**幂等**的，中断了直接重跑（已下好的会跳过）。

### 它按这个顺序做四件事

| 步 | 命令 | 产出 |
|---|---|---|
| 2 | `data.download --bars 1h 15m 5m` | `candles/<bar>/*.parquet`、`meta/instruments.parquet`、`meta/candidate_pool.json` |
| 3 | `data.asset_class --build` | `meta/inst_category.json`（**必须**：OKX 的 USDT 永续里混着代币化股票与商品 —— 本机快照实测 **crypto 283 / equity 186 / commodity 8**，不分类就等于悄悄换了研究标的） |
| 4 | `data.funding_build` | `funding/*.parquet`（**拼接序列**：OKX 只留约 3 个月，远期来自 Binance） |
| 5 | 内置体检 | 打印每个 bar 的文件数与最新 bar 落后多久 |

### 规模与耗时（160 个标的）

| bar | 池 | 起点 | 请求数 |
|---|---|---|---|
| 1h | full（160 只） | 2021-01-01 | ~27,000 |
| 15m | top40 | 2024-01-01 | ~12,800 |
| 5m | top8 | 2025-07-01 | ~3,500 |
| funding | full | 2021-01-01 | ~10,000 |

**合计约 5.4 万请求；按 11 req/s 满速约 80 分钟**（实际受服务端限速会更久）。
1h 单独跑约 56 分钟。

---

## 🔴 三个必须知道的坑

### ① `--end` **不要用默认值**

`data.download` 的 `--end` 默认是硬编码的 **`2026-09-26`**，而它是**排他上界**。
照默认跑会静静少二十来天 —— **不报错**，后续回测就在旧数据上出结论。

脚本里已改成显式给**明天 UTC**：

```bash
END="$(date -u -d tomorrow +%Y-%m-%d)"
python -m crypto_ls_research.data.download --bars 1h --end "$END"
```

### ② 候选池是**从今天的实时挂单**建的 ⇒ 跨机器不可比

`build_candidate_pool()` 的取数是：**今天成交额前 120 名 ∪ 2022-01-01 前上市的全部**
（本机当前 160 只）。它明确带着 survivorship bias（已摘牌的合约不在今天的列表里）。

所以在**另一台机器上重建**的池子，与你本机那份**不是同一个宇宙** ——
两边的回测数字**不可直接比较**。

> 要让两边可比：把 `data_cache/meta/` 一起拷过去（几十 KB），
> 或在新机器下载时加 `--reuse-pool`（需要先有那份 `candidate_pool.json`）。

### ③ `CRYPTO_CACHE_DIR` 一旦设了，两边必须指同一个

默认是 `<repo>/data_cache`（`settings.CACHE_DIR`，唯一来源）。
设了环境变量就指向别处 —— 但**下载器和读取器必须指同一份**，
否则"下的和读的不是一份缓存"，表现为"明明下了却没有数据"。

```bash
# 例：把缓存放到大盘
export CRYPTO_CACHE_DIR=/data/crypto_cache
python -m crypto_ls_research.data.download --bars 1h --end "$END"
```

---

## 3b) 控制台页头显示「基线数据缺失」？

**这是预期的** —— `artifacts/` 被 gitignore，所以新机器上那份产物不存在。
页头的每一个数字都读自：

```
artifacts/v5_1d_all5/tables/01_headline_metrics.json
```

重建它**只要 10 秒**（`run.research` 的 `base` 阶段就够，页头只读这一个文件）：

```bash
python scripts/rebuild_optimal_baseline.py
```

`init_linux.sh` 的第 5b 步会自动做这件事。脚本的参数**从 `webapp/spec.py` 的
`OPTIMAL_CLI` / `OPTIMAL_OVERRIDES` 派生**（不写字面量），所以它跑的就是
验收时那套配置。

> ⚠️ 重建出来的数字会带**今天的**区间与候选池，与历史记录的
> `Sharpe 1.9372` **不会完全相同**。原因是两条**机制**（不是配置错）：
>
> 1. **`--end` 会被忽略**：`store.load_panels()` 刻意把网格延伸到缓存里最新
>    那根 bar（注释写明是为了"不截断样本尾部"）⇒ 传 `--end 2026-09-29`
>    没用，区间总是到"今天"。
> 2. **候选池是「今天的实时挂单」**：2026-09-29 → 10-16 之间新上市的合约
>    会进池并被选中，改变组合权重。
>
> 🔴 **所以这个脚本默认拒绝覆盖已有的基线** —— `v5_1d_all5` 的数字被
> `FINDINGS.md`、`spec.py` 注释、以及 `test_failure_attribution.py` 的
> 5 个回撤窗口引用，重跑它们会立刻失效，而且**换不回来**
> （`baseline.pkl` 33MB 且 `artifacts/` 被 gitignore）。
> 真要重跑：先 `cp -r artifacts/v5_1d_all5 /tmp/backup`，再 `--force`。
>
> 实测对照（同一台机器，2026-10-16）：
>
> | | 记录值（2026-09-29） | 重建值（2026-10-16） |
> |---|---|---|
> | 宇宙 | 140 只 | 154 只 |
> | Sharpe | 1.9372 | 1.9688 |
> | 最大回撤 | −13.31% | −13.31% |
>
> 宇宙从 140 → 154 是因为多了 14 个**新上市**合约（`CT`/`QUANT`/`MEGA`…）。

## 4) 验证装好了

```bash
pytest crypto_ls_research/tests -q          # 全量测试
./scripts/init_linux.sh --check             # 数据体检
```

体检正常的样子：

```
CACHE_DIR = /path/to/crypto_ls_research/data_cache
候选池 full = 160 只
candles/1h     160 个文件  最新 bar=2026-10-16 05:00:00+00:00  落后 2.1 h
funding/      160 个文件
meta/inst_category.json  OK
```

---

## 4a) ⚠️ 小内存机器（2GB 及以下）：先加 swap

**`-9` / `137` / `Killed` 都是同一件事：SIGKILL —— 被 OOM killer 杀了**，
**不是代码报错**（所以日志里不会有 traceback）。

### 三个地方都会撞，因为都跑同一套回测引擎

| 你在做什么 | 报什么 | 峰值内存（实测） |
|---|---|---|
| 命令行跑回测 | `退出码 -9`，日志停在 `panels: ...` | **2.19 GB** |
| 控制台点「⚡ 一键准备」 | `Killed` + `[exit code 137]` | **~2.0 GB** |
| 控制台点「仅刷新行情」 | 通常没事（只增量下载） | 很小 |

> 🔴 **「一键准备（刷新行情＋生成计划）」= 刷新 + 跑一次完整回测**。
> 页面自己也写了「首次要跑一次完整回测」。`target()` 里那句提示是
> 「正在计算信号（**复用回测引擎**，约 40 秒）」。
> 所以**控制台占 182MB + 回测 1.9GB ≈ 2.1GB** —— 2GB 机器必然被杀。

⚠️ 而且**它不是慢慢泄漏**：实测连算两次，峰值都是 1817 MB（内存会释放）。
是**单次峰值**就超了。

### 确认是 OOM

```bash
dmesg | grep -i -E "killed process|out of memory" | tail -5
free -h
```

（`START_CONSOLE.command` 里那行
`line 88: read: read error: 0: Bad file descriptor` **不是错误** ——
那是进程被杀后脚本还想等你按回车，而终端已经断了。看 `137` 和 `Killed` 就够了。）

### 原来 4a 的症状说明（命令行回测）

```bash
dmesg | grep -i -E "killed process|out of memory" | tail -5
```

### 内存需求是实测出来的

| 宇宙 | `load_panels` 后 | 回测峰值 |
|---|---|---|
| 154 只（crypto，全量） | **0.55 GB** | **1.76 GB** |
| 60 只 | ~0.4 GB | ~1.0 GB |
| 40 只 | ~0.3 GB | ~0.8 GB |

> 这两列是**降过两轮内存之后**的数字（原先是 1.24 GB / 2.19 GB）：
> ① `_read_one` 读入即转 float32 + `del raw`；
> ② `wide()` 一步构造成 float32（原先 `DataFrame → reindex → astype`
>    **三次全量拷贝**，这一项就省了 **418 MB**）。
> 两轮都验证过**数值逐位不变**。

**2GB 机器仍建议加 swap**（峰值 1.76 GB 已能跑，但没有余量 ——
控制台同时开着就超了）：



```bash
# 建一个 4G 的 swap 文件（一次性，几分钟）
sudo fallocate -l 4G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=4096
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 开机自动挂
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 确认
free -h && swapon --show
```

> ⚠️ 用 `fallocate` 在有些文件系统（如某些 overlayfs）上会失败，
> 那就用 `dd`（慢但一定能成）。见上面的 `||` 分支。

**加 4GB swap 之后够不够？** 算给你看：

```
控制台 182 MB + 生成计划峰值 1804 MB = 1986 MB
物理 2048 MB                       → 已经能过（但没余量）
加 4GB swap 后可用 6144 MB          → 富余 4158 MB ✓
```

**降完内存后已经能过**，但**没有余量** —— 浏览器多开几个标签页就危险。
所以还是建议加 swap。

### 或者：跑的时候把控制台停掉腾地方

「生成计划」是按需起子进程跑回测的，所以：

1. 先在控制台点「↻ **仅刷新行情**」（只增量下载，几 MB，几十秒），等它完成 ——
   这一步不吃内存，**大概率不会死**；
2. 然后**关掉控制台**（它占 182 MB），在命令行跑一次回测
   （`--stages base`，或直接 `scripts/rebuild_optimal_baseline.py`）；
3. 再开控制台，这时信号能从**磁盘缓存**（`signal_cache.json`）直接读，
   不用重算。

> ✅ 顺带核实过：守护进程的配置（`engine.DEFAULT_SIGNAL`）与验收基线
> （`spec.OPTIMAL_CLI`）**是同一套** —— 都是 `1h / rebalance_days=1.0 /
> asset_class=crypto` + 五因子。所以页头那个「已验收最优」与守护进程实际在跑的
> **口径一致**，不存在"页头说的和实际做的不一样"。

### 「仅刷新行情」会不会也死？

不会 —— 那条路只跑 `data.download --incremental --only-candles --reuse-pool`，
**不加载面板、不跑回测**，内存占用很小。**放心点。**

加完 swap 后重跑即可 —— **宇宙不变，所以数字仍与验收基线同口径**
（只是跑到 swap 时会慢一些）。

### 不想加 swap？那就缩小宇宙（但数字不可比）

```bash
python -m crypto_ls_research.run.research --stages base --tag v5_1d_all5 \
  --bar 1h --rebalance-days 1.0 --asset-class crypto \
  --max-insts 40 \
  --override 'factors.subset=["momentum","flow","range_pos","hitrate","rev_short"]' \
  --override 'portfolio.max_weight_per_instrument=0.20' \
  --override 'execution.max_daily_turnover=0.20'
```

⚠️ 但宇宙从 154 只变 40 只 ⇒ **Sharpe 会明显不同、且与验收基线不可比**。
页头会显示这个新数字，而它**不是**验收的那个配置。

## 4b) 回测失败了，日志在哪？

**`logs/` 目录，文件名形如 `research-<tag>-<时间戳>.log`。**

```bash
# 看最新那份
ls -t logs/research-*.log | head -1 | xargs tail -50

# 只看错误
grep -nE "!! stage|Traceback|FAILED stages" logs/research-*.log
```

⚠️ **这个日志是 2026-10-16 才加的**，之前回测**只在终端打印、不写任何文件** ——
窗口一关、或 ssh 断线，失败原因就没了。如果你在旧版上跑，只能靠终端回滚。

### 想让输出一边打终端一边落盘（旧版也能用的办法）

```bash
python -m crypto_ls_research.run.research --stages base 2>&1 | tee logs/my_run.log
```

⚠️ 用 `tee`，**不要**用 `> logs/x.log`：后者会让终端**一片空白**，
你会以为卡死了（回测要跑几十分钟）。

### 回测失败时，日志会告诉你什么

1. **`!! stage <名字> failed: <异常>`** —— 哪个阶段挂了；
2. **完整 traceback** —— 出错的文件与行号；
3. 结尾的 **`### FAILED stages: [...]`** —— 失败清单；
4. **进程退出码非零** —— 所以 `cmd && next` 的写法不会再误判成功。

> ⚠️ 注意「阶段失败」与「整体成功」是两回事：某个阶段挂了，
> **前面的阶段产物仍然在盘上**，但它们**不完整**。日志最后会写明这一点。

## 5) 日常使用

```bash
# 增量刷新行情（约 15 秒，只补尾巴；不要重跑全量）
python scripts/refresh_data_once.py 1h

# 控制台
python -m webapp.server 8790
# 从外网访问（隧道，不用域名不用证书）
./scripts/START_CONSOLE_NGROK.sh
```

### ⚠️ 两种"回测"，要不要先刷新行情，答案相反

- **研究/验证回测**（`scripts/exp_*.py`、验收基线）：**不刷，也不能刷**。
  它读 `data_cache/` 的 parquet（`load_panels` 只读盘不拉网）。
  要**可复现** —— 每次跑前拉一次网 = 每次在**不同的**历史上下结论，基线立刻不可比。
- **实盘/纸面的**（`engine.target()`）：**必须先刷新**。
  它就是**用回测引擎在最新缓存上跑一遍** ⇒ 缓存不新、目标仓就是旧的。

---

## 附：不用脚本的手工版

```bash
export OKX_PROXY=http://127.0.0.1:7890          # ⚠️ 别忘了这行（见上）
END="$(date -u -d tomorrow +%Y-%m-%d)"

python -m crypto_ls_research.data.download --bars 1h 15m 5m --end "$END"
python -m crypto_ls_research.data.asset_class --build
python -m crypto_ls_research.data.funding_build --rate 8 --workers 8
```

换币安行情做场所稳健性对比（写到 `data_cache_binance/`，与本机那份并存）：

```bash
CRYPTO_CACHE_DIR=data_cache_binance python -m crypto_ls_research.data.download \
    --source binance --bars 1h --reuse-pool --only-candles --end "$END"
```

> ⚠️ 币安是**权重受限**端点（1500 bar/页 = 10 权重，2400/min 预算）
> ⇒ 约 4 req/s，脚本会自动把默认的 11 降下来。
> ⚠️ 两个场所的 panel **同时持会 OOM** ⇒ 用完即 `del` + `gc.collect()`。
