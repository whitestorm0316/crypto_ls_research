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

## 保证金模式（Q36）的细节与审计

审计脚本 `scripts/audit_margin_mode.py [mode]`（默认 `demo`），产物
`artifacts/margin_mode/tables/41_position_margin.csv`、`41b_mode_summary.csv`、
`41c_liq_distance_by_mode.csv`、`41d_market_move_liquidation.csv`；
守卫 `crypto_ls_research/tests/test_margin_mode.py`（10 条，**11 变异全抓**）。
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

## 记忆文件本身的预算

⚠️ **2026-09-30 更正：之前记的"~16.5 KB 注入预算"没有复现出来。**

实测：会话开始时 `MEMORY.md` = **17,160 B**，而系统提示里注入的 `working_memory_content`
**逐字包含到文件最后一行**（`## 通知 / OKX` 的 `notional` 那条，结尾"外部 payload 先打一个
样本的全部字段再写聚合"）—— **尾部完整，没有截断**。所以那个 16.5 KB 的数字**至少不是硬上限**。

⇒ **不要为了一个复现不出来的数字删掉真规则**（这正是"判据坏了不许靠改判据变绿"的反面：
判据没坏就别说它坏）。仍然值得做的：往 `MEMORY.md` 加东西时**顺手压掉等量内容**，保持它
可读、不膨胀；长推导放本文件（`RULES-OPS.md` 不自动注入，所以它的体积不是问题，但它也因此
**不会**进入上下文——真正的规则要留在 `MEMORY.md`）。
