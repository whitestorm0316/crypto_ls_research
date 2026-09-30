# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的、必须照做的）。长推导/表格在 `RULES-OPS.md`（**不注入**）；数字在
> `artifacts/FINDINGS.md` 与 `.workbuddy-ai/memory/<日期>.md`。
> ⚠️ **注入上限 ~17.2 KB**（实测）。**加东西必须等量压掉**，细则写 `RULES-OPS.md`；
> 加完 `wc -c` 复核，**超 16.5 KB 就再压**。

## 环境 / 沙箱 / 进程
- Python：`/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` **静默返 0 行** → 用 `grep -E`；`--include=*.py` 在 zsh 报错 → 用 Grep 工具。
  **无 `timeout`**；`ps` 被禁 → 探活 `kill -0 $(cat <pidfile>)`；**禁 `rm -rf`**。
- ⚠️ **`/tmp` 在两次工具调用之间被清空** → pidfile/log **放仓库内**（`logs/`）。
- 控制台 **8790**；`server.py::main()` 默认 **8770**（隔壁 a-stock 在用）→ **必须位置参数传**
  `python -m webapp.server 8790`。漏掉 = `OSError: [Errno 48]`，而 `lsof`/`netstat` **查不到**。
- **沙箱里 `curl` 探活不可信**（127.0.0.1 被代理接管，服务没起也回 **502**）→ 判活/读 API 用**裸 socket**
  （`socket.create_connection` + 手写 HTTP/1.1）；`lsof -i` 也可能**静默无输出**。
- 长任务用技能 `persistent-background-process/scripts/daemon_run.py`（**不在仓库**，**位置参数**
  `<cwd> <logfile> <pidfile> <cmd...>`）；**`nohup ... &` 活不过一轮调用**。
- **数据流水线**：`data.download --bars 1h 15m 5m` → `data.asset_class --build` → `data.funding_build`
  （缓存只存已收盘 bar）。改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**；
  **静态文件（`app.js`）每请求读盘，改它不用**。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`，2026-09-29 起）
- 唯一来源 `config.settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到 `engine.DEFAULT_SIGNAL`
  （守护进程）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」）。**两处都不许写字面量**。
- **三档必须一起报**（Sharpe/CAGR/MaxDD/回撤天数/年换手/成本）：`v5_1d_all5` **1.9372 / 39.12% / −13.31% /
  155.8 / 70.30 / 4.77%**；`v4_1d` **1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；`v3`
  **1.758 / 21.24% / −7.57% / 156.4 / 23.90 / 2.59%**。**1 天 ≠ 更优**：拿 **2.22 倍回撤深度**、
  **2.94 倍换手**换的（回撤天数几乎一样：155.8 vs 156.4）。
- **门禁数 ≠ 行数**：完整全阶段 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**（跳过 `mc` 的 tag
  只有 13 门禁 + 3 行「未评估」）。⚠️ `OPTIMIZATION_RESULTS.md` 里 3 天时代的数字是**数据重建前**的，
  **看产物，别抄报告。**
- **「没评估」不许长得像「通过」**：MC 块曾 `if mc:` → 产物不在盘就**一行不输出**，于是 `v3`/`v4_1d` 的
  「13/13」是在**唯一可证伪的判据从未跑过**时报出来的。合法两类：`(informational)` 与 `未评估`；测试
  **同时扣掉**这两类，**未声明的变红**。守卫 `test_acceptance_completeness.py`。

## 网格 / 因子集：唯一来源
- `ACCEPTED_REBALANCE_DAYS`（`1.0`）**五处引用、不许写字面量**：`engine.DEFAULT_SIGNAL`、`auto_ctl.start`
  签名默认、`auto_trader`/`run_live` argparse 默认、`webapp.spec.OPTIMAL_CLI`、`scripts/auto_demo.py`。
- 守卫 `test_accepted_grid.py`（13 条 / **9 变异全抓**）断言**一致**而非数值。CLI 默认必须**过
  `main()` 实测**（签名默认与 CLI 兜底是两条路径）；`engine.py` 3 处 `check_plan(` 里第 3 处永不 warn
  → 只要求能 warn 的 2 处；文案从 `engine.grid_days()` 派生。
- **换口径时最先坏掉的是前端那份拷贝**（`OPT.cli` 曾硬编码 `rebalance_days: 3` → 页面「偏离最优 1 处」、
  横幅走「有差异」分支、**连 Sharpe 都不印**）。现由 `diffFromOptimal()`/`gridDaysText()` 收口。
  `PRESETS` 里**两个 id 不许是同一配置**（按 `(cli, overrides)` 去重）。
- **比因子集之前先查两侧选名中位数**；**把「差异」叫「不成立」之前先算 σ**。`deflated_haircut`
  **按 tag 读**（跨 tag 比是错的）。

## 已完成检验的结论（Q32–Q35）——**推导/数字在 `FINDINGS.md` + `RULES-OPS.md`**
- **Q33**：**自写估计量必须先验证再信**（`_ols_nw` 曾把 HAC meat **多除一个 n** → t 虚高 ~45 倍）；
  **条件子集的「年化收益」无意义**，报条件均值/条件 Sharpe。**必须披露**：熊市 0.898、**高波动 0.118**。
- **Q34**：**`long_ret`/`short_ret` 是带符号 P&L** ⇒ 镜像把 long 映到 **−short**（写成 `+short` 是最易犯的错）；
  `flip_book=True` 翻转的是**装配好的目标**（不是 `score = -score`）。
- **Q35**：**三条通道别混**（影线**逐位不进损益**，但 `range_pos` 读 180 根滚动极值 ⇒ **影线是信息**；
  缺口改损益但**选股逐位不变**）。**扛不住**：**无止损/止盈/算法单**、**无账户级回撤熔断**
  （`dd_scale` 读**回测模拟净值**）⇒ 唯一反应是下一个网格点（≤24h）。守卫 `test_wick_channels.py`。

## 保证金模式：已钉死全仓 `cross`（唯一常量，2026-09-30 落地）
- **逐仓对这个对冲账本是反的**：逐仓每条腿 `1/L − mmr`（3× **32.8%**），全仓 `(1 − mmr·G)/G`
  （`G=0.446` → **223.8%**）；任意 `L≥1, G≤1` 下**全仓 ≥ 逐仓**。
- **模式 = `settings.MARGIN_MODE` 常量**：`LiveEngine.td_mode` **只读**（赋值抛异常）、
  构造传冲突值抛错、`/api/live/*` 冲突回 **400**、控制台**只读展示**、运行记录带 `td_mode`。
  **不许再有 `td_mode` 字面量或第二份可写副本。**
- ⚠️ **杠杆是账户级配置 `(instId, mgnMode, posSide)`，订单不带**；`set-leverage` 四坑
  （缺 `posSide`→400、`batch-`→404、每合约两方向各一次、**回读延迟 ~60s**）。
  ⚠️ **改配置 ≠ 改持仓**（在场那条腿 `margin`/`liqPx` 不重算）⇒ **真杠杆只能从 `liqPx` 反推**。
- ⚠️ **闸门假设必须显式**：`RiskConfig.leverage_in_force`（**不是上限**，上限在 `LiveLimits`）；
  `liquidation_ok` 另有 `leverage=` 覆盖。公式**仍是逐仓口径** ⇒ 它是**波动率筛选**、
  不是账户偿付约束；改公式会**移动验收 Sharpe**，别顺手改。对账 `limits.margin_headroom_violations()`
  → `status()["margin_headroom"]`（**warn 不 block**）。守卫 `test_margin_mode.py`
  （**28 条 / 7 变异全抓**）。**细节在 `RULES-OPS.md`。**
- 🔴 **Q44（只诊断，未修）：`account()` 的 `cur_sz` 跨模式求和**（`engine.py:472` 不分 `mgnMode`），
  而 `Plan.td_mode` 是**整个 plan 的单一值** ⇒ 两种模式并存的合约，引擎按**两腿之和**下单、
  订单只动全仓腿 ⇒ ① 逐仓腿**永久冻结**（8 条，恰是爆仓距离最差的）② `tgt < 逐仓量` 时
  **被拒单**（`sell/long` 不能开空），永不收敛。**改它 = 改下单量，需授权。**

## 展示层：每个数字都要追到产物
- **禁止**在 `app.js`/`spec.py` 写死指标，**禁止前端留一份最优参数集拷贝**：唯一来源 `/api/spec`
  的四个 headline 字段；引用旧指标必须带 `spec.RECORDED_NOTE`。**页面提到一个控件，就必须真有那个控件**；
  `scripts/smoke_live.js` 必须**逐字段镜像 `boot()`**（漏字段不报错，只让标记**双双消失**）。
- **判据坏了不许靠改判据变绿**（资金费那条 FAIL 继续报，旁边加 `pass=None` 信息行、不计入门禁数）；
  `check_traps` 的 `rev_short` **错过两次** ⇒ **警告的理由必须和用户做的事是同一件事**；
  **禁止「整文件不含某字符串」式断言**（`spec.py` 合法地枚举因子名）→ 切出定义块再查；
  **相邻对断言**优于「包含某个数」（`1.937` 是 `1.9375` 的子串）。纪律见 `dashboard-data-consistency`。

## 控制台 / 启动脚本（Q39）
- ⚠️ **`/api/` 的错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回 `501 + HTML` ⇒ 前端
  `resp.json()` 抛 `Unexpected token '<'`，**根因全被藏住**。`server.py::Handler.send_error()` 已覆盖
  （**只**对 `/api/`，静态页仍 HTML）。旧进程 `GET` 全正常，**只有新方法回 501**。
  守卫 `test_console_api_contract.py`（真起服务 + 裸 socket）。
- **`fmt.dol` 两位小数**；例外是**真·亚分钱**（`|v|<0.005` 用有效数字，`$0.00` 会假装"这是零"）。
  ⚠️ **负号在 `$` 前**（`-$1,234.50`）⇒ 先排绝对值再补符号；`-0` 仍是 `$0.00`。
- ⚠️ 写启动/变异脚本三坑（细节 `RULES-OPS.md`）：`.command` 选解释器要验**能 import numpy/pandas**，
  不是「可执行」（`~/.workbuddy/binaries` **是错的**，真路径带 `-ai`）；bash **`$VAR（` 会吞全角括号**
  ⇒ 紧跟非 ASCII 写 `${VAR}`；**变异脚本还原放 `finally`** + 首尾打 sha。

## 数据源可切换 / 换交易所（Q40）——**表格全在 `RULES-OPS.md`**
- **缓存目录唯一来源 = `settings.CACHE_DIR`**（读 `CRYPTO_CACHE_DIR`）；`store.CACHE` 与 `download.CACHE`
  都**从它派生，不许各自再算一遍**。
- **`binance_client.klines_range()` 返回与 OKX 完全同形的 9 列**。最贵的坑是字段映射：
  **`vol_ccy` ← 基础币量、`amount` ← 计价币额**（`store.vwap = amount/vol_ccy` 必须是价格量级；写反
  不报错，只会让 vwap 变 ~价格²）。守卫 `test_data_source_switch.py`（11 条）。
- ⚠️ **`min_avg_amount_usd` 是绝对美元阈值**（币安成交额是 OKX 的 **3.67 倍** ⇒ 同一个 `$3M` 在 OKX 筛出
  **16** 名、币安 **39** 名）⇒ **换数据源必须先看「有效横截面宽度」**。
- ⚠️ **比两家价格前先除掉固定面值因子**（币安 `1000PEPEUSDT` 这类**天然 ~1000 倍**）；收益是尺度无关的。
- ⚠️ **验收的 1.94 落在 ADV 门槛的尖峰上**（OKX 宇宙 ~16 时 1.942，邻居只有 1.184 / 1.511；币安最高
  **1.494**）⇒ **1.94 至少部分是 (OKX 数据, 这个阈值) 的组合属性**（**关掉门槛 1.93 → 0.07**）。
  **"同一份标的清单" ≠ "同一段历史"**（91/131 币安上市更晚）。复现 `scripts/binance_backtest.py --selectivity`；
  ⚠️ 同时持两个场所的 panel 会 OOM ⇒ **用完即 `del` + `gc.collect()`**。

## 失效归因（Q41–Q43）——**只诊断，未优化**；推导/表格在 `FINDINGS.md` Q41–Q43 + `RULES-OPS.md`
- **恒等式 `net = gross − fee − spread − impact **+** funding`**（`engine.py:433` 把带符号的
  `funding` **加**进账）⇒ 写成 `− funding` 会把每笔资金费记成**贷记**（全期 `+0.0918`）。
- ⚠️ **5 个回撤窗口不是同一套边界**：W2 的第二个数是**恢复日**、W3 的第一个数是**回撤内部的局部高点**
  ⇒ 必须**同时报 `stated` 与 `episode`（真峰→谷）**；**W2 的 stated 净收益是 +0.0188（正的）**，
  不能当亏损归因。**W4 落在样本第一个月（index=1）**。
- ⚠️ **`ic_mom[t]` 是同期量** ⇒ 用同期 IC 做 regime 标签 = 「拿结果解释结果」：**同一批 386 天**，
  同期 Sharpe **−4.97** vs 滞后 **+3.01**（**符号翻转**）。默认列只能是 `mom_ic_5d_trailing`。
- ⚠️ **IC ≠ 尾部价差**：IC 看整个横截面，P&L 只看两端，两者可以不同号（`rev_short` 就是）。
- ⚠️ **事前状态量先去趋势再谈预测力**：宽度 vs 序号 Spearman **+0.398** ⇒ 滚动百分位下 **W3 从「窄」
  翻「宽」**；去趋势后 |t| **全 < 1**，`cap×n≤1` 静默等权已实测（cv=1.2e-17）**但不致损** ⇒
  **两条候选解释均被否定**。**按每单位毛敞口算 W4 才最严重（−0.2558）**。
- ⚠️ **空腿亏损必须三分：市场 / 选股 / 加权**（逐点恒等）。判「挤压」**只能看「选股」那一项**
  （等权口径）；`excess_mw`（市值加权减等权池子）是**错的判据** —— 两口径不同，牛市里恒为负，
  且把「空得最多的那几笔涨得最多」误记成「被挤压」。
  Q43 实测：**5/5 窗口选股效应不显著**（Welch p 0.14–0.73）、**4/5 窗口大涨占比低于池子**
  ⇒ **「空腿被崩跌式反弹挤压」已否定**；亏损主要是**做空了一个上涨的市场**（W2 100%、W4 66%），
  W1 例外来自**仓位集中度**。复现 `python scripts/short_squeeze.py`。
- 🔴 **头号反复缺陷：「算不出来」被静默丢掉，最该看的窗口恰好消失。** 已犯 **3 次**
  （`momentum_reversal` 的「池 ≥20 名」、`rotation_table`/`liquidity_table` 的 `windows` 不生效、
  Q42 的 W4 滚动百分位无定义）⇒ **「样本不足 / 参数不生效 / 算不出来」必须显式输出一行 + 一个可用性
  布尔列，绝不许 `continue` 掉**；新函数收参数要有断言钉住它生效。

## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多 `sell/long`、**加空
  `sell/short`、减空 `buy/short`**；flip = 两腿。
- `_orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢了符号** → 加空的腿全变买入，**静默**
  （订单被接受、计划看着正常，仓位却反向走）。实测 demo 多头 **92.0%** vs 目标 **47.6%**
  ⇒ **成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；守卫 `test_order_sides.py`。

## 自动交易 / 设置持久化 / 三个「倍数」——**完整规则在 `RULES-OPS.md`（同名章节）**
- **`not_due` 是 `warn` 不是 `block`** ⇒ 到期判定必须写在调度器里（断言「**`execute` 根本没被调用**」），
  **绝不传 `force`**；闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；**「还差几根」=
  `R − since`**；`refresh_after_hours` **不决定窗口打开时刻**，刷新间隔必须**小于一根 bar**。
- **保存必须以「落盘」收尾并回落盘路径**；`load_limits()` 的 `None`（从未配置）**≠** `{}`（全零上限、拒掉每笔）。
- **`set_leverage` 不改任何一笔订单**；**放大目标敞口也无效** —— **硬不变量，别「修」它**（`build_plan()`
  **没有 leverage 参数**）。**唯一有效旋钮 = `execution.max_daily_turnover`**，但放开节流**会打坏策略**
  （Sharpe 1.82→1.10→0.46）。**杠杆唯一作用是保证金**（$70 在 5× 下缓冲 $0 ⇒ **18.4% 反向波动即亏光**）。

## 测试约定
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一个格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**不断言顺序。
- pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留目录 → 49 error）；**每次跑都给独立 TMPDIR**。
- ⚠️ **对账断言要用「独立算一遍」的量**：拿 `short_pnl`（本身就来自引擎 `name_gross`）去和
  `bars['short_ret']` 比，只钉住**区间分组** —— **把前向起点挪一根 bar 它不会红**。
  必须有由价格**独立算出**的列（Q43 的 `*_px`）来钉**口径**。**变异测试当场抓出过这条假守卫。**
- 新断言必须变异测试，且**必须验证「真的注入进去了」**（`src.count(old) == 1`）。用**备份+还原**（`cp` →
  改 → 跑 → `cp` 回 → 比 sha256），**不要 `git stash`**；一次性脚本用完移出仓库。**要同时断言
  「写入后 sha 变了」与「还原后 sha 回来了」。**
- 新 helper 要在测试**函数内** import（模块级 import 不存在的符号 → 整文件 collection error）。
- **多进程 sweep 不能从 stdin 跑**（spawn 报 `FileNotFoundError: '<stdin>'`）→ `n_jobs=1` 或写成真实文件。
- `smoke_live.js` MiniDom：`clearClassScope` 子树级失效必须；`#content` 重建要清 `__lastHtml`。

## 通知 / OKX
**细节全在 `RULES-OPS.md`**。必记：**配完不重启 → 守护进程静默不发**（按 mtime 热读）；`send()`
**永不抛**；**HTTP 200 不是结论** —— 四家**全部**用 200 返回「被拒」；OKX envelope `code` 与逐行
`sCode` 是**两层**，只看 `code` 会把 19 单全记失败。⚠️ **仓位 USD 名义只能读 `notional`**：
`sz × markPx` 差一个**合约面值**倍数（BTC `ctVal=0.01` ⇒ **100 倍**），**不报错、看着完全合理**。
