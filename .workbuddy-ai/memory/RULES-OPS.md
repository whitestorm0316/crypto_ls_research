# 运维细则（MEMORY.md 的附录）

> 从 `MEMORY.md` 拆出来的细节，**不自动注入**——需要时用 Read 打开。
> `MEMORY.md` 里保留的是高频、会踩错的规则；这里放「配一次就长期不动」的操作细节。

## 通知（execution/notify.py）
- provider：`feishu` / `wecom` / `serverchan` / `pushplus` / `webhook` / `none`；配置 `config/notify.json`。
- 入口 `execution.notify --set-url <webhook>`：顺带写 `enabled:true` 并**立刻发一条测试消息**。
- 写盘走**临时文件 + rename + chmod 0600**（不能原地截断，否则守护进程可能读到半个文件）。
- **守护进程按 mtime 热读**（`_notifier_now()`）：不实现这一点的话，配完不重启 → `--test` 通过而守护
  进程**静默不发**，且没有任何报错。
- **绝不能影响交易**：`send()` 永不抛异常，失败回 `{"ok": False}`。例行 `not_due` / `noop` 默认不发；
  重复失败按 `dedup_sec` 去重；`traded` 事件**从不抑制**。
- `redact()` 除了常规字段，还要脱敏**末段路径**——飞书 token 长在 `…/bot/v2/hook/<id>` 的**路径**里，
  不在 query 里。但脱敏**只影响显示** → **验证方式是配好真机器人后 grep 产物**，不是看日志好看。
- **HTTP 200 不是结论**。四家 provider **全部**用 200 返回「被拒」：

  | provider | 成功码 | 「被拒」码 |
  |---|---|---|
  | 飞书 | `code: 0` | `19021` / `19024` |
  | 企微 | `errcode: 0` | `93000` |
  | Server酱 | `code: 0` | `40001` |
  | pushplus | `code: 200` | `999` |

  → 必须走 `_verdict()` 判成功，不能看 status code。
- **飞书**（用户选定）：`msg_type: "post"`。签名**反直觉**：HMAC 的 **key** = `"{timestamp}\n{secret}"`、
  **消息体为空**——按常规「把消息当 HMAC 输入」写必返 `19021`。
- **微信个人号没有官方 webhook**。要推到微信，两条路：企业微信群机器人 + 后台「微信插件」，
  或 Server酱 / PushPlus（**别选 `webhook`** 这个 provider）。
- appSecret 在**钥匙串**里 → **不要去读它**。

## OKX 交互
- 批量接口（`place_batch` 之类）有**两层**结果：
  - envelope 的 `code` 只说**请求**层面（`1` = 全败 / `2` = 部分成功）；
  - 每行的 `sCode` 才是**那一单**的结论。
  把 `code:"2"` 当失败 → 19 单全记 `n_ok=0`，而实际成交了 11 单。
- **被拒订单不进交易所订单簿**，逐单原因只在**响应里出现一次** → 必须在边界处就记下来，
  事后回查订单历史是查不到的。
- `place_batch` 按行内**回显的 `clOrdId`** 绑定请求行，**不靠数组下标**（交易所不保证顺序）。
- **模拟盘 ≠ 实盘合约池**：实盘 ~492 个合约 / 模拟盘 184 个。所以计划期就要以 `warn venue_missing`
  **点名**缺失的腿；模拟盘对缺失腿返回 `51087`。
- clOrdId 前缀按 run 派生：`mode + sha1[:8]`。

## 数据流水线
- 顺序：`data.download --bars 1h 15m 5m` → `data.asset_class --build` → `data.funding_build`。
- **缺 `meta/inst_category.json` 会让 pool 测试失败**。
- `config/okx_creds.json` 从 `*.example` 复制，**只有 live 需要**。
- **缓存只存已收盘 bar**（160/160 同步）。

## 前端检查脚本
- `node scripts/check_js.js webapp/static/app.js` — 未定义的被调用标识符。
- `node scripts/check_dom_refs.js webapp/static/index.html webapp/static/app.js` — 引用的 id 是否都有定义。
- `node scripts/smoke_ui.js` / `node scripts/smoke_live.js` — 无需浏览器，可直接跑。
- ⚠️ `scripts/verify_input_persist.js` 与 `scripts/verify_auto_panel.js` 硬编码
  `C:/Program Files/Google/Chrome/Application/chrome.exe` → **本机必崩（ENOENT）**，不是回归。

## 一次性的坑（记录，避免重复踩）
- `planner._orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢了符号** → 每条「加空」的腿
  都变成买入。**静默**：订单被接受、不报错、计划看着正常，只是仓位朝反方向走。实测 demo 实际多头
  **92.0%** vs 目标 **47.6%** → **demo 至今的成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`。
- `acceptance_checks` 的 MC 块曾写成 `if mc:` → 文件不在盘上就**一行都不输出**，于是「13/13」是在
  唯一可证伪的判据**从未跑过**时报出来的。现改为输出显式 `pass=None`「未评估」行。
- `webapp/server.py::main()` 的默认端口是 **8770**（隔壁 a-stock 项目在用）→ 必须 `python -m webapp.server 8790`。
- 本沙箱 **`/tmp` 在两次工具调用之间被清空** → pidfile / log 一律放仓库内 `logs/`。
