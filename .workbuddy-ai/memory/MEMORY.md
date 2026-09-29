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
  `<cwd> <logfile> <pidfile> <cmd...>`。**`nohup ... &` 活不过一轮工具调用**，只有 `daemon_run.py` 能活。
  改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**。
- **存活只看心跳 `artifacts/live/<mode>/auto.json` 里的 `pid`**：`artifacts/live/<mode>/auto.pid`
  那个 **JSON 会陈旧**，不是「启动返回 ok」。

## 数据流水线（细节见 `RULES-OPS.md`）
`data.download --bars 1h 15m 5m` → `data.asset_class --build` → `data.funding_build`；缓存只存已收盘 bar。

## 验收口径 = 1 天网格 + **五因子全选**（`v5_1d_all5`，2026-09-29 起）
- 唯一来源 `config.settings.ACCEPTED_FACTORS` / `ACCEPTED_REBALANCE_DAYS`，派生到 `engine.DEFAULT_SIGNAL`
  （守护进程跑的）与 `webapp.spec.OPTIMAL_OVERRIDES`（控制台标「最优」的）。**两处都不许写字面量**。
- **三档必须一起报**：`v5_1d_all5` **1.9372 / 39.12% / −13.31% / 回撤 155.8 天 / 换手 70.30 / 成本 4.77%**；
  `v4_1d`（1 天 2 因子）**1.8215 / 32.53% / −16.79% / 244.9 / 70.23 / 6.05%**；
  `v3`（3 天）**1.758 / 21.30% / −7.57% / 23.92**。**1 天 ≠ 更优**（拿 2.22 倍回撤、2.94 倍换手换的）。
- **门禁数 ≠ 行数**：完整全阶段运行 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**；**跳过 `mc` 的 tag
  只有 13 条门禁，并多出 3 行「未评估」**。
- ⚠️ `OPTIMIZATION_RESULTS.md` 里 3 天时代的数字（如 v2 = 1.937）是**数据重建前**的。**看产物，别抄报告。**

## 门禁的完整性
- **「没评估」不许长得像「通过」**：MC 块曾 `if mc:` → 产物不在盘上就**一行不输出**，于是 `v3`/`v4_1d`
  的「13/13」是在**唯一可证伪的判据从未跑过**时报出来的。必须输出显式 `pass=None` 行。合法两类：
  `(informational)`（量了但不当门禁）与 `未评估`（从没评估）；测试**同时扣掉**这两类，**未声明的变红**。
  守卫 `tests/test_acceptance_completeness.py`。

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
- 保留：① **3 天网格排序反转**（1.762 vs 1.581）⇒ 增益是 **(因子集, 网格) 组合**的属性；② 折 **test** 均值
  不占优（2.029 vs 1.957）而 **train** 高得多（1.872 vs 1.503）；③ **锁定 2026 更差**（2.740 → 2.172）；
  ④ **实际交易 horizon（1 天）IC 掉到不显著**（+0.0225/t=3.10 → +0.0051/t=0.70）—— 收益来自**选股与分散**。
  **独立 book 仍亏钱**（毛 Sharpe 全负、混合权重全 0.0）。
- ⚠️ **`deflated_haircut` 必须按 tag 读**（该表每次运行重生成：`v4_1d` 1.9375 / `v5_1d_all5` 1.9570）——
  **跨 tag 比是错的**；按 tag 读两个口径**都没过自己那份**，五因子更接近（−0.020 vs −0.117）。
- ⚠️ `check_traps` 的 `rev_short` 条目**错过两次**：引过**任何产物里都查不到**的毛 Sharpe；引过
  **独立 book** 证据去说**复合分量**的事。**警告的理由必须和用户做的事是同一件事。**
- `cap × n ≤ 1` → `construct.inverse_vol_weights` **静默退化成等权**（`cap=0.20` ⇒ 任一侧 ≤5 名）。
  比因子集**必须先查两侧选名中位数是否相同**；**把「差异」叫「不成立」之前先算 σ**（胜率差 0.19pp
  在 σ=1.09pp 面前什么都不说明）。

## 盲测：收益是策略还是市场（FINDINGS Q33）
- **三个问题可分离**：① **BETA** —— β≈0 是**构造出来的**（净敞口 mean-abs 0.051），测它是**同义反复**；
  ② **LUCK**（置换分数）—— **唯一可证伪**；③ **DIRECTION** —— 分腿与牛/震荡/熊。
- 结论 **策略**：α 年化 42.7% / t=4.89、R² 0.0024–0.0216（**对带宽 0–48 lags 不敏感**）；零假设
  p = 0.0050 / 0.0050 / 0.0323，安慰剂平均 Sharpe −0.74…−1.13（200 次 0 次达标）。**必须披露**：熊市 0.898、
  **高波动 0.118**。数字见 `FINDINGS.md` Q33 与 `35_market_regression.csv`。
- **自写估计量必须先验证再信**：`_ols_nw` 曾把 HAC 的 meat **多除一个 n** → t 虚高 ~45 倍；用**移动块
  bootstrap** 对合成 AR(1) 校准才定下 lag 数。**条件子集的「年化收益」无意义**（复利一个按符号选出来的
  子集是在量选择），要报条件均值与条件 Sharpe。

## 反向检验：反过来做（FINDINGS Q34）
- `run_backtest(flip_book=True)` 翻转**装配好的目标**（`base = -base`，在 `select_book` 之后），
  **不是** `score = -score`（后者会连选股一起改，因为 `_stable_book` 的留任缓冲是路径依赖的）。
  守卫 `tests/test_flip_book.py`（5 条 / **4 变异全抓**）。
- 恒等式 **`net_flip = −(net + 2·cost)`** —— 镜像要**再付一份同额换手成本**，所以亏损**大于**原版收益。
  必须逐 bar 报**最大绝对偏差**（不是均值）。四对 arm 层层剥掉「读 equity」的东西，**关掉 ADV 上限后
  每一行都是 0.000e+00**（逐位），`Σnet` 精确反号。
- ⚠️ **`long_ret`/`short_ret` 是按持仓符号取的带符号 P&L** → 镜像把 long 映到 **−short**；
  写成 `long_flip == +short_normal` 是最容易犯的错（我犯过一次，被恒等式表抓住）。
- ⚠️ **破坏精确镜像的两个量都读 `equity`**：`impact_rate(...)` 与 `adv_cap_delta` 的
  `scale = min(1, budget/(|delta|·equity))`；聚合残差**恰好等于** `Σcost_flip − Σcost_normal`。
- 结果：正常 **+1.9368** / 反过来（照做）**−2.2572** / 纯镜像 **+1.7088 → −1.7088**（精确反号）；
  **逐年 6 个窗口零例外**（正常全正、镜像全负）。
- ⚠️ **两个「反过来」不是一回事**：「照做」会被回撤缩放压小（毛敞口 0.6425 → 0.2522），
  所以 −2.26 是**下界**；只有**关风控 + 关 ADV 上限**才是精确镜像。

## 展示层：每个数字都要追到产物
- **禁止**在 `app.js`/`webapp/spec.py` 写死指标，**禁止前端留一份最优参数集拷贝**：唯一来源 `/api/spec`
  的 `baseline`/`acceptance`/`optimal_cli`/`optimal_headline`；引用旧指标必须带 `spec.RECORDED_NOTE`。
- `scripts/smoke_live.js` 必须**逐字段镜像 `boot()`**：漏字段不报错，只让标记**双双消失**，
  而那和「一切正常」长得一模一样。**页面提到一个控件，就必须真的有那个控件**（断言查引用能解析到控件）。
- **判据坏了不许靠改判据变绿**：资金费那条 FAIL 继续报，旁边加 `pass=None` 信息行、不计入门禁数。
- **禁止「整文件不含某字符串」式断言**（`spec.py` 合法地枚举因子名）→ **切出定义块**再查。
  **相邻对断言**优于「包含某个数」：`1.937` 是 `1.9375` 的子串。
## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多 `sell/long`、
  **加空 `sell/short`、减空 `buy/short`**、平空 `buy/short`、开空 `sell/short`；flip = 两腿。
- `planner._orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢了符号** → 每条加空的腿都变成
  买入，**静默**（订单被接受、不报错、计划看着正常，只是仓位朝反方向走）。实测 demo 实际多头 **92.0%**
  vs 目标 **47.6%** → **demo 至今的成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；
  守卫 `tests/test_order_sides.py`（8 转移 × 2 模式 + **性质断言**）。

## 三个「倍数」只有一个改订单（推导与实测表见 `RULES-OPS.md`）
- **账户杠杆 `set_leverage` 不改任何一笔订单**（只把维持保证金压到 1/L）；**放大目标敞口也无效**
  （换手预算是**净值的比例**，目标 ×m ⇒ 节流 ÷m ⇒ 执行额不变）。硬不变量，**别「修」它**。
  结构原因：`build_plan()` **没有 leverage 参数**（`tests/test_size_invariance.py` 钉签名与调用点）。
- **唯一有效的旋钮 = `execution.max_daily_turnover`**（$70 上 0.2 → **6 笔/39.6%**，1.0 → **14 笔/93.1%**）。
  ⚠️ 但把它当**回测参数**扫（`15_sensitivity_ofat.csv`）Sharpe **1.82→1.10→0.46** —— **放开节流就打坏策略**。
- **不变量是「净值 × 节流」**：`$70 节流1.0` ≡ `$350 节流0.2`，但只买到**首期订单量**，买不到目标规模
  （目标恒为 `1.0887 × NAV`）。
- **杠杆唯一的作用是保证金**：$70 在 5× 下最多持 $350 —— **正好用光、缓冲 $0**，是**极限**不是**能力**
  （5.44× 毛敞口 → **18.4% 反向波动即亏光**）。⚠️ `§16.5`「$70 加 L 倍 ≡ 有效资金 $70L」**与实现不符**
  （已加 §16.5.1），**但 MDD 列仍有效**。
- **测量纪律**：比杠杆必须用**同一个引擎**（价格缓存 15s TTL）；新建 `LiveEngine` 会重新拉价 →
  **价格漂移**会被误读成「杠杆有影响」。敞口闸门取 `max_gross_frac`/`max_gross_notional` **更紧的那个**。
  **仓位小先查换手爬坡、拒腿、订单方向，别先怀疑杠杆。**

## 本金门槛（实测，别抄报告旧数）
- **出厂节流 0.2 下**、同信号、空账户、冻结行情覆盖率：**$70 → 39.4%**、$200 → 73.9%、
  **$500 → 96.8%**、$1,000 → 98.3%、**$2,000 → 100%**；`min_viable_capital` ≈ **$1,048**。
- $70 权重误差中位 **0.0663** ≈ 单腿平均目标权重 **0.0640** → **误差与信号同量级**。`min_nav_usd`
  默认 **$50** → $70 **不会被风控拦住**。产物 `scripts/plan_report.py --mode demo`。

## 自动交易（无人值守调仓）
- 入口 `... execution.auto_trader --mode demo --interval 30 --rebalance-days 1.0 --bar 1h
  --refresh-after-hours 1.0`；策略 `engine.DEFAULT_SIGNAL`。实盘需 `--allow-live`。
- **`not_due` 是 `warn` 不是 `block`** → 引擎**不拦**非调仓日，到期判定必须写在调度器里，断言
  「**`execute` 根本没被调用**」。**绝不传 `force`**。闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；旧规则 `since >= R`
  会**恒定落后回测一个周期**。实盘补一根「执行 bar」→ 窗口 **2 根 bar 宽**。**「还差几根」= `R − since`**
  （没有 `+1`）；后端 `explain_not_due` 与前端 `dueHint` 是两个渲染点，测试必须**从 fixture 派生**。
- **`next_decision_ts` 不是「未来的下一个决策时刻」**：它是「最后一笔已记账网格点 + R」，**常常已过去**。
  前端只展示后端原话，**不重算闸门**。
- **面板/时刻三坑（展开见 `RULES-OPS.md`）**：① `load_panels` 默认 `extend_to_last=False` **必须保留**
  （回测唯一入口，改了 v3 存档全废），只有实盘路径传 `True`；② **窗口 = 北京 11:10–13:10**（成交价
  `open(G+1)`，无前视），修法 `signal.book_with_execution_bar`；③ 信号缓存 key 必须含代码指纹
  （`_code_signature()` = `mtime:size`）。
- **`refresh_after_hours = 1.0` 是每 tick 都刷新**，它**不决定窗口打开时刻**，只决定**抗漏 tick 余量**：
  刷新间隔必须**小于一根 bar**，否则漏一个 tick → `since` 0→2 → **整天不成交而日志全是 `not_due`**。
  **只能看 `bars_since_decision`，不能看时钟。**
- **限额闸门比 `plan.realised_gross`**（被节流后），不是 `target.gross`（NAV 倍数）。
- **跨进程调仓锁** `store.rebalance_lock(mode)`：控制台与调度器并发 `execute()` 会各发**完整**订单 →
  仓位翻倍。`O_EXCL` 抢占，TTL 900s。`os.kill(pid,0)` 的 `EPERM` = **存在但无权发信号** → **存活**。
- `gross_realised` 必须是**实际成交后的账**（被拒的腿保持原仓位），不是目标值。

## 设置持久化（「保存了重启不生效」）
- 保存必须以**落盘**收尾并回**落盘路径**（限额在 `artifacts/live/<mode>/limits.json`，**故意不在
  `LEDGER_FILES`**）。只赋内存再回 `{"ok":True}`，与「按钮没接线」无法区分。
- `load_limits()` 的 `None`（从未配置）**≠** `{}`（用户清空 → 全零上限，拒掉每笔）→ 没值必须传 `None`。
- 同名不同义必须标注：账户区「换手预算」= `execution.max_daily_turnover`；限额表 `max_turnover_frac`
  = **硬闸门**（超即整批拒绝）；两个调用点同源（`_turnover_from()`）。`onclick = liveSaveLimits` 会把
  MouseEvent 当 `requireDue`（恒真）→ 必须包 `() => ...`；无显式传参时**读屏幕勾选框**。

## 通知 / OKX
**细节全在 `RULES-OPS.md`**（provider 状态码表、OKX 两层响应）。必须记住的：**配完不重启 → 守护进程
静默不发**（按 mtime 热读）；`send()` **永不抛**；**HTTP 200 不是结论** —— 四家**全部**用 200 返回
「被拒」；OKX 批量响应 envelope `code` 与逐行 `sCode` 是**两层**，只看 `code` 会把 19 单全记失败。

## 测试约定
- 期望值必须**从 fixture 派生**，别写死；断言用**页面同一个格式化器**推导（时区一改就红）。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数）；并行化后断言**重数**不断言顺序。
- pytest 必须 `TMPDIR="$(mktemp -d)"`（撞 root 残留目录 → 49 error）；**同一 TMPDIR 连跑多个进程也会
  偶发全量 setup error** → 每次跑都给独立 TMPDIR。
- 新断言必须变异测试，且**必须验证「真的注入进去了」**（`src.count(old) == 1`）。用**备份+还原**
  （`cp` → 改 → 跑 → `cp` 回 → 比 sha256），**不要 `git stash`**；一次性脚本用完移出仓库。
  **变异脚本要同时断言「写入后 sha 变了」与「还原后 sha 回来了」。**
- 新 helper 要在测试**函数内** import（模块级 import 不存在的符号 → 整文件 collection error）。
- **多进程 sweep 不能从 stdin 跑**（spawn 报 `FileNotFoundError: '<stdin>'`）→ 烟测用 `n_jobs=1`
  或把脚本写成真实文件。
- `smoke_live.js` MiniDom：**子树级失效** `clearClassScope` 是必须的；`#content` 重建时必须清 `__lastHtml`；
  stub 要**比被测代码更忠于浏览器**。**结构断言**（数调用点、查引用能解析）是合法的——当"真跑一遍"
  需要网络/信号时；但要写清为什么。
