# crypto_ls_research

OKX USDT 本位永续上的**横截面多空策略**：动态 PIT 流动性池 + 多因子打分 + Top-K 多空 +
反向波动率加权 + Beta 对冲 + 真实成本/资金费回测，外加一个能在本机跑的**参数中台**
（Web 控制台）和一套**无人值守自动交易**。

研究实现的完整推导（Phase 1–37：公式 / 单元测试 / 潜在风险 / 验证方法）在
[`crypto_ls_research/README.md`](crypto_ls_research/README.md)，本文件只负责「怎么把它跑起来」。

---

## ⚠️ 先说三件必踩的事

1. **仓库里没有数据，也没有产物。**
   `data_cache*/`（约 305 MB）、`artifacts/**`、`logs/`、`config/*creds*.json` 全在
   `.gitignore` 里。新 clone 下来 `data_cache/` 是空的 ⇒ 点「运行回测」会直接
   `FileNotFoundError: data_cache/meta/inst_category.json`。**先跑初始化**（见下）。

2. **`--end` 默认是「明天 UTC」，不是某个冻结日期。**
   想要和某份历史记录可比，必须显式给 `--end`，否则你比的是另一段时间。

3. **横截面宽度会随缓存增长而变。**
   `load_panels(insts=None)` 读的是**整个 `data_cache/candles/<bar>/` 目录**，而下过的标的
   只会越积越多 ⇒ 同一份代码 + 同一组参数，隔几周再跑就是另一个横截面（实测同一配置
   125 个标的 Sharpe 1.696、164 个标的 2.014）。要复现就用 `--insts-from`（见「可复现性」）。

---

## 快速开始

依赖：**Python ≥ 3.10** + `numpy pandas scipy matplotlib seaborn pytest`
（脚本**不会**自动 `pip install`，缺包会列出来让你自己选装法）。

### macOS

```bash
git clone git@github.com:whitestorm0316/crypto_ls_research.git
cd crypto_ls_research

./scripts/init_macos.command    # 双击也行：1) 快速(只 1h) 2) 全量(1h+15m+5m) 3) 只体检
./START_CONSOLE.command         # 起控制台（默认 http://127.0.0.1:8790）
```

### Linux / 手动

```bash
./scripts/init_linux.sh --quick    # 只下 1h，约 40–60 分钟；--check 只体检
./scripts/init_linux.sh            # 全量（1h + 15m + 5m），约 1.5–2.5 小时

# 或者一步步来
python -m crypto_ls_research.data.download --dry-run          # 先看请求预算
python -m crypto_ls_research.data.download --bars 1h --end 2026-10-20
python -m crypto_ls_research.data.asset_class --build         # 资产分类快照（研究/风控要用）
python -m crypto_ls_research.data.funding_build               # 资金费拼接（OKX 只留 3 个月）
```

初始化是**幂等**的：中断了直接重跑，已下载的会跳过。

### 起控制台

```bash
python -m webapp.server 8790
```

⚠️ **端口必须显式传**：`server.py::main()` 的默认值是 `8770`（历史值，隔壁 a-stock 在用），
漏掉就是 `OSError: [Errno 48] Address already in use`。首次打开页面会引导你创建账号
（口令 + 二重认证 TOTP），凭据落在 `config/auth.json`（0600，已 gitignore）。

页面顶部会在**数据没初始化时先给一条红色横幅**，写清缺什么、该跑哪条命令——不用等点回测才知道。

---

## 跑一次回测

```bash
python -m crypto_ls_research.run.research \
  --tag my_run --bar 1h --rebalance-days 1 --asset-class crypto \
  --start 2021-01-01 --end 2026-10-20 --stages base charts
```

- `--stages` **默认是 14 个阶段**（含 Monte-Carlo，单跑约 36 分钟）。只想看指标就写
  `base charts`（1.5–4 分钟，取决于池宽）；想凑齐验收门禁用 `full` 包（见 `webapp/spec.py::STAGE_BUNDLES`）。
- 产物：`artifacts/<tag>/tables/*.csv|json`、`artifacts/<tag>/charts/*.png`
- 日志：`logs/research-<tag>-<时间戳>.log`（**默认就开**，因为回测常常几十分钟，关窗口就没了）
- 阶段失败会**非零退出**并打印 `### FAILED stages`（早期版本退出码是 0，调用方会误判成功）

常用参数：

| 参数 | 说明 |
|---|---|
| `--bar` | `1h`（验收口径）/ `15m` / `4h` |
| `--rebalance-days` | 调仓网格，验收口径是 `1`（1 天） |
| `--asset-class` | `crypto` 剔除 OKX 上的代币化股票/ETF 与商品标的；`all` 不筛 |
| `--max-insts N` | 只取 ADV 前 N 个（调试/快速验证用，会改变横截面） |
| `--insts-from PATH` | 冻结横截面，见下 |
| `--override k.sub=v` | 点号路径覆盖配置，可重复，如 `execution.max_daily_turnover=0.2` |
| `--quick` | 降低统计检验的重采样次数（置换 40 vs 200、block bootstrap 10 vs 30、置换数 8 vs 30）：更快，但结论更粗，**别用它的数字下结论** |

---

## 可复现性：冻结横截面

每次运行都会把**实际参与回测的标的清单**写进 `artifacts/<tag>/tables/00c_universe_insts.json`
（含 `source`，说明这份清单是怎么来的）。要复现同一个横截面：

```bash
python -m crypto_ls_research.run.research --tag rerun \
  --insts-from artifacts/v5_1d_all5/tables/00c_universe_insts.json \
  --start 2021-01-01 --end 2026-10-20 --stages base
```

给了 `--insts-from` 就**不再按资产类二次过滤**（那是建清单时该做的事）。清单与缓存没有交集时
会直接报错退出，不会静默跑一个空宇宙。

> **记录值 ≠ 现在跑出来的值**，这不是 bug：候选池是按「建池那天的实时挂单」建的，新合约会进来、
> 老合约会下架，缓存也只会越积越宽。要比数字，必须同时钉住**标的集合**和**截止日**。

---

## 目录结构

```
crypto_ls_research/          研究内核（数据 / 因子 / 组合 / 风控 / 回测 / 分析）
  data/                      download · asset_class · funding_build · store（面板）
  factors/ signals/ portfolio/ risk/ execution/ backtest/ analysis/
  run/research.py            研究编排器（多阶段 + 日志 + 产物）
webapp/                      参数中台（本地 Web 控制台，仅标准库 HTTP）
  server.py                  API + 一次性回测队列 + 每日调度
  static/                    index.html · app.js · style.css
  spec.py                    控制台参数表 / 预设 / 已证伪配置 / argv 构造
  live_api.py trades.py auth.py auto_schedule.py
scripts/                     init_linux.sh · init_macos.command · exp_*.py（实验）· 审计脚本
config/                      okx_creds.example.json · notify.example.json（真实凭据不入库）
artifacts/                   产物（gitignore）；顶层 FINDINGS/OPTIMIZATION_RESULTS/FINAL_REPORT 随仓库带走
data_cache/                  K 线 + 资金费缓存（gitignore）
```

---

## 配置与环境变量

| 变量 | 作用 |
|---|---|
| `CRYPTO_CACHE_DIR` | 缓存根目录（唯一来源 `settings.CACHE_DIR`）。换交易所/复用别人的缓存就指这里 |
| `OKX_PROXY` | 三个客户端共用的代理，**默认 `http://127.0.0.1:7897`**；置空 = 直连。换机器最常撞的坑：代理端口不对会报 `[Errno 111] Connection refused`，看不出是代理问题 |
| `FUNDING_DIR` | 资金费目录，默认 `funding_hyb`（OKX+Binance 拼接）；`funding` = 纯 OKX（只有约 3 个月） |
| `LS_HOST` | 控制台监听地址，默认 `127.0.0.1`。**明文绑非本机直接拒绝启动** |
| `LS_TLS_CERT` / `LS_TLS_KEY` | 想绑外网就必须同时给证书（口令与 TOTP 会经这条连接传，而登进来能下真钱单） |
| `TZ` | 通知/日志里的时间显示 |

凭据文件（都不入库）：

```bash
cp config/okx_creds.example.json config/okx_creds.json   # OKX API（下单才需要）
cp config/notify.example.json    config/notify.json      # 飞书 webhook（可选）
# config/auth.json 由控制台首次打开时自动创建
```

飞书通知有内置 CLI，不用手改 JSON：

```bash
python -m crypto_ls_research.execution.notify --show
python -m crypto_ls_research.execution.notify --test
python -m crypto_ls_research.execution.notify --set-url '<webhook>'
```

---

## 测试

```bash
TMPDIR="$(mktemp -d)" python -m pytest crypto_ls_research/tests -q
```

`TMPDIR` 不是可选的：pytest 一个进程跑到底，撞上 root 残留会整片 error。
重测试（会加载完整面板的）在内存不足时会跳过而不是被 OOM 杀掉。

---

## 无人值守交易

```bash
./START_AUTO_TRADER_DEMO.command    # 模拟盘演练
./STOP_AUTO_TRADER.command          # 停
./INSTALL_LIVE_TIMER.command        # 安装定时器（实盘）
```

调度器**活在控制台进程里**（`webapp/auto_schedule.py`，启动时按开关自恢复），走和网页按钮
同一条队列。细节、闸门顺序、通知怎么配，见 [`AUTO_TRADER_README.md`](AUTO_TRADER_README.md)。

---

## 文档索引

| 文件 | 内容 |
|---|---|
| [`crypto_ls_research/README.md`](crypto_ls_research/README.md) | 研究实现全推导（Phase 1–37） |
| [`artifacts/FINDINGS.md`](artifacts/FINDINGS.md) | 已完成的检验与结论（Q1–Q63） |
| [`artifacts/OPTIMIZATION_RESULTS.md`](artifacts/OPTIMIZATION_RESULTS.md) | 参数网格 / 因子集 / 容量 |
| [`artifacts/FINAL_REPORT.md`](artifacts/FINAL_REPORT.md) | 交付报告 |
| [`AUTO_TRADER_README.md`](AUTO_TRADER_README.md) | 自动交易与实盘操作 |
| [`DEPLOY_LINUX.md`](DEPLOY_LINUX.md) | 部署：小内存机器加 swap、systemd timer、TLS、日志在哪 |

---

## 风险声明

这是**会下真钱单**的代码。默认绑定 `127.0.0.1`、默认口令 + TOTP 二重认证、明文绑外网直接
拒绝启动——这些都不是装饰。回测里的数字是**某时点的快照**，跨时点不可直接比较；
`artifacts/FINDINGS.md` 里记着每一处已知的失效模式，改策略前先读它。
