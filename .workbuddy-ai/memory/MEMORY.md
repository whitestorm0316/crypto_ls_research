# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的、必须照做的）。长推导/表格在 `RULES-OPS.md`（**不注入**）；数字在
> `artifacts/FINDINGS.md` 与 `.workbuddy-ai/memory/<日期>.md`。
> ⚠️ **注入上限 ~17.2 KB**（实测）。**加东西必须等量压掉**，细则写 `RULES-OPS.md`；
> 加完 `wc -c` 复核，**超 16.5 KB 就再压**。

## 环境 / 沙箱 / 进程
- Python：`/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` **静默返 0 行** → 用 `grep -E`；`--include=*.py` zsh 报错 → 用 Grep 工具。
  **无 `timeout`**；`ps` 被禁 → 探活 `kill -0 $(cat <pidfile>)`；**禁 `rm -rf`**。
- ⚠️ **`/tmp` 在两次工具调用之间被清空** → pidfile/log **放仓库内**（`logs/`）。
- 控制台 **8790**；`server.py::main()` 默认 **8770**（隔壁 a-stock 在用）→ **必须位置参数传**
  `python -m webapp.server 8790`。漏掉 = `OSError: [Errno 48]`，而 `lsof`/`netstat` **查不到**。
- **沙箱里 `curl` 探活不可信**（127.0.0.1 被代理接管，没起也回 **502**）→ 判活/读 API 用**裸 socket**
  （`socket.create_connection` + 手写 HTTP/1.1）；`lsof -i` 也可能**静默无输出**。
- 长任务用技能 `persistent-background-process/scripts/daemon_run.py`（**不在仓库**，**位置参数**
  `<cwd> <logfile> <pidfile> <cmd...>`）；**`nohup ... &` 活不过一轮调用**。
- **数据流水线**：`data.download --bars 1h 15m 5m` → `data.asset_class --build` → `data.funding_build`
  （缓存只存已收盘 bar）。改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**；
  **静态文件（`app.js`）每请求读盘，不用重启**。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`，2026-09-29 起）
- 唯一来源 `config.settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到 `engine.DEFAULT_SIGNAL`
  （守护进程）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」）。**都不许写字面量**。
- **三档必须一起报**（Sharpe/CAGR/MaxDD/回撤天数/年换手/成本）：`v5_1d_all5` **1.9372 / 39.12% / −13.31% /
  155.8 / 70.30 / 4.77%**；`v4_1d` **1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；`v3`
  **1.758 / 21.24% / −7.57% / 156.4 / 23.90 / 2.59%**。**1 天 ≠ 更优**：拿 **2.22 倍回撤深度**、
  **2.94 倍换手**换的（回撤天数几乎一样：155.8 vs 156.4）。
- **门禁数 ≠ 行数**：完整全阶段 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**（跳 `mc` 只有 13 门禁）。⚠️ `OPTIMIZATION_RESULTS.md` 里 3 天时代数字是**数据重建前**的。
- **「没评估」不许长得像「通过」**：MC 块曾 `if mc:` → 产物不在盘就**一行不输出**，于是 `v3`/`v4_1d` 的
  「13/13」是在**唯一可证伪的判据从未跑过**时报出来的。合法两类：`(informational)` 与 `未评估`；测试
  **同时扣掉**这两类，**未声明的变红**。守卫 `test_acceptance_completeness.py`。

## 网格 / 因子集：唯一来源
- `ACCEPTED_REBALANCE_DAYS`（`1.0`）**五处引用、不许写字面量**：`engine.DEFAULT_SIGNAL`、`auto_ctl.start`
  签名默认、`auto_trader`/`run_live` argparse 默认、`webapp.spec.OPTIMAL_CLI`、`scripts/auto_demo.py`。
- 守卫 `test_accepted_grid.py`（13 条 / **9 变异全抓**）断言**一致**而非数值。CLI 默认必须**过
  `main()` 实测**（签名默认与 CLI 兜底是两条路径）；`engine.py` 3 处 `check_plan(` 里第 3 处永不 warn
  → 只要求能 warn 的 2 处；文案从 `engine.grid_days()` 派生。
- **换口径时最先坏掉的是前端那份拷贝**（`OPT.cli` 曾硬编码 `rebalance_days: 3` → 页面「偏离最优」、
  横幅走「有差异」分支、**连 Sharpe 都不印**）⇒ 现由 `diffFromOptimal()`/`gridDaysText()` 收口。
  `PRESETS` 里**两个 id 不许是同一配置**。
- **比因子集之前先查两侧选名中位数**；**把「差异」叫「不成立」之前先算 σ**。`deflated_haircut`
  **按 tag 读**（跨 tag 比是错的）。

## 已完成检验的结论（Q32–Q35）——**推导/数字在 `FINDINGS.md` + `RULES-OPS.md`**
- **Q33**：**自写估计量必须先验证再信**（`_ols_nw` 曾把 HAC meat **多除一个 n** → t 虚高 ~45 倍）；
  **条件子集的「年化收益」无意义**（报条件均值/条件 Sharpe）。**必须披露**：熊市 0.898、**高波动 0.118**。
- **Q34**：**`long_ret`/`short_ret` 是带符号 P&L** ⇒ 镜像把 long 映到 **−short**（写成 `+short` 是最易犯的错）；
  `flip_book=True` 翻转的是**装配好的目标**（不是 `score = -score`）。
- **Q35**：**三条通道别混**（影线**逐位不进损益**，但 `range_pos` 读 180 根滚动极值 ⇒ **影线是信息**；
  缺口改损益但**选股逐位不变**）。**扛不住**：**无止损/止盈/算法单**、**无账户级回撤熔断**
  （`dd_scale` 读**回测模拟净值**）⇒ 唯一反应是下一网格点。守卫 `test_wick_channels.py`。

## 保证金模式：单源常量 `settings.MARGIN_MODE` = **`isolated`**（2026-09-30 定案）
- **两次决定，理由不同、都成立**：① **频率** —— 逐仓每腿 `1/L − mmr`（3× **32.8%**），全仓
  `(1 − mmr·G)/G`（`G=0.43` → **231%**），任意 `L≥1,G≤1` 下**全仓 ≥ 逐仓**；② **偿付** ——
  **空头亏损无上限**，全仓下单合约暴涨打爆**整个账户**（实测最脆弱那条 **30.7 倍**）。
  **选了偿付**：全仓更不易被强平但一爆是全部；逐仓封顶在那条腿但常被强平。
- 🔴 **模式必须和杠杆一起动**：逐仓距离就是 `1/L − mmr` ⇒ 实测强平频率（K=10/年，上界）
  **3× → 29.5 次**、2× → 10.1、**1× → 1.0** ⇒ **按 1× 跑**。代价**不随流动性下降**
  （三档 ADV 频率 0.671/0.597/0.675%）⇒ **换宇宙解决不了**。
- **唯一来源 = 常量**：`td_mode` **只读**（赋值抛异常）、构造传冲突值抛错、`/api/live/*` 冲突回 **400**、
  控制台**只读展示**且**标签从服务端值派生**（写死会在常量翻转时显示**相反**的模式）、运行记录带它。
  **不许第二份可写副本。** ⚠️ **测试里的「冲突值」必须派生**（`_OTHER_MODE`）：写死会让测试
  在常量翻转后**不再测试任何东西却依然全绿**。
- ⚠️ **杠杆是账户级配置 `(instId, mgnMode, posSide)`，订单不带**；`set-leverage` 四坑
  （缺 `posSide`→400、`batch-`→404、每合约两方向各一次、**回读延迟 ~60s**）。
  🔴 **`positions[].lever` 是配置镜像、会滞后撒谎** —— **报杠杆前必须从 `liqPx` 反推**
  （`L = 1/(dist+mmr)`）。已因此误报过一次「ETH short 20×」（实为 1.01×）。
- ⚠️ **闸门假设必须显式**：`RiskConfig.leverage_in_force`（**不是上限**，上限在 `LiveLimits`）；
  公式**仍是逐仓口径** ⇒ 它是**波动率筛选**、不是偿付约束；改它会**移动验收 Sharpe**。
  对账 `margin_headroom_violations()` → `status()["margin_headroom"]`（**warn 不 block**）。
  守卫 `test_margin_mode.py`（**28 条 / 变异 4/5，第 5 条刻意不抓**）。
- 🔴 **Q44（只诊断，未修）：`account()` 的 `cur_sz` 跨模式求和**（`engine.py:472` 不分 `mgnMode`），
  而 `Plan.td_mode` 是**整个 plan 的单一值** ⇒ 引擎按**两腿之和**下单、订单只动一种腿 ⇒
  ① 另一种腿**永久冻结** ② `tgt < 该腿量` 时**被拒单**（`sell/long` 不能开空），永不收敛。
  **改它 = 改下单量，需授权。**

## 展示层：每个数字都要追到产物
- **禁止**在 `app.js`/`spec.py` 写死指标，**禁止前端留最优参数集拷贝**（模式**显示标签**也写死过
  一次）：唯一来源 `/api/spec` 的四个 headline 字段；引用旧指标必须带 `spec.RECORDED_NOTE`。
  **页面提到一个控件，就必须真有那个控件**；`smoke_live.js` 必须**逐字段镜像 `boot()`**
  （漏字段不报错，只让标记**双双消失**）。
- **判据坏了不许靠改判据变绿**（资金费那条 FAIL 继续报，旁边加 `pass=None` 信息行、不计入门禁数）；
  `check_traps` 的 `rev_short` **错过两次** ⇒ **警告的理由必须和用户做的事是同一件事**；
  **禁止「整文件不含某字符串」式断言**→ 切定义块再查；**相邻对断言**优于「包含某数」
  （`1.937` 是 `1.9375` 的子串）。纪律见 `dashboard-data-consistency`。

## 控制台 / 启动脚本（Q39）
- ⚠️ **`/api/` 错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回 `501 + HTML` ⇒ 前端
  `resp.json()` 抛 `Unexpected token '<'`，**根因全被藏住**。`server.py::Handler.send_error()` 已覆盖（**只**对 `/api/`）。旧进程 `GET` 全正常，**只有新方法回 501**。
  守卫 `test_console_api_contract.py`。
- **`fmt.dol` 两位小数**；例外是**真·亚分钱**（`|v|<0.005` 用有效数字，`$0.00` 会假装"这是零"）。
  ⚠️ **负号在 `$` 前**（`-$1,234.50`）⇒ 先排绝对值再补符号；`-0` 仍是 `$0.00`。
- ⚠️ 写启动/变异脚本三坑（细节 `RULES-OPS.md`）：`.command` 选解释器要验**能 import numpy/pandas**；
  bash **`$VAR（` 吞全角括号** ⇒ 紧跟非 ASCII 写 `${VAR}`；**变异脚本还原放 `finally`** + 首尾打 sha。

## 数据源可切换 / 换交易所（Q40）——**表格全在 `RULES-OPS.md`**
- **缓存目录唯一来源 = `settings.CACHE_DIR`**（读 `CRYPTO_CACHE_DIR`）；`store.CACHE` 与 `download.CACHE`
  都**从它派生，不许各自再算一遍**。
- **`binance_client.klines_range()` 返回与 OKX 完全同形的 9 列**。最贵的坑是字段映射：
  **`vol_ccy` ← 基础币量、`amount` ← 计价币额**（`vwap = amount/vol_ccy` 必须是价格量级；写反
  不报错，只会让 vwap 变 ~价格²）。守卫 `test_data_source_switch.py`（11 条）。
- ⚠️ **`min_avg_amount_usd` 是绝对美元阈值**（币安成交额是 OKX 的 **3.67 倍** ⇒ 同一个 `$3M` 在 OKX 筛出
  **16** 名、币安 **39** 名）⇒ **换数据源必须先看「有效横截面宽度」**。
- ⚠️ **比两家价格前先除掉固定面值因子**（`1000PEPEUSDT` 这类**天然 ~1000 倍**）；收益尺度无关。
- ⚠️ **验收的 1.94 落在 ADV 门槛的尖峰上**（OKX 宇宙 ~16 时 1.942，邻居只有 1.184 / 1.511；币安最高
  **1.494**）⇒ **1.94 至少部分是 (OKX 数据, 这个阈值) 的组合属性**（**关掉门槛 1.93 → 0.07**）。
  **"同一份标的清单" ≠ "同一段历史"**（91/131 币安上市更晚）。复现 `binance_backtest.py --selectivity`；
  ⚠️ 同时持两个场所的 panel 会 OOM ⇒ **用完即 `del` + `gc.collect()`**。

## 失效归因（Q41–Q43）——**只诊断，未优化**
🔴 **细节整节在 `RULES-OPS.md`**（**`net = gross − …**+** funding` 的符号**、5 个回撤窗口边界不同、同期 IC 是「拿结果解释结果」、IC≠尾部价差、空腿亏损三分、**头号反复缺陷「算不出来」被静默丢掉**）。


## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多 `sell/long`、**加空
  `sell/short`、减空 `buy/short`**；flip = 两腿。
- `_orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢符号** → 加空腿全变买入、**静默**
  （订单被接受、计划正常，仓位反向走）。实测 demo 多头 **92.0%** vs 目标 **47.6%**
  ⇒ **成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；守卫 `test_order_sides.py`。

## 自动交易 / 设置持久化 / 三个「倍数」——**完整规则在 `RULES-OPS.md`（同名章节）**
- **`not_due` 是 `warn` 不是 `block`** ⇒ 到期判定必须写进调度器（断言「**`execute` 没被调用**」），
  **绝不传 `force`**；闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；**「还差几根」=
  `R − since`**；`refresh_after_hours` **不决定窗口打开时刻**，刷新间隔必须**小于一根 bar**。
- **保存必须以「落盘」收尾并回落盘路径**；`load_limits()` 的 `None`（从未配置）**≠** `{}`（全零、拒每笔）。
- **`set_leverage` 不改任何一笔订单**；**放大目标敞口也无效** —— **硬不变量，别「修」它**（`build_plan()`
  **没有 leverage 参数**）。**唯一有效旋钮 = `execution.max_daily_turnover`**，但放开节流**会打坏策略**
  （Sharpe 1.82→1.10→0.46）。**杠杆唯一作用是保证金**（$70 在 5× 下缓冲 $0 ⇒ **18.4% 反向波动即亏光**）。

## 无人值守开关 + 保证金显示（2026-10-10）
- 🔴 **执行者是控制台自己，不是独立守护进程**（Q49）：控制台双重 fork + `setsid()`、
  父进程 launchd ⇒ 能长期活着；Web 按钮派生的守护进程**会被进程树回收**（老毛病）。
  调度器 `webapp/auto_schedule.py` 活在控制台内，**启动时自恢复**（`server.py::main`
  读开关重建线程），否则"重启一次再也不自动"而页面仍显「已开启」。
- **必须走和网页按钮同一个作业队列**（`live_api._submit`），不自己调 `execute()`。
- 🔴 **到点判定是「窗口」不是「阈值」**：`at <= now <= at+10min`，错过**不补**
  （实测：第一版 `now>=at`，15:25 重启控制台时它立刻拿过期信号跑了一次）。
  幂等账**只在成功提交后写**；失败要能重试。
- **`enabled`（落盘许可位）与 `running`（调度线程活着）是两个量**，派生
  `will_trade = enabled AND running`。⚠️ 测试须覆盖**四种组合** —— 只测 `(False,False)`
  会让两者同值 ⇒ **断言恒真**（假守卫）。⚠️ mock 要打**对对象**（`running` 来自
  `_REGISTRY`，不是 `auto_ctl`）。**实盘无确认短语闸门**（用户裁定）。
- 🔴 **默认关、且必须 `is True` 不能用真值测试**：`resolve_auto_enabled` 对非 dict /
  缺失 / 类型不对（`"false"`、`1`）一律 False。⚠️ `"false"` 在 Python 里是**真值**，
  用 `bool()` 会把开关**静默打开**（变异测试验证过这条）。
- 🔴 **其余细节（保证金只读字段不折算、`LIVE_DIR` 无 env、先关后刷）整节在 `RULES-OPS.md`。**


## 定时器 / 实盘 opt-in
🔴 **细节整节在 `RULES-OPS.md`**（`allow_live=False` 硬编码导致实盘起不来、`LABELS/UNITS` 按模式分开、**`launchctl` 不能从沙箱注册**、守卫须早于第一次 `auto_ctl.` 调用）。**2026-10-10 起执行者改为控制台自身**（见下节）。


## 测试约定
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**不断序。
- pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留 → 49 error）；**每次独立 TMPDIR**。
- ⚠️ **对账断言要用「独立算一遍」的量**：拿 `short_pnl`（本身来自引擎 `name_gross`）去比
  `bars['short_ret']`，只钉住**区间分组** —— **前向起点挪一根 bar 它不会红**。
  必须有价格**独立算出**的列（Q43 的 `*_px`）钉**口径**。**变异测试当场抓出过这条假守卫。**
- 新断言必须变异测试，且**验证「真的注入进去了」**（`src.count(old) == 1`）。用**备份+还原**（`cp` →
  改 → 跑 → `cp` 回 → 比 sha256），**不要 `git stash`**；脚本用完移出仓库。**同时断言
  「写入后 sha 变了」与「还原后 sha 回来了」。**
- 新 helper 要在测试**函数内** import（模块级 import 不存在符号 → 整文件 collection error）。
- **多进程 sweep 不能从 stdin 跑**（spawn 报 `FileNotFoundError`）→ `n_jobs=1` 或真实文件。
- `smoke_live.js` MiniDom：`clearClassScope` 子树级失效必须；`#content` 重建要清 `__lastHtml`。

## 通知 / OKX
细节在 `RULES-OPS.md`。必记：**配完不重启 → 静默不发**（按 mtime 热读）；`send()` **永不抛**；
**HTTP 200 不是结论** —— 四家**全部**用 200 返「被拒」；OKX envelope `code` 与逐行 `sCode` 是**两层**。⚠️ **仓位 USD 名义只能读 `notional`**：
`sz × markPx` 差一个**合约面值**倍数（BTC `ctVal=0.01` ⇒ **100 倍**），**不报错、看着合理**。
