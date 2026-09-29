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
- **`nohup ... &` 活不过一轮工具调用**（Bash 返回时整个进程组被回收：实测 pid 立刻消失、日志只有表头）。
  长任务只能走 `persistent-background-process/scripts/daemon_run.py`（double-fork + `setsid`）。
- **`artifacts/live/<mode>/auto.pid` 是个 JSON，会陈旧**：它曾写着 pid 78448，而真正在跑的是 5756
  （与 `logs/auto_demo.pid`、`auto.json` 心跳一致）。**判活只看 `auto.json` 里的 `pid`。**
- **多进程 sweep 不能从 stdin 跑**：`python - <<'PY'` 里调 `run_sweep(n_jobs>1)` 会因
  `multiprocessing.spawn` 去读 `<stdin>` 而报 `FileNotFoundError: '/…/<stdin>'`。
  烟测要么 `n_jobs=1`，要么把脚本写成真实文件。

## 面板 / 时刻三坑（展开）
1. **`load_panels` 的 `extend_to_last` 默认必须是 `False`** —— 它是回测的唯一入口，改成 `True` 会让
   存档全部失效（v3 的 689 → 690 根 bar）。只有实盘路径显式传 `True`。
2. **窗口 = 北京 11:10–13:10**：成交价取 `open(G+1)`（无前视），所以窗口在决策点**收盘后**才打开。
   修法是 `signal.book_with_execution_bar`。
3. **信号缓存 key 必须含代码指纹**：`_code_signature()` = `mtime:size`。否则改了信号最多 `ttl`(30min)
   不可见，表现为「代码改了但结果没变」。

## 三个「倍数」：推导与实测
`build_plan()` 的签名里**没有** leverage 参数（`tests/test_size_invariance.py` 同时钉**签名**与**调用点**），
所以账户杠杆在结构上就够不到 sizer。$70 上实测四行（不设 / 1× / 3× / 10×）**逐字段相同**：
**6 笔 / $5.51 / 39.4%**。

放大目标敞口同样无效：换手预算是**净值的比例**，目标权重 ×m ⇒ `turnover_wanted` ×m ⇒
`turnover_scale` 变成 1/m ⇒ 执行额 `m·w·scale/m` 一点没变。**这是硬不变量，不要「修」它。**

| 旋钮 | $70 上的效果 |
|---|---|
| `set_leverage` 1× / 3× / 10× | 6 笔 / 39.4%（**逐字段相同**） |
| 目标权重 ×m | 执行额**不变** |
| `execution.max_daily_turnover` 0.2 → 1.0 | 6 笔/39.6% → **14 笔/93.1%** |

⚠️ 但 `max_daily_turnover` 当**回测参数**扫（`artifacts/v5_1d_all5/tables/15_sensitivity_ofat.csv`）：
**0.2 → 0.5 → 1.0 的 Sharpe = 1.82 → 1.10 → 0.46**。**放开节流本身就把策略打坏。**

不变量是 **`净值 × 节流`**：`$70 节流1.0` ≡ `$350 节流0.2`（名义额/覆盖率/笔数相同），
但只买到**首期订单量**，买不到 $350 的**目标规模**（目标恒为 `1.0887 × NAV`）。

杠杆唯一的作用是**保证金**：$70 在 5× 下最多持 $350 毛敞口 —— **正好用光、缓冲 $0**，是**极限**不是
**能力**；5.44× 毛敞口下 **18.4% 的反向波动就亏光**。

⚠️ `OPTIMIZATION_RESULTS.md §16.5`「$70 加 L 倍杠杆 ≡ 有效资金 $70L」**与实现不符**（已加 §16.5.1），
**但 MDD 列仍有效**。

**测量纪律**：比杠杆必须用**同一个引擎**（价格缓存 15s TTL）。新建 `LiveEngine` 会重新拉价，
各行就会因为**价格漂移**而不同，并被误读成「杠杆有影响」。

## 反向策略检验（Q34）的设计与读数
脚本 `scripts/exp_reverse_strategy.py`，产物 `artifacts/reverse_strategy/tables/36*.csv`。

`flip_book=True` 翻转的是**装配好的目标**（`base = -base`，位置在 `select_book` / `build_units` /
`side_gross_targets` 之后），**不是** `score = -score` —— 后者会连选股一起改（`_stable_book` 的留任缓冲
是路径依赖的），比较就被污染了。

恒等式 **`net_flip = −(net + 2·cost)`**。必须逐 bar 报**最大绝对偏差**（不是均值），
因为单个坏 bar 会藏在均值里。四对 arm 层层剥掉「读 `equity`」的东西：

| arm 对 | 风控 | 成本 | ADV 上限 | `gross_flip == −gross` |
|---|---|---|---|---|
| A / B | 开 | 开 | 默认 | 3.60e−02 |
| A1 / B1 | 关 | 开 | 默认 | **0.000e+00** |
| A2 / B2 | 关 | 关 | 默认 | 5.76e−03 |
| A3 / B3 | 关 | 关 | **关** | **0.000e+00** |

破坏精确镜像的两个量**都读 `equity`**：
- `impact_rate(delta, adv, equity, vol)` —— 两臂反向复利 ⇒ participation 不同（fee/spread 逐位精确）。
- `adv_cap_delta` 的 `scale = min(1, budget/(|delta|·equity))` —— 一旦某臂净值涨到上限咬住，
  持仓就不再精确镜像。**把 ADV 上限调开后每一行回到 0.000e+00**，这就是证明。

⚠️ **`long_ret` / `short_ret` 是按持仓符号取的带符号 P&L**（`long_ret = Σ held·(held>0)·r`），
所以镜像把 long 映到 **−short**，不是 `+short`。写成 `long_flip == +short_normal` 是最容易犯的错。

**两个「反过来」不是一回事**：
1. **照做**（风控开）：反向 book 一路亏 → 回撤缩放把毛敞口从 0.6425 压到 **0.2522** ⇒
   亏损被风控**缩小**了，Sharpe **−2.26 是下界**。
2. **纯镜像**（关风控 + 关 ADV 上限）：逐位精确，Sharpe **精确反号**（+1.7088 → −1.7088）。
