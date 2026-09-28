# 项目长期约定 — crypto_ls_research

## 环境 / 沙箱坑
- Python 一律用托管 venv：`/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python`
  （系统 python3 无 numpy/pandas）。pandas 3.0 下全量测试 **267 通过**。
- 每次 `open()` ≈19ms（性能归因**先量地板**，别把自己的代码当罪魁）；
  `grep '\|'` 交替**静默返回 0 行** → 一律 `grep -E "a|b"`；**没有 `timeout` 命令**；
  `ps` 被禁 → 探活用 `kill -0 $(cat <pidfile>)`；**禁用 `rm -rf`**（用户反对删权限弹窗），
  临时目录用 `TMPDIR="$(mktemp -d)"`。

## 端口 / 进程
- 控制台**只用 8899**；**8770 是隔壁项目 a-stock**，会串台。确认身份看页面 `<title>`
  是否「流动性流向 × 趋势」。
- 长任务必须双 fork 守护（技能 `persistent-background-process`）。`daemon_run.py` 是
  **位置参数** `<cwd> <logfile> <pidfile> <cmd...>`（写成 `--pidfile` 会 FileNotFoundError）。
  pidfile：`/tmp/crypto_server.pid`、`/tmp/crypto_auto_<mode>.pid`；日志在 `logs/`。
- 改 `webapp/*.py` 或 `crypto_ls_research/**` **必须重启 8899**；只改
  `app.js` / `index.html` / `style.css` 不用。

## 数据流水线（数据不入库，clone 后重建）
1. `python -m crypto_ls_research.data.download --bars 1h 15m 5m`
2. `... data.asset_class --build`（缺 `meta/inst_category.json` 会让 pool 测试失败）
3. `... data.funding_build`（OKX 只留 3 个月资金费，其余币安补齐）
`config/okx_creds.json` 从 example 复制，仅 live 需要。

## 展示层：页面每个数字都要追到产物
- **禁止**在 `app.js` / `webapp/spec.py` 写死「当前最优」指标；唯一来源是 `/api/spec` 的
  `baseline` / `acceptance`（实算）。`spec.py::OPTIMAL_HEADLINE` 只是历史记录，
  `server.py::_headline()` 用实算覆盖；引用旧指标必须带 `spec.RECORDED_NOTE`。
- 数据重建后 v3 真值：**Sharpe 1.758 / CAGR 21.24% / MDD −7.57%**（记录值 1.937 / 25.09% / −12.72%）。
- 验收**当前只能算 3/4**（本机只重跑过 base 阶段，且**资金费符号相对记录值翻号**）→
  页面显示 `3/4 通过（证据不全，完整需 16 项）`。**开实盘前应先查清这个翻号。**

## 设置持久化（「保存了重启不生效」）
- 保存必须以**落盘**收尾：只赋内存 `engine.limits` 再回 `{"ok":True}`，与「按钮没接线」
  进程内无法区分。`/api/live/limits` 先 `store.save_limits()`，再把**落盘路径**回前端。
- 限额在 `artifacts/live/<mode>/limits.json`，**故意不在 `LEDGER_FILES`**
  （「重置纸面账户」不该顺手忘掉风控上限）。
- `load_limits()` 的 `None`（从未配置 → 出厂默认）**≠** `{}`（用户清空 → 全零上限，会拒掉每笔）。
- `LiveLimits.from_dict(None)` 返回**全默认对象**：`live_api.engine()` 与 `run_live.py` 没值时
  必须传 `None`，否则静默覆盖用户保存的限额。
- 同名不同义必须标注：账户区「换手预算（策略节流）」= `execution.max_daily_turnover`（决定每期执行百分比）；
  限额表 `max_turnover_frac` = **硬闸门**（超即整批拒绝）。两者数值都可能是 0.2。
- 一个定义两个调用点必须同源：`summary()` 与 `turnover_state()` 共用 `_turnover_from()`，
  被丢掉的日期用 `turnover_stale` 披露。
- `onclick = liveSaveLimits` 会把 MouseEvent 当 `requireDue`（恒真）→ 必须包
  `() => liveSaveLimits()`，且无显式传参时**读屏幕勾选框**。断言要抓 POST body 字段。
- 杠杆面板的 `0.47× / 20%` 同属写死缺陷类 → 改 `S.baseline.gross_avg` / `st.turnover_budget` 实算。
- 只读验证：`curl /api/live/status?mode=paper` 看 `limits_saved`（`null` = 磁盘从没写过）、`limits_path`。

## 自动交易（无人值守调仓）
- 入口 `python -m crypto_ls_research.execution.auto_trader --mode demo --interval 60`；
  策略固定 `engine.DEFAULT_SIGNAL`（= v3），不存在第二份"最优"。实盘需 `--allow-live`。
  心跳 `artifacts/live/<mode>/auto.json`、审计 `auto.jsonl`。
- **`not_due` 是 `warn` 不是 `block`**（已有测试固化）→ 引擎**不会**拦非调仓日。
  到期判定必须写在调度器里，断言「**`execute` 根本没被调用**」。
- 闸门顺序：熔断（连信号都不算，`eng.calls == []`）→ 数据新鲜度（含增量刷新）→ 到期 → 风控上限。
- **绝不传 `force`**（它唯一的语义是消掉计划外警告）。
- **跨进程调仓锁** `store.rebalance_lock(mode)`：`_LOCK` 只是进程内，控制台与调度器是两个进程，
  并发 `execute()` 会各按同一持仓发**完整**订单 → 仓位翻倍。锁文件 `O_EXCL` 抢占，TTL 900s，
  加在 `LiveEngine.execute()` / `flatten()` 外层。
- `os.kill(pid, 0)` 的 `PermissionError`(EPERM) = **进程存在但无权发信号**，必须当**存活**
  （当已死会偷走别人的锁）。
- `gross_realised` 必须是**实际成交后的账**（被拒的腿保持原仓位），不是 `plan.realised_gross`。
- 回测 0.47× 与实际 1.11× 不矛盾：0.472 是**实际持仓**毛敞口均值，1.032 是**目标**均值。
- 调仓通知 `execution/notify.py`（provider：feishu / wecom / serverchan / pushplus / webhook / none，
  配置 `config/notify.json`，模板 `notify.example.json`）。配置入口是**一行命令**
  `python -m crypto_ls_research.execution.notify --set-url <webhook>`（顺带 enabled:true
  **并立刻发一条测试消息**）；`--show` / `--test` 是只读自检。写盘走**临时文件+rename**
  且 **chmod 0600**（URL 带密钥），从模板 seed（模板还负责给出 `provider`）。
  **守护进程按 mtime 热读配置**（`AutoTrader._notifier_now()`）—— 否则用户配完不重启，
  `--test`（新进程）通过而守护进程静默不发；注入的 notifier 永不替换（否则测试会读开发者自己的配置）。
  **通知绝不能影响交易**：`send()` 永不抛、失败回 `{"ok":False}`。webhook URL 含密钥 → 全部
  `redact()`。例行 `not_due` / `noop` 默认不发；重复失败按 `dedup_sec` 去重，`traded` 从不抑制。
  连接器（企业微信/飞书）都**替代不了**它：守护进程没有工具面，只能自己 POST。
- **密钥不只在查询串里**（真踩过）：`redact()` 原来只处理 `?key=`，而**飞书把 token 放在路径**
  （`…/bot/v2/hook/<id>`，Server酱 同理 `…/<SENDKEY>.send`）→ 第一次真配机器人就把明文 token
  写进了 `logs/auto_demo.log` 和 `artifacts/live/*/auto.json`（都是 0644）。
  现在 `redact()` 同时脱敏**末段路径**（≥12 字符且含数字，或 ≥20 字符），`/hook` 这类短词保持可读
  （URL 仍能认出是哪类机器人）。**验证方式是配好真机器人后 grep 产物，不是读代码**；
  日志也顺手 `chmod 0600`。脱敏**只影响显示**：传输层仍收完整 URL（真发一条即可证明）。
  双面断言：把整个路径吃掉的「过度脱敏」同样是错的。
- **飞书连接器对这件事无效**（查过了，别再试）：连接器只在 agent 轮次里可用，而调仓发生在
  无人值守的守护进程里。另外本机 `lark-cli` **实际没装** —— PATH 里的
  `~/.workbuddy-ai/binaries/node/cli-connector-packages/bin` 是空目录，`@larksuite/cli` 不在任何
  node_modules；"已连接"只是 `~/.lark-cli/config.json` + 令牌文件写好了，CLI 本体缺失。
  凭证位置：appId 与用户 open_id 在 `~/.lark-cli/config.json`，appSecret 在**钥匙串**
  （`appsecret:<appId>`）→ **不要去读**（会弹钥匙串权限窗，用户反对弹窗）。
- **HTTP 200 不是结论**：四家**全部**用 200 返回「被拒」（飞书 `19021/19024`、企业微信 `errcode 93000`、
  Server酱 `code 40001`、pushplus `code 999`）。只看状态码会把「根本没发出去」报成成功，而安静会被
  读成「策略没动作」。成功码各家不同：飞书/Server酱 `code:0`、pushplus `code:200`、企业微信 `errcode:0`
  → `_verdict()`，与 OKX 两层信封同一类。generic `webhook` 豁免（对端自定义 body，读不出就别猜）。
- **飞书**（用户选定）：`msg_type:"post"`（不是 `text`，否则逐条列表被压平）。签名**反直觉**：
  HMAC 的 **key** = `"{timestamp}\n{secret}"`、**消息为空** → `feishu_sign()`，写成常规形式必 `19021`。
  安全模式三选一：签名（填 `secret`）/ 自定义关键词（填 `keyword`，拼在标题前）/ IP 白名单。
  **刻意不用卡片**：卡片渲染 markdown 更好看，但官方没说关键词过滤对卡片是否生效，而 `post` 的
  text/title 明确会过滤 → 用 `_plain_md()` 去掉 `**`/反引号：宁可少粗体，不能被静默拒收。
- **微信个人号没有官方 webhook**（第三方 hook 有封号风险）。到个人微信：企业微信群机器人 + 后台
  「我的企业 → 微信插件」，或 Server酱 / PushPlus（走公众号）。**别选 `webhook`**——那只 POST 到自己的服务器。

## OKX 交互铁律
- 批量接口**两层**：envelope `code` 只说请求（1 全败 / 2 部分成功），每行 `sCode` 才是该单结论。
  踩过的坑：`code:"2"` 当成失败 → 19 单全记 `n_ok=0`，实际成交 11 单。
- **被拒订单不进交易所订单簿**（pending / history 都查不到），逐单原因只在响应里**出现一次**
  → 必须在边界处记下。
- `place_batch` 按行内回显的 `clOrdId` 绑定请求行，不靠数组下标。
- **模拟盘 ≠ 实盘合约池**（实盘 ~492 / 模拟盘 184）：计划期就要以 `warn venue_missing` 点名；
  模拟盘对缺失腿返回 `51087`。
- clOrdId 前缀按 run 派生 `_clordid_prefix(run_id)`（mode + sha1[:8]），否则同月内重复 id 无法对账。

## 交互延迟（「感觉老是有延迟和 bug」）
- 先归因：`/api/live/status?mode=demo` 1.4s = `account_config()` 415ms + `positions()` 311ms
  + `equity_usdt()` 305ms + `summary()` 100ms。已修：config TTL 缓存(120s)、
  `account()` 用 `ThreadPoolExecutor(2)` 并行 positions/equity（616→332ms）、
  `summary()` 用 `_count_lines()` 数行。结果 demo ~0.8s。
- 前端：`pollRuns` 互斥锁、`startLog` 同 run 不重建定时器、`livePaintAll` 按 `__lastHtml` 备忘、
  切模式丢旧快照 + `statusLoading` 占位。
- **断言用性质不用秒表**（请求计数 / 定时器 identity / 容器写入次数 / 两个 Event 会合）；
  并行化后断言**重数**不断言顺序。

## 测试约定
- 期望值必须**从 fixture 派生**，别写死（写死的断言会诱使人把它改成恒真）。
- 跑 pytest 必须 `TMPDIR="$(mktemp -d)"`，否则撞上 root 残留目录 → 49 error。
- 新断言必须变异测试。`git show HEAD:` 只在 HEAD 只差本轮时才是好基线；否则用**外科式基线**：
  复制文件、只回退本轮新增段（每段 `assert src.count(old) == 1`），期望**恰好 N 条**变红。
  Python 侧用备份+还原（`cp` 出去 → 改 → 跑 → `cp` 回 → `diff -q`），**不要 `git stash`**。
  前端 `SMOKE_APP=<变体> node scripts/smoke_live.js <base>`；一次性变异脚本用完移出仓库。
  新 helper 要在测试**函数内** import（模块级 import 不存在的符号 → 整个文件 collection error）。
- `smoke_live.js` MiniDom：支持 `queryAllClass` / `contentWrites`；**子树级失效**
  `clearClassScope` 是必须的；`#content` 重建时必须清 `__lastHtml`；
  stub 要**比被测代码更忠于浏览器**，否则会凭空造出线上不存在的 bug。
