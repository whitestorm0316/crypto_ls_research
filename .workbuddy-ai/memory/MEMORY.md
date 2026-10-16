# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的、必须照做的）。长推导/表格在 `RULES-OPS.md`（**不注入**）；
> 数字在 `FINDINGS.md` 与日志 `<日期>.md`。⚠️ **注入上限 ~17.2 KB**：**加东西必须等量
> 压掉**（细则写 `RULES-OPS.md`），加完 `wc -c` 复核、**超 16.5 KB 再压**。

## 环境 / 沙箱 / 进程
- Python：`~/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` **静默返 0 行** → 用 `grep -E`；`--include=*.py` zsh 报错 → 用 Grep 工具。
  **无 `timeout`**；`ps` 禁 → 探活 `kill -0 $(cat <pidfile>)`；**禁 `rm -rf`**。
- ⚠️ **`/tmp` 在两次工具调用间被清空** → pidfile/log **放仓库内**。
- 控制台 **8790**；`server.py::main()` 默认 **8770**（隔壁 a-stock 在用）→ **必须位置参数传**
  `python -m webapp.server 8790`。漏掉 = `OSError: [Errno 48]`，`lsof` **查不到**。
- **沙箱里 `curl` 探活不可信**（127.0.0.1 被代理接管，没起也回 **502**）→ 判活/读 API 用
  **裸 socket**。
- 长任务用技能 `persistent-background-process/scripts/daemon_run.py`（**不在仓库**，**位置
  参数** `<cwd> <logfile> <pidfile> <cmd...>`）；**`nohup ... &` 活不过一轮调用**。
- **数据流水线**：`data.download --bars 1h 15m 5m` → `data.asset_class --build` →
  `data.funding_build`。改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**；
  **静态文件（`app.js`）每请求读盘，不用重启**。
## 两种「回测」：要不要先刷新行情，答案相反（Q52）
- **研究/验证回测**（`scripts/exp_*.py`、验收基线）**不刷、也不能刷**：读 `data_cache/` 的
  parquet（`load_panels` **只读盘不拉网**）。要**可复现** —— 每次跑前拉网 = 每次在**不同的**
  历史上下结论，基线立刻不可比。刷新是**独立显式**一步，刷完**旧数字作废**。
- **实盘/纸面的**（`engine.target()`）**必须先刷新**：它是**用回测引擎在最新缓存上跑一遍**
  ⇒ 缓存不新、目标仓就是旧的（`auto_runner` 的顺序因此是 `refresh → 计划 → 下单`）。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`，2026-09-29 起）
- 唯一来源 `config.settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到
  `engine.DEFAULT_SIGNAL`（守护进程）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」）。
  **都不许写字面量**。
- **三档必须一起报**（Sharpe/CAGR/MaxDD/回撤天数/年换手/成本）：`v5_1d_all5`
  **1.9372 / 39.12% / −13.31% / 155.8 / 70.30 / 4.77%**；`v4_1d`
  **1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；`v3`
  **1.758 / 21.24% / −7.57% / 156.4 / 23.90 / 2.59%**。**1 天 ≠ 更优**：拿 **2.22 倍
  回撤深度**、**2.94 倍换手**换的（回撤天数几乎一样：155.8 vs 156.4）。
- **门禁数 ≠ 行数**：完整全阶段 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**（跳 `mc` 只有 13 门禁）。
  ⚠️ `OPTIMIZATION_RESULTS.md` 里 3 天时代数字是**数据重建前**的。
- **「没评估」不许长得像「通过」**：MC 块曾 `if mc:` → 产物不在盘就一行不输出，于是
  「13/13」是在**唯一可证伪的判据从未跑过**时报出来的。合法两类：`(informational)` 与
  `未评估`；测试**同时扣掉**这两类，**未声明的变红**。守卫 `test_acceptance_completeness.py`。

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

## 已完成检验的结论（Q32–Q35）——**推导/数字在 `FINDINGS.md`**
- **Q33**：**自写估计量必须先验证再信**（`_ols_nw` 曾把 HAC meat 多除一个 n ⇒ t 虚高 ~45
  倍）；**条件子集的「年化收益」无意义**；**必须披露**熊市 0.898、**高波动 0.118**。

## 保证金模式（Q44–Q47）——**细节整节在 `RULES-OPS.md`**
- **单源常量 `settings.MARGIN_MODE` = `isolated`**。**模式必须和杠杆一起动**：逐仓距离
  就是 `1/L − mmr` ⇒ 实测强平频率 **3× → 29.5 次/年**、**1× → 1.0** ⇒ **按 1× 跑**；
  代价**不随流动性下降** ⇒ **换宇宙解决不了**。
- 🔴 **`positions[].lever` 是配置镜像、会滞后撒谎** ⇒ **报杠杆前必须从 `liqPx` 反推**
  （`L = 1/(dist+mmr)`）。已因此误报过一次「ETH short 20×」（实为 1.01×）。
- ⚠️ **杠杆是账户级配置 `(instId, mgnMode, posSide)`、订单不带**；`set-leverage` 四坑
  （缺 `posSide`→400、`batch-`→404、每合约两方向各一次、**回读延迟 ~60s**）。
- ⚠️ **`td_mode` 只读**（赋值抛异常）、构造传冲突值抛错、`/api/live/*` 冲突回 400；标签从
  服务端值派生（写死会显示**相反**的模式）。⚠️ 测试里的「冲突值」必须**派生**。
- 🔴 **Q44（只诊断未修）：`account()` 的 `cur_sz` 跨模式求和** ⇒ 引擎按两腿之和下单、
  订单只动一种腿 ⇒ 另一种腿**永久冻结**、`tgt < 该腿量` 时**被拒单**，永不收敛。改它需授权。

## 展示层：每个数字都要追到产物
- **禁止**在 `app.js`/`spec.py` 写死指标，**禁止前端留最优参数集拷贝**（模式**显示标签**也写死过）：
  唯一来源 `/api/spec` 的四个 headline 字段；引用旧指标须带 `spec.RECORDED_NOTE`。
  **页面提到一个控件就必须真有那个控件**；`smoke_live.js` 须**逐字段镜像 `boot()`**。
- **判据坏了不许靠改判据变绿**（资金费那条 FAIL 继续报，旁边加 `pass=None` 信息行、不计门禁）；
  `check_traps` 的 `rev_short` **错过两次** ⇒ **警告的理由必须和用户做的事是同一件事**；
  **禁止「整文件不含某字符串」**→切定义块再查；**相邻对断言**优于「包含某数」
  （`1.937` 是 `1.9375` 的子串）。纪律见 `dashboard-data-consistency`。

## 控制台 / 启动脚本（Q39）——**细节在 `RULES-OPS.md`**
- ⚠️ **`/api/` 错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回 `501 + HTML`
  ⇒ 前端 `resp.json()` 抛 `Unexpected token '<'`，**根因全被藏住**。`server.py::send_error()`
  已覆盖（**只**对 `/api/`）。⚠️ 旧进程 `GET` 全正常，**只有新方法回 501**。
  守卫 `test_console_api_contract.py`。
- **`fmt.dol` 两位小数**，例外是**真·亚分钱**（`|v|<0.005` 用有效数字）；⚠️ **负号在 `$` 前**
  （`-$1,234.50`）⇒ 先排绝对值再补符号；`-0` 仍是 `$0.00`。
- ⚠️ 写启动/变异脚本三坑：`.command` 选解释器要验**能 import numpy/pandas**；bash
  **`$VAR（` 吞全角括号** ⇒ 紧跟非 ASCII 写 `${VAR}`；**变异脚本还原放 `finally`** + 打 sha。

## 数据源可切换 / 换交易所（Q40）——**表格全在 `RULES-OPS.md`**
- **缓存目录唯一来源 = `settings.CACHE_DIR`**（读 `CRYPTO_CACHE_DIR`）；`store.CACHE` 与
  `download.CACHE` **都从它派生，不许各自再算一遍**。
- **`binance_client.klines_range()` 返回与 OKX 同形的 9 列**。⚠️ 最贵的坑是字段映射：
  **`vol_ccy` ← 基础币量、`amount` ← 计价币额**（写反不报错，只让 vwap 变 ~价格²）。
  守卫 `test_data_source_switch.py`（11 条）。
- ⚠️ **`min_avg_amount_usd` 是绝对美元阈值**（币安成交额是 OKX 的 **3.67 倍** ⇒ 同一 `$3M`
  在 OKX 筛出 **16** 名、币安 **39** 名）⇒ 换源先看「有效横截面宽度」。
- ⚠️ **比两家价格前先除掉固定面值因子**（`1000PEPEUSDT` 天然 ~1000 倍）。
- ⚠️ **验收的 1.94 落在 ADV 门槛的尖峰上**（邻居仅 1.184/1.511；币安最高 **1.494**）⇒ 它至少
  部分是 (OKX 数据, 这个阈值) 的组合属性（**关门槛 1.93→0.07**）。**"同一份清单" ≠ "同一段
  历史"**（91/131 币安上市更晚）。⚠️ 两场所 panel 同持会 OOM ⇒ 用完即 `del`。

## 失效归因（Q41–Q43）——**只诊断，未优化**
🔴 **细节整节在 `RULES-OPS.md`**（`net = gross − …**+** funding` 的符号、5 个回撤窗口
边界不同、同期 IC 是「拿结果解释结果」、IC≠尾部价差、空腿亏损三分、
**头号反复缺陷「算不出来」被静默丢掉**）。

## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多
  `sell/long`、**加空 `sell/short`、减空 `buy/short`**；flip = 两腿。
- `_orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢符号** → 加空腿全变买入、
  **静默**（订单被接受、计划正常，仓位反向走）。实测 demo 多头 **92.0%** vs 目标 **47.6%** ⇒
  **成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；守卫 `test_order_sides.py`。

## 自动交易 / 设置持久化 / 三个「倍数」——**完整规则在 `RULES-OPS.md`（同名章节）**
- **`not_due` 是 `warn` 不是 `block`** ⇒ 到期判定必须写进调度器（断言「**`execute` 没被调用**」），
  **绝不传 `force`**；闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；**「还差几根」=
  `R − since`**；`refresh_after_hours` **不决定窗口打开时刻**，刷新间隔必须**小于一根 bar**。
- **保存必须以「落盘」收尾并回落盘路径**；`load_limits()` 的 `None`（从未配置）**≠** `{}`（全零、拒每笔）。
- **`set_leverage` 不改任何一笔订单**；**放大目标敞口也无效** —— **硬不变量，别「修」它**（`build_plan()`
  **没有 leverage 参数**）。**唯一有效旋钮 = `execution.max_daily_turnover`**，但放开节流**会打坏策略**
  （Sharpe 1.82→1.10→0.46）。**杠杆唯一作用是保证金**（$70 在 5× 下缓冲 $0 ⇒ **18.4% 反向波动即亏光**）。

## 无人值守开关（2026-10-10/12）
- 🔴 **执行者是控制台自己**（双重 fork + `setsid()` ⇒ 长期活着），`webapp/auto_schedule.py`，
  **启动时自恢复**。**必须走和网页按钮同一个作业队列**（`live_api._submit`）。
- 🔴 **到点判定是「窗口」不是「阈值」**：`at <= now <= at+10min`，错过**不补**
  （实测：第一版 `now>=at`，15:25 重启时立刻拿过期信号跑了一次）。幂等账**成功后才写**。
- **`enabled`/`running` 是两个量**，派生 `will_trade`。⚠️ 测试覆盖**四种组合**
  （只测 `(False,False)` ⇒ 断言恒真）；⚠️ mock 打**对对象**（`_REGISTRY`）。
- 🔴 **细节（`is True` 不能真值测试、`LIVE_DIR` 无 env、保证金只读）在 `RULES-OPS.md`。**

## 无人值守 + 通知（Q49–Q53）——**细节整节在 `RULES-OPS.md`**
- 🔴 **执行者是控制台自己**（`webapp/auto_schedule.py`，**启动时自恢复**），**走和网页按钮
  同一个作业队列**。到点判定是**窗口**不是阈值、错过**不补**；`enabled`/`running` 是两个量、
  派生 `will_trade`（⚠️ 测试覆盖**四种组合**、mock 打**对对象**）。
- 🔴 **通知要和「执行」一起搬**；`execute()` 的 `action` 恒为 `"rebalance"` ⇒ **必须翻译**；
  拦截原因填 **`blocked_by`**。⚠️ **返回是嵌套的**而格式化器读**扁平**键 ⇒ 消息**全是「—」**。
- ⚠️ **两个毛敞口差一个 NAV 倍** ⇒ **倍数大得离谱时先怀疑单位**；通知带净值/保证金/可用。
- 🔴 **沙箱代理只在工具调用期间存活**（控制台继承 `HTTP_PROXY`）⇒ 11:00 `urlopen` 报
  **`Errno 61`**，而**订单照下** ⇒ "交易成功、通知静默"。修法 `build_opener(ProxyHandler({}))`。
- ⚠️ **通知结果落进 `runs.jsonl`**（写回要 `reversed()`）；**自检端点须由控制台进程自己发**。
- 🔴 **模块重复加载**（顶层名 vs 包名是两个对象）⇒ 线程建在 A、HTTP 读 B。**统一用包路径导入**。

## 鉴权 / 二重认证 / 外网（Q54）——**细节整节在 `RULES-OPS.md`**
- 🔴 **闸门在唯一入口**（`do_GET/POST/DELETE` 开头一行 `_gate()`），**不散在各端点**；
  `_PUBLIC_API` 是**白名单**（新端点默认要登录）。未登录回 **JSON 401**（不是 302）；闸门须在
  **路由之前** ⇒ 未登录时「存在/不存在」的路径回**同一个** 401（否则是枚举通道）。
- 🔴 **白名单别手写文件名**（我写 `/app.js`，页面请求 `/static/app.js` ⇒ CSS/JS 被 401
  挡掉只剩空壳，**日志一切正常**）⇒ 静态资源用**前缀** `/static/`；守卫**从 `index.html`
  抓 `src=`/`href=`** 逐个断言，不在测试里再抄一份。
- 🔴 **同一条规则写两遍就会不一致**：`_gate` 的「未建账号」分支曾**又抄一遍**白名单
  （只认文件名不认前缀）⇒ 首次使用时 `/static/app.js` 401 ⇒ **连「创建账号」按钮都没有**。
  只留 `_needs_auth` 一处定义 + "`_gate` 代码行不许出现 `_PUBLIC_STATIC`" 守卫。
- 🔴 **前端改动必须真渲染再断言**：我连续两轮 grep/裸 socket "验证通过"而用户看到空白页。
  headless Chrome：`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  --headless --disable-gpu --no-sandbox --dump-dom <url>`（或 `--screenshot=`）。
- 🔴 **`ROOT` 从别的文件抄会多爬一级**：`crypto_ls_research/execution/*` 在根下**两层**、`webapp/*`
  **一层** ⇒ 照搬会让凭据落到**仓库外面**（`.gitignore` 管不到、删也删不掉）。
- 🔴 **`==` 与 `compare_digest` 行为完全相同** ⇒ 只验 True/False 的测试**抓不到**替换，
  必须**断言语义**并精确到那一处（文件里可能有多处）。
- **明文绑非本机 ⇒ 拒绝启动**；Cookie 的 `Secure` **只在 TLS 下加**（明文加它浏览器丢
  Cookie、本机永远登不上）。守卫 `test_auth.py`（36 条 / 变异 10/10）。

## 测试约定
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**不断序。
- pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留 → 49 error）；**每次独立 TMPDIR**。
- ⚠️ **对账断言要用「独立算一遍」的量**：拿 `short_pnl`（本身来自引擎 `name_gross`）去比
  `bars['short_ret']`，只钉住**区间分组** —— **前向起点挪一根 bar 它不会红**。
  必须有价格**独立算出**的列（Q43 的 `*_px`）钉**口径**。**变异测试当场抓出过这条假守卫。**
- 新断言必须变异测试，且**验证「真的注入进去了」**（`src.count(old) == 1`）。用**备份+还原**
  （`cp`→改→跑→`cp` 回→比 sha256），**不要 `git stash`**；脚本用完移出仓库。
- 新 helper 要在测试**函数内** import（模块级 import 不存在符号 → 整文件 collection error）。
- **多进程 sweep 不能从 stdin 跑**（spawn 报 `FileNotFoundError`）→ `n_jobs=1` 或真实文件。
- `smoke_live.js` MiniDom：`clearClassScope` 子树级失效必须；`#content` 重建要清 `__lastHtml`。

## 通知 / OKX
细节在 `RULES-OPS.md`。必记：**配完不重启 → 静默不发**（按 mtime 热读）；`send()` **永不抛**；
**HTTP 200 不是结论**（四家全部用 200 返「被拒」）；OKX envelope `code` 与逐行 `sCode`
是**两层**。⚠️ **仓位 USD 名义只能读 `notional`**：`sz × markPx` 差一个**合约面值**倍数
（BTC `ctVal=0.01` ⇒ **100 倍**），**不报错、看着合理**。
