# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的）。长推导/表格在 `RULES-OPS.md`（**不注入**）；数字在
> `FINDINGS.md` 与日志。⚠️ **上限 ~17.2 KB**：**加东西必须等量压掉**，加完 `wc -c` 复核。

## 环境 / 沙箱 / 进程
- Python：`~/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` **静默返 0 行** → 用 `grep -E`；`--include=*.py` zsh 报错 → 用 Grep 工具。
  **无 `timeout`**；`ps` 禁 → 探活 `kill -0 $(cat <pidfile>)`。
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
## 两种「回测」：要不要先刷新行情，答案相反（Q52）——细节在 `RULES-OPS.md`
- **研究/验证回测**（`scripts/exp_*.py`、验收基线）**不刷、也不能刷**：读 `data_cache/`
  的 parquet（`load_panels` **只读盘不拉网**）。要**可复现** —— 每次跑前拉网 = 每次在
  **不同的**历史上下结论。⚠️ **记录值是某时点快照**（见 Q60）。
- **实盘/纸面的**（`engine.target()`）**必须先刷新**：它**用回测引擎在最新缓存上跑一遍**。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`）——**数字在 `FINDINGS.md`**
- 唯一来源 `settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到
  `engine.DEFAULT_SIGNAL`（守护进程）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」）。
  **都不许写字面量**。⚠️ 已核实两者**同一套**（`1h/1.0/crypto` + 五因子）。
- **三档必须一起报**：`v5_1d_all5` **1.9372 / 39.12% / −13.31% / 155.8 / 70.30 / 4.77%**；
  `v4_1d` **1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；
  `v3` **1.758 / 21.24% / −7.57% / 156.4 / 23.90 / 2.59%**。**1 天 ≠ 更优**：拿 **2.22 倍回撤
  深度**、**2.94 倍换手**换的（回撤天数几乎一样：155.8 vs 156.4）。
- **门禁数 ≠ 行数**：完整全阶段 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**。
  **「没评估」不许长得像「通过」**（MC 块曾 `if mc:` ⇒ 产物不在盘就一行不输出 ⇒
  「13/13」是在**唯一可证伪的判据从未跑过**时报出来的）。守卫
  `test_acceptance_completeness.py`。

## 网格 / 因子集：唯一来源——**细节整节在 `RULES-OPS.md`**
- `ACCEPTED_REBALANCE_DAYS`（`1.0`）**五处引用、不许写字面量**（`engine.DEFAULT_SIGNAL`、
  `auto_ctl.start`、`auto_trader`/`run_live` argparse、`webapp.spec.OPTIMAL_CLI`、
  `scripts/auto_demo.py`）。守卫 `test_accepted_grid.py`（13 条 / **9 变异全抓**）。
- **换口径时最先坏的是前端那份拷贝**（`OPT.cli` 曾硬编码 `rebalance_days: 3` ⇒ **连 Sharpe
  都不印**）⇒ 由 `diffFromOptimal()`/`gridDaysText()` 收口。**比因子集前先查两侧选名中位数**；
  **叫「不成立」前先算 σ**；`deflated_haircut` **按 tag 读**。

## 已完成检验的结论（Q32–Q35）——**推导/数字在 `FINDINGS.md`**
- **Q33**：**自写估计量必须先验证再信**（`_ols_nw` 曾把 HAC meat 多除一个 n ⇒ t 虚高 ~45
  倍）；**条件子集的「年化收益」无意义**；**必须披露**熊市 0.898、高波动 0.118。

## 保证金模式（Q44–Q47）——**细节整节在 `RULES-OPS.md`**
- **单源 `settings.MARGIN_MODE` = `isolated`**；**模式必须和杠杆一起动**（逐仓距离
  = `1/L − mmr`）⇒ 实测强平频率 **3× → 29.5 次/年**、**1× → 1.0** ⇒ **按 1× 跑**。
- 🔴 **`positions[].lever` 是配置镜像、会滞后撒谎** ⇒ **报杠杆前必须从 `liqPx` 反推**
  （`L = 1/(dist+mmr)`）。已因此误报过「ETH short 20×」（实为 1.01×）。
- ⚠️ 杠杆是**账户级配置**、订单不带；**`td_mode` 只读**（写死标签会显示**相反**模式）。
- 🔴 **Q44（只诊断未修）：`account()` 的 `cur_sz` 跨模式求和** ⇒ 引擎按两腿之和下单、
  订单只动一种腿 ⇒ 另一种腿**永久冻结**、`tgt < 该腿量` 时**被拒单**。改它需授权。

## 展示层 / 控制台（Q39）——**细节在 `RULES-OPS.md`**
- **禁止**在 `app.js`/`spec.py` 写死指标、**禁止前端留最优参数集拷贝**（模式**显示标签**也写死过）：
  唯一来源 `/api/spec` 四个 headline 字段；引用旧指标须带 `spec.RECORDED_NOTE`。**页面提到一个
  控件就必须真有那个控件**；`smoke_live.js` 须**逐字段镜像 `boot()`**。
- **判据坏了不许靠改判据变绿**（资金费那条 FAIL 继续报，旁边加 `pass=None` 行、不计门禁）；
  **警告的理由必须和用户做的事是同一件事**；**禁止「整文件不含某字符串」**→切定义块再查；
  **相邻对断言**优于「包含某数」。纪律见 `dashboard-data-consistency`。
- ⚠️ **`/api/` 错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回 `501 + HTML`
  ⇒ 前端 `resp.json()` 抛 `Unexpected token '<'`、**根因全藏住**。**`fmt.dol` 两位小数**；
  **负号在 `$` 前**。⚠️ 写变异脚本：解释器要验**能 import numpy/pandas**；bash **`$VAR（`
  吞全角括号** ⇒ 写 `${VAR}`；**还原放 `finally`**。

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

## 无人值守 + 通知（Q49–Q53）——**细节整节在 `RULES-OPS.md`**
- 🔴 **执行者是控制台自己**（`webapp/auto_schedule.py`，**启动时自恢复**），**走和网页按钮
  同一队列**。到点判定是**窗口**不是阈值、错过**不补**；`enabled`/`running` 是两个量、
  派生 `will_trade`（⚠️ 测试覆盖**四种组合**、mock 打**对对象**）。
- 🔴 **通知要和「执行」一起搬**；`execute()` 的 `action` 恒为 `"rebalance"` ⇒ **必须翻译**；
  拦截原因填 **`blocked_by`**。⚠️ **返回是嵌套的**而格式化器读**扁平**键 ⇒ 消息**全是「—」**。
- 🔴 **沙箱代理只在工具调用期间存活**（控制台继承 `HTTP_PROXY`）⇒ 11:00 `urlopen` 报
  **`Errno 61`**，而**订单照下** ⇒ "交易成功、通知静默"。修法 `build_opener(ProxyHandler({}))`。
  ⚠️ **通知结果落进 `runs.jsonl`**（写回要 `reversed()`）；**自检端点须由控制台进程自己发**。
- 🔴 **模块重复加载**（`import live_api` vs `from webapp import live_api` 是两个对象）⇒
  线程建在 A、HTTP 读 B。**统一用包路径导入**。

## 鉴权 / 二重认证 / 外网（Q54–Q57）——**细节整节在 `RULES-OPS.md`**
- 🔴 **闸门在唯一入口**（`_gate()` 挂在 `do_GET/POST/DELETE` 开头），**不散在各端点**；
  `_PUBLIC_API` 是**白名单**。未登录回 **JSON 401**（不是 302）；闸门须在**路由之前** ⇒
  未登录时「存在/不存在」路径回**同一个** 401（否则是枚举通道）。
- 🔴 **白名单别手写文件名**（写 `/app.js` 页面请求 `/static/app.js` ⇒ 只剩空壳、**日志正常**）
  ⇒ 用**前缀**；守卫**从 `index.html` 抓 `src=`/`href=`**。**同一条规则写两遍就会不一致**。
- 🔴 **前端改动必须真渲染再断言**（我两轮 grep/裸 socket "通过"、用户看到空白页）。
  🔴 **`ROOT` 从别的文件抄会多爬一级**（`webapp/*` 一层、`execution/*` 两层）。
- 🔴 **`==` 与 `compare_digest` 行为相同** ⇒ 只验 True/False 的测试**抓不到**替换。
- **明文绑非本机 ⇒ 拒绝启动**；⚠️ `Secure` 跟**「浏览器那一段」的协议**。🔴 **隧道 ≠
  TLS 已解决** ⇒ 明文绑 `127.0.0.1`；🔴 **Host 头要校验**（回 **421**）⇒ 防 rebinding。
- 🔴 **门禁测试两个方向都要验**。⚠️ **变异 `-k` 选错 ⇒ 0 selected 被当成「抓到」**。

## 新机器 / 产物初始化 + 内存（Q58–Q62）——**细节在 `RULES-OPS.md`**
- 入口 `scripts/init_linux.sh`（幂等）→ `download` → `asset_class --build` → `funding_build`
  → **5b 重建基线产物** + **内存体检**。⚠️ **数据/产物/凭据全被 gitignore**。
- 🔴 **代理端口写在默认值里**：三客户端读同一个 `OKX_PROXY`（**默认 `127.0.0.1:7897`**）
  ⇒ 换机器报 **`[Errno 111] Connection refused`**（**看不出是代理问题**）。
  `export OKX_PROXY=...`；空 ⇒ 直连。⚠️ **模块导入时**求值 ⇒ **必须先 export**。
- 🔴 **`--end` 默认硬编码 `2026-09-26`（排他上界）** ⇒ 照默认跑**静静少二十来天、不报错**。
- 🔴 **页头「基线数据缺失」是必然**。重建 **10 秒**：`rebuild_optimal_baseline.py`（只跑
  `base`；参数**从 `webapp/spec.py` 派生**）。⚠️ **`--override` 走 `json.loads`** ⇒ 列表须写
  **JSON 数组**；`asset_class` 缺新合约 **fail-loud 退出**。
- 🔴 **重建的数字 ≠ 记录值，不可修复**（`--end` 被刻意忽略 + 候选池是「今天的挂单」）。
  **记录值只是某时点快照。** 🔴 **别覆盖已验收基线**（默认拒绝，要 `--force`）。
- 🔴 **`-9` / `137` / `Killed` = SIGKILL = OOM**（不是报错）⇒ 日志**无 traceback**。
  三处都会撞：命令行回测（`-9`）、**控制台「⚡ 一键准备」**（`137`；= 刷新 + **跑一次完整
  回测**，控制台 182 MB + 计划 1.8 GB）、**「仅刷新行情」安全**（不跑回测）。
  ✅ 已降 **2193→1758 MB**，常驻 `load_panels` **1236→555 MB**，**数值逐位不变**：
  ① `_read_one` 读入即转 **float32** + `del raw`；② **`wide()` 一步构造**
  `pd.DataFrame(cols, index=grid, dtype="float32")` —— 原先 `DataFrame→reindex→astype`
  是**三次全量拷贝**（8 字段×3 份 ≈1.5GB 瞬时），这项省 **418 MB**。
  ⇒ **2GB 机器仍建议加 swap**。⚠️ 不是泄漏。
- ⚠️ **从缓存/产物里看到的配置可能是旧的** ⇒ 报"配置是什么"直接读 `DEFAULT_SIGNAL` /
  `spec.OPTIMAL_CLI`，别读某次运行的快照。

## 飞书通知配置（Q63）——**细节在 `RULES-OPS.md`**
- **`config/notify.json` 被 gitignore**（里面是 webhook URL = 凭据）⇒ **每台机器都要配**。
- ⭐ **有内置 CLI**（不用手改 JSON）：`python -m crypto_ls_research.execution.notify`
  带 `--show` / `--test` / `--set-url` / `--set-secret` / `--set-keyword`。
  三个 `--set-*` **写完立刻自检**；`--set-url` 自动写 **`enabled: true`**（手改最容易漏）。
- 🔴 **飞书安全设置必须和配置对上**：签名校验→`--set-secret`；自定义关键词→
  `--set-keyword`（不对上 ⇒ **`code 19024`**）；IP 白名单→不用填但要填出口 IP。
- `notify_on` 默认 `traded/blocked/error`（**`not_due` 刻意不推**）；`modes` 默认
  `demo/live`（**不含 `paper`**）。⚠️ **改配置不用重启控制台**（每次发送重读盘）。

## 回测日志 / 失败退出码（Q61）——细节在 `DEPLOY_LINUX.md` / `RULES-OPS.md`
- 🔴 **原先不写任何日志文件**（76 个 `print()` 全去 stdout）⇒「失败了去哪看日志」
  **没有答案**。⭐ 现在 `--log` **默认开**（`logs/research-<tag>-<时间戳>.log`）。
- 🔴 **阶段失败原先退出码是 0** ⇒ `init_linux.sh`/CI/`cmd && next` 把**失败当成功**。
  现在收集 `failed`、收尾 `sys.exit(1)` + `### FAILED stages`。
- ⚠️ **改完 `.py` 第一件事是 `ast.parse`**。⚠️ 看日志用 `... 2>&1 | tee logs/x.log`。

## 测试约定——**细则在 `RULES-OPS.md`**
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**
  不断序。pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留 → 49 error）。
- ⚠️ **对账断言要用「独立算一遍」的量**：拿引擎自己的 `short_pnl` 去比 `bars['short_ret']`
  只钉住**区间分组** —— **前向起点挪一根 bar 它不会红**。必须有**价格独立算出**的列
  （Q43 的 `*_px`）钉**口径**。**变异测试当场抓出过这条假守卫。**
- ⚠️ **新断言必须变异测试**，并验证「真的注入进去了」（用文件 sha 比对）。⚠️ **变异 `-k`
  选错 ⇒ 0 selected 会被当成「抓到」**。
- 🔴 **重测试必须主动缩小输入**：`pytest` **一个进程跑到底**，子进程退出后**主进程常驻
  不退还** ⇒ "每条单独都过"的测试合起来能把机器吃爆（**137，且套件突然没了、
  没有任何失败信息**）。**制造失败要挑最便宜的路径**（我用"不存在的因子名"，
  结果付了一次完整回测 1.8GB；改成"不存在的 stage 名" ⇒ 137 → 1.07 秒）。
  只验证"日志落盘"之类的机制时加 `--max-insts 12`（105 秒 → 9 秒）。
- ⚠️ **解析文本做守卫先确认真的解析到了内容**（按行解析 shell 而调用是**续行**的 ⇒ 参数
  一个没抓到、注入 `--bogus-flag` 全绿）⇒ **先拼续行** + 前提断言；
  ⚠️ **扫全文件找违规会扫到自己的注释** ⇒ 只看非 `#` 行。

## 通知 / OKX
细节在 `RULES-OPS.md`。必记：**配完不重启 → 静默不发**（按 mtime 热读）；`send()` **永不抛**；
**HTTP 200 不是结论**（四家全部用 200 返「被拒」）；OKX envelope `code` 与逐行 `sCode`
是**两层**。⚠️ **仓位 USD 名义只能读 `notional`**：`sz × markPx` 差一个**合约面值**倍数
（BTC `ctVal=0.01` ⇒ **100 倍**），**不报错、看着合理**。
