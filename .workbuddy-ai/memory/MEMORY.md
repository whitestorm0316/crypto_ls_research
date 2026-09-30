# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的、必须照做的）；来龙去脉在 `.workbuddy-ai/memory/<日期>.md` 与 `artifacts/FINDINGS.md`。

## 环境 / 沙箱 / 进程
- Python：`/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` **静默返 0 行** → 用 `grep -E`；`--include=*.py` 在 zsh 报错 → 用 Grep 工具。
  **无 `timeout`**；`ps` 被禁 → 探活 `kill -0 $(cat <pidfile>)`；**禁 `rm -rf`**；`TMPDIR="$(mktemp -d)"`。
- ⚠️ **`/tmp` 在两次工具调用之间被清空** → pidfile/log **放仓库内**（`logs/`）。
- 控制台 **8790**；`webapp/server.py::main()` 默认 **8770**（隔壁 a-stock 在用）→ **必须位置参数传**：
  `python -m webapp.server 8790`。漏掉 = `OSError: [Errno 48]`，而 `lsof`/`netstat` **查不到监听**。
- **沙箱里 `curl` 探活不可信**（127.0.0.1 被代理接管，服务没起也回 **502**）→ 判活/读 API 用**裸 socket**
  （`socket.create_connection` + 手写 HTTP/1.1）。`lsof -i` 也可能**静默无输出**。
- 长任务用技能 `persistent-background-process/scripts/daemon_run.py`（**不在仓库**），**位置参数**
  `<cwd> <logfile> <pidfile> <cmd...>`。**`nohup ... &` 活不过一轮工具调用**。
  改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**。
- **存活只看心跳 `artifacts/live/<mode>/auto.json` 里的 `pid`**（`auto.pid` 那个 JSON 会陈旧）。
- **数据流水线**：`data.download --bars 1h 15m 5m` → `data.asset_class --build` → `data.funding_build`；
  缓存只存已收盘 bar（细节见 `RULES-OPS.md`）。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`，2026-09-29 起）
- 唯一来源 `config.settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到 `engine.DEFAULT_SIGNAL`
  （守护进程跑的）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」的）。**两处都不许写字面量**。
- **三档必须一起报**：`v5_1d_all5` **1.9372 / 39.12% / −13.31% / 回撤 155.8 天 / 换手 70.30 / 成本 4.77%**；
  `v4_1d`（1 天 2 因子）**1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；
  `v3`（3 天）**1.758 / 21.30% / −7.57% / 23.92**。**1 天 ≠ 更优**（拿 2.22 倍回撤、2.94 倍换手换的）。
- **门禁数 ≠ 行数**：完整全阶段运行 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**（跳过 `mc` 的 tag
  只有 13 条门禁 + 3 行「未评估」）。⚠️ `OPTIMIZATION_RESULTS.md` 里 3 天时代的数字是**数据重建前**的，
  **看产物，别抄报告。**

## 门禁的完整性
- **「没评估」不许长得像「通过」**：MC 块曾 `if mc:` → 产物不在盘上就**一行不输出**，于是 `v3`/`v4_1d`
  的「13/13」是在**唯一可证伪的判据从未跑过**时报出来的。合法两类：`(informational)` 与 `未评估`；
  测试**同时扣掉**这两类，**未声明的变红**。守卫 `tests/test_acceptance_completeness.py`。

## 网格 / 因子集：唯一来源
- `ACCEPTED_REBALANCE_DAYS`（`1.0`）**五处引用、不许写字面量**：`engine.DEFAULT_SIGNAL`、`auto_ctl.start`
  签名默认、`auto_trader`/`run_live` argparse 默认、`webapp.spec.OPTIMAL_CLI`、`scripts/auto_demo.py`。
- 守卫 `tests/test_accepted_grid.py`（13 条 / **9 变异全抓**）断言**一致**而非数值。CLI 默认必须**过 `main()`
  实测**（签名默认与 CLI 兜底是两条路径）；`engine.py` 3 处 `check_plan(` 里第 3 处永不 warn → 只要求能
  warn 的 2 处；文案从 `engine.grid_days()` 派生。
- **换口径时最先坏掉的是前端那份拷贝**（`OPT.cli` 曾硬编码 `rebalance_days: 3` → 页面显示「偏离最优 1 处」、
  横幅走「有差异」分支、**连 Sharpe 都不印**）。现由 `diffFromOptimal()`/`gridDaysText()` 收口。
  `PRESETS` 里**两个 id 不许是同一个配置**（守卫按 `(cli, overrides)` 去重）。

## 五因子的保留项（转正 ≠ 四项消失）
- 保留：① 3 天网格排序反转（1.762 vs 1.581）⇒ 增益是 **(因子集, 网格) 组合**的属性；② 折 test 不占优
  而 train 高得多；③ 锁定 2026 更差（2.740 → 2.172）；④ 实际 horizon（1 天）IC 不显著 ——
  收益来自**选股与分散**。**独立 book 仍亏钱**。数字见 `FINDINGS.md` Q32。
- ⚠️ **`deflated_haircut` 必须按 tag 读**（每次运行重生成）—— **跨 tag 比是错的**。
- ⚠️ `check_traps` 的 `rev_short` 条目**错过两次** ⇒ **警告的理由必须和用户做的事是同一件事。**
- `cap × n ≤ 1` → `inverse_vol_weights` **静默退化成等权**（`cap=0.20` ⇒ 任一侧 ≤5 名）。
  比因子集**先查两侧选名中位数**；**把「差异」叫「不成立」之前先算 σ**。

## 盲测：收益是策略还是市场（FINDINGS Q33）
- **三个问题可分离**：① **BETA** —— β≈0 是**构造出来的**，测它是**同义反复**；② **LUCK**（置换分数）
  —— **唯一可证伪**；③ **DIRECTION** —— 分腿与牛/震荡/熊。
- 结论 **策略**：α 年化 42.7% / t=4.89、R² 0.0024–0.0216（**对带宽 0–48 lags 不敏感**）；零假设
  p = 0.0050 / 0.0050 / 0.0323。**必须披露**：熊市 0.898、**高波动 0.118**。
- **自写估计量必须先验证再信**：`_ols_nw` 曾把 HAC 的 meat **多除一个 n** → t 虚高 ~45 倍；
  **条件子集的「年化收益」无意义**，报条件均值与条件 Sharpe。数字见 `FINDINGS.md` Q33。

## 反向检验：反过来做（FINDINGS Q34）
- `flip_book=True` 翻转**装配好的目标**（`select_book` 之后 `base = -base`），**不是** `score = -score`
  （后者连选股一起改：`_stable_book` 的留任缓冲是路径依赖的）。守卫 `tests/test_flip_book.py`
  （5 条 / **4 变异全抓**）。
- 恒等式 **`net_flip = −(net + 2·cost)`**（镜像要再付一份同额换手成本）；正常 **+1.9368** /
  照做反向 **−2.2572** / 纯镜像 **+1.7088 → −1.7088**（**精确反号**，关 ADV 上限后每行 `0.000e+00`）；
  **逐年 6 窗口零例外**。
- ⚠️ **`long_ret`/`short_ret` 是带符号 P&L** ⇒ 镜像把 long 映到 **−short**（写成 `+short` 是最易犯的错）；
  **两个「反过来」不是一回事**（「照做」被回撤缩放压小 ⇒ −2.26 是**下界**）。
  **分层表与全部细节在 `RULES-OPS.md`。**
## 插针（wick）：价格面扛得住，信息面不设防（FINDINGS Q35）
- **三条通道别混，混着答两头都错**：① **收益路径** `ret_exec[t] = open[t+1]/open[t] − 1` **不读
  `high`/`low`** ⇒ 影线**逐位不进损益**；② **信号路径** `range_pos`（**五因子之一**）读 180 根滚动极值 ⇒
  **影线是信息**：**一根宽 bar 压扁它 7.5 天，全管线无去刺**；③ `open` 在 `factors/engine.py` **从不出现**
  ⇒ 缺口改损益但**选股与目标权重全样本逐位不变**。
- ⚠️ 缺口解析式 **`w × Σ held_i·(open[t+1]/open[t])`**，**不是** `w × net_exposure`（名义额按**冲击后**价标记）。
- **扛得住（构造出来的）**：净/毛 **0.0797**（全市场 −20% 只亏 1.74%）；`max_gross_frac=1.0` ⇒ 爆仓需同向 99.5%。
- **扛不住**：**无止损 / 止盈 / 算法单**、**无账户级回撤熔断**（`dd_scale` 读**回测模拟净值**）⇒ 唯一反应是
  下一个网格点（≤24h）。**四个「屏幕上有但不咬人」**见 `RULES-OPS.md`；守卫 `tests/test_wick_channels.py`。

## 保证金模式：钉死全仓 `cross`，不许做成选项（FINDINGS Q36 / Q37 / Q38）
- **逐仓对这个对冲账本是反的**：逐仓每条腿 `1/L − mmr`（3× **32.8%** / 5× **19.5%** / 100× **0.5%**），
  全仓 `(1 − mmr·G)/G`（`G=0.446` → **223.8%**）；任意 `L≥1, G≤1` 下**全仓 ≥ 逐仓**。
  对冲腿的盈利在逐仓下救不了另一条腿，强平还会**把对冲拆成裸方向仓**。
- ⚠️ **`td_mode` 不落盘、无唯一主人**：`LiveLimits`/`limits.json`/成交记录都没有，`AutoTrader`
  **从不传它** ⇒ 守护进程恒 `cross`；控制台可被一次请求改成 `isolated`，**重启即静默回 `cross`**。
- ⚠️ **杠杆是账户级配置 `(instId, mgnMode, posSide)`，订单不带**：`place_order` 无杠杆字段 ⇒ 持仓
  **继承**交易所已有值；`_apply_leverage` 被 `set_leverage=None` 门控 ⇒ **空转**（7 次执行记录
  **没有一个带 `leverage` 键**）。写 `set-leverage` **四坑**：缺 `posSide` → **400**、
  `batch-set-leverage` → **404**、每合约要 long/short **各一次**、**回读有几十秒延迟**（写完立刻读
  会骗你，必须等 ~60s 复核）。
- ⚠️ **改配置 ≠ 改持仓**（Q38）：格子改了、`lever` 字段也跟着变，**但已在场那条腿的 `margin`/`liqPx`
  不会重算**（NEAR 实测 3→5：两者**逐位不变**，>65s 仍不变）⇒ **「统一设成 3×」救不了 BTC 那条
  100× 腿**，只能平掉重开或加保证金。真杠杆只能从 `liqPx` 反推 `L ≈ 1/(|liqPx/mark − 1| + mmr)`。
- ⚠️ **`liquidation_ok` 读 `rcfg.max_leverage` 而非实际 `lever`** ⇒ 闸门恒假设 19.50%。实测 demo：
  **9 个合约同时两种模式**、全账户默认 **3×**（连未碰过的 LINK/AVAX/ATOM），`BTC isolated = 100×`
  是**遗留值** ⇒ 该腿距爆仓 **0.57%**，闸门**偏 33 倍**。
  审计 `scripts/audit_margin_mode.py`；守卫 `tests/test_margin_mode.py`（14 条 / 2 条 `xfail(strict=True)`）。

## 展示层：每个数字都要追到产物
- **禁止**在 `app.js`/`webapp/spec.py` 写死指标，**禁止前端留一份最优参数集拷贝**：唯一来源 `/api/spec`
  的四个 headline 字段；引用旧指标必须带 `spec.RECORDED_NOTE`。**页面提到一个控件，就必须真有那个控件**；
  `scripts/smoke_live.js` 必须**逐字段镜像 `boot()`**（漏字段不报错，只让标记**双双消失**）。
- **判据坏了不许靠改判据变绿**（资金费那条 FAIL 继续报，旁边加 `pass=None` 信息行、不计入门禁数）；
  **禁止「整文件不含某字符串」式断言**（`spec.py` 合法地枚举因子名）→ 切出定义块再查；
  **相邻对断言**优于「包含某个数」（`1.937` 是 `1.9375` 的子串）。
- 完整纪律见技能 `dashboard-data-consistency` 与 `RULES-OPS.md`。

## 控制台 / 启动脚本（FINDINGS Q39）
- ⚠️ **`/api/` 的错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回
  `501 + HTML` ⇒ 前端 `resp.json()` 抛 `Unexpected token '<'`，**根因全被藏住**。
  `server.py::Handler.send_error()` 已覆盖（**只**对 `/api/`，静态页仍 HTML）。
  **重启只对 `.py` 是必需的** —— 静态文件（`app.js`）**每请求读盘**（实测服务端字节 == 磁盘字节），
  改它**不用重启**；改 `server.py` 才要。旧进程 `GET` 全正常，**只有新方法回 501**。
  守卫 `tests/test_console_api_contract.py`（真起服务 + 裸 socket）。
- **`fmt.dol` 带两位小数**（`$2,586.98`；原来 `Math.round` 显示成 `$2,587`）。唯一例外是
  **真·亚分钱**（`|v|<0.005` 用有效数字）—— `$0.00` 会假装"这是零"，比不显示小数更糟。
  ⚠️ **负号必须在 `$` 前**：直接拼 `'$' + v.toLocaleString()` 得到 `$-1,234.50`（符号打架）
  ⇒ 先取绝对值排版、再补符号；`-0` 仍是 `$0.00`。
- ⚠️ **`.command` 选解释器要验「能 import numpy/pandas」，不是「可执行」**：候选表里的
  `~/.workbuddy/binaries` **是错的**（真路径 `~/.workbuddy-ai/binaries`）⇒ 回退到
  `/opt/homebrew/bin/python3`（**没有 numpy**）⇒ 报错在 `import numpy` 处，**指不到根因**。
- ⚠️ **bash `$VAR（` 会把全角括号吞进变量名**（`set -u` 下 `unbound variable`）⇒
  紧跟非 ASCII 时必须写 `${VAR}`。
- ⚠️ **变异脚本：还原放 `finally`**（漏过一次，`app.js` 被留在变异态）；收尾打 sha。

## 数据源可切换 / 换交易所（FINDINGS Q40）
- **缓存目录唯一来源 = `settings.CACHE_DIR`**（读 `CRYPTO_CACHE_DIR`，默认路径逐字不变）；
  `store.CACHE` 与 `download.CACHE` 都**从它派生，不许各自再算一遍**。
- **`binance_client.klines_range()` 返回与 OKX 完全同形的 9 列**（下游零改动）。字段映射是最贵的坑：
  **`vol_ccy` ← 基础币量、`amount` ← 计价币额**（`store.vwap = amount/vol_ccy` 必须是价格量级；
  写反不报错，只会让 vwap 变 ~价格²）。守卫 `tests/test_data_source_switch.py`（11 条）。
- ⚠️ **比两家价格一致性前必须先除掉固定面值因子**：币安有 `1000PEPEUSDT` 这类 1000 倍计价合约，
  **价格水平比天然 ~1000**（第一版没除，p99 报出 999.36）。收益是尺度无关的，只影响水平比较。
- ⚠️ **`min_avg_amount_usd` 是绝对美元阈值，不是尺度无关的量**：币安报的 USDT 成交额是 OKX 的
  **3.67 倍** ⇒ 同一个 `$3M` 在 OKX 筛出 **16** 名、币安筛出 **39** 名 —— **有效横截面被换掉了**，
  两边根本不是同一个策略。换数据源/换场所必须先看**有效横截面宽度**。
- ⚠️ **验收的 1.94 落在 ADV 门槛的尖峰上**：OKX 宇宙 ~16 时 1.942，左右邻居只有 1.184（~24.5）
  和 1.511（~10.2）；币安全程最高 **1.494**，且在宇宙 10–50 之间平得多（1.34–1.49）。
  ⇒ **1.94 至少部分是 (OKX 数据, 这个阈值) 的组合属性，不是纯市场属性。**
- ⚠️ **关掉 ADV 门槛 OKX 从 1.93 掉到 0.07** ⇒ 结果高度依赖**一个宇宙阈值参数**。
- **"同一份标的清单" ≠ "同一段历史"**：91/131 个标的币安上市更晚（UMA 2023-05 vs 2021-01）。
- 复现 `python scripts/binance_backtest.py --selectivity`；⚠️ 同时持两个场所的 panel 会 OOM
  （exit 137）⇒ **每个场所用完即 `del` + `gc.collect()`**。

## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多 `sell/long`、
  **加空 `sell/short`、减空 `buy/short`**、平空 `buy/short`、开空 `sell/short`；flip = 两腿。
- `planner._orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢了符号** → 每条加空的腿都变成
  买入，**静默**（订单被接受、不报错、计划看着正常，只是仓位朝反方向走）。实测 demo 实际多头 **92.0%**
  vs 目标 **47.6%** → **demo 至今的成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；
  守卫 `tests/test_order_sides.py`（8 转移 × 2 模式 + **性质断言**）。

## 三个「倍数」只有一个改订单（推导与实测表见 `RULES-OPS.md`）
- **`set_leverage` 不改任何一笔订单**（只压维持保证金）；**放大目标敞口也无效**（换手预算是净值的比例）。
  **硬不变量，别「修」它** —— `build_plan()` **没有 leverage 参数**（`tests/test_size_invariance.py`
  钉签名与调用点）。
- **唯一有效旋钮 = `execution.max_daily_turnover`**（$70 上 0.2 → **6 笔/39.6%**，1.0 → **14 笔/93.1%**），
  ⚠️ 但当**回测参数**扫 Sharpe **1.82→1.10→0.46** —— **放开节流就打坏策略**。不变量是「净值 × 节流」。
- **杠杆唯一作用是保证金**：$70 在 5× 下最多持 $350 = **正好用光、缓冲 $0**（**18.4% 反向波动即亏光**）。
  ⚠️ `§16.5`「$70 加 L 倍 ≡ 有效资金 $70L」**与实现不符**（已加 §16.5.1），但 MDD 列仍有效。
- **测量纪律**：比杠杆必须用**同一个引擎**（价格缓存 15s TTL）；**仓位小先查换手爬坡、拒腿、订单方向。**

## 本金门槛（实测；表在 `RULES-OPS.md`，别抄报告旧数）
- 出厂节流 0.2 下：$70 → **39.4%** 覆盖率、$500 → 96.8%、$2,000 → 100%；`min_viable_capital` ≈ **$1,048**。
  $70 的权重误差中位 **0.0663** ≈ 单腿平均目标权重 **0.0640** ⇒ **误差与信号同量级**。
  `min_nav_usd` 默认 **$50** ⇒ $70 **不会被风控拦住**。产物 `scripts/plan_report.py --mode demo`。

## 自动交易（无人值守调仓）
- 入口 `... execution.auto_trader --mode demo --interval 30 --rebalance-days 1.0 --bar 1h
  --refresh-after-hours 1.0`；策略 `engine.DEFAULT_SIGNAL`。实盘需 `--allow-live`。
- **`not_due` 是 `warn` 不是 `block`** ⇒ 引擎**不拦**非调仓日，到期判定必须写在调度器里，断言
  「**`execute` 根本没被调用**」。**绝不传 `force`**。闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；旧规则 `since >= R`
  会**恒定落后回测一个周期**。实盘补一根「执行 bar」⇒ 窗口 **2 根宽**；**「还差几根」= `R − since`**。
  **`next_decision_ts` 不是「未来的下一个决策时刻」**（= 最后一笔已记账网格点 + R，常已过去）；
  后端 `explain_not_due` 与前端 `dueHint` 是两个渲染点，测试必须**从 fixture 派生**。
- **面板/时刻三坑**（`load_panels` 的 `extend_to_last=False` 必须保留、窗口 = 北京 11:10–13:10 成交价
  `open(G+1)`、信号缓存 key 含代码指纹）**展开全在 `RULES-OPS.md`**。
- **`refresh_after_hours` 只决定抗漏 tick 余量，不决定窗口打开时刻**：刷新间隔必须**小于一根 bar**，
  否则漏一个 tick ⇒ `since` 0→2 ⇒ **整天不成交而日志全是 `not_due`**。**只看 `bars_since_decision`。**
- **限额闸门比 `plan.realised_gross`**（被节流后），不是 `target.gross`。
- **跨进程调仓锁** `store.rebalance_lock(mode)`（`O_EXCL`，TTL 900s）：并发 `execute()` 会各发
  **完整**订单 ⇒ 仓位翻倍。`os.kill(pid,0)` 的 `EPERM` = **存在但无权发信号** ⇒ **存活**。
- `gross_realised` 必须是**实际成交后的账**（被拒的腿保持原仓位），不是目标值。

## 设置持久化（「保存了重启不生效」）
- 保存必须以**落盘**收尾并回**落盘路径**（限额在 `artifacts/live/<mode>/limits.json`，**故意不在
  `LEDGER_FILES`**）。只赋内存再回 `{"ok":True}`，与「按钮没接线」无法区分。
- `load_limits()` 的 `None`（从未配置）**≠** `{}`（用户清空 → 全零上限，拒掉每笔）→ 没值必须传 `None`。
- 同名不同义必须标注：账户区「换手预算」= `execution.max_daily_turnover`；限额表 `max_turnover_frac`
  = **硬闸门**（超即整批拒绝）；两个调用点同源（`_turnover_from()`）。`onclick = liveSaveLimits` 会把
  MouseEvent 当 `requireDue`（恒真）→ 必须包 `() => ...`；无显式传参时**读屏幕勾选框**。

## 测试约定
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一个格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**不断言顺序。
- pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留目录 → 49 error）；**每次跑都给独立 TMPDIR**。
- 新断言必须变异测试，且**必须验证「真的注入进去了」**（`src.count(old) == 1`）。用**备份+还原**
  （`cp` → 改 → 跑 → `cp` 回 → 比 sha256），**不要 `git stash`**；一次性脚本用完移出仓库。
  **变异脚本要同时断言「写入后 sha 变了」与「还原后 sha 回来了」。**
- 新 helper 要在测试**函数内** import（模块级 import 不存在的符号 → 整文件 collection error）。
- **多进程 sweep 不能从 stdin 跑**（spawn 报 `FileNotFoundError: '<stdin>'`）→ 用 `n_jobs=1` 或写成真实文件。
- `smoke_live.js` MiniDom：`clearClassScope` 子树级失效必须；`#content` 重建要清 `__lastHtml`；
  **结构断言**在"真跑一遍"需网络/信号时合法。

## 通知 / OKX
**细节全在 `RULES-OPS.md`**（provider 状态码表、OKX 两层响应）。必须记住的：**配完不重启 → 守护进程
静默不发**（按 mtime 热读）；`send()` **永不抛**；**HTTP 200 不是结论** —— 四家**全部**用 200 返回
「被拒」；OKX 批量响应 envelope `code` 与逐行 `sCode` 是**两层**，只看 `code` 会把 19 单全记失败。
⚠️ **仓位 USD 名义只能读 `notional`**：`sz × markPx` 差一个**合约面值**倍数（BTC `ctVal=0.01` ⇒
**100 倍**），而且**不报错、数字看着完全合理**。外部 payload 先打一个样本的全部字段再写聚合。

