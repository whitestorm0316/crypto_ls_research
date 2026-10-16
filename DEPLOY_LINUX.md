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
