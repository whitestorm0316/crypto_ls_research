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
- ⚠️ **仓位 USD 名义只能读 `notional` 字段**。`sz × markPx` 会差一个**合约面值（`ctVal`）倍数**：
  `BTC-USDT-SWAP` 的 `ctVal = 0.01`，`pos = -1.05` 张 = 0.0105 BTC，
  所以 `sz × markPx` 给出 **87,646** 而真值是 **876.46** —— **差 100 倍**，且**不报错、数字看着合理**。
  同理 `target_positions[k]['px']` 也是**每张**的价格，不能直接乘 `sz` 当名义额。
  **拿到外部 payload 先打印一个样本的全部字段再写聚合，不要照名字猜**
  （`notionalUsd` 这个名字根本不存在，猜它就会静默退化成 `sz × price`）。

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

## 插针 / 闪崩压力测试（Q35）的细节

脚本 `scripts/exp_wick_stress.py`，产物 `artifacts/wick_stress/tables/40*.csv`，
守卫 `crypto_ls_research/tests/test_wick_channels.py`（9 条，**7 变异全抓**）。

### 三条通道（**混着答两头都错**）
| 通道 | 机制 | 代码位置 | 实测 |
|---|---|---|---|
| **L 收益路径** | `ret_exec[t] = open[t+1]/open[t] − 1` | `backtest/engine.py::pick_exec_price`（`next_open` → 直接返回 `arr["open"]`） | 影线**逐位不进损益**：`net_ret/gross_ret/gross_exposure/turnover/dd_scale` 全 `0.000e+00`，`n_bars_differing = 0` |
| **S 信号路径** | `range_pos`（**五因子之一**）读 `high/low` 的 **180 根滚动极值**；`atr_pct` 同源 | `factors/engine.py` | 全样本 30% 影线：**94.70% 的 bar 换仓**、净值 **−23.26%**、Sharpe −0.21；ATR×3 时 −45.98% |
| **P 缺口 = `open` 位移** | `open` 在 `factors/engine.py` **一次都不出现** | 同上 | 冲击 `open` 改损益，**选股与目标权重全样本逐位不变**（`maxdev_gross_exposure_before_shock = 0.000000`） |

**盲区注入窗口**（唯一正确写法）：
```python
maxlb = int(round(30.0 * BARS_PER_DAY[BAR]))
early = np.arange(0, max(1, warmup - maxlb - 2))     # 所有决策回看窗之外
late  = np.arange(last_dec + 1, T - 1)               # 最后一次决策之后
blind_idx = np.concatenate([early, late]) if len(late) else early
```
`range_days = 7.5 天 = 180 根`：**一根宽 bar 会把 `range_pos` 压扁整整 180 根**，
且管线里**没有任何去刺（de-spike）**。

### ⚠️ 单 bar 缺口的解析式（**朴素写法是错的**）
```
正确：Δnet ≈ w × Σ_i held_i · (open[t+1] / open[t])
错误：w × net_exposure
```
名义额按**冲击后**价格标记 ⇒ 多一个价格比因子。实测 −10% 全市场：`−0.008693910`（实测）
vs `−0.008691644`（朴素式），**差 0.03%**；**单名臂朴素式连符号都会错**。
残差 < 2e-9（= float32 权重矩阵精度）。

| arm | 预测 1 bar | 实测 1 bar | 残差 | Δ净值 |
|---|---|---|---|---|
| Pm10 全市场 −10% | −0.008693912 | −0.008693910 | +1.8e−09 | −0.89% |
| Pm20 全市场 −20% | −0.017387824 | −0.017387823 | +1.1e−09 | −1.90% |
| Ps50 单名 BTC −50% | −0.046186162 | −0.046186163 | −6.2e−10 | −6.08% |
| Ps90 单名 BTC −90% | −0.083135092 | −0.083135093 | −1.1e−09 | −13.18% |

### 扛得住的部分（都是**构造出来的**）
- 净/毛敞口 **mean 0.0797 / p95 0.2049 / max 0.4289** ⇒ 全市场 −20% 只亏 **1.74%** 净值。
- 单名 −90% ⇒ −8.31% 净值（`max_weight_per_instrument = 0.20` 封顶）。
- **爆仓距离**（全 cross，`mmr = 0.005`，同向移动 `m ≥ (1 − mmr·G)/G`）：

  | `G`（毛/净值） | 爆仓所需同向移动 |
  |---|---|
  | 0.292（demo 当前） | 341.97% |
  | 0.9636（策略目标） | 103.28% |
  | **1.0（实盘上限 `max_gross_frac`）** | **99.50%** |
  | 2.0（`risk.max_gross_exposure`） | 49.50% |

- 5.7 年最差单小时 `net_ret` **−4.257%**（2024-03-05 04:00Z，−23.5σ）；`long_ret` −5.216%；
  `short_ret` −3.865%。
- 退市压力交叉验证：Sharpe 1.7469~2.0194（中位 1.9216，基线 1.9372）、`max_dd` −11.30%~−19.40%
  （中位 −13.32%，基线 −13.31%）⇒ **8 名 × −50~−90% + 永久停牌对 MDD 几乎无影响**。

### 扛不住的部分
- **无止损 / 无止盈 / 无算法单**：`okx_private.place_order` 只发
  `{"instId","tdMode","side","posSide","ordType":"market","sz"}`（+ 可选 `px`/`clOrdId`/`reduceOnly`/`tgtCcy`），
  **没有** `slTriggerPx`/`tpTriggerPx`/`attachAlgoOrds` ⇒ 唯一反应是**下一个网格点（最长 24h）**。
- **无账户级回撤熔断**：`dd_scale` 读的是**回测里的模拟净值**（`equity` 从 1.0 起复利），
  与真实账户无关 ⇒ 控制台显示的 `dd_scale` 看着像保护，其实不咬真实账户。

### 四个「屏幕上有、但不咬人」的缺口
1. **`dd_stop_new_entries_above` 被丢弃**：`ds, _stop = risk.dd_scale(...)`，`_stop` **从不读**，
   而 `README §6` 声称「并置 `stop_new_entries` 标志」。
2. **`risk.atr_days = 1.0` 是死字段**：只在 `settings.py` 声明一次，`factors/engine.py`
   硬编码 `atr_bars = max(2, int(round(1 * bars_per_day)))`。
3. **无账户级回撤熔断**（见上）。
4. **`liqPx` 取了但不看**：`execution/engine.py:472` 放进持仓明细，`webapp/` 从不渲染、从不设闸。

### ATR / 爆仓闸门**现在不咬人**
`liquidation_ok`：`liq_dist = 1/max_leverage − mmr = 1/5 − 0.005 = 0.195`，
`need = 3 × ATR%` ⇒ 门槛 **`ATR% ≤ 6.5%`**。实际 **140/140 = 100% 通过**
（ATR 中位 2.03%、p90 3.03%、max 5.28%）。把阈值 ×1.5 / ×2 / ×3 才分别降到 97.14% / 90.00% / 59.29%。
**方向别写反**：抬高杠杆上限会**收紧**这个闸门（`thr20 < thr5 < thr3`）。

### ⚠️ 复现存档的硬要求
`load_panels(bar, start, end)` 的 `end` **只是建议值**（`stop = last + step if extend_to_last else last`，
再 `grid = grid[grid <= last]`），缓存每天在长 ⇒ **不截断就复现不出存档**。
第一版重跑 Sharpe **1.941678** vs 存档 **1.937163**；把每个 panel 字段 `reindex(grid)` 到存档 bar 网格后
**差 +0.00e+00**、期末净值 12 位相同。脚本里必须有 `repro = abs(m0 - arch_s) < 1e-9` 自检并大声报警。

### 另外两条测量纪律
- **`disable_risk_overlays` 是 `run_backtest` 的关键字，不是 `BacktestConfig` 字段**：
  `synth_cfg(disable_risk_overlays=True)` **静默无效**（设了个没人读的属性），
  「关风控」的断言其实在**开风控**下跑。必须作为 kwargs 展开传进 `run_backtest`。
- **`panels_to_arrays` 把价格降成 float32** ⇒ 两个 arm 的**差值**有 ~1e-7 的绝对地板
  （`F32_FLOOR`）。曾用 1e-9 通过，**只因那个合成 book 恰好在冲击 bar 上净敞口为 0**，是运气不是精度。
- **`total_scale` 在非决策 bar 上是 0.0**（`acc = {c: np.zeros(T)}`，只在 `is_exec` 分支赋值）⇒
  任何「scale 必须等于 1.0」的检查都要先限定在 `ts != 0.0` 的 bar 上。

### 换手爬坡（从 MEMORY.md 挪来）
出厂 `max_daily_turnover = 0.20` 下，从 0 爬到 0.96 毛敞口要 **~5 次调仓**；
中间态「实际只到目标 30%」是**正常**的，**不是**拒腿 / 方向 / 杠杆问题。

## 保证金模式：单源常量，**取值 = `isolated`**（2026-09-30 二次决定）

> 下面两节（Q36 / Q38）是**当时测得的事实与推导**，保留不动。这一节记**改法**。

**⚠️ 取值变过一次：`cross` → `isolated`（2026-09-30 同日晚）。两次都是用户要求。**
- 第一次钉死 `cross`，理由是**频率**：逐仓每条腿 `1/L − mmr`（3× → 32.8%），全仓 `(1−mmr·G)/G`
  （`G=0.43` → 231%），任意 `L≥1,G≤1` 下**全仓 ≥ 逐仓**。
- 第二次改 `isolated`，理由是**偿付**：**空头亏损无上限**，全仓下单个合约暴涨打爆**整个账户**
  （实测最脆弱那条只要 **30.7 倍**）。**频率风险 vs 偿付风险，选了后者。**
- **两条都成立，不矛盾**：全仓更不容易被强平，但一旦爆就是全部；逐仓封顶在那条腿，但经常被强平。
- 🔴 **模式必须和杠杆一起动**：逐仓的距离就是 `1/L − mmr`，实测频率（154 标的 × 5.7 年，
  日内极值=上界，K=10 账本）：**3× → 每年约 29.5 次**强制平仓、2× → 10.1、1.5× → 4.5、**1× → 1.0**。
  ⇒ **账本按 1× 跑**。代价**不随流动性下降**（三档 ADV $56M/$11M/$3M 的频率 0.671%/0.597%/0.675%），
  **换宇宙解决不了**。

**改了哪些文件**

| 文件 | 改动 |
|---|---|
| `config/settings.py` | `MARGIN_MODE`（**唯一来源**）取值 `isolated`；`RiskConfig.max_leverage` **改名** `leverage_in_force`（它是**建模假设**，不是上限）；新增 `__post_init__` 校验 `≥ 1` |
| `execution/engine.py` | `td_mode` 是**只读 property**（赋值抛 `AttributeError`）；构造传冲突值抛 `ValueError`；运行记录（rebalance + flatten）带 `td_mode`；新增 `margin_headroom()`，`status()["margin_headroom"]` 暴露 |
| `execution/limits.py` | `margin_headroom_violations(positions, assumed_leverage, flag_ratio=1.2)`：用 `liqPx` 反推**真实杠杆**与假设对账，**warn 不 block** |
| `execution/planner.py` / `okx_private.py` | `td_mode` 默认值从字面量改为 `MARGIN_MODE` |
| `risk/engine.py` | `liquidation_ok` 读 `leverage_in_force` + 新增 `leverage=` 覆盖；新增 `isolated_liq_distance` / `implied_leverage` |
| `webapp/live_api.py` | `route_post` 对冲突的 `td_mode` 回 **400**（不是静默忽略，也不是 500） |
| `webapp/static/app.js` | 模式从 `<select>` 改为**只读展示**；`liveTradeSettings()` **不再发** `td_mode`；**标签从服务端值派生**（原来写死「全仓 cross」⇒ 常量一改就显示**相反**的模式） |
| `webapp/spec.py` | `risk.max_leverage` → `risk.leverage_in_force`，标签改「清算闸门假设杠杆」 |

**⚠️ 三个必须记住的判断**

1. **`warn` 不是 `block`**（对账）：挡住调仓会**冻住**一个「最好的补救恰恰就是这次调仓」的账本。
2. **「算不出来」要显式说**：账户有逐仓腿但一条都没读到 `liqPx` ⇒ 输出
   `margin_headroom_unavailable`（「没有评估」，不是「通过」）。
3. **公式**仍是逐仓口径 ⇒ 这个闸门本质是**波动率筛选**，不是账户偿付约束。
   改成按模式选公式会让它**基本失效** ⇒ **宇宙变宽 ⇒ 验收 Sharpe 变**。**刻意没做。**

**验证**：全量 `568 passed / 1 skipped / 2 xfailed`；`tests/test_margin_mode.py`
**28 条 + 2 `xfail(strict)`**；变异 **4/5 被抓**（前端标签写死 / `_OTHER_MODE` 塌成同值 /
只读属性返回字面量 / API 守卫拿字面量比）。**漏掉的第 5 条是「把常量翻回 `cross`」——
刻意不抓**：测试断言的是**性质**不是**取值**，与 `test_accepted_grid.py` 同一条纪律。
**反向验证**：常量翻回 `cross` 时全量 568 + 冒烟全过 ⇒ 开关**干净可逆**。
`node scripts/smoke_live.js` 全部通过（含新增「标签跟着服务端值走」）。

⚠️ **测试里的「冲突值」必须派生**：三处曾把 `"isolated"` 写死当冲突值 —— 常量一翻，
这些测试**不再测试任何东西却依然全绿**。现由 `_OTHER_MODE` 派生。

**重启后线上复核**（裸 socket）：`/api/spec` 200；`POST /api/live/execute td_mode=cross` → **400**；
两模式 `td_mode='isolated'`。⚠️ 注意 `positions` 在 `status["account"]["positions"]`，
**不在顶层** —— 读错路径会得到「0 个仓位」的假象。

⚠️ **落地后账户里还有 12 条全仓腿 + 8 条逐仓腿**：模式翻成 `isolated` 后订单带 `tdMode=isolated`
⇒ **变成 12 条全仓腿动不了**（Q44 的镜像）。存量仓位必须平掉重开。

### 🔴 Q44：钉死 `cross` 后暴露的**第三条** —— `cur_sz` 跨保证金模式求和（**只诊断，未修**）

`execution/engine.py:472`：

```python
cur_sz[inst] = cur_sz.get(inst, 0.0) + sz      # 不区分 r["mgnMode"]
```

`Plan.td_mode` 是**整个 plan 的单一值**（`planner.py:100`，恒 `MARGIN_MODE`），逐单无差异；
而 `cur_sz` 是**两条腿之和** ⇒ `d = tgt_sz - cur_sz`（`planner.py:217`）**系统性偏大**。

**实测（demo 2026-09-30 18:36，8 个合约并存、全部同向）**：

| 合约 | posSide | 全仓 sz | 逐仓 sz | `cur_sz` 报告 | 虚高 |
|---|---:|---:|---:|---:|---:|
| FIL-USDT-SWAP | long | 15,230 | 7,054 | 22,284 | +46.3% |
| SUI-USDT-SWAP | long | 2,213 | 818 | 3,031 | +37.0% |
| ENA-USDT-SWAP | long | 915 | 394 | 1,309 | +43.1% |
| NEAR-USDT-SWAP | long | 28.9 | 7.1 | 36.0 | +24.6% |
| LIT-USDT-SWAP | short | −312 | −461 | −773 | +147.8% |
| ARB-USDT-SWAP | short | −679.2 | −171.7 | −850.9 | +25.3% |
| PEPE-USDT-SWAP | short | −19.1 | −13.4 | −32.5 | +70.2% |
| ETH-USDT-SWAP | short | −1.35 | −4.4 | −5.75 | +326% |

**后果（两条，均已按代码路径核实）**

1. **逐仓腿永久冻结**：订单恒 `tdMode=cross`，只动全仓腿；而这 8 条恰是**爆仓距离最差**的
   （逐仓中位 **32.8%** vs 全仓中位 **4341%**）。**钉死 `cross` 挡住了新增，没挡住存量。**
2. **两条分歧**：`tgt ≥ iso` → 收敛到 `全仓 = tgt − iso`（净敞口对，但留在高风险桶）；
   `tgt < iso`（含掉出宇宙、`tgt=0`）→ 需减的量 `cross+iso−tgt > cross`，
   `long_short_mode` 下 `sell/long` **结构上不能开空** ⇒ **被拒单**，永不收敛、每调仓点重试。
   ⚠️ **不会反向翻腿**（初稿猜错）：`posSide` 由**目标符号**决定（`planner.py:204`），不是 `cur_sz`。
   （`planner.py:199-203` 的 close 分支 `reduce_only=False`，挡不住。）

**未修原因**：修它 = 改**下单量** = 改执行路径；Q36/Q37 的授权**不覆盖这一条**。
候选 **A**（`cur_sz` 只聚合 `cross`）/ **B**（`td_mode` 逐单派生，但等于把「逐仓」重新变成可选项）
均已记档。**共同前提：先把 8 条逐仓腿平掉**，否则是在带病账户上选算法。

**账户状态**：`BTC-USDT-SWAP isolated` 的 **100× 配置仍在**（`41e` `at_max=True`，
若被使用爆仓距离仅 **0.50%**），但**当前无 BTC 逐仓持仓**（BTC 在全仓 3×、距离 43.4%）
⇒ 当日那条距爆仓 0.57% 的腿**已不在场**，风险由「在场」转为「潜在」。

---

## 保证金模式（Q36）的细节与审计

审计脚本 `scripts/audit_margin_mode.py [mode]`（默认 `demo`），产物
`artifacts/margin_mode/tables/41_position_margin.csv`、`41b_mode_summary.csv`、
`41c_liq_distance_by_mode.csv`、`41d_market_move_liquidation.csv`；
守卫 `crypto_ls_research/tests/test_margin_mode.py`（**28 条 + 2 xfail，7 变异全抓**）。
脚本**直接用 `LiveEngine(mode).account()`**（不需要控制台在跑，也不需要裸 socket）。

### 两条公式（`mmr = risk.DEFAULT_MMR = 0.005`）

| 模式 | 爆仓所需同向移动 | 数值 |
|---|---|---|
| `isolated` | `1/L − mmr` | `L=3` → **32.83%**；`L=5` → **19.50%**；`L=100` → **0.50%** |
| `cross` | `(1 − mmr·G)/G` | `G=0.4458` → **223.80%**；`G=1.0` → **99.50%** |

**定理**：对任意 `L ≥ 1, G ≤ 1` 有 `cross ≥ isolated`，唯一取等号的是 `L=1, G=1`。
守卫在 `L ∈ {1,2,3,5,10,20,50,100,125} × G ∈ {0.05,0.29,0.44,0.96,1.0}` 网格上逐点断言。

**为什么逐仓对**对冲**账本是反的**：空头的浮盈躺在另一个保证金桶里，**救不了多头腿**。
这个账本净/毛只有 ~0.08，市场 −20% 时账户净亏 ~1.6% 净值 —— **账户毫发无损，但逐仓的
每条多头腿都会死**。而且一条腿强平后**对冲就断了**，剩下的腿变成**裸方向仓**。

### ⚠️ 实测的混合账本（2026-09-30 10:57）

```
NAV 56,083.67  毛敞口 25,004 (0.4458 x NAV)  净敞口 4,528 (0.1811 of gross)
引擎 td_mode = 'cross'   set_leverage = None   limits.max_leverage = 5
      cross: 12 名  名义 16,321 (65.3%)  lever 3~3   最近爆仓 355.54%  中位 5482.29%  (有 liqPx 5/12)
   isolated:  9 名  名义  8,683 (34.7%)  lever 3~100 最近爆仓   0.58%  中位   32.17%  (有 liqPx 9/9)
```

| BTC-USDT-SWAP short | mgnMode | lever | 名义 | liqPx | 距爆仓 |
|---|---|---|---|---|---|
| 09-29 老仓 | cross | 3 | $875 | 4,654,699 | +5,484% |
| 09-30 新仓 | **isolated** | **100** | $1,783 | **83,847.85**（markPx 83,352.73） | **+0.59%** |

**成因**（`artifacts/live/demo/runs.jsonl`）：09-29 16:53 那次执行用 `cross`（默认）→ 12 个 cross 仓；
之后控制台 `td_mode` 下拉被切到 `isolated`，09-30 10:50 的**两次**执行（相隔 22 秒，各 18 单 9 成 9 败）
把**同样的 9 个合约**又开了一份 `isolated` 仓。9 败全是模拟盘没有的合约
（`51001` 合约不存在 / `51087` 已下架 / `51169` 无持仓可平）。

### 两个「看不见」

1. **`td_mode` 不落盘、无唯一主人。** 只是某个 `LiveEngine` 的内存属性：
   `LiveLimits` 无此字段 ⇒ `limits.json` 无此字段；**`fills.jsonl` 里没有 `tdMode`**（只有
   `clOrdId/instId/side/posSide/sz/px/fee/notional/...`）；`AutoTrader.__init__` **从不传它**
   ⇒ **守护进程恒 `cross`**，控制台可被 `{"td_mode":"isolated"}` 改成 `isolated` 且**重启即静默回 `cross`**。
   实测同一时刻：`/api/live/status?mode=demo` 报 **`isolated`**，同进程新建 `LiveEngine(mode="demo")`
   报 **`cross`** —— **同一个账户**。⇒ 下一笔订单用哪种模式取决于**哪个进程先提交**。
2. **`liquidation_ok` 对模式不敏感，且读 `rcfg.max_leverage` 而不是交易所上实际生效的 `lever`。**
   `liq_dist = 1/max(rcfg.max_leverage, 1e-9) − mmr`（`rcfg.max_leverage = 5` ⇒ 19.50%）。
   所以它可以一边认为"19.5% 很安全"，一边账户上躺着 **100×** 的逐仓仓（真实 0.58%，**偏 33 倍**）。
   而且 `set_leverage = None` 时**系统从不设杠杆** ⇒ 逐仓腿取交易所默认值（BTC 就是 100×）。

### 建议的处置顺序

1. 先处理 `BTC-USDT-SWAP` 的 100× 逐仓空头（0.58%，几小时内可能被强平）。
2. 把 `td_mode` 从控制台下拉里拿掉，钉成账户级常量 `cross`（**不要**做成可切换选项）。
3. 让 `td_mode` **落盘 + 进成交记录**，否则事后无法判断某笔成交用的是哪种模式。
4. 对账 9 个重复合约（OKX 当两个独立仓位，`_okx_state` 把 `sz` 相加 ⇒ 敞口对、**风险口径错**）。
5. 让 `liquidation_ok` 至少读**实际** `lever`（`position.lever`），否则闸门可以被 33 倍地绕过。

## 逐仓杠杆「能不能统一设成 3×」（Q38）的细节

### 为什么"逐仓倍数会变"是个错误前提

杠杆是**账户级配置**，键是 `(instId, mgnMode, posSide)` —— **每个合约 × 每种模式 × 每个方向一个格子**。
`leverage-info` 返回 **两条**记录（long / short），这就是"逐方向"的直接证据：

```
LINK-USDT-SWAP isolated = {'long': '3', 'short': '3'}
```

全账户实测（12 个持仓合约 + 3 个从未交易过的对照 LINK/AVAX/ATOM）：

| | cross 格子 | isolated 格子 |
|---|---|---|
| 全部 | **3×** | **3×** |
| `BTC-USDT-SWAP` | 3× | **100×**（= 该合约 `max_lever`） |

⇒ **切模式时"倍数变了"只是读了另一个格子**；BTC 的逐仓格子是唯一例外，且是遗留值。

### 写入实测：能改，但有四个坑

| # | 坑 | 证据 |
|---|---|---|
| ① | `mgnMode=isolated` **必须带 `posSide`** | 不带 → **HTTP 400**；带 `posSide=long` → 200 |
| ② | `batch-set-leverage` **不存在** | `POST /api/v5/account/batch-set-leverage` → **404**；数组 body 打 `set-leverage` → 400 |
| ③ | 一次只改**一个方向** | 设 long 不动 short ⇒ **每合约两次** |
| ④ | **回读有几十秒延迟** | `set-leverage` 立刻回 200 并回显请求值；`leverage-info`/持仓的 `lever` 继续显示旧值 |

④ 的实测：连发 set 4 / 3 / 2，三次回读都是 **10**；45 秒后变成 **2**（最后落地的那个）。
⇒ **写完立刻回读不能当验证。**

### ⚠️ 核心：改配置 ≠ 改持仓

干净腿对照（NEAR isolated long，$350 名义，3×，**从未被碰过**）：

```
before (clean)   config=3  pos.lever=3  margin=116.906  liqPx=3.317332  距离=32.44%
after 3→5 +65s   config=5  pos.lever=5  margin=116.906  liqPx=3.317332  距离=32.53%
                 ^^^^^^^^^^             ^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^
                 配置和 lever 都变了     逐位不变        逐位不变
```

距离那 0.09pp 的差来自 `markPx` 移动，**不是爆仓价**。`lever` 是**配置的镜像**，不是持仓被计价的杠杆。

**唯一能读真相的办法**是从 `liqPx` 反推（`audit_margin_mode.implied_leverage()`）：

```
L ≈ 1 / (|liqPx/markPx − 1| + mmr)
```

⚠️ 它是**估计量**：OKX 用逐合约维持保证金率（含手续费），我们假设统一 `mmr=0.005`。
9 条腿实测：`config=3` 反推 **2.98–3.30**（±10%）、`config=100` 反推 **93.63**（−6%）。
⇒ 只能当**粗脱钩探测器**（阈值 20%），不是精确读数。产物 `41f_config_vs_actual.csv`。

### ⚠️ 这次实验动了账户，必须交代

ARB 那条逐仓空腿从 3× 变成 **~2×**（`margin` 174.75、`liqPx` 0.302064 ⇒ 实际 2.03×），
配置值已还原成 3。**方向上是变安全**（距爆仓 48.73% vs 32.83%）。
要真正改回 3× 只能**平掉重开** —— 交易动作，**没确认不做**。
BTC 那条 100× 腿**全程未动**。

### 结论

**"统一设成 3×"治不了根。** 真正的问题是逐仓把对冲账本拆成 N 笔独立方向押注（Q36）。
建议顺序（都要确认）：① 决定是否保留逐仓（建议**不保留**，钉死 `cross`）→ ② 若保留则修
`set_leverage`（补 `posSide` + 每合约两次 + 写完等 60s 用 `liqPx` 复核）→ ③ BTC 那条腿单独处理
（平掉重开需约 **$594** 保证金，或直接加保证金）。

## 控制台契约 / 启动脚本（Q39 细节）

### `/api/` 的错误**必须**是 JSON —— `BaseHTTPRequestHandler` 默认给 HTML

对**没有实现的方法**，`BaseHTTPRequestHandler.handle_one_request` 会调
`send_error(501, "Unsupported method ('DELETE')")`，回一张
`Content-Type: text/html` 的错误页。所以「前端 `resp.json()` + 一个可能缺方法的服务端」
= 一个**把根因藏起来**的报错：

```
删除失败：Unexpected token '<', "<!DOCTYPE "... is not valid JSON
```

真正的原因一个字都没露出来。实测（2026-09-30）：控制台进程 09-29 18:51 起，
`do_DELETE` 09-30 00:46 才进仓库 ⇒ 改过 `webapp/` 没重启 ⇒ 501 + HTML。

* **服务端**：`webapp/server.py::Handler.send_error()` 覆盖 —— `/api/` 下任何错误都回
  JSON 信封；**静态资源/页面仍走父类 HTML**（浏览器直接打开坏链接时要好看）。
  测这条必须用**未实现的方法**（`PUT`）打**非** `/api/` 路径，因为 `do_GET` 自己就对
  未知路径回 JSON 404（既有行为），`GET` 分辨不出覆盖范围。
* **前端**：`deleteRun` 先 `resp.text()` 再决定怎么解释；501 直接点名「控制台多半没重启」。
* 守卫 `tests/test_console_api_contract.py`（10 条，**真起服务** + 裸 socket 打请求）。

### 改 `webapp/*.py` 必须重启 8790 —— 而且"看起来正常"是它最坏的样子

旧进程照样服务页面、`GET /api/*` 全正常，**只有新加的方法回 501 + HTML**。
判活/复现都用**裸 socket**（`curl` 在本沙箱说谎）。重启：
`python <skill>/persistent-background-process/scripts/daemon_run.py <cwd> <log> <pid> <venv python> -m webapp.server 8790`
（端口**必须位置参数传**，否则落到 8770 与隔壁项目撞）。

⚠️ **范围要收窄：只有 `.py` 需要重启。** 实测 `GET /static/app.js` 回的字节与磁盘
**逐位相同**（174,031 B）⇒ 静态文件是**每请求读盘**的，改 `app.js` **不用**重启。
（之前写成"改 `webapp/` 必须重启"太宽，会让人白白重启。）

### `fmt.dol` 必须带两位小数

原来是 `Math.round(v)` ⇒ `$2,586.98` 显示成 `$2,587`。改成
`toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})`；
**唯一例外是真·亚分钱**（`|v| < 0.005` 用 `toPrecision(3)`）——
`$0.00` 会假装"这是零"，比不显示小数更糟。

⚠️ **负号必须在 `$` 之前**：`-$1,234.50`。原来直接拼 `'$' + v.toLocaleString()`
得到 `$-1,234.50` —— 币种符号和负号打架。写法：**先取绝对值排版，再补符号**
（`(n < 0 ? '-$' : '$') + body`），`-0` 仍是 `$0.00`（不是 `-$0.00`）。

### ⚠️ 「测试全绿」≠「显示是对的」：断言可能把**现状**写进去了

`d(-1234.5) === '$-1,234.50'` 这条断言**一路是绿的**，因为它是照着当时的输出写的 ——
它锁住的是**现状**，不是**意图**。所以那个难看的负号位置活了很久没人发现。

⇒ 写断言时问一句：**这个字符串是"应该这样"还是"现在这样"？**
凡是"现在这样"的，都要回头确认一遍它是否真的是应该的样子。
（同类：`smoke_live.js` 的 MiniDom 结构断言、快照式断言。）

### `.command` 启动脚本：判据是「**能 import 依赖**」，不是「可执行」

候选表里曾写 `~/.workbuddy/binaries/...`，**真路径是 `~/.workbuddy-ai/binaries/...`**
（少一个 `-ai`）⇒ 一路回退到 `/opt/homebrew/bin/python3`，而它**没有 numpy** ⇒
`ModuleNotFoundError: No module named 'numpy'`，报错**指不到"解释器选错了"**。

* 本机实测：托管 venv `numpy 2.5.3 / pandas 3.0.6`；`/opt/homebrew/bin/python3` **没有**；
  `/usr/bin/python3` 有 `2.0.2 / 2.3.3`。
* 修法：逐个候选 `"$cand" -c 'import numpy, pandas'`，失败就记进 `PY_TRIED` 继续找；
  显式 `PY` 也过同一道检查。全部失败时把试过的路径**列出来**。
* 三个脚本（`START_AUTO_TRADER_DEMO` / `START_CONSOLE` / `STOP_AUTO_TRADER`）**同步改**，
  并 `chmod +x`（原来都是 `-rw-r--r--`，双击不会执行）。

### ⚠️ bash：`$VAR（` 会把全角括号吞进变量名

`PY_TRIED="... $PY（显式指定...）"` 在 `set -u` 下报
`PY�: unbound variable` —— bash 把多字节的 `（` 当成标识符的一部分了。
**`$VAR` 后面紧跟非 ASCII 时必须写 `${VAR}`。**

### ⚠️ 变异脚本：还原必须放 `finally`，shell 变量必须显式传参

写变异脚本时我踩了两个：
1. `subprocess` 里引用了 shell 变量 `NODE`（heredoc 不继承未 export 的变量）⇒
   `NameError` 在**注入之后**抛出 ⇒ **`app.js` 被留在变异态**（`fmt.dol` 变回取整）。
   ⇒ 还原放 `finally`，并**在收尾打印每个文件的 sha**。
2. 变异脚本用 `python - "$WORK"` 传参，别靠环境。

### `smoke_live.js` 现在 stub 了 `alert` / `confirm`

以前没 stub ⇒ `deleteRun()` 第一句 `confirm(...)` 就 `ReferenceError` ⇒
**删除路径一直测不到**。现在 `ALERTS` / `CONFIRMS` 是数组，可断言弹窗内容。
（金额与删除共 11 条新断言，变异全抓。）

## 换数据源 / 换交易所（Q40 细节）

### 缓存目录：`settings.CACHE_DIR` 是唯一来源

```python
# config/settings.py
CACHE_DIR = os.environ.get("CRYPTO_CACHE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data_cache"))
```
`store.CACHE = CACHE_DIR`、`download.CACHE = CACHE_DIR` —— **都不许自己再算一遍**。
判据：`tests/test_data_source_switch.py::test_cache_dir_has_exactly_one_source`。

用法：
```
CRYPTO_CACHE_DIR=$PWD/data_cache_binance python -m crypto_ls_research.data.download \
    --source binance --bars 1h --reuse-pool --only-candles
python scripts/binance_backtest.py --selectivity
```

### 币安 K 线 → OKX 行形（`binance_client.klines_range`）

| OKX `history-candles` | 币安 `klines` | 用途 |
|---|---|---|
| `[0] ts` | `[0] openTime` | 网格 |
| `[1..4] o/h/l/c` | `[1..4]` | 同 |
| `[5] vol`（张数） | `[5] volume`（基础币） | 只被 MC bootstrap 当伴随序列 |
| `[6] volCcy`（**基础币**） | `[5] volume` | **`store.vwap = amount/vol_ccy`** |
| `[7] volCcyQuote`（**计价币**） | `[7] quoteVolume` | ADV 门槛 + `flow` 因子 |
| `[8] confirm` | **无** → `closeTime < now` | 丢掉未收盘 bar |

⚠️ **`vol_ccy` / `amount` 写反不报错**，只会让 vwap 变成 ~价格²。
守卫靠**性质断言**（`amount/vol_ccy` 必须落在价格量级），不是靠比对字面值 ——
字面值断言在字段对调时也会"看起来对"。

⚠️ **`_BAR_MS` 查表要显式校验**：否则 `bar="7h"` 抛的是 `KeyError` 而不是带候选列表的
`ValueError`（`_BAR_API` 的校验在 `klines_page` 里，查表在它之前）。

⚠️ **币安 `limit=1500` 的权重是 10**，上限 2400 weight/min ⇒ **安全速率 4 req/s**，
不是资金费那条路用的 8（那个端点权重 1）。超了会吃 418/429 + 退避，19 分钟的活变一小时。

### 比较两个场所的行情：三件事必须先做

1. **除掉固定面值因子再比价格水平。** 币安把若干合约按 1000 倍计价
   （`1000PEPEUSDT` / `1000SHIBUSDT` / `1000BONKUSDT`），**水平比天然 ~1000**。
   正确做法：`ratio = close_bn/close_okx`，**除以每列的 `ratio.median()`**，再量离散度。
   第一版没除 ⇒ p99 报 **999.36**，差点当成"价格差 999 倍"。
   **收益是尺度无关的**，所以这个只影响水平比较、不影响收益相关。
2. **对齐到 `inner` 交集**：两边缓存的最新 bar 不同（币安 09-30 00:00 / OKX 06:00），
   且列集合要取交集。
3. **注意覆盖度**：91/131 个标的币安上市更晚。**"同一份标的清单" ≠ "同一段历史"**。

### ADV 门槛不是尺度无关的量（Q40 的核心）

`universe.min_avg_amount_usd` 是**绝对美元**阈值，而 `amount` 是**交易所自己报的成交额**。
币安/OKX = **3.67 倍** ⇒ 同一个 `$3M` 在 OKX 筛出 **16.1** 名、币安 **38.7** 名。

**所以"换数据源"实验必须同时报「有效横截面宽度」**，否则会把
"换了个策略"当成"换了个数据源"。

决定性做法：**把门槛按各自 ADV 分布的分位数扫**，横轴用**实际筛出的名字数**
（而不是门槛金额）对齐，再比 Sharpe。

| 实际宇宙 | ~5.7 | ~10.5 | ~16.5 | ~25 | ~37 | ~50 |
|---|---|---|---|---|---|---|
| OKX | −0.091 | 1.511 | **1.942** | 1.184 | 0.949 | 0.528 |
| 币安 | 0.986 | 1.340 | 1.372 | 1.494 | 1.024 | **1.464** |

⚠️ 顺带否掉了一个假设：宇宙 16 < `2×top_k = 20`，怀疑长短名单重叠。
**实测每档 `mean_long_overlap` 都是 0.00** —— `select_book` 按分数正负分腿，
构造上不可能重叠。**先测再猜**；这个数现在固定在扫描输出里。

### 内存

同时持有两个场所的 panel（131×50k×7×float32 ≈ **190 MB/个**）+ 多次回测的残留
⇒ **exit 137（OOM）**。每个场所用完即 `del panels, adv, pooled` + `gc.collect()`。

## 自动交易 / 设置持久化 / 三个「倍数」（从 `MEMORY.md` 下沉，2026-09-30）

> 以下三节原本整段在 `MEMORY.md`；因注入预算紧张下沉到本文件。**规则本身不变**，`MEMORY.md` 只留指针。

### 自动交易（无人值守调仓）
- 入口 `... execution.auto_trader --mode demo --interval 30 --rebalance-days 1.0 --bar 1h
  --refresh-after-hours 1.0`；策略 `engine.DEFAULT_SIGNAL`。实盘需 `--allow-live`。
- **`not_due` 是 `warn` 不是 `block`** ⇒ 引擎**不拦**非调仓日，到期判定必须写在调度器里，断言「**`execute`
  根本没被调用**」。**绝不传 `force`**。闸门顺序：熔断 → 数据新鲜度 → 到期 → 风控上限。
- **到期闸门是「边沿」不是「水平」**：`rebalance_window_open(since) == (since == 1)`；旧规则 `since >= R`
  会**恒定落后回测一个周期**。实盘补一根「执行 bar」⇒ 窗口 **2 根宽**；**「还差几根」= `R − since`**。
  **`next_decision_ts` 不是「未来的下一个决策时刻」**（= 最后一笔已记账网格点 + R，常已过去）；后端
  `explain_not_due` 与前端 `dueHint` 是两个渲染点，测试必须**从 fixture 派生**。
- **`refresh_after_hours` 只决定抗漏 tick 余量，不决定窗口打开时刻**：刷新间隔必须**小于一根 bar**，否则漏
  一个 tick ⇒ `since` 0→2 ⇒ **整天不成交而日志全是 `not_due`**。**只看 `bars_since_decision`。**
- **限额闸门比 `plan.realised_gross`**（被节流后），不是 `target.gross`。**跨进程调仓锁**
  `store.rebalance_lock(mode)`（`O_EXCL`，TTL 900s）：并发 `execute()` 会各发**完整**订单 ⇒ 仓位翻倍。
  `os.kill(pid,0)` 的 `EPERM` = **存在但无权发信号** ⇒ **存活**。`gross_realised` 必须是**实际成交后的账**
  （被拒的腿保持原仓位），不是目标值。

### 设置持久化（「保存了重启不生效」）
- 保存必须以**落盘**收尾并回**落盘路径**（限额在 `artifacts/live/<mode>/limits.json`，**故意不在
  `LEDGER_FILES`**）。只赋内存再回 `{"ok":True}`，与「按钮没接线」无法区分。
- `load_limits()` 的 `None`（从未配置）**≠** `{}`（用户清空 → 全零上限，拒掉每笔）→ 没值必须传 `None`。
- 同名不同义必须标注：账户区「换手预算」= `execution.max_daily_turnover`；限额表 `max_turnover_frac`
  = **硬闸门**（超即整批拒绝）；两个调用点同源（`_turnover_from()`）。`onclick = liveSaveLimits` 会把
  MouseEvent 当 `requireDue`（恒真）→ 必须包 `() => ...`；无显式传参时**读屏幕勾选框**。

### 三个「倍数」只有一个改订单
- **`set_leverage` 不改任何一笔订单**（只压维持保证金）；**放大目标敞口也无效**（换手预算是净值的比例）。
  **硬不变量，别「修」它** —— `build_plan()` **没有 leverage 参数**（`tests/test_size_invariance.py` 钉签名）。
- **唯一有效旋钮 = `execution.max_daily_turnover`**（$70 上 0.2 → **6 笔/39.6%**，1.0 → **14 笔/93.1%**），
  ⚠️ 但当**回测参数**扫 Sharpe **1.82→1.10→0.46** —— **放开节流就打坏策略**。不变量是「净值 × 节流」。
- **杠杆唯一作用是保证金**：$70 在 5× 下最多持 $350 = **正好用光、缓冲 $0**（**18.4% 反向波动即亏光**）。
  ⚠️ `§16.5`「$70 加 L 倍 ≡ 有效资金 $70L」**与实现不符**（已加 §16.5.1），但 MDD 列仍有效。
- **测量纪律**：比杠杆必须用**同一个引擎**（价格缓存 15s TTL）；**仓位小先查换手爬坡、拒腿、订单方向。**
- **本金门槛**（别抄报告旧数）：出厂节流 0.2 下 $70 → **39.4%** 覆盖率、$500 → 96.8%、$2,000 → 100%；
  `min_viable_capital` ≈ **$1,048**。$70 的权重误差中位 **0.0663** ≈ 单腿平均目标权重 **0.0640**
  ⇒ **误差与信号同量级**。`min_nav_usd` 默认 **$50** ⇒ $70 **不会被风控拦住**。
  产物 `scripts/plan_report.py --mode demo`。

## 事前宽度检验（Q42 细节）——「宇宙变窄」到底有没有用

**跑法**：`python scripts/exante_width.py`（约 20 s，纯只读，**不需要重建面板**）。
产物 `artifacts/exante_width/{config.json, summary.json, tables/70*}`；
结论 `artifacts/FINDINGS.md` Q42；报告 `artifacts/FAILURE_ATTRIBUTION.md` 第十一节。
守卫 `tests/test_exante_width.py`（11 条）。

### 为什么要做

Q41 把「宇宙变窄」写成「伴随现象 / 放大器」就停手了 —— 那是**没检验就下结论**。
它是 Q41 里**唯一**同时满足三点的候选：① 4/5 窗口成立；② **事前可见**；③ 能挂上具体机制
（`cap × n ≤ 1` ⇒ `inverse_vol_weights` 静默退化成等权）。

### 三个必须处理的坑

1. **宽度有长期趋势**：与调仓点序号 Spearman **+0.398**；年均 2021 **11.7** → 2024 **23.1** → 2026 **13.1**。
   ⇒ 必须用**滚动百分位**（只用 t 之前 180 期）。原始口径会骗你。
2. **未来损益也有趋势** ⇒ 除原始损益外还要看**滚动去均值**版本。
3. **宽度 0 的调仓点**（实测 **13** 个）无仓位、损益恒为 0 ⇒ 剔除并报数。

### 结论（两个假设都被否定）

* **宽度 → 下一期损益：没有预测力。** 8 个组合（原始/去趋势 × 净/毛 × 原始宽度/滚动百分位）
  的 |t| **全部 < 1**。按滚动百分位分 5 组：Q1（最窄，11.8 名）净 **+0.0013**、
  Q3 **+0.0017**、Q5（最宽，21.6 名）+0.0011；最窄−最宽 = +0.0002，
  Welch p = 0.763、**块置换 p = 0.776**。**最窄那组是正的。**
* **`cap × n ≤ 1` 的静默等权真的发生了，但不造成损失。** 先在真实账本上确认机制：
  绑定组权重离散系数 **1.24e-17**（精确等权）、不绑定组 0.391。
  但绑定组下一期损益**略好**：原始 +0.0014 vs +0.0009（块置换 p = 0.413）；
  去趋势 +0.0012 vs −0.0001（Welch p = 0.094，块置换 p = 0.060）。
  ⚠️ **不要**因此去调大 `max_weight_per_instrument`：那是无证据的参数改动，且抬高集中度。

### 两个对 Q41 的更正

* **「4/5 窗口宇宙更窄」被趋势污染。** 滚动百分位下：W1 0.189（窄）、W2 0.878（宽）、
  **W3 0.746（宽，与原始口径翻转）**、W5 0.261（窄）、W4 **不适用**。
  ⇒ Q41 的 D 从「伴随现象」降级为「**没有证据**」。
* **W4 是样本第一个月（index = 1），且按每单位敞口是最严重的窗口。**
  W4 −0.0934 / 敞口 0.365 = **−0.2558**，W1 −0.2110、W5 −0.1822、W2 −0.1392、W3 −0.1377。
  名义 DD 最小只是因为**换手节流让账本只投了 36.5%**。W4 的「宇宙 7.4 名」主要是
  **数据可用性**（2021 年池子里只有 11.7 个合格名字），cap 绑定占比 **88.4%**（正常 13.0%）。
  冷启动（前 180 期）vs 成熟期：净损益 +0.0004 vs +0.0010（Welch p = 0.311、块置换 p = 0.375）
  ⇒ 不显著更差，但**敞口只有一半**（0.524 vs 1.028）。

### 第三次踩到同一个坑

⚠️ **W4 差点又被静默丢掉**（滚动百分位需要 180 期历史，而 W4 在第一个调仓点），
第一版直接把它从表里删了。**这已是第三次**：
1. `momentum_reversal` 要求池 ≥20 名 ⇒ 5 个回撤窗口全 0 样本；
2. `rotation_table` / `liquidity_table` 的 `windows` 参数被接受却不生效；
3. Q42 的 W4 滚动百分位无定义。
⇒ **纪律：「样本不足 / 参数不生效 / 算不出来」必须显式输出一行 + 一个可用性布尔列，
绝不许 `continue` 掉。** 否则最该看的那个样本恰好消失，而且**看不出来**。

### 变异测试记录（5 个，全被抓）

`width_pct` 把当前点算进历史 / 改用**未来**窗口 / 前向损益差一根 bar /
`cap_binding` 判据 `<=` 改 `<` / 静默丢掉 `width_pct` 算不出的窗口。
其中一条是**真正的无未来函数检验**：把回测截断到前 k 期后，前 k−1 期的 `width_pct`
必须**逐位不变**（`types.SimpleNamespace` 造截断结果，不需要复制大对象）。

---

## 空腿挤压检验（Q43 细节）——「被崩跌式反弹挤压」是**已否定**的

**跑法**：`python scripts/short_squeeze.py`（约 30 s，**纯只读**，用已有产物，不重建面板）。
产物 `artifacts/short_squeeze/{config.json, summary.json, tables/71*}`（`config.json` 里
`"optimization_performed": false`）；结论 `artifacts/FINDINGS.md` Q43；报告 `artifacts/FAILURE_ATTRIBUTION.md`
第十二节；模块 `crypto_ls_research/analysis/failure_attribution.py` 的
`label_stated / _bar_simple_returns / _period_sum / short_book_frame / short_book_table /
short_contributors / squeeze_verdict`。守卫 `tests/test_short_squeeze.py`（13 条）。

### 为什么要做

Q41 把「空腿被崩跌式反弹挤压」列为头号「证据不足 / 无法确认」——因为当时只测了空腿的
**加权损益**，从没测过**被做空那些名字同期的收益分布**。「空腿亏钱」本身不是证据：做空一个
上涨的市场必然亏钱。

### 核心：空腿亏损必须**三分**（逐点恒等式）

对每个调仓点 i，把空腿加权名字收益 `mw_ret_names`（= `−short_pnl / short_gross`）分解：

```
mw_ret_names = pool_mean + excess_select + excess_weight
    pool_mean      = 池子（全部合格名字）等权收益        ← 市场
    excess_select  = ew_mean − pool_mean               ← 选股（等权口径）
    excess_weight  = mw_ret_names − ew_mean            ← 加权（仓位集中度）
```

**只有 `excess_select` 能判「挤压」** —— 它与 `pool_mean` **同为等权口径**，剔除了「做空一个
上涨的市场」这个必然项。`short_book_table` 另给一条 NAV 三分解
（`short_pnl_market / _select / _weight`），三项相加 = `short_pnl`（残差 4.4e-16）。

### ⚠️ 最贵的坑：`excess_mw` 是**错的判据**

`excess_mw = mw_ret_names − pool_mean` 拿**市值加权**减**等权**池子 —— 两个口径不同。牛市里它
**恒为负**，于是把「空得最多的那几笔恰好涨得最多」误记成「被挤压」。**第一版就是这么报的**，
必须换成 `excess_select`。符号陷阱：`mw_ret = short_pnl/short_gross` 是**损益**（正 = 空腿亏），
`mw_ret_names = −mw_ret` 才是名字**收益**；拿损益去比等权收益在结构上就是错的。

### 结论

* **选股效应 5/5 窗口不显著**：Welch p **0.14–0.73**、块置换 p **0.08–0.73**（在 `excess_select` 上跑）。
* **大涨占比 4/5 窗口低于池子**（`frac_big` 用 `> +5%` 计数）。
* 亏损主要是**做空了一个上涨的市场**：W2 市场项占 **100%**、W4 占 **66%**；
  W1 的例外来自**仓位集中度**（加权项），不是选股。
* ⇒ **「空腿被崩跌式反弹挤压」已否定**（`squeeze_verdict`：W1/W2/REST = 已否定，
  W3/W4/W5 = 证据不足 —— 因为那几个窗口样本少）。

### 对账与「假守卫」

* **区间分组**：`short_pnl` 对 `bars['short_ret']` 逐点求和，`max|Δ| = 1.3e-09`（float32 精度）。
  分片口径：决策 `d = dec[i]`、执行 `t = d+1`，第 i 期收益是 bar 区间 `[dec[i]+1, dec[i+1]+1)`。
* ⚠️ **这条对账是假守卫**：`short_pnl` 本身就来自引擎 `name_gross`，两边同源 ⇒ **把前向起点挪一根
  bar 它不会红**，只钉住「区间怎么分组」。**变异测试当场抓出**。
* 修法：加**由价格独立算出**的列 `short_pnl_px / long_pnl_px`
  （`R_k = Σ_{t∈[a,b)} (px[t+1]/px[t] − 1)`，再 `Σ held·R`）与 `name_gross` 对账
  （`max|Δ| = 2.9e-09`），**这才钉住价格口径**。⇒ 纪律：**对账要用「独立算一遍」的量。**

### 变异测试记录（5 个，全被抓）

前向起点 ±1 bar / `excess_select` 误用 `excess_mw` / `mw_ret_names` 漏负号 /
`frac_big` 阈值改向 / 声明过的窗口被 `continue` 掉。**第 2 条在第 1 轮暴露了上面那条假守卫**
（4/5），补上价格列后重跑 **5/5**。

---

## 记忆文件本身的预算

⚠️ **2026-09-30（第二次）更正：真的存在注入上限，位置在 17.2 KB 与 20.8 KB 之间。**

数据点：
- 会话开始时 `MEMORY.md` = **17,160 B** → 注入的 `working_memory_content` **逐字包含到文件最后一行**，没截断。
- 同日晚些时候 `MEMORY.md` = **20,844 B** → 系统在注入时**截断**，并在提示里要求先压缩。
⇒ 上限 ∈ **(17,160, 20,844)**。**保守目标 ≤ 16.5 KB**，留余量。

处置（本次已做）：`MEMORY.md` 压到 **15,135 B / 12 节**（原 20,844 B / 19 节，**−27%**）。
手法 = ① 合并同类节（`自动交易`+`设置持久化`+`三个「倍数」`→ 一节）；② **整段下沉**：把细则原文搬到
本文件（`## 自动交易 / 设置持久化 / 三个「倍数」`），`MEMORY.md` 只留**指针 + 铁律**；③ 已完成检验的
结论（Q32–Q35）只留**会再踩的规则**，数字指向 `FINDINGS.md`。

⇒ **规则**：往 `MEMORY.md` 加东西时**顺手压掉等量内容**；**细则一律写本文件**（它不自动注入，体积不是
问题，但它也因此**不进上下文**）——**真正的铁律必须留在 `MEMORY.md`**。加完用
`wc -c .workbuddy-ai/memory/MEMORY.md` 复核，**超过 16.5 KB 就再压**。

---

## 失效归因（Q41–Q43）——**只诊断，未优化**；推导/表格在 `FINDINGS.md` Q41–Q43 + `RULES-OPS.md`
- **恒等式 `net = gross − fee − spread − impact **+** funding`**（`engine.py:433` 把带符号的
  `funding` **加**进账）⇒ 写成 `− funding` 会把每笔资金费记成**贷记**（全期 `+0.0918`）。
- ⚠️ **5 个回撤窗口不是同一套边界**（W2 第二个数是恢复日、W3 第一个数是回撤内部局部高点）
  ⇒ 必须**同时报 `stated` 与 `episode`（真峰→谷）**；**W2 的 stated 净收益是 +0.0188（正的）**。
  **W4 落在样本首月（index=1）**。
- ⚠️ **`ic_mom[t]` 是同期量** ⇒ 用同期 IC 做 regime 标签 = 「拿结果解释结果」：**同一批 386 天**，
  同期 Sharpe **−4.97** vs 滞后 **+3.01**（**符号翻转**）。默认列只能是 `mom_ic_5d_trailing`。
- ⚠️ **IC ≠ 尾部价差**：IC 看整个横截面，P&L 只看两端，可以不同号（`rev_short` 就是）。
- ⚠️ **事前状态量先去趋势再谈预测力**：宽度 vs 序号 Spearman **+0.398** ⇒ 滚动百分位下 **W3 从「窄」
  翻「宽」**；去趋势后 |t| **全 < 1**，`cap×n≤1` 静默等权已实测**但不致损** ⇒ **两条解释均被否定**。
  **按单位毛敞口算 W4 才是最严重（−0.2558）**。
- ⚠️ **空腿亏损必须三分：市场 / 选股 / 加权**（逐点恒等）。判「挤压」**只能看「选股」那一项**
  （等权口径）；`excess_mw`（市值加权减等权池子）是**错的判据**（两口径不同，牛市里恒为负）。
  Q43：**5/5 窗口选股效应不显著**、**4/5 窗口大涨占比低于池子** ⇒ **「被崩跌式反弹挤压」已否定**；
  亏损主要是**做空了一个上涨的市场**（W2 100%、W4 66%），W1 例外来自**仓位集中度**。
- 🔴 **头号反复缺陷：「算不出来」被静默丢掉。** 已犯 **3 次**（`momentum_reversal` 的「池 ≥20 名」、
  `rotation_table`/`liquidity_table` 的 `windows` 不生效、Q42 的 W4 无定义）⇒ **「样本不足 / 参数
  不生效 / 算不出来」必须显式输出一行 + 一个可用性布尔列，绝不许 `continue` 掉**。

---

## 定时器 / 实盘 opt-in
🔴 **细节整节在 `RULES-OPS.md`**（`allow_live=False` 硬编码导致实盘起不来、`LABELS/UNITS` 按模式分开、**`launchctl` 不能从沙箱注册**、守卫须早于第一次 `auto_ctl.` 调用）。**2026-10-10 起执行者改为控制台自身**（见下节）。

---

## 已完成检验的结论（Q32–Q35）——**推导/数字在 `FINDINGS.md` + `RULES-OPS.md`**
- **Q33**：**自写估计量必须先验证再信**（`_ols_nw` 曾把 HAC meat **多除一个 n** → t 虚高 ~45 倍）；
  **条件子集的「年化收益」无意义**（报条件均值/条件 Sharpe）。**必须披露**：熊市 0.898、**高波动 0.118**。
- **Q34**：**`long_ret`/`short_ret` 是带符号 P&L** ⇒ 镜像把 long 映到 **−short**（写成 `+short` 是最易犯的错）；
  `flip_book=True` 翻转的是**装配好的目标**（不是 `score = -score`）。
- **Q35**：**三条通道别混**（影线**逐位不进损益**，但 `range_pos` 读 180 根滚动极值 ⇒ **影线是信息**；
  缺口改损益但**选股逐位不变**）。**扛不住**：**无止损/止盈/算法单**、**无账户级回撤熔断**
  （`dd_scale` 读**回测模拟净值**）⇒ 唯一反应是下一网格点。守卫 `test_wick_channels.py`。

---

## 通知必须和「执行」一起搬（2026-10-12，Q50）
- 🔴 **搬走执行、留下通知 = 静默不发。** `notify_for()` 原本只在 `auto_trader` 里调用；
  执行搬进控制台后那条路**没人发通知**（用户报「交易了但没飞书」）。**换地方执行时必须
  问：它原有的副作用（通知/记账/审计）跟过来了吗？**
- 🔴 **两个词汇表不一致**：`engine.execute()` 的 `action` **恒为 `"rebalance"`**，
  真正区分结果的是 `stage`；而 `notify_on` 认 `traded`/`blocked`/`error`。
  ⇒ 必须**翻译**（`_STAGE_TO_ACTION`）。不翻译即使调了也得到"该结果不推送"。
- ⚠️ 拦截原因要填 **`blocked_by`**（`format_rebalance()` 读的键），不是 `errors`。
  **别猜字段名，去看格式化器读什么。**
- 🔴 **模块重复加载**：`server.py` 把 `webapp/` 加进 `sys.path` ⇒ `import live_api`（顶层名）
  与 `from webapp import live_api`（包名）是**两个模块对象**，各有一份
  `_REGISTRY`/`_JOBS`/`_ENGINES` ⇒ **线程建在 A、HTTP 读 B**，日志说"已恢复"而页面
  显示"没在跑"。**统一用包路径导入**。守卫 `test_auto_notify.py`。
- ⚠️ **"扫全文件找违规"会扫到自己注释**（断言 server.py 不许有某写法时命中 4 处，全是
  解释它错的注释）⇒ 只看非 `#` 行。⚠️ **测试别带"已把被测对象换掉"的 fixture**。
- 守卫 `test_auto_notify.py`（11 条 / 变异 6/6）。

---

## 两种「回测」：要不要先刷新行情，答案相反（Q52）
- **研究/验证回测**（`scripts/exp_*.py`、验收基线）**不刷、也不能刷**：读
  `data_cache/` 的 parquet（`load_panels` **只读盘不拉网**）。回测要**可复现** ——
  每次跑前拉一次网 = 每次在**不同的**历史上下结论，基线数字立刻不可比。
  刷新是**独立显式**一步（`data.download` → `asset_class` → `funding_build`），
  刷完**历史变了、旧数字作废、必须重新基线**。
- **实盘/纸面的「回测」**（`engine.target()` → `compute_live_target`）**必须先刷新**：
  它就是**用回测引擎在最新缓存上跑一遍** ⇒ 缓存不新，目标仓就是旧的。
  这正是 `auto_runner` 用 `refresh → 计划 → 下单` 顺序的原因。

---

## 数据源可切换 / 换交易所（Q40）——**表格全在 `RULES-OPS.md`**
- **缓存目录唯一来源 = `settings.CACHE_DIR`**（读 `CRYPTO_CACHE_DIR`）；`store.CACHE` 与 `download.CACHE`
  都**从它派生，不许各自再算一遍**。
- **`binance_client.klines_range()` 返回与 OKX 同形的 9 列**。最贵的坑是字段映射：
  **`vol_ccy` ← 基础币量、`amount` ← 计价币额**（`vwap = amount/vol_ccy` 必须是价格量级；写反
  不报错，只让 vwap 变 ~价格²）。守卫 `test_data_source_switch.py`（11 条）。
- ⚠️ **`min_avg_amount_usd` 是绝对美元阈值**（币安成交额是 OKX 的 **3.67 倍** ⇒ 同一个 `$3M` 在 OKX 筛出
  **16** 名、币安 **39** 名）⇒ **换数据源必须先看「有效横截面宽度」**。
- ⚠️ **比两家价格前先除掉固定面值因子**（`1000PEPEUSDT` 这类**天然 ~1000 倍**）；收益尺度无关。
- ⚠️ **验收的 1.94 落在 ADV 门槛的尖峰上**（OKX 宇宙 ~16 时 1.942，邻居只有 1.184 / 1.511；币安最高
  **1.494**）⇒ **1.94 至少部分是 (OKX 数据, 这个阈值) 的组合属性**（**关掉门槛 1.93 → 0.07**）。
  **"同一份标的清单" ≠ "同一段历史"**（91/131 币安上市更晚）。复现 `binance_backtest.py --selectivity`；
  ⚠️ 同时持两个场所的 panel 会 OOM ⇒ **用完即 `del` + `gc.collect()`**。

---

## 控制台 / 启动脚本（Q39）
- ⚠️ **`/api/` 错误必须是 JSON**：`BaseHTTPRequestHandler` 对**没实现的方法**回 `501 + HTML`
  ⇒ 前端 `resp.json()` 抛 `Unexpected token '<'`，**根因全被藏住**。`server.py::send_error()`
  已覆盖（**只**对 `/api/`）。⚠️ 旧进程 `GET` 全正常，**只有新方法回 501**。
  守卫 `test_console_api_contract.py`。
- **`fmt.dol` 两位小数**；例外是**真·亚分钱**（`|v|<0.005` 用有效数字，`$0.00` 会假装"这是零"）。
  ⚠️ **负号在 `$` 前**（`-$1,234.50`）⇒ 先排绝对值再补符号；`-0` 仍是 `$0.00`。
- ⚠️ 写启动/变异脚本三坑（细节 `RULES-OPS.md`）：`.command` 选解释器要验**能 import
  numpy/pandas**；bash **`$VAR（` 吞全角括号** ⇒ 紧跟非 ASCII 写 `${VAR}`；**变异脚本还原放
  `finally`** + 首尾打 sha。

---

## 保证金模式：单源常量 `settings.MARGIN_MODE` = **`isolated`**（2026-09-30 定案）
- **两次决定，理由不同、都成立**：① **频率** —— 逐仓每腿 `1/L − mmr`（3× **32.8%**），全仓
  `(1 − mmr·G)/G`（`G=0.43` → **231%**），任意 `L≥1,G≤1` 下**全仓 ≥ 逐仓**；② **偿付** ——
  **空头亏损无上限**，全仓下单合约暴涨打爆**整个账户**（实测最脆弱那条 **30.7 倍**）。
  **选了偿付**：全仓更不易被强平但一爆是全部；逐仓封顶在那条腿但常被强平。
- 🔴 **模式必须和杠杆一起动**：逐仓距离就是 `1/L − mmr` ⇒ 实测强平频率（K=10/年，上界）
  **3× → 29.5 次**、2× → 10.1、**1× → 1.0** ⇒ **按 1× 跑**。代价**不随流动性下降** ⇒
  **换宇宙解决不了**。
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

---

## 鉴权 / 二重认证 / 外网访问（Q54 细节，2026-10-16）

从 `MEMORY.md` 下沉。**加鉴权解决的首先不是"外网风险"，而是本机越权**：
控制台原本绑 `127.0.0.1` 且**零鉴权**，而"只有本机能访问"被当成了安全边界 ——
但那是本机**进程之间没有边界**。同机任何进程都能 `POST /api/live/execute` 下真钱单、
`GET /api/live/creds` 读 key、`POST /api/live/flatten` 一键平仓。

### 模块与接口
- `webapp/auth.py`（**纯标准库**，无第三方依赖）：
  - `hash_password` / `verify_password` —— `hashlib.scrypt`，串
    `scrypt$n$r$p$salt$hash` **自描述**（调参不迁移）；比较用 `hmac.compare_digest`。
  - `totp_at` / `verify_totp` / `new_totp_secret` / `otpauth_url` —— 手写 RFC 6238
    （30s / 6 位 / SHA1），**对过 6 组官方向量**。`window=1`（±30s）；
    传 `used` 集合即启用**重放防护**（记已用步数，落盘只留最近 20 个）。
  - `AuthStore` —— `is_configured / create_user / new_session / resolve_session /
    drop_session / drop_all_sessions / note_fail / note_ok / locked_for`。
    文件 `config/auth.json`，**0600**，临时文件 + `os.replace` 原子替换。
  - `LoginFlow` —— 两步登录之间的**一次性**票据，TTL 120s，`claim()` 用 `pop`。
- `webapp/server.py`：
  - `_PUBLIC_API`（白名单）/ `_PUBLIC_STATIC`
  - `Handler._gate()` —— 挂在 `do_GET` / `do_POST` / `do_DELETE` **开头**
  - `Handler._auth_route()` —— `/api/auth/{status,setup,login,verify,logout}`
  - `_resolve_bind()` —— 监听地址 + TLS；**明文绑非本机直接 `sys.exit`**
  - `_cookie()` —— `HttpOnly` + `SameSite=Strict` + **仅 TLS 下** `Secure`
- `scripts/START_CONSOLE_TLS.sh` —— 外网启动脚本（缺证书就报错退出）

### 参数
| 项 | 值 | 说明 |
|---|---|---|
| scrypt | n=2^14, r=8, p=1 | 约 50–100ms/次 |
| TOTP | 30s / 6 位 / SHA1 / window=1 | 与 Google Authenticator 等一致 |
| 会话 TTL | 8 小时，**不滑动** | 滑动 = "一直在用就永不过期" |
| 节流 | 5 次失败锁 300s | `MAX_FAILS` / `LOCKOUT_SEC` |
| 票据 TTL | 120 秒 | 口令过 → TOTP 提交 |
| TLS 环境变量 | `LS_HOST` / `LS_TLS_CERT` / `LS_TLS_KEY` | 私钥别进仓库 |

### 踩过的坑
1. 🔴 **`ROOT` 从别的文件抄会多爬一级**。`crypto_ls_research/execution/credentials.py`
   在根下**两层**（`dirname(dirname(__file__))` + `".."`），而 `webapp/auth.py` 只在
   **一层**。照搬 ⇒ `AUTH_FILE` 落到 `~/code/config/auth.json`（仓库**外面**）：
   `.gitignore` 管不到、删仓库里的文件删不掉账号、换 clone 后"登录不上"无线索。
   守卫：`test_auth_file_lives_inside_the_repo` + `test_auth_file_is_gitignored`
   （后者用 `git check-ignore` **实查规则**，不是看 .gitignore 里有没有那行字）。
2. ⚠️ **`==` 与 `compare_digest` 行为完全相同** ⇒ 任何只验 True/False 的测试都抓不到
   替换。必须**断言语义**（`inspect.getsource` 里有 `compare_digest`），且要**精确到
   具体那一处**（文件里有两处用它，泛泛地查函数里有它会漏）。
3. ⚠️ **`_read()` 有两条"读不到"的路径**：`except`（OSError/ValueError）与
   `else {}`（**类型不对**）。只测前者 ⇒ 把后者改成"返回假账号"测试仍全绿。
   两条各要一条用例。
4. ⚠️ **闸门在路由之前** ⇒ 未登录时"存在"与"不存在"的路径必须回**同一个** 401，
   否则是一条**枚举端点**的通道（`test_unauthenticated_gets_401_not_404`）。
5. ⚠️ 未登录回 **JSON 401 + `need_login`**，不是 302 重定向：前端一律 `resp.json()`，
   HTML 重定向页会抛 `Unexpected token '<'`、真因全被藏住（同 Q39 那条）。
6. ⚠️ **`Secure` 只在 TLS 下加**：明文加它浏览器会丢 Cookie ⇒ 本机开发永远登不上
   （一个"安全设置把功能全废"的典型）。两种情形都实测过。
7. ⚠️ **冒烟 stub 会断言 app.js 结尾** —— 入口从 `boot()` 换成 `authRender()` 后
   直接抛错（错误消息自己写了"stub 需要更新"）。冒烟现需
   `LS_TEST_USER` / `LS_TEST_PASSWORD`，且**真走登录**而不是绕过鉴权。

---

## 订单方向（踩过最贵的一脚）
- `long_short_mode`：**`posSide` = 动哪个仓、`side` = 方向**。加多 `buy/long`、减多
  `sell/long`、**加空 `sell/short`、减空 `buy/short`**；flip = 两腿。
- `_orders_for_instrument` 曾用 `abs(tgt)-abs(cur)` 推 `side`，**丢符号** → 加空腿全变买入、**静默**
  （订单被接受、计划正常，仓位反向走）。实测 demo 多头 **92.0%** vs 目标 **47.6%** ⇒
  **成交记录不能当策略演练**。修法 `d = tgt_sz - cur_sz`；守卫 `test_order_sides.py`。

---

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

---

## 新机器初始化数据（Q58–Q60 细节）

## 新机器初始化数据（Q58–Q60）——**`init_linux.sh` + `DEPLOY_LINUX.md`**
- 入口 `scripts/init_linux.sh`（幂等）→ `download` → `asset_class --build` → `funding_build`
  → **5b 重建基线产物**。依赖只有 **numpy/pandas/scipy/matplotlib/seaborn/pytest**。
  ⚠️ **`data_cache*/`、`artifacts/**`、凭据全被 gitignore** ⇒ 新机器上空的。
- 🔴 **代理端口写在默认值里**：三个客户端（`okx_client`/`binance_client`/`okx_private`）读同一个
  `OKX_PROXY`，**默认 `127.0.0.1:7897`**（开发机端口）⇒ 换机器报 **`[Errno 111] Connection
  refused`**（**看不出是代理问题**）。修法只需 `export OKX_PROXY=...`，**不用改代码**；
  `OKX_PROXY=`（空）⇒ 直连。⚠️ `DEFAULT_PROXY` 是**模块导入时**求值 ⇒ **必须 export 再起进程**。
  脚本有**代理体检**（下载前，不通就 exit 1）。
- 🔴 **`--end` 默认是硬编码 `2026-09-26` 且为排他上界** ⇒ 照默认跑**静静少二十来天、不报错**。
  必须显式给 `$(date -u -d tomorrow +%Y-%m-%d)`（macOS 是 `-v+1d`）。
- 🔴 **`artifacts/` 被 gitignore ⇒ 新机器页头必显示「基线数据缺失」**（只追踪 6 个 .md）。
  重建 **10 秒**：`scripts/rebuild_optimal_baseline.py`（只跑 `base`；参数**从 `webapp/spec.py`
  派生**）。⚠️ **`--override` 走 `json.loads`** ⇒ 列表必须写成 **JSON 数组**（逗号串会被
  **逐字符**解析成 36 个因子）。⚠️ `asset_class` 缺新合约 **fail-loud 退出**（不是警告）。
- 🔴 **重建的数字 ≠ 记录值，且不可修复**（两条机制）：① **`--end` 被 `load_panels` 刻意
  忽略**（网格总延伸到缓存最新 bar，"不截断样本尾部"）⇒ 区间总是到"今天"；② 候选池从
  「今天的实时挂单」建 ⇒ 新上市合约进池。实测 9-29→10-16：宇宙 **140→154**、
  Sharpe **1.9372→1.9688**（用基线 140 只后 **1.9258**，残差来自区间）。**记录值只是
  某时点快照。**
- 🔴 **别覆盖已验收基线**：`baseline.pkl`(33MB) + `tables/` 是**同一 tag 的两半**，只
  `cp -r tables` 等于没备份。实测覆盖后 5 个回撤窗口立刻失效（W2 −0.1082→−0.1131）且
  **换不回来** ⇒ `rebuild_optimal_baseline.py` **默认拒绝覆盖**（要 `--force`）。
- ⚠️ **`CRYPTO_CACHE_DIR` 设了就必须两端指同一个**；要两边可比就连 `meta/` 一起拷。
