# Crypto Futures Liquidity-Flow Trend Market-Neutral Strategy

从 0 到 1 的系统化研究实现：**动态 PIT 流动性池 + 横截面多因子打分 + Top-K 多空 +
反向波动率加权 + Beta 对冲 + 多层风控 + 真实成本/资金费率回测 + 严格证伪**。

数据源：OKX USDT 本位永续（公共行情 API，无 Key）。
本文件按 Phase 组织，逐条给出「代码 / 公式 / 单元测试 / 输入输出 / 潜在风险 / 验证方法」。

---

## 0. 快速开始

> **从 git clone 下来的仓库不含数据**：`data_cache/`（305M）与 `artifacts/**`（692M）都在
> `.gitignore` 里，需要按下面的步骤 1)–2) 重新生成；`artifacts/` 顶层的三份交付文档
> （`FINDINGS.md` / `OPTIMIZATION_RESULTS.md` / `FINAL_REPORT.md`）随仓库一起带走。
> **`config/okx_creds.json` 也不在仓库里**（内含真实 API Key）——
> 复制 `config/okx_creds.example.json` 后自行填写。

```bash
# 1) 下载 K 线（约 5.5 万次请求，1.5 小时）
python -m crypto_ls_research.data.download --dry-run      # 先看预算
python -m crypto_ls_research.data.download --bars 1h 15m 5m

# 2) 拼接资金费（OKX 只留 3 个月，其余用币安补齐）—— 必须做
python -m crypto_ls_research.data.funding_build

# 3) 单元测试（192 个，含未来函数证明、资金费符号、账本恒等式、权重 sizing 退化与 §37 退化输入）
python -m pytest crypto_ls_research/tests -q

# 4) 主研究（1h / 日频调仓 / 全部分析阶段）
python -m crypto_ls_research.run.research --bar 1h --stages base exec beta funding \
        freq turnover ablation ic sens wf mc capacity bias charts --n-jobs 4

# 5) 汇总报告
python -m crypto_ls_research.run.report
```

产物：`artifacts/tables/*.csv`、`artifacts/charts/*.png`、`artifacts/REPORT.md`、
`artifacts/baseline.pkl`（完整持仓路径，供后续分析复用）。
用 `--tag NAME` 可把多频/多参数研究分到 `artifacts/NAME/` 下互不覆盖。

---

## 1. 时序协议（最重要的一件事）

```
decision bar  d        信号由 <= d 的 bar 计算（d 收盘）
signal        S[d]
order         d+1      在 bar d+1 开始时下单
execution px  px[d+1]  bar d+1 的成交价（next_open / vwap / twap / close 可选）
holding       [d+1, d+2] 用 px[d+2] 盯市
```

代码里 `held[t]` 由 `S[t-1]` 构造，收益为 `px[t+1]/px[t] - 1`。
**不存在任何用当根 bar 价格成交的代码路径。**

推论（也是单元测试的不变量）：把 `t0` 之后的全部数据替换成垃圾，
则 `net_ret[0 .. t0-2]` 必须逐位不变 —— 因为 bar `t0-1` 的盯市本身要用 `px[t0]`。
`tests/test_no_lookahead.py::test_backtest_pnl_is_invariant_to_future` 验证这一点，
并额外断言「污染后未来段确实变了」，防止测试自欺。

---

## 2. Phase 1 — 数据读取 + PIT Universe

**代码**：`data/okx_client.py`、`data/download.py`、`data/store.py`、`universe/pit.py`
**测试**：`tests/test_universe.py`（6 个）、`tests/test_no_lookahead.py::test_pit_universe_is_invariant_to_future_liquidity`

### 核心公式

```
amount_t          = volCcyQuote_t                      (OKX 直接给 USDT 成交额)
ADV_t             = mean(amount, liq_window_days * bars_per_day)     (滚动、仅历史)
age_t             = t - first_valid_index                             (上市时长, bar)
eligible_t        = isfinite(close_t) & (close_t > 0)
                    & (ADV_t >= min_avg_amount) & (age_t >= min_history_bars)
pool_t            = { i : rank_i(ADV_t) <= pit_topn,  i ∈ eligible_t }
```

### 输入 / 输出示例

```python
from crypto_ls_research.data.store import load_panels
from crypto_ls_research.universe.pit import universe_mask, listing_age_bars
panels = load_panels("1h", "2021-01-01", "2026-09-26")
mask, rank = universe_mask(close_t, age_t, adv_t, cfg.universe, min_history_bars=720)
```

### 潜在风险

| 风险 | 处理 |
|---|---|
| 退市合约不可得（OKX 对已下架合约返回 `51001`） | 无法消除；在 `analysis/bias.py` 中做退市冲击压力测试量化 |
| 流动性排名用了未来数据 | ADV 为滚动均值；测试中对未来 ADV ×1000 后池子不变 |
| 新币提前入场 | 以真实首根 K 线为唯一上市时间，测试 `test_no_trade_before_listing_end_to_end` |
| 缺失 bar 被当成 0 成交 | 网格对齐后缺失值为 NaN，`universe_mask` 要求 `isfinite(close)` |

### 验证方法

`pytest tests/test_universe.py`：年龄门、流动性门、排名单调性、TopN 上限、
多空池规模、端到端「上市前不交易」。

---

## 3. Phase 2 — Factor Engine

**代码**：`factors/engine.py`　**测试**：`tests/test_factors.py`（7 个）

```
F1 Momentum   mom_raw = close_t / close_{t-L} - 1
              vol     = std(logret, V) * sqrt(bars_per_year)
              momentum= mom_raw / vol                       (风险调整动量)

F2 Flow       flow    = mean(amount, S) / mean(amount, L)   (S=1d, L=30d)

F3 RangePos   hi = max(high, R), lo = min(low, R)
              range_pos = clip((close - lo) / (hi - lo), 0, 1)
              hi == lo  ->  NaN（不是 0.5；测试覆盖）

F4 HitRate    hitrate = mean(1[close_t > close_{t-1}], N)
```

### 潜在风险

| 风险 | 处理 |
|---|---|
| `close/close.shift(L)` 泄漏 | 全部为 `rolling`/正 `shift`；`test_factors_are_invariant_to_future` 用垃圾数据污染未来后因子逐位不变 |
| 波动率为 0 导致除零 | `vol==0 -> NaN`，不做无穷大 |
| 区间退化为点 | 分母 <= 0 置 NaN |
| 未上市期间出现数值 | 价格 NaN 传播，因子为 NaN |

### 验证方法

对每个因子用**手算期望值**比对（动量除以年化波动率、flow 的短长均值比、
range_pos 在单调序列上趋近 0/1、hitrate 等于上涨 bar 占比、ADV 等于滚动均值、ATR% 等于 4%）。

---

## 4. Phase 3 — Cross-sectional Score

**代码**：`signals/cross_section.py`　**测试**：`tests/test_portfolio.py` 中的 cross-section 部分

```
标准化（仅在 pool_t 内部，且只用当根截面）：
  MAD 去极值:  med ± k·1.4826·MAD
  或百分位截断: [q_p, q_{1-p}]
  z = (x - mean_pool) / std_pool          再硬截断到 ±4

综合分：
  score = Σ_k w_k · z_k ,   w 归一化到 Σw = 1
  默认 A: 1.00/0.40/0.30/0.20 -> 0.526/0.211/0.158/0.105
```

> 权重被归一化是一个**显式设计选择**：这样 `min_abs_score` 始终是「z 单位」，
> 不同权重方案之间可直接比较。原始权重比 0.4 不等于归一化后的 0.211，
> 因此报告里同时给出两套数字。

### 潜在风险

| 风险 | 处理 |
|---|---|
| 用全样本均值/方差标准化 | 每次 rebalance 现算，只吃当前 pool |
| 单个 20σ 异常值主导排序 | MAD 去极值 + ±4 硬截断 |
| 缺失因子被当成 0 | 只在非 NaN 因子上加权平均，权重和为 1 |
| pool 太小导致 z 无意义 | `n < 5` 直接返回 NaN |

---

## 5. Phase 4–6 — Portfolio / Volatility Weight / Beta Neutral

**代码**：`portfolio/construct.py`、`portfolio/beta_neutral.py`
**测试**：`tests/test_portfolio.py`（20 个，含权重 sizing 退化与启用边界的回归）

```
选名：long = score 最高的 K 个（且 score >= min_abs_score）
      short = score 最低的 K 个（且 score <= -min_abs_score）
      hold_rank_buffer: 在位名次仍在 K+buffer 内则保留
      n_drop: 单次最多替换 n_drop 个在位名

权重：w_i ∝ |score_i|^sp · (1/vol_i)^vp       每侧归一化到 Σw = 1
      单名上限 cap: 注水法迭代封顶后重归一化
      cap·K <= 1 时不可行 -> 退化为等权（不返回未归一化向量）

Beta 中性：
      A 法  g_long = g_short = G/2                    （毛敞口对齐）
      B 法  k = clip(β_u / β_v, 0.5, 2)               （β_u, β_v 为单位侧加权 β）
            g_long = G/(1+k),  g_short = G·k/(1+k)
            => g_long·β_u - g_short·β_v = 0  且总毛敞口恒为 G
```

> ⚠️ **默认组合恰好落在权重的退化边界上（重要）**
>
> 退化判据是 `cap · K <= 1.0 + 1e-12`，而**规格默认 `top_k = 10` + `cap = 0.10`
> 正好等于 1.0** → 默认配置下**根本不会执行 `|score|/vol` 加权，实际跑的是等权**。
> 实测：`K=10, cap=0.10` 时每侧权重恒为 `0.1`（不同取值个数 = 1）；且与显式
> `vol_weight_power=0, score_weight_power=0` 的结果**逐字节相同**。
>
> 要让 sizing 真正生效，需 `cap > 1/K`（K=10 时取 0.20）。启用后
> Sharpe 1.279 → **1.378**、CAGR 14.36% → **16.65%**，代价是 MDD −9.49% → −12.25%，
> 且 cap 0.15/0.20/0.40 非单调（1.239 / 1.378 / 1.317）——
> **取 0.20 是「启用该机制」，不是「调到最优值」。**
>
> 回归测试：`test_boundary_cap_equals_one_over_k_also_degrades_to_equal_weight`、
> `test_cap_above_one_over_k_activates_real_tilting`。

### 输入输出示例

```python
sel = select_book(score, mask, cfg.portfolio, prev_long, prev_short)   # 快照式
u_long, u_short = build_units(score, vol_row, sel, cfg.portfolio)
g_long, g_short = side_gross_targets(beta_u, beta_v, 1.0, cfg.portfolio, "B_beta_neutral")
```

### 潜在风险

| 风险 | 处理 |
|---|---|
| 单名上限不可行导致敞口塌陷 | 显式退化为等权，并加了对应测试 |
| 负数/NaN 波动率造出负权重 | 用 `|score|` 与 `1/vol` 的正量构造；NaN 用截面中位数填补 |
| 「等毛敞口」被当成市场中性 | 已实现并对比：报告显示 A 法残留 `|β|≈0.073`，B 法 `≈0.009` |
| β 估计用了未来 | 滚动窗口 `cov(r_i, r_btc)/var(r_btc)`，仅历史 |
| 换手失控 | `hold_rank_buffer` / `n_drop` 直接限制替换数 |

### 验证方法

对合成数据断言：B 法组合 β 精确为 0（`atol=1e-9`）、总毛敞口守恒、
A 法在 β 异质时残留显著 β、上限约束生效、`n_drop` 确实限制替换数。

---

## 6. Phase 7 — Risk Engine

**代码**：`risk/engine.py`　**测试**：`tests/test_execution_costs.py`（risk 部分，9 个）

```
总缩放 = regime × btc_vol × vol_target × drawdown

regime      : BTC 20d 动量 < 0        -> ×0.55
btc_vol     : BTC 年化波动在 [soft, hard] 线性降档到 min_scale
vol_target  : clip(target_vol / realized_vol, 0.3, 2.0)
              realized_vol 用**未加杠杆的基础组合**收益估计，避免自指不稳定
drawdown    : <10% ×1.0 | 10-15% ×0.75 | 15-20% ×0.5 | >20% ×0.25
              并置 stop_new_entries 标志（flatten 会永久锁死，故下限 > 0）

单币约束：
  liquidation: 3·ATR% <= 1/L_in_force - mmr     （默认 5x -> 阈值 ATR% = 6.5%）
  ADV 参与率 : |Δw_i|·equity <= 5% · ADV_i ，按名字逐项截断
  换手预算   : Σ|Δw| 受 max_daily_turnover · gross 约束，比例式部分调仓
```

### 潜在风险

| 风险 | 处理 |
|---|---|
| 回撤止损在谷底把仓位永久清零 | 下限 0.25，且只禁「新开仓」不禁「减仓」 |
| realized_vol 自指导致震荡 | 用未缩放基础组合的收益序列估计 |
| 风控用到未来权益 | 所有缩放都在决策 bar 用当时的 equity/drawdown |
| 未知流动性被当成无限 | ADV 为 NaN 时预算置 0 |

---

## 7. Phase 8–9 — Execution / Funding / Fee / Slippage

**代码**：`backtest/costs.py`、`backtest/engine.py::pick_exec_price`
**测试**：`tests/test_execution_costs.py`

```
成交价：next_open | next_vwap(=amount/volCcy) | next_twap(=(h+l+c)/3) | next_close

单边成本率 = fee + half_spread + impact
  fee        = (1-p)·taker + p·maker        默认 p=0（全 taker）
  half_spread= 0.6bps(ADV >= $25M) 否则 3bps      —— 按**滚动** ADV 分档
  impact     = 0.6 · daily_vol · sqrt(participation)
  participation = |Δnotional| / ADV

资金费率约定：结算费率 > 0 时**多头付给空头**
  funding_i = −rate_i × net_exposure_i      ← 即 bars["funding"]，本身**已含符号**（正 = 收入）
```

**账本恒等式（每一根 bar 都必须成立，50,256 根上最大残差 4.3e−19）：**

```
net_ret = gross_ret − (fee + spread + impact) + funding
```

> **资金费是现金流，不是成本 —— 这里是加号。** 这是本项目踩过的最重的一个坑：
> 早期版本写成 `net = gross − cost_total − fund`，而 `bars["funding"]` **本身已经是**
> 符号化现金流（正=收入，`fund == −rate × net_exposure`），于是减号造成**二次取负**，
> 每一笔资金费支出都被记成了收入。
> 判据很简单：**改符号前先问自己"这是成本还是现金流"**。
> 同一约束适用于 `analysis/metrics.py::bars_variant`：零手续费场景仍要结算资金费，
> 所以 `no_trading_cost = gross_ret + funding`（不是减）。
> 现在由 `tests/test_funding_sign_convention.py` 钉死（正费率 → 多头**付**钱、
> ×2 时毛收益不变），并由账本恒等式测试在 50,256 根 bar 上逐 bar 校验。
>
> 连带规则：往 `cost_total` 里加钱**必须同步写 fee / spread / impact 明细列**，
> 否则费用审计会对不上账。

没有任何「往返成本常数」。entry / exit 各自按当时的 ADV、波动率、参与率计价。

> **报告口径陷阱（同源）**：`Trading Cost Drag` 是**手续费+价差+冲击**；
> 而 `Cost Drag` 是 **(交易成本 − 资金费收入)/年**，即"毛→净的总拖累"。
> 以 `v3` 为例：交易成本 **2.068%/yr**，资金费收入抵掉 17.4%，净拖累 **1.709%/yr**。
> 混用会**低估手续费规模约 21%**。

### 资金费数据来源（重要，且必须诚实披露）

OKX 公开接口 `/api/v5/public/funding-rate-history` **只保留约 3 个月**的结算记录。
实测（BTC-USDT-SWAP）：最早可回溯到 2026-06-22，再往前翻页返回空。本回测窗口是
2021-01-01 起，所以**纯 OKX 资金费在 95% 的样本上等于 0** —— 这会静默粉饰一个
市场中性策略的真实成本，因此不能接受。

按项目数据源约定（OKX 优先，OKX 取不到的才用其他来源），用币安 `fapi/v1/fundingRate`
补齐 OKX 不保留的时段（币安返回自上市以来的完整历史）。

| 处理 | 做法 |
|---|---|
| 拼接 | 以**每个合约自己的**首个 OKX 结算点为界：之前用币安，之后用 OKX 实测 |
| 周期差异 | 币安已把多数合约从 8h 改为 4h/1h。结算费率是「每周期费率」，故把每个 8h 窗口内的币安费率**求和**，得到等价的 8h charge，两个来源量纲一致 |
| 未取到币安历史 | 用同一时刻**已成功拼接合约的中位数**填充，在 `funding_coverage.csv` 中标注为 `okx+median_proxy` |

**覆盖率真相（已更新到最终口径，勿用旧值）**：

| 指标 | 首轮 | **最终（`--only-proxy --retries 12` 续跑后）** |
|---|---|---|
| OKX 合约数 | 161 | 161 |
| 币安**符号**找到 | 126 | 126 |
| 币安**历史真正取到**（`okx+binance`，`real_splice`） | 38 | **126 / 161** |
| 仅中位数代理（`okx+median_proxy`） | 123 | **35** |
| **修复效果** | 资金费 P&L **−1.11%**（净支出） | 资金费 P&L **+2.06%**（**净收取**） |

> **口径陷阱（第一次修的）**：元数据曾把「找到币安符号」（126）当成「取到币安历史」，
> 于是把只有代理值的合约错标成 `okx+binance`。
> 判据必须是「历史是否**真的取到**」，即 `real_splice` 集合，而不是 `inst in matched`。
>
> **第二次才是根治**：首轮 126 个"币安有符号但没取到历史"的合约里有 **88 个**不是不存在，
> 而是**被币安限流打掉**（418/429）。带 `--only-proxy --retries 12` 续跑后 38 → **126**。
> 教训：**覆盖率跌到可疑地步时，先怀疑"抓取失败"再怀疑"数据源缺失"。**

**代理误差诊断**（在 38 个有重合窗口的合约上实测，`funding_proxy_diag.csv`）：

| 指标 | 值 |
|---|---|
| 重合区间样本数 | 38 个合约 |
| 相关系数（均值 / 中位数 / 10 分位） | 0.63 / 0.67 / 0.32 |
| 水平差（币安 − OKX） | −0.52 bps / 每 8h 结算 ≈ −0.57% / 年 |
| 拼接后中位年化资金费 | 12.4% / 年 |

**影响边界**：

- 上面这张代理误差表**仍然有效且不可消除** —— OKX 与币安同一合约同一时刻的资金费
  **相关性只有 0.63（p10 仅 0.32）**。这是两个交易所各自的资金费机制不同造成的，重跑也修不好。
- ⚠️ 旧文案里「代理名字占**换手的 61.5%**、净 PnL 的 63.9%」是**首轮 38/161 覆盖率下的口径，
  已过期作废。** 覆盖修到 126/161 之后只剩 **35 个代理合约**，剩余影响量级为 ±0.1–0.2%/年。
- 资金费无论如何只影响**资金费项**，**不影响价格 P&L**（alpha 的载体）与交易成本。
- `v3` 下资金费是**净收取 +2.06%**（占总收益约 1.4%），抵掉 17.4% 的交易成本；
  敏感性阶梯 ×0 / ×1 / ×1.5 / ×2 → Sharpe 0.588–0.703（非单调）
  → **资金费假设不是结论的一阶驱动项**。

结论：相关性 0.63 属**中等**、水平偏差小（−0.57%/年），但它是**不可消除**的误差，只能披露不能修。
**剩余改进路径**：把最后 35 个代理补成真值（预期影响 ±0.1–0.2%/年），优先级低于退市数据。

复现命令：

```bash
python -m crypto_ls_research.data.funding_build     # 生成 data_cache/funding_hyb/
FUNDING_DIR=funding python -m ...                   # 强制使用纯 OKX（仅 3 个月）
```

`data/store.py` 读取 `FUNDING_DIR`（默认 `funding_hyb`）。目录缺失时**不会静默降级**：
会打印 warning 并回落到纯 OKX，避免「资金费=0」被误当成真实结果。

### 潜在风险

| 风险 | 处理 |
|---|---|
| 用全样本 ADV 分档 = 未来函数 | 用决策 bar 的滚动 ADV |
| 用收益指数当美元本金算参与率 | 已修：`eq_dollars = initial_capital × equity_index` |
| 持仓中数据消失被静默忽略 | 缺口期按最后价格平仓（收益记 0）、收 taker+最宽价差、计入 `n_stale` |
| 资金费符号写反 | **三层防守**：① `tests/test_funding_sign_convention.py` 断言「正费率→多头付」且 ×2 时毛收益不变；② 逐 bar 账本恒等式 `net == gross − 成本 + funding` 在 50,256 根 bar 上最大残差 4.3e−19；③ `bars_variant` 的 `no_trading_cost = gross + funding` 也锁了测试 |
| OKX 只保留 3 个月资金费 | 币安补齐（真拼接已达 126/161）+ 量化不可消除的代理误差 0.63 相关性，并提供 ×0/×1/×1.5/×2 敏感性阶梯 |
| 覆盖率被误判为 100% | 判据是 `real_splice`（历史真的取到），不是「找到符号」；且限流失败 ≠ 数据缺失，用 `--only-proxy --retries 12` 续跑 |
| 分析阶段误用「位置对齐」 | `ic.align_panels()` 按**合约名**对齐，列名重复或缺名都直接报错（见 §13） |

---

## 8. Phase 10 — Backtest

**代码**：`backtest/engine.py::run_backtest`　**输出**：`BacktestResult`

单层 Python 循环（每 bar 一次小向量运算），所有路径依赖控制（回撤阶梯、
波动率目标、换手预算、ADV 截断、数据消失）都在循环内实时计算。
为保证扫描可并行，因子面板在采样到决策 bar 后立即释放；
面板以 float32 存储（相对精度 ~1e-7，远细于可交易粒度，但省一半内存）。

返回对象包含：
- `bars`：逐 bar 的净/毛/多头/空头收益、费、价差、冲击、资金费、换手、
  毛/净/β 敞口、多空持仓数、风控缩放、回撤、最大参与率、stale 计数
- `weight_matrix (D×N)`、`name_gross (D×N)`、`name_cost`、`name_turnover` → 单币归因
- `score_matrix`、`mask_matrix`、`factor_zs`、`fc_raw`、`beta_matrix`、`adv_matrix`
  → 免重跑即可做 IC / 归因

---

## 9. Phase 11 — Attribution

**代码**：`analysis/metrics.py`、`analysis/attribution.py`、`analysis/ic.py`

- **成本情景分解**（最关键的一张表）：同一条持仓路径按 4 种成本假设估值
  gross / 零交易成本 / 零资金费 / net
- **多空归因**：多腿、空腿、净头寸分别给全套指标
- **因子消融**：单因子、两两、留一、四因子全开（15 个组合）× 毛/净两口径
- **Rank IC**：`Spearman(score_d, r_{d→d+h})`，采用**可执行口径**
  （从 bar d+1 的成交价起算），并按 1h/4h/12h/1d/3d/7d 给 IC 衰减
- **单币归因 / 集中度**：Top1/Top5 占比、HHI、剔除 BTC+ETH 后的剩余收益
- **市场状态归因**：牛/熊/震荡 × 高/低波动 × BTC 强/弱趋势 × 山寨季
  （涨跌均只用滚动窗口计算；「BTC 主导率」用「BTC 30d − 山寨中位数 30d」代理）

---

## 10. Phase 12–13 — Walk-forward & Monte Carlo

**代码**：`analysis/walkforward.py`、`analysis/montecarlo.py`

- **定参稳定性**：同一套参数在 2021…2026H2 各段的独立表现
- **选参 walk-forward**：train 上挑最优参数 → 在未见过的 test 上评估，
  报告 `oos_decay = train_sharpe - test_sharpe`（过拟合税）
- **H1 截面置换**：每个 rebalance 在池内打乱真实 score（分布与风控完全一致，只毁掉信息）
- **H2 随机打分**：i.i.d. 高斯分数
- **H3 区块置换代理价格**：全局置换 1 日区块的对数收益并重新积分价格，
  保留截面相关结构与量价配对，只摧毁序列依赖 —— 最强也最贵的零假设
- 置换 p 值：`p = (1 + #{placebo >= real}) / (1 + n)`

---

## 11. Phase 14–15 — Paper / Live Trading

**当前状态：执行层已实现（`crypto_ls_research/execution/`），三模式 paper/demo/live。**

执行链：`signal.py`（跑 `run_backtest` 取最后调仓目标，**不重算信号**）→
`planner.py`（量化张数）→ `limits.py`（五道风控闸门）→ `engine.py` →
`store.py`。已实现的能力：

- **OKX 私有 API**（`okx_private.py`）：v5 签名、批量下单（`/trade/batch-orders`）、
  下单不盲重试（抛 `AmbiguousError` 要求对账）、确定性 `clOrdId`。
- **订单状态机与部分成交**：`reconcile()` 拉 `order_by_clordid` 更新状态 +
  `fills` 按 `tradeId` 去重。
- **跨进程 `rebalance_lock`**（PID + TTL）：防「双 actor 各发一次完整订单 → 仓位翻倍」。
- **目标书幂等**：`build_plan(current_sz → target_weights)` 算差量。
- **换手预算对齐回测**：`turnover_budget` 不设会跑到 2.2× 已验证敞口。
- **凭证管理**（`credentials.py`）：demo/live 分槽、env > file、脱敏、原子写、
  kill switch（文件实现 fail closed）。
- **`BacktestResult.weight_matrix`** 就是每日目标仓位快照（可执行尺寸）；
  `risk/engine.py` 的函数都是无状态纯函数，可直接复用到实时风控。

上线前仍须补齐的**非代码**项：成交假设的真实校准（薄池半价差 3bps 是分档常数
不是实测报价）、实盘资金费进本地归因、影子盘（只记不下）先跑 4–8 周。

---

## 12. 隐性偏差审计

`analysis/bias.py` 会输出一张审计表，逐项给出状态、证据与偏差方向。其中一条是
**FAIL - UNREMOVABLE**：

> OKX 公共 API 对已下架合约返回 `51001`（FTT / SRM / ANC / CVC / TON 实测均不可得），
> 因此样本天然只包含幸存者。方向上这会**高估多头腿**——被下架的往往正是
> 先暴涨、后崩盘的动量龙头。缓解手段是退市冲击压力测试：按每年约 6% 的退市率
> 注入「单根 -50%~-90% 后停止报价」，重跑并报告 Sharpe 分布。

---

## 13. 已知简化与边界

| 项目 | 现状 | 影响 |
|---|---|---|
| 退市合约 | 数据源层面不可得 | 多头收益被高估，已量化但不可消除 |
| **候选池由「当前」24h 成交量选出** | top-120 按今日成交量 + 2022 前上市的全部合约 | 池子本身带有一层「活到今天」的选择偏差。缓解：64 个早期上市合约不看成交量一律纳入；PIT 门槛只按历史上每根 bar 的流动性排名取 Top-100。但「2022 年后上市且在今日之前退市」的合约仍然不可见 |
| **2021–2026 资金费部分为币安拼接** | OKX 只留 3 个月；**38/162 真正拼到币安历史，124 个用中位数代理**（代理名字占换手 61.5%） | 只在能重合的 38 个上实测代理误差：相关系数 0.63、水平差 −0.57%/年。故必须看 ×0/×1/×1.5/×2 阶梯（Sharpe 0.588–0.703），不能把 ×1 当精确值 |
| 订单簿/真实价差 | 用 ADV 分档 + 平方根冲击代理 | 高流动性品种上偏乐观，冷门品种偏保守 |
| 撮合与部分成交 | 假设按 bar 价格全额成交，受 ADV 参与率上限约束 | 大资金下真实成交可能更差 |
| 资金费结算时刻 | 归入其所在的 bar | 对 8h 结算周期为一根 bar 的近似 |
| Maker/Taker | 默认全 taker（`passive_fill_ratio=0`） | 保守 |
| BTC 主导率 | 用「BTC 相对山寨中位数」代理 | 无链上/市值权重数据 |
| **标的池含非加密合约** | 161 个合约中有 **34 个是代币化股票/商品/ETF**（NVDA、TSLA、GOOGL、META、MSTR、MU、AMD、QQQ、SOXL、XAU、XAG、CL、BZ…）；实际被交易的有 **10 个** | 占总成本 **4.10%**、换手 4.21%、净 PnL **0.04%**（净贡献≈0）。剔除约可降低 **0.37pp/年**成本拖累。这些合约在 OKX 上同样 24/7 交易（实测周末 NaN=0），故不产生数据缺口，但**改变了策略的定义与执行假设**，应视为标的定义选择 |

### 分析阶段的合约轴对齐（已加防护）

回测结果对象里的每合约矩阵（`score_matrix` / `weight_matrix` / `name_gross`…）宽度
等于**当时**缓存里的合约数。缓存可能在阶段之间变大（例如某个下载在运行中途结束并
补上了之前失败的合约），此时新加载的面板比结果宽，任何按**位置**使用矩阵的代码都会
静默错位（或直接 broadcast 报错）。

`analysis/ic.py::align_panels()` 现在按**合约名**对齐，并且：

- 缺名 → 直接抛错，提示「缓存已变化，请重跑 base 阶段」
- 面板列名重复 → 抛错（`df[list]` 在重名列上会返回多于请求数的列，反而重新引入错位）
- 对齐后宽度与请求数不符 → 抛错

回归测试见 `tests/test_panel_alignment.py`（6 个用例，含「面板变宽」与「列序打乱」
两种场景下 IC 必须完全不变）。

### 成本口径

`bars["funding"]` 是资金费**现金流**（`-Σ w·rate`），**正数 = 收入**。美元中性的组合
大部分会互相抵消，所以它常常是小额**收益**。因此指标输出把它单列为
`Funding P&L (total, frac)`，不再叫 "Funding Cost"，也不再加进成本拖累；成本拖累
= `(交易成本 − 资金费收入) / 年数`。回归测试：
`test_metrics_do_not_report_funding_income_as_a_cost`。

成本情景表遵守同一个约定：`no_trading_cost = gross_ret + funding`（零手续费场地
**仍然要结算资金费**），`no_funding = gross_ret − 交易成本`，因此
`net − no_funding` 恒等于 `bars["funding"]`。
