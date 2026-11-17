# 项目长期约定 — crypto_ls_research

> **只放规则**（会踩错的）。长推导/表格在 `RULES-OPS.md`（**不注入**）；数字在
> `FINDINGS.md` 与日志。⚠️ **上限 ~17.2 KB**：**加东西必须等量压掉**，加完 `wc -c` 复核。

## 环境 / 沙箱 / 进程
- Python：`~/.workbuddy-ai/binaries/python/envs/default/bin/python`。
- `grep '\|'` 静默返 0 行 → 用 `grep -E`/Grep 工具；**无 `timeout`**；`ps` 禁（`pgrep`/`lsof`
  可用）→ 探活 `kill -0 $(cat <pidfile>)`；**`/tmp` 两次调用间被清空** → pidfile/log 放仓库内。
- 控制台 **8790**；`server.py::main()` 默认 8770 → **必须位置参数** `python -m webapp.server 8790`。
- 沙箱里 curl 探活不可信（127.0.0.1 被代理接管）→ 判活/读 API 用**裸 socket**。
- 长任务用 `persistent-background-process/scripts/daemon_run.py`（位置参数
  `<cwd> <log> <pid> <cmd...>`）；`nohup ... &` 活不过一轮调用。`echo ===` 在 zsh 报错。
- 数据流水线：`download --bars 1h 15m 5m` → `asset_class --build` → `funding_build`。
  改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8790**；静态文件每请求读盘不用重启。

## 两种「回测」要不要先刷新行情，答案相反（Q52）
- 研究/验证回测（`exp_*`、验收基线）**不刷也不能刷**（load_panels 只读盘，要可复现）；
  实盘/纸面（`engine.target()`）**必须先刷新**（用回测引擎在最新缓存上跑）。

## 验收口径 = 1 天网格 + 五因子（`v5_1d_all5`）——数字在 `FINDINGS.md`
- 唯一来源 `settings.ACCEPTED_FACTORS`/`ACCEPTED_REBALANCE_DAYS`，派生到
  `engine.DEFAULT_SIGNAL` 与 `webapp.spec.OPTIMAL_OVERRIDES`，**都不许写字面量**（1h/1.0/crypto+五因子）。
- 三档必须一起报（1d ≠ 更优：拿 2.22× 回撤深度、2.94× 换手换的）。
- 完整全阶段 = **19 行 = 16 门禁 + 3 条 `pass=None` 信息行**；「没评估」不许长得像「通过」。
  守卫 `test_acceptance_completeness.py`。

## 目标波动 / 敞口上限（Q77，细节 `2026-11-16.md`）
- `RiskConfig.target_vol_annual`=**0.50**（2026-11-16 由 0.30 上调）。面板 `spec.py` 的 default
  必须同步（否则「恢复默认」改回 30% 当 override 发出去）；实盘/自动交易**不用另改**——
  `DEFAULT_SIGNAL.overrides` 不带 `risk.*`，直接继承 dataclass。
- 报「闸门会拦多少」要用闸门读的量：`check_plan` 看 `plan.realised_gross`（过换手预算的账面），
  不是原始目标。50% 下原始目标中位 1.51×、实际均值 0.77×、>1.0× 占 19.7%。
- `max_gross_frac`=**1.5**（`LiveLimits` 默认 + `live/limits.json` 两处）。
- 基线已按 50% 重跑（base 13s）：CAGR 48.8 / vol 23.0 / Sharpe 1.828 / MDD −16.0；旧产物在
  `artifacts/_baseline_at30/`。只跑 base ⇒ 门禁「未评估」，不许假装 16/16。
- 🔴 回撤窗口配对取**最深**那段（`hit.loc[hit["depth"].idxmin()]`）不是 `iloc[0]`：窗口边界是
  用户给的、谷底是市场给的，换口径后谷底会滑出窗口（W5 曾把 −12% 报成 −0.02%）。
- 🔴 `short_book_table` 的三项分解只在 `isfinite` 点上求和、`short_pnl` 是全量 ⇒ 必须显性报
  `short_pnl_unattributed`（曾静默差 0.006，Q42「算不出来被丢掉」第四次）。

## 网格 / 因子集：唯一来源
- `ACCEPTED_REBALANCE_DAYS`（1.0）**五处引用**（DEFAULT_SIGNAL、auto_ctl.start、
  auto_trader/run_live argparse、spec.OPTIMAL_CLI、auto_demo）不许写字面量。守卫 `test_accepted_grid.py`。
- 换口径最先坏前端拷贝（OPT.cli 曾硬编码 ⇒ 连 Sharpe 都不印）⇒ `diffFromOptimal()`/`gridDaysText()` 收口。
  比因子集先查选名中位数；叫「不成立」先算 σ；`deflated_haircut` 按 tag 读。

## 检验结论（Q32–Q35）/ 失效归因（Q41–Q43）——细节在 `FINDINGS.md`、`RULES-OPS.md`
- 自写估计量必须先验证再信（`_ols_nw` 曾 t 虚高 ~45×）；条件子集「年化收益」无意义；
  必须披露熊市 0.898、高波动 0.118。
- 归因只诊断未优化；**头号反复缺陷「算不出来」被静默丢掉**。

## 保证金模式（Q44–Q47）——细节在 `RULES-OPS.md`
- 单源 `settings.MARGIN_MODE`=isolated；模式必须和杠杆一起动 ⇒ 强平频率 3×→29.5/年、
  1×→1.0 ⇒ **按 1× 跑**。
- 🔴 `positions[].lever` 是配置镜像会撒谎 ⇒ 报杠杆前必须从 `liqPx` 反推（L=1/(dist+mmr)）。
- ⚠️ 杠杆是账户级配置、订单不带；`td_mode` 只读（写死标签显示相反模式）。
- 🔴 Q44（未修）：`account()` 的 `cur_sz` 跨模式求和 ⇒ 另一种腿永久冻结、被拒单。改需授权。

## 展示层 / 控制台（Q39 / Q73–Q78）
- 禁止在 app.js/spec.py 写死指标或最优参数；唯一来源 `/api/spec`。页面提到一个控件就必须真有。
- 判据坏了不许靠改判据变绿（`pass=None` 不计门禁）；警告理由必须和用户做的事同一件事；
  禁止「整文件不含某字符串」→ 切定义块；**相邻对断言**优于「包含某数」。见 `dashboard-data-consistency`。
- `/api/` 错误必须是 JSON（501+HTML ⇒ 前端根因全藏住）。`fmt.dol` 两位小数、负号在 `$` 前。
  变异脚本：解释器要能 import numpy/pandas；bash 写 `${VAR}`；还原放 `finally`。
- 🔴 **改前端先看三个约束**（Q73）：①`app.js`（~4900 行）被 8 个 harness（`smoke_ui|smoke_live|
  check_dom_refs|check_js|verify_*`）+ 2 个守卫直接读取 ⇒ **拆文件会让它们静默失效**。②`/static/`
  走 `os.path.basename()` 扁平化 ⇒ 新 JS 平铺，子目录 404。③登录页渲染进 `#content`。
- 缺口（Q73）：回测取消**已接**；**撤单** `okx_private` 的 `cancel_order/cancel_all/orders_pending`
  **全仓零调用点**；**暂停 ≠ 停止** 未区分。
- 🔴 **P0 已落地**（Q73，方案 `docs/UI_UX_REFACTOR_PLAN.md`）：tokens + `ui.js`、取消接线、订单
  枚举翻译、canvas `ResizeObserver`、登录页整屏（`body.authmode`）、侧栏**常驻**、原生
  `confirm/alert` 全退场、加载骨架。**「64px 图标导航」故意没做**（4 个页面当时不存在）。
- 🔴 **Q76 运行记录页**：①终态必须**重拉一次详情**（`S.detail` 是 running 时抓的，没有
  `result/metrics`），`RUN_TERMINAL`=`done/partial/failed/cancelled`，只在跃迁那一刻拉。
  ②批量删除**逐条** `DELETE /api/runs/<id>` 复用服务端规则（queued/running 回 409；最优 tag 只删
  列表项+日志；产物 **rename 进 `artifacts/.trash/`**）；勾选态放 `S`；`.runs.bulk` 列数 **+1**。
  ③基线重跑：tag 取 `optimal_tag`（不许写死）、阶段取**最宽 bundle**、参数取 spec 不取面板。
- 🔴 **Q78 标的池页**（`/api/universe` → `universe/snapshot.py`）：①门槛的 `adv` 是**每根 bar 的
  均成交额**（`amount.rolling(liq_window×bpd).mean()`），**不是日额**：$3M/1h ≈ $72M/天，按日算
  过线 196 只、按 bar 算 16 只（差 12 倍）；`adv_cap`=`adv×5%` 同理。②加第 N 个 tab 必须把
  `.xxxOnly` 加进**每一条** `#content.<tab>` 显隐规则（漏一条＝两页叠着）。③新面板每次
  `renderContent` 都要从 `S` 重绘（`pollRuns` 2.5s）。④宽表配 `.uniwrap{overflow-x:auto}` 自己滚。
  ⑤`_read_one` 的「空文件」≠「文件缺失」。
- 🔴 换壳四条铁律：①**文案一个字都不能改**（`smoke_live`/`verify_auto_panel` 用 `includes('正在
  读取账户')`/`!includes('正在读取')` 判状态）⇒ 纯骨架让断言静默失效。②`.skel` 是 flex column 且
  高度 auto ⇒ `.skbar` 的 `flex:1`＝`flex-basis:0` 塌成 0 高 ⇒ 必须真渲染看竖排。③`git commit -m`
  含反引号被 zsh 替换 ⇒ 写文件 `-F`。④`display:none` 退出栅格 ⇒ 收起侧栏时 `.content` 掉进 0 宽
  那列压成 **40px 缝**（`gridTemplateColumns` 仍显示 `0px 1280px`）⇒ 配**单列** `.main`，判据量
  `getBoundingClientRect().width`。

## 数据源可切换（Q40）——表格在 `RULES-OPS.md`
- 缓存目录唯一来源 `settings.CACHE_DIR`（`CRYPTO_CACHE_DIR`）；字段映射最贵的坑：
  `vol_ccy`←基础币量、`amount`←计价币额（写反不报错，vwap 变价格²）。
- `min_avg_amount_usd` 是绝对美元阈值（币安成交额 3.67×OKX）；验收 1.94 落在 ADV 门槛尖峰
  （关门槛 1.93→0.07）；两场所 panel 同持会 OOM。守卫 `test_data_source_switch.py`。

## 订单方向
- `posSide`=动哪个仓、`side`=方向：加多 buy/long、减多 sell/long、**加空 sell/short、减空 buy/short**；flip=两腿。
- 曾用 `abs(tgt)-abs(cur)` 推 side 丢符号 ⇒ 加空全变买入、静默。修法 `d = tgt_sz - cur_sz`；守卫 `test_order_sides.py`。

## 自动交易 / 设置持久化 ——完整规则在 `RULES-OPS.md`
- `not_due` 是 warn 不是 block ⇒ 到期判定写进调度器（断言 execute 没被调用）、绝不传 force；
  闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。到期闸门是「边沿」：
  `rebalance_window_open(since)==(since==1)`；「还差几根」=R−since；刷新间隔必须小于一根 bar。
- 保存必须落盘收尾；`load_limits()` 的 None（从未配置）≠ {}（全零拒每笔）。
- `set_leverage` 不改订单、放大敞口无效——硬不变量别「修」；唯一有效旋钮
  `max_daily_turnover`，放开会打坏策略（1.82→1.10→0.46）。

## 合约宇宙（Q66）——新合约不自动算上、退市不自动剔
- 宇宙 = `list_cached_insts()` = 盘上 parquet，不是 OKX 实时列表；刷新走 `--reuse-pool`
  冻结池 ⇒ 新合约永远没 parquet（实测 OKX 494、盘上缺 299/crypto 133）。退市残留有兜底
  （无 bar ⇒ 因子 NaN ⇒ 不进新仓；有持仓 planner 市价平）。
- 跟上要三件一起：①download 不带 --reuse-pool ②asset_class --build ③specs.refresh_specs()。
  少② ⇒ resolve_insts fail-loud；换池=换宇宙不可比。
- 🔴 SystemExit 不是 Exception 子类 ⇒ except Exception 抓不到 ⇒ 曾杀守护进程（auto_trader.once、
  auto_schedule._run_once 已降级：本轮失败+不写幂等账+下轮重试）。

## 宇宙体检（Q67）
- `execution/universe_drift.py::drift_report()`：只读比对盘上 vs OKX 实时（新达标/退市残留/
  缺分类/缺规格；池规则**复用** `build_candidate_pool`）。**永不抛**——它挂在 `/api/live/status`
  轮询上。TTL 6h 落盘、失败退避 15 分钟；快照是 body 包裹结构——**失败不能覆盖上次成功体**
  （stale 兜底靠它）。前端 `liveDriftHtml()` 挂 `#liveDrift`；守卫 `test_universe_drift.py`。
- **补齐按钮**（Q67.1）：`universe_upgrade.py` 四步（download 不带 --reuse-pool + --incremental
  → asset_class --build → refresh_specs → funding_build），失败即停；前后各跑一次体检；
  路由走任务队列，前端有缺口才显示、confirm 门控。**故意不自动重建基线/重算信号**。
  🔴 `funding_build` 的 end 曾硬编码 2026-09-26 → 已改 `default_end()`。
- 🔴 **长任务的子进程必须流式**（Q71）：`subprocess.run(capture_output=True)` 把 stdout
  整段缓冲到结束 ⇒ ①下载(30min)/④funding(17min) 在界面上只有一行「…」，和卡死长得一样。
  改 Popen 逐行转发 + **`PYTHONUNBUFFERED=1`** + 30s 心跳 + 超时 **kill 且 wait**。
  诊断：`pgrep` 找子进程、`lsof` 采样 TCP（有=在下载）、产物 mtime 重建时间线。

## 无人值守 + 通知（Q49–Q53）——细节在 `RULES-OPS.md`
- 执行者是控制台自己（auto_schedule，启动自恢复），走和网页按钮同一队列；到点是窗口不是
  阈值、错过不补；enabled/running 两个量、派生 will_trade。
- 通知要和「执行」一起搬；action 恒为 rebalance 必须翻译；拦截原因填 blocked_by；
  返回嵌套 vs 格式化器读扁平 ⇒ 消息全是「—」。沙箱代理只在工具调用期间存活 ⇒ 定时通知
  Errno 61 而订单照下 ⇒ `build_opener(ProxyHandler({}))`。模块重复加载（`import live_api` vs
  `from webapp import live_api`）⇒ 线程建在 A、HTTP 读 B ⇒ 统一包路径导入。
- 🔴 **三个执行体并存会双下单**（Q72，细节 `RULES-OPS.md`）：①控制台 `auto_schedule`（线程，
  账=`auto_schedule.json`）②守护进程 `auto_trader`（launchd，**无独立账**）③`scheduled_tick.py`
  （只保进程活着）。守护进程的闸门是「**网格边沿**」：`rebalance_due=(bars_since_decision==1)`
  ⇒ 决策点后一根 bar 期间**一直为真**；`not_due` 是 **warn 不是 block** ⇒ 控制台路径照样下单；
  换手账**共享**（`state.json`）⇒ 预算闸门救不了。来源：`runs.jsonl`（引擎）vs `auto.jsonl`（守护）。

## 鉴权 / 二重认证（Q54–Q57）——细节在 `RULES-OPS.md`
- 闸门在唯一入口（_gate 挂 do_GET/POST/DELETE 开头、路由之前）；_PUBLIC_API 白名单用**前缀**
  （手写文件名 ⇒ 只剩空壳日志正常）；未登录回 JSON 401，存在/不存在同响应（防枚举）。
- **前端改动必须真渲染再断言**（grep/裸 socket「通过」用户看空白页）；ROOT 从别的文件抄会多
  爬一级。CSS 用控制台 + 静态 harness + agent-browser **计算样式断言**；变量先定义再用
  （--text-1 曾未定义、--shadow 被当颜色 ⇒ 整条声明静默失效）。
- `==` 与 compare_digest 行为相同 ⇒ 只验 True/False 抓不到替换。明文绑非本机拒绝启动；
  隧道 ≠ TLS ⇒ 绑 127.0.0.1；Host 头校验回 421。门禁测试两个方向都要验。

## 新机器 / 产物初始化 + 内存（Q58–Q62）——细节在 `RULES-OPS.md`
- 入口 `scripts/init_linux.sh`（幂等）→ download → asset_class → funding → 重建基线 + 内存体检；
  页头「基线数据缺失」是必然，重建 10 秒 `rebuild_optimal_baseline.py`（默认拒绝覆盖）。
- 🔴 代理默认值 `OKX_PROXY=127.0.0.1:7897` 写在代码里、模块导入时求值 ⇒ 换机器 Errno 111；
  必须先 export（空=直连）。🔴 `--end` 默认硬编码 2026-09-26 排他 ⇒ 静静少二十来天。
- 🔴 `-9`/137 = SIGKILL = OOM、无 traceback；三处撞：命令行回测、控制台「一键准备」
  （182MB+1.8GB）、「仅刷新行情」安全。2GB 机器必须加 swap（峰值 ~2.0GB）。
- 🔴 **性能红线（Q68）**：`wide()` 的「一步构造」`DataFrame(cols, index=grid, dtype=...)`
  是逐列对齐慢路径——**慢 8.6 倍**（109.8s vs 12.8s）、峰值反而更高。快路径 =
  `DataFrame(cols).astype("float32").reindex(grid)`。守卫双向钉死，**禁止回潮**；
  性能改动必须当场 A/B 计时，只测内存不测时间会带着 8.6× 倒退上线。

## 飞书通知 / OKX（Q63）——细节在 `RULES-OPS.md`
- `config/notify.json` gitignore（webhook=凭据）⇒ 每台机器都要配。内置 CLI：
  `python -m crypto_ls_research.execution.notify --show/--test/--set-url/--set-secret/--set-keyword`
  （--set-url 自动写 enabled:true；手改最易漏）。安全设置必须对上（否则 code 19024）。
- notify_on 默认 traded/blocked/error（not_due 刻意不推）；modes 默认 demo/live。改配置不用重启。
- send() 永不抛、配完不重启就静默不发（按 mtime 热读）；HTTP 200 不是结论；OKX envelope code
  与逐行 sCode 两层。仓位 USD 名义只能读 notional（sz×markPx 差合约面值倍数，不报错、看着合理）。

## 测试约定——细则在 `RULES-OPS.md`
- `--log` 默认开（logs/research-<tag>-<时间戳>.log）；阶段失败收尾 sys.exit(1)+### FAILED
  （原先退出码 0 ⇒ init 脚本把失败当成功）。改完 .py 第一件事 ast.parse。
- 期望值从 fixture 派生；断言用页面同一格式化器推导；用性质不用秒表；pytest 必须
  `TMPDIR="$(mktemp -d)"`。
- 对账断言要有「独立算一遍」的量钉口径（引擎自己的量只钉分组）。新断言必须变异测试
  + sha 验证真的注入；变异 -k 选错 ⇒ 0 selected 被当成「抓到」；**一次只注入一个**，
  OOM 被 kill 时 `finally` 不执行会留下残留变异（用 `git status` + sha 复核）。
- 重测试主动缩小输入（单进程跑到底 ⇒ 137 且无失败信息）；只验机制加 `--max-insts 12`。
  解析文本先确认真的解析到内容（续行、前提断言）；扫全文件会扫到自己注释。
- 🔴 **行为测试不许把被测组件本身 stub 掉**（`ui.modal = async () => true` 只证明「拿到
  true 之后干什么」）⇒ 交互层用 **jsdom 真 click**（托管 node 的 `node_modules` 里有，
  `NODE_PATH` 指过去；eval 出的 `const ui` 不进全局 ⇒ 同一段脚本末尾 `globalThis.ui = ui`）。
  组件库生成 `data-*` 必须 **kebab-case**（`data-uimOk` 被解析器小写成 `data-uimok`，
  选择器 `[data-uim-ok]` 匹配不上 ⇒ 按钮静默无响应）。
- 🔴 **`smoke_ui`/`smoke_live` 必须先加载 `ui.js`**（app.js 依赖 `ui`，漏了就是
  `ui is not defined`；smoke_live 里要单独 `vm.runInContext` 跑 ui.js 再跑 app.js）。
  无 TOTP 时可用 `LS_TEST_COOKIE=ls_session=...` 逃生口，但它**绕过登录流程**
  ⇒ 只能调试脚本本身，不能当日常验证。
