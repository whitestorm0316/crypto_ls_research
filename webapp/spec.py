"""Parameter specification for the interactive web console.

Single source of truth for:
  * which config fields the UI exposes, and how to render each one,
  * the **verified-optimal defaults** (the accepted revision, see `OPTIMAL_TAG`),
  * the list of configurations this project has already *falsified*, so the
    console can warn before the user spends 40 minutes re-running a known
    dead end.

The trap table is not decoration.  Every entry below cost a real experiment;
the console exists partly so nobody has to pay for it twice.

This module owns the *presentation* of the accepted configuration, not the
numbers in it: the grid comes from `config.settings.ACCEPTED_REBALANCE_DAYS`,
which is also what `execution.engine.DEFAULT_SIGNAL` reads.  A literal here is
how the console and the desk end up describing two different strategies.
"""
from __future__ import annotations

# The accepted rebalance grid and factor set.  Imported, never re-typed:
# `execution.engine` reads the same two constants, and the whole point is that
# the desk's plan and the console's "已验收最优" cannot drift apart.  (The grid
# did: the desk planned on 3 days while the daemon traded daily, and nothing
# compared the two.  The factor set had the same exposure with no guard at all --
# see `tests/test_accepted_factor_set.py`.)
from crypto_ls_research.config.settings import (ACCEPTED_FACTORS,
                                                ACCEPTED_REBALANCE_DAYS)

# ---------------------------------------------------------------------------
# the verified optimum (artifacts/OPTIMIZATION_RESULTS.md, tag `v5_1d_all5`)
# ---------------------------------------------------------------------------
#: The accepted revision's artifact namespace.  `v5_1d_all5` is the **five-factor
#: set on the 1-day grid** -- the grid the unattended daemon trades and the
#: factor set it now trades too, so the acceptance describes the configuration
#: that is actually running.  It cleared 16/16 gates, and it is the **first tag
#: on disk whose acceptance includes the three placebo nulls**: every earlier run
#: was launched without `--stages mc`, so those criteria were silently absent
#: from the count (`tests/test_acceptance_completeness.py`).
#:
#: Older bases stay on disk as records and remain comparable; they are simply no
#: longer the basis:
#:   * `v4_1d` -- two factors (`range_pos + hitrate`) on the 1-day grid,
#:   * `v3`    -- two factors on the 3-day grid.
#: See `artifacts/FINDINGS.md` Q22 (the grid move), Q32/Q33 (the factor move).
OPTIMAL_TAG = "v5_1d_all5"
OPTIMAL_CLI = {
    "bar": "1h",
    "rebalance_days": ACCEPTED_REBALANCE_DAYS,
    "asset_class": "crypto",
}
OPTIMAL_OVERRIDES = {
    "factors.subset": list(ACCEPTED_FACTORS),
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}

# The headline recorded when the accepted revision was measured.  It is a
# **historical record**, not the current truth: the console must report what the
# artifacts on disk say, so `server.py::_headline` overrides every field it can
# measure from `artifacts/<OPTIMAL_TAG>/tables/01_headline_metrics.json` and keeps
# this dict only as the fallback for fields with no artifact (and for when the tag
# has not been re-run).
#
# This distinction is not pedantry.  After the market data was rebuilt the 3-day
# configuration measured Sharpe 1.758 instead of the recorded 1.937, so shipping a
# recorded dict as "the" headline made the page claim both numbers at once.
#
# 2026-09-29 (a): the acceptance basis moved from the 3-day grid (`v3`) to the
# 1-day grid (`v4_1d`) -- the one the daemon actually trades.
# 2026-09-29 (b): the basis moved again, from the pruned two factors to all five
# (`v5_1d_all5`).  The record below is that run.
#
# The two earlier bases, for reference (same dataset):
#   * 1 day, two factors (`v4_1d`): Sharpe 1.821 / CAGR 32.53% / MDD −16.79%
#     / 年换手 70.23 / 成本拖累 6.05%.
#   * 3 days, two factors (`v3`): Sharpe 1.758 / CAGR 21.24% / MDD −7.57%
#     / 年换手 23.90.
# **Both numbers belong in any citation.**  The 1-day grid buys CAGR and Sharpe
# and pays 2.22x the drawdown and 2.94x the turnover; the five-factor set buys
# further on the same grid but **loses the locked 2026 slice** (2.172 vs 2.740)
# and the two most recent calendar years -- see FINDINGS Q33 before quoting any
# of this as an improvement.
OPTIMAL_HEADLINE = {
    "sharpe": 1.937163,
    "cagr": 0.391165,
    "mdd": -0.133149,
    "dd_days": 155.83,
    "cost_drag": 0.047710,
    # `funding` is the field where the sign question matters most, so it is spelled
    # out.  On this run the book measured **+0.091827** -- but the level is not
    # comparable to the 1-day two-factor run's +0.0095, because the *same* table
    # says the sign is decided by where the sample ends (2021-2024 = +0.1458
    # credit, 2025-now = −0.0540 cost) and net/gross is 0.263.  The roadmap
    # expected a net receiver; `optimize_report.acceptance_checks` reports that as
    # a pass/fail gate and discloses the instability in two `pass=None`
    # informational rows next to it.  So **do not quote this number as a property
    # of the strategy**, and do not "fix" the acceptance by inverting the
    # criterion -- see `tests/test_acceptance_funding.py`.
    "funding": 0.091827,
    "ann_turnover": 70.297,
    "acceptance": "16/16 通过",
}

# Suffix for every Sharpe comparison below that was measured on the v3 acceptance
# dataset.  Those before/after pairs are experiment *records* and belong together;
# they must not be read as current numbers (see OPTIMAL_HEADLINE above).
RECORDED_NOTE = "（历史记录值，非当前产物）"

FACTOR_NAMES = ["momentum", "flow", "range_pos", "hitrate", "rev_short"]
FACTOR_LABEL = {
    "momentum": "momentum（动量，{d} 日）",
    "flow": "flow（流动性流向）",
    "range_pos": "range_pos（区间位置）",
    "hitrate": "hitrate（胜率）",
    "rev_short": "rev_short（短周期反转，{d} 日）",
}

# ---------------------------------------------------------------------------
# the parameter schema
# ---------------------------------------------------------------------------
# level: "core"  -> shown by default
#        "adv"   -> hidden behind the 高级 collapsible
SPEC: dict = {
    "groups": [
        {
            "id": "factors",
            "label": "因子",
            "desc": "决定横截面打分用什么信号。这是全项目收益最大的一项："
                    "在简报的 4 因子上**加** rev_short 值 +0.076 Sharpe，"
                    "而 4 因子本身又优于把因子剪到只剩 2 个的旧口径。"
                    "注意这一项的排序在换网格时会反转（见 FINDINGS Q32）。",
            "items": [
                {"key": "factors.subset", "label": "因子子集", "type": "multi",
                 "options": FACTOR_NAMES, "level": "core",
                 "default": OPTIMAL_OVERRIDES["factors.subset"],
                 # Derived, not typed: this sentence named the old optimum by hand
                 # and would have kept saying `range_pos + hitrate` after the basis
                 # moved to all five.  A help string is a number like any other.
                 "help": "复合打分实际使用的因子。被选中的因子按 profile 权重内部重新归一化。"
                         "★ 实测最优 = " + " + ".join(OPTIMAL_OVERRIDES["factors.subset"])
                         + "。"},
                {"key": "factors.neutralize", "label": "因子中性化", "type": "bool",
                 "default": False, "level": "core",
                 "help": "让每个因子只保留与其余因子正交的部分。"
                         "研究简报 §38 要求的一步——实测为破坏性的，见否决清单。"},
                {"key": "factors.neutralize_rescale", "label": "中性化后重新标准化",
                 "type": "bool", "default": True, "level": "adv",
                 "help": "把残差重新拉回横截面单位方差，使 profile 权重含义不变。"},
                {"key": "factors.winsor_method", "label": "极值处理", "type": "select",
                 "options": ["mad", "percentile", "none"], "default": "mad",
                 "level": "core",
                 "help": "横截面 z-score 之前的缩尾方式。mad = 中位绝对偏差。"},
                {"key": "factors.mad_k", "label": "MAD 倍数 k", "type": "number",
                 "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5, "level": "adv"},
                {"key": "factors.pct_clip", "label": "分位数裁剪", "type": "number",
                 "default": 0.02, "min": 0.0, "max": 0.2, "step": 0.005, "level": "adv",
                 "help": "仅在「极值处理 = percentile」时生效。"},
                {"key": "factors.mom_lookback_days", "label": "momentum 回看（日）",
                 "type": "number", "default": 5.25, "min": 0.25, "max": 60.0,
                 "step": 0.25, "level": "core"},
                {"key": "factors.mom_vol_days", "label": "momentum 波动归一（日）",
                 "type": "number", "default": 2.0, "min": 0.25, "max": 30.0,
                 "step": 0.25, "level": "adv"},
                {"key": "factors.flow_short_days", "label": "flow 短窗（日）",
                 "type": "number", "default": 1.0, "min": 0.25, "max": 10.0,
                 "step": 0.25, "level": "core"},
                {"key": "factors.flow_long_days", "label": "flow 长窗（日）",
                 "type": "number", "default": 30.0, "min": 1.0, "max": 120.0,
                 "step": 1.0, "level": "core"},
                {"key": "factors.range_days", "label": "range_pos 区间（日）",
                 "type": "number", "default": 7.5, "min": 0.5, "max": 60.0,
                 "step": 0.5, "level": "core"},
                {"key": "factors.hitrate_days", "label": "hitrate 回看（日）",
                 "type": "number", "default": 5.25, "min": 0.5, "max": 60.0,
                 "step": 0.25, "level": "core"},
                {"key": "factors.rev_short_days", "label": "rev_short 回看（日）",
                 "type": "number", "default": 1.0, "min": 0.1, "max": 10.0,
                 "step": 0.1, "level": "adv",
                 "help": "负数风险调整收益。仅当子集里含 rev_short 时有意义。"},
                {"key": "factors.default_profile", "label": "复合权重 profile",
                 "type": "select",
                 "options": ["A_MOM_TILT", "B", "C", "D_EQUAL"],
                 "default": "A_MOM_TILT", "level": "core",
                 "help": "各因子的相对权重（选中子集后内部重新归一化）。"},
            ],
        },
        {
            "id": "portfolio",
            "label": "组合构建",
            "desc": "选多少名字、每个名字多重。注意 sizing 在窄池里会退化为等权——"
                    "这在几何上无解，且实测「修它」会亏钱。",
            "items": [
                {"key": "portfolio.top_k", "label": "每侧选币数 top_k", "type": "int",
                 "default": 10, "min": 1, "max": 40, "step": 1, "level": "core",
                 "help": "多头与空头各取打分最高/最低的 K 个名字。"},
                {"key": "portfolio.max_weight_per_instrument",
                 "label": "单名权重上限 cap", "type": "number",
                 "default": 0.20, "min": 0.02, "max": 1.0, "step": 0.01,
                 "level": "core",
                 "help": "占单侧名义本金的比例。★ 实测最优 0.20（规格默认 0.10 会让 "
                         "cap×n 正好等于 1，即静默退化为等权）。"},
                {"key": "portfolio.cap_width_slack", "label": "宽度自适应 cap 松弛",
                 "type": "number", "default": 0.0, "min": 0.0, "max": 2.0,
                 "step": 0.05, "level": "core",
                 "help": "cap_eff = max(cap, (1+slack)/n)，让窄池里 sizing 继续生效。"
                         "看着该采纳，实测被逐折检验否决 → 见否决清单。"},
                {"key": "portfolio.min_abs_score", "label": "打分绝对值下限",
                 "type": "number", "default": 0.0, "min": 0.0, "max": 3.0,
                 "step": 0.05, "level": "core",
                 "help": "低于该打分的名字直接不入池。0 = 关闭。"},
                {"key": "portfolio.vol_weight_power", "label": "波动倒数幂",
                 "type": "number", "default": 1.0, "min": 0.0, "max": 3.0,
                 "step": 0.25, "level": "adv",
                 "help": "权重 ∝ |score|^sp × (1/vol)^vp。为 0 即不按波动缩放。"},
                {"key": "portfolio.score_weight_power", "label": "打分幂",
                 "type": "number", "default": 1.0, "min": 0.0, "max": 3.0,
                 "step": 0.25, "level": "adv"},
                {"key": "portfolio.beta_neutral_mode", "label": "敞口模式", "type": "select",
                 "options": ["B_beta_neutral", "A_gross_matched"],
                 "default": "B_beta_neutral", "level": "core",
                 "help": "B = 按 beta 中性；A = 多空名义本金对齐。"},
                {"key": "portfolio.beta_lookback_days", "label": "beta 回看（日）",
                 "type": "number", "default": 7.0, "min": 1.0, "max": 60.0,
                 "step": 0.5, "level": "adv"},
                {"key": "portfolio.n_drop", "label": "单次最多换手名字数",
                 "type": "int", "default": 999, "min": 1, "max": 999, "step": 1,
                 "level": "adv",
                 "help": "999 = 不限制（换手预算由执行层约束）。"},
                {"key": "portfolio.hold_rank_buffer", "label": "留仓缓冲",
                 "type": "int", "default": 0, "min": 0, "max": 10, "step": 1,
                 "level": "adv"},
            ],
        },
        {
            "id": "execution",
            "label": "执行与成本",
            "desc": "成交价口径与成本项。换手预算是唯一真正调出来的参数（+0.201）。",
            "items": [
                {"key": "execution.max_daily_turnover", "label": "每日换手预算",
                 "type": "number", "default": 0.20, "min": 0.02, "max": 2.0,
                 "step": 0.01, "level": "core",
                 "help": "每日最多换掉多少比例的总名义本金。★ 实测最优 0.20"
                         "（规格默认 0.50 太松、0.10 太紧，且 0.10 会抬波动）。"},
                {"key": "execution.exec_price", "label": "成交价口径", "type": "select",
                 "options": ["next_open", "next_vwap", "next_twap", "next_close"],
                 "default": "next_open", "level": "core",
                 "help": "信号 bar 之后哪一档成交。★ next_open 最优；next_close 实测更差。"},
                {"key": "costs.taker_fee", "label": "taker 费率", "type": "number",
                 "default": 0.0005, "min": 0.0, "max": 0.002, "step": 0.00005,
                 "level": "core", "format": "pct"},
                {"key": "costs.maker_fee", "label": "maker 费率", "type": "number",
                 "default": 0.0002, "min": 0.0, "max": 0.002, "step": 0.00005,
                 "level": "adv", "format": "pct"},
                {"key": "costs.passive_fill_ratio", "label": "被动成交比例",
                 "type": "number", "default": 0.0, "min": 0.0, "max": 1.0,
                 "step": 0.05, "level": "adv",
                 "help": "0 = 全部按 taker 计（保守默认）。"},
                {"key": "costs.half_spread_bps_base", "label": "半价差（主流合约, bps）",
                 "type": "number", "default": 0.6, "min": 0.0, "max": 20.0,
                 "step": 0.1, "level": "adv"},
                {"key": "costs.impact_coef", "label": "冲击成本系数", "type": "number",
                 "default": 0.6, "min": 0.0, "max": 3.0, "step": 0.1, "level": "adv",
                 "help": "impact = coef × 日波动 × sqrt(参与率)。"},
                {"key": "costs.funding_multiplier", "label": "资金费倍数", "type": "number",
                 "default": 1.0, "min": 0.0, "max": 3.0, "step": 0.25, "level": "core",
                 "help": "1.0 = 真实历史资金费（本项目已修为净收入 +2.06%）。"},
                {"key": "costs.fee_multiplier", "label": "费率压力倍数",
                 "type": "number", "default": 1.0, "min": 1.0, "max": 5.0,
                 "step": 0.25, "level": "adv"},
            ],
        },
        {
            "id": "risk",
            "label": "风控叠加",
            "desc": "全部是无状态纯函数，可直接复用到实时风控。",
            "items": [
                {"key": "risk.target_vol_annual", "label": "目标年化波动",
                 "type": "number", "default": 0.30, "min": 0.05, "max": 1.0,
                 "step": 0.01, "level": "core", "format": "pct"},
                {"key": "risk.min_gross_exposure", "label": "最小总敞口",
                 "type": "number", "default": 0.30, "min": 0.0, "max": 1.0,
                 "step": 0.05, "level": "core"},
                {"key": "risk.max_gross_exposure", "label": "最大总敞口",
                 "type": "number", "default": 2.00, "min": 0.1, "max": 4.0,
                 "step": 0.05, "level": "core"},
                {"key": "risk.btc_vol_cap_enabled", "label": "BTC 波动率上限门控",
                 "type": "bool", "default": True, "level": "core"},
                {"key": "risk.btc_vol_soft", "label": "波动率软阈值",
                 "type": "number", "default": 0.60, "min": 0.1, "max": 3.0,
                 "step": 0.05, "level": "core",
                 "help": "超过此年化波动开始降杠杆。★ 阈值调优只值 +0.07 且一折跑输"
                         "→ 结论是「不要动」，保持默认。"},
                {"key": "risk.btc_vol_hard", "label": "波动率硬阈值",
                 "type": "number", "default": 1.20, "min": 0.2, "max": 5.0,
                 "step": 0.05, "level": "core"},
                {"key": "risk.btc_vol_min_scale", "label": "最低缩放",
                 "type": "number", "default": 0.35, "min": 0.0, "max": 1.0,
                 "step": 0.05, "level": "core"},
                {"key": "risk.btc_vol_window_days", "label": "波动率窗口（日）",
                 "type": "number", "default": 10.0, "min": 1.0, "max": 60.0,
                 "step": 1.0, "level": "adv"},
                {"key": "risk.regime_enabled", "label": "趋势 regime 门控",
                 "type": "bool", "default": True, "level": "adv"},
                {"key": "risk.regime_bear_scale", "label": "熊市缩放",
                 "type": "number", "default": 0.55, "min": 0.0, "max": 1.0,
                 "step": 0.05, "level": "adv"},
                {"key": "risk.max_adv_participation", "label": "单次最大 ADV 参与率",
                 "type": "number", "default": 0.05, "min": 0.001, "max": 0.5,
                 "step": 0.005, "level": "adv",
                 "help": "容量上限的硬约束所在。"},
                {"key": "risk.leverage_in_force", "label": "清算闸门假设杠杆",
                 "type": "number", "default": 5.0, "min": 1.0, "max": 20.0,
                 "step": 0.5, "level": "adv",
                 "help": "闸门按 `3·ATR% ≤ 1/L − mmr` 筛标的时**假设**的杠杆，"
                         "不是账户上限（上限在实盘限额表里）。实盘用 liqPx 反推的真实"
                         "杠杆与它对账。"},
            ],
        },
        {
            "id": "universe",
            "label": "标的池",
            "desc": "PIT 严格按「首个有效 K 线」判定上市，绝不用快照字段。",
            "items": [
                {"key": "universe.min_avg_amount_usd", "label": "30 日成交额门槛（USD）",
                 "type": "number", "default": 3_000_000.0, "min": 100_000.0,
                 "max": 200_000_000.0, "step": 500_000.0, "level": "core",
                 "help": "★ 宁可窄而深：$3M 门槛（池宽 10–22 名字）优于 $1M 宽池。"},
                {"key": "universe.pit_topn", "label": "PIT 流动性排名截断",
                 "type": "int", "default": 100, "min": 10, "max": 200, "step": 5,
                 "level": "core"},
                {"key": "universe.min_history_days", "label": "最短上市历史（日）",
                 "type": "number", "default": 30.0, "min": 1.0, "max": 180.0,
                 "step": 1.0, "level": "core"},
                {"key": "universe.liq_window_days", "label": "流动性均值窗口（日）",
                 "type": "number", "default": 30.0, "min": 1.0, "max": 120.0,
                 "step": 1.0, "level": "adv"},
            ],
        },
    ],
    "global": [
        {"key": "_cli.bar", "label": "K 线频率", "type": "select",
         "options": ["15m", "1h", "4h"], "default": "1h", "level": "core",
         "help": "★ 1h 最优。15m 会让换手成本吃掉全部收益。"},
        {"key": "_cli.rebalance_days", "label": "调仓间隔（日）", "type": "number",
         "default": 1.0, "min": 0.25, "max": 30.0, "step": 0.25, "level": "core",
         "help": "★ 当前验收口径 = **1 天**（`v4_1d`），与守护进程实际跑的网格一致。"
                 "代价必须一起说：1 天档换手约为 3 天的 2.9 倍、回撤约 2.2 倍深。"
                 "3 天（`v3`）的产物仍在盘上，可作对照。"
                 "具体数值以 /api/spec 实算为准，不写死在这里。"},
        {"key": "_cli.asset_class", "label": "池子范围", "type": "select",
         "options": ["crypto", "all"], "default": "crypto", "level": "core",
         "help": "crypto = 剔除 OKX 上代币化股票/ETF 与商品标的。"
                 "★ 增益 +0.285 但 100% 来自 2026，尚未跨期验证。"},
        {"key": "_cli.start", "label": "起始日期", "type": "text",
         "default": "2021-01-01", "level": "core"},
        {"key": "_cli.end", "label": "结束日期", "type": "text",
         "default": "2026-09-26", "level": "core"},
        {"key": "_cli.capital", "label": "初始资金（USD）", "type": "number",
         "default": 100000.0, "min": 10000.0, "max": 100_000_000.0,
         "step": 10000.0, "level": "adv",
         "help": "容量曲线：$10M 时 Sharpe 1.937 → 1.696。" + RECORDED_NOTE},
    ],
}

# ---------------------------------------------------------------------------
# stage bundles — timing measured on this machine (6 cores, --n-jobs 4)
# ---------------------------------------------------------------------------
STAGE_BUNDLES = [
    {"id": "fast", "label": "快速回测", "stages": ["base", "charts"],
     "eta": "约 50 秒", "desc": "只要核心指标与净值曲线。默认。"},
    {"id": "standard", "label": "标准研究", "stages": [
        "base", "exec", "funding", "turnover", "beta", "ic", "wf", "capacity",
        "charts"], "eta": "约 10 分钟",
     "desc": "加执行价对照、资金费敏感性、换手阶梯、beta 中性化、IC、"
             "走前 4 折、容量曲线。"},
    {"id": "full", "label": "完整验收", "stages": [
        "base", "decomp", "exec", "turnover", "funding", "beta", "neutral",
        "multi", "regime", "wf", "ic", "capacity", "bias", "sens", "charts"],
     "eta": "约 25 分钟", "desc": "再加因子中性化对照、多周期反转、regime 网格、"
             "偏差审计、敏感度网格。不含 Monte-Carlo（单跑约 36 分钟）。"},
]

# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
PRESETS = [
    {
        "id": "v3_optimal", "label": "已验收最优 · 1 天网格", "badge": "默认",
        # 这里**不写指标数字**：它们由 server.py 在 /api/spec 里按产物现算后填进来
        # （见 `_presets`）。写死过一次，数据重建后选择器和页头就对不上了。
        # 因子名同理——它曾写死成 `range_pos + hitrate`，口径换到五因子后会继续
        # 说旧的那套。现在从 `OPTIMAL_OVERRIDES` 派生，改口径它自己跟着变。
        # `id` 保持 `v3_optimal` 是因为前端按 id 选中它；验收口径已改为 1 天网格 +
        # 五因子（`OPTIMAL_TAG = v5_1d_all5`），见文件顶部。
        "desc": "因子取 " + " + ".join(OPTIMAL_OVERRIDES["factors.subset"])
                + "、换手预算 20%、单名上限 20%、1h 频率 1 天调仓、crypto 池。",
        "cli": dict(OPTIMAL_CLI), "overrides": dict(OPTIMAL_OVERRIDES),
    },
    {
        "id": "spec_default", "label": "规格默认（对照）",
        "desc": "研究简报的原始参数。实测正好落在局部最差点：Sharpe 0.588。"
                "用来量化「优化到底值多少」。",
        "cli": {"bar": "15m", "rebalance_days": 1.0, "asset_class": "all"},
        "overrides": {
            "factors.subset": ["momentum", "flow", "range_pos", "hitrate"],
            "portfolio.max_weight_per_instrument": 0.10,
            "execution.max_daily_turnover": 0.50,
        },
    },
    {
        "id": "only_range_pos", "label": "只留 range_pos",
        "desc": "把因子子集压到单个最强因子——留一法里贡献最大的一项，"
                "但去掉 hitrate 后实测会掉 Sharpe。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["range_pos"],
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
    {
        "id": "four_factors", "label": "四因子（旧规格默认）",
        "desc": "简报的四个因子，其余取最优。曾经是「剪枝」的对照组，现在反过来"
                "成了**加因子**的对照组：加上 rev_short 值 +0.076 Sharpe。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["momentum", "flow", "range_pos", "hitrate"],
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
    {
        "id": "with_neutralize", "label": "开中性化（预期崩）",
        "desc": f"把否决清单里的一项真的跑一遍，自己看它怎么崩：1.937 → 1.596 {RECORDED_NOTE}。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["range_pos", "hitrate"],
            "factors.neutralize": True,
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
    {
        "id": "add_rev_short", "label": "2 因子 + rev_short（3 因子）",
        "desc": "在旧的 2 因子口径上再加 rev_short。全样本 1 天网格上它**抬高** "
                "Sharpe（1.862→1.937），但 3 天网格排序反转、逐年折 test 均值不占优。"
                "rev_short 现在是验收口径的一部分，这条留作「只加它、不加另外两个」"
                "的对照。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["range_pos", "hitrate", "rev_short"],
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
]

# ---------------------------------------------------------------------------
# falsified configurations — the console warns on these before running
# ---------------------------------------------------------------------------
def check_traps(ov: dict, cli: dict) -> list:
    """Return warnings for configurations this project has already rejected."""
    out = []

    def get(k, d=None):
        return ov.get(k, d)

    subset = get("factors.subset")
    if subset is not None:
        subset = list(subset)
        if get("factors.neutralize") is True:
            out.append({
                "sev": "high", "key": "factors.neutralize",
                "title": "因子中性化已被实测否决",
                "body": "本实现下 v3 从 1.937 掉到 1.596；原四因子 1.567 → 0.217；"
                        "五因子 1.586 → 0.177。横截面只有约 16 个名字，回归掉噪声元"
                        "吃掉的自由度远大于信息量。正解是剪冗余因子，不是正交化。"
                        + RECORDED_NOTE,
            })
        if "rev_short" in subset:
            out.append({
                # `sev` is no longer "high": `rev_short` is now part of the
                # *accepted* configuration (`config.settings.ACCEPTED_FACTORS`), so
                # this entry fires on the console's own default preset.  Greeting
                # the user's optimum with a red 「已被否决」 banner would be a lie
                # about the basis.  The verdict changed shape with the basis; the
                # *evidence* did not change and must not be deleted -- see
                # `tests/test_accepted_factor_set.py`, which pins both halves.
                "sev": "info", "key": "factors.rev_short",
                "title": "反转腿已并入验收口径 —— 稳健性是已知保留项",
                # History of this entry, because it has been wrong twice:
                #   1. it quoted five gross Sharpes (-0.569 / -0.976 / ...) that match
                #      **no artifact on disk** and carried no `RECORDED_NOTE`, while
                #      the neutralisation entry above does carry one.  The numbers are
                #      gone rather than refreshed: the sign plus the artifact/column
                #      name is the traceable form and cannot silently go stale.
                #   2. it argued from the *standalone book* while the user was changing
                #      a *composite component*, where rev_short measurably raises
                #      Sharpe.  A warning whose reason is about a different
                #      construction than the one being changed teaches the user to
                #      ignore the table.
                "body": "**已采纳**，所以这不是「被否决的配置」，而是一条保留项披露。"
                        "**支持**：它自己的 rank IC 在 1–12 根 bar（≤0.5 天）上显著为正"
                        "（t=2.5 / 4.6 / 3.2），到 3 天归零、7 天转负；与另外四个因子的"
                        "相关是 −0.20…−0.49；作为复合分量把全样本 1 天 Sharpe 从 1.862 "
                        "抬到 1.937（`34_neutralisation_study.csv`）。"
                        "**保留**：① 换到 3 天网格排序**反转**（1.762 → 1.581）；"
                        "② 逐年折 test Sharpe 均值不占优（2.029 vs 1.957）而 train 均值"
                        "高得多（1.872 vs 1.503）—— 拟合的形状；"
                        "③ **锁定的 2026 段反而更差**（2.740 → 2.172），逐年里 2024 与 "
                        "2026H1+ 两年退步；"
                        "④ 复合分数在**实际交易的那个 horizon**（1 天）上 IC 掉到不显著"
                        "（+0.0225/t=3.10 → +0.0051/t=0.70）—— 收益改善来自选股与分散，"
                        "不是分数变强。"
                        "**仍然成立的**：作为**独立 book** 它亏钱 —— 5 个 horizon 的毛 "
                        "Sharpe 全为负，book 层混合的 45 个权重格子里最优权重也全是 0.0"
                        "（`at_boundary=true`、`plateau=false`），见 "
                        "`30_reversal_book.csv` 的 `Sharpe_gross` 列与 "
                        "`31b_blend_summary.json`。方向没写反（rev_short = −rev_raw/vol）。"
                        "**零假设检验**（`22_montecarlo_summary.json`，盘上第一份）：三种"
                        "置换 p = 0.0050 / 0.0050 / 0.0323，安慰剂组合平均 Sharpe "
                        "−0.74…−1.13 —— 排序确实带信息，不是运气。"
                        "复现：`scripts/exp_factor_set_grid.py`、"
                        "`scripts/exp_market_vs_strategy.py`。",
            })
        if not subset:
            out.append({"sev": "high", "key": "factors.subset",
                        "title": "因子子集为空", "body": "至少选一个因子。"})

    cap = get("portfolio.max_weight_per_instrument")
    k = get("portfolio.top_k")
    if cap is not None and k is not None and float(cap) * int(k) <= 1.0 + 1e-12:
        out.append({
            "sev": "high", "key": "portfolio.max_weight_per_instrument",
            "title": f"cap×n = {float(cap)*int(k):.2f} ≤ 1 → sizing 会退化为等权",
            "body": f"cap {cap} × top_k {k} 正好铺满单侧本金，"
                    "等权是唯一可行解，|score|/vol 权重完全不会生效。"
                    "本项目曾因此把「等权红利」误读成「窄书红利」。"
                    "把 cap 提到 0.20 以上，或反过来把 top_k 调大。",
        })
    if k is not None and int(k) == 5:
        out.append({
            "sev": "high", "key": "portfolio.top_k",
            "title": "top_k = 5 已被实测否决",
            "body": "K=5 比 K=10 高 +0.105 看着是增益，但 K=5 时 cap×n 正好 = 1.0、"
                    "100% 的调仓都退化成等权——量到的是等权红利 +0.1525，"
                    "不是宽度红利 +0.0580。让 sizing 真正生效后 K=5 掉到 1.889 "
                    "（低于 v3 的 1.937），10 个 arm 全部过不了验收判据。"
                    + RECORDED_NOTE,
        })
    slack = get("portfolio.cap_width_slack")
    if slack is not None and float(slack) > 0:
        out.append({
            "sev": "high", "key": "portfolio.cap_width_slack",
            "title": "宽度自适应 cap 已被逐折检验否决",
            "body": "全样本看着该采纳（slack=1.00 时 1.937 → 1.966），"
                    "但逐年 Δ 里 2026 是 −0.976，15 个季度折只有 47% 为正，"
                    "锁定样本 CAGR 从 75.6% 掉到 44.4%。"
                    "反推出的结论是：在 3–7 个名字的横截面里，等权本身就是风控。"
                    + RECORDED_NOTE,
        })
    mas = get("portfolio.min_abs_score")
    if mas is not None and float(mas) > 0:
        out.append({
            "sev": "mid", "key": "portfolio.min_abs_score",
            "title": "硬阈值型选币参数是孤峰不是平台",
            "body": "实测曲线 0.588 / 0.591 / 1.116 / 0.493 / 0.892 —— 单点凸起。"
                    "池子只有 10–22 个名字，Top-K 几乎不 binding，"
                    "硬阈值就成了唯一的开关，调它等于调机制而不是调参数。",
        })
    ep = get("execution.exec_price")
    if ep == "next_close":
        out.append({"sev": "mid", "key": "execution.exec_price",
                    "title": "next_close 已被实测否决",
                    "body": "与路线图预期反号：1.887 vs next_open 的 1.937。"
                            + RECORDED_NOTE})
    k_open = get("costs.funding_multiplier")
    if k_open == 0:
        out.append({"sev": "mid", "key": "costs.funding_multiplier",
                    "title": "资金费被设为 0 = 关闭这项",
                    "body": "本项目把资金费修成了净收入 +2.06%。设 0 会让结果等于"
                            "「假设历史资金费从来不存在」，不是「零成本」。"})
    soft, hard = get("risk.btc_vol_soft"), get("risk.btc_vol_hard")
    if soft is not None and hard is not None and float(soft) >= float(hard):
        out.append({"sev": "high", "key": "risk.btc_vol_soft",
                    "title": "软阈值必须小于硬阈值", "body": "否则降杠杆曲线无定义。"})

    bar, rd = cli.get("bar"), cli.get("rebalance_days")
    if bar and rd:
        bpd = {"5m": 288, "15m": 96, "1h": 24, "4h": 6, "12h": 2, "1d": 1}[bar]
        reb = max(1, int(round(float(rd) * bpd)))
        if reb < 24:
            out.append({"sev": "mid", "key": "_cli.rebalance_days",
                        "title": f"调仓周期 {reb} 根 bar < 1 天，属于被否决的方向",
                        "body": "IC 形状显示信号很慢（4h IC −0.0227、7d 才 +0.0325），"
                                "1h 调仓的净 Sharpe 只有 0.065。"
                                "「提高调仓频率」在本项目是明确不要做的。"})
    if bar == "15m" and rd and float(rd) <= 1.0:
        out.append({"sev": "mid", "key": "_cli.bar",
                    "title": "15m + 1 天调仓 = 规格默认的亏损区",
                    "body": "这就是 Sharpe 0.588 那一组。想看对照可以跑，"
                            "但别把它当成候选配置。"})
    return out


def build_run_args(ov: dict, cli: dict, stages: list, tag: str, n_jobs: int = 4) -> list:
    """Turn a UI payload into the exact argv of run.research."""
    import json
    args = [
        "-m", "crypto_ls_research.run.research",
        "--tag", tag,
        "--bar", str(cli.get("bar", "1h")),
        # 兜底值取**当前验收口径**（`OPTIMAL_CLI`），不写死数字：验收口径已经换过一次
        # （3 天 → 1 天），写死的 3.0 会在 cli 缺键时静默跑出一个不是口径的网格。
        "--rebalance-days", str(cli.get("rebalance_days", OPTIMAL_CLI["rebalance_days"])),
        "--asset-class", str(cli.get("asset_class", "crypto")),
        "--start", str(cli.get("start", "2021-01-01")),
        "--end", str(cli.get("end", "2026-09-26")),
        "--capital", str(cli.get("capital", 100000.0)),
        "--n-jobs", str(int(n_jobs)),
        "--stages", *stages,
    ]
    for k, v in ov.items():
        args += ["--override", f"{k}={json.dumps(v)}"]
    return args
