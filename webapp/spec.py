"""Parameter specification for the interactive web console.

Single source of truth for:
  * which config fields the UI exposes, and how to render each one,
  * the **verified-optimal defaults** (revision v3),
  * the list of configurations this project has already *falsified*, so the
    console can warn before the user spends 40 minutes re-running a known
    dead end.

The trap table is not decoration.  Every entry below cost a real experiment;
the console exists partly so nobody has to pay for it twice.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# the verified optimum (artifacts/OPTIMIZATION_RESULTS.md, tag `v3`)
# ---------------------------------------------------------------------------
OPTIMAL_TAG = "v3"
OPTIMAL_CLI = {
    "bar": "1h",
    "rebalance_days": 3.0,
    "asset_class": "crypto",
}
OPTIMAL_OVERRIDES = {
    "factors.subset": ["range_pos", "hitrate"],
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}
OPTIMAL_HEADLINE = {
    "sharpe": 1.937327,
    "cagr": 0.250868,
    "mdd": -0.127185,
    "dd_days": 245.92,
    "cost_drag": 0.017090,
    "funding": 0.020574,
    "ann_turnover": 23.887,
    "acceptance": "16/16 通过",
}

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
            "desc": "决定横截面打分用什么信号。这是全项目收益最大的一项——"
                    "把冗余因子剪掉值 +0.371 Sharpe。",
            "items": [
                {"key": "factors.subset", "label": "因子子集", "type": "multi",
                 "options": FACTOR_NAMES, "level": "core",
                 "default": OPTIMAL_OVERRIDES["factors.subset"],
                 "help": "复合打分实际使用的因子。被选中的因子按 profile 权重内部重新归一化。"
                         "★ 实测最优 = range_pos + hitrate。"},
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
                {"key": "risk.max_leverage", "label": "账户最大杠杆",
                 "type": "number", "default": 5.0, "min": 1.0, "max": 20.0,
                 "step": 0.5, "level": "adv"},
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
         "default": 3.0, "min": 0.25, "max": 30.0, "step": 0.25, "level": "core",
         "help": "★ 3 天最优。信号是慢的（4h IC 为负、7d IC 才转正），"
                 "高频调仓必亏。"},
        {"key": "_cli.asset_class", "label": "池子范围", "type": "select",
         "options": ["crypto", "all"], "default": "crypto", "level": "core",
         "help": "crypto = 剔除 OKX 上代币化股票/ETF 与商品（161 → 134）。"
                 "★ 增益 +0.285 但 100% 来自 2026，尚未跨期验证。"},
        {"key": "_cli.start", "label": "起始日期", "type": "text",
         "default": "2021-01-01", "level": "core"},
        {"key": "_cli.end", "label": "结束日期", "type": "text",
         "default": "2026-09-26", "level": "core"},
        {"key": "_cli.capital", "label": "初始资金（USD）", "type": "number",
         "default": 100000.0, "min": 10000.0, "max": 100_000_000.0,
         "step": 10000.0, "level": "adv",
         "help": "容量曲线：$10M 时 Sharpe 1.937 → 1.696。"},
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
        "id": "v3_optimal", "label": "v3 已验收最优", "badge": "默认",
        "desc": "Sharpe 1.937 / CAGR 25.09% / MDD −12.72%，验收 16/16。"
                "因子取 range_pos + hitrate。",
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
        "id": "four_factors", "label": "四因子（不剪枝）",
        "desc": "保留规格的四个因子，其余取最优。用来单独看「剪因子」值多少。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["momentum", "flow", "range_pos", "hitrate"],
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
    {
        "id": "with_neutralize", "label": "开中性化（预期崩）",
        "desc": "把否决清单里的一项真的跑一遍，自己看它怎么崩：1.937 → 1.596。",
        "cli": dict(OPTIMAL_CLI), "overrides": {
            "factors.subset": ["range_pos", "hitrate"],
            "factors.neutralize": True,
            "portfolio.max_weight_per_instrument": 0.20,
            "execution.max_daily_turnover": 0.20},
    },
    {
        "id": "add_rev_short", "label": "加短周期反转",
        "desc": "把 rev_short 放进子集——实测该 book 毛 Sharpe 全为负，"
                "属已否决方向。",
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
                        "吃掉的自由度远大于信息量。正解是剪冗余因子，不是正交化。",
            })
        if "rev_short" in subset:
            out.append({
                "sev": "high", "key": "factors.rev_short",
                "title": "反转腿已被实测否决",
                "body": "5 个 horizon 的独立反转 book 毛 Sharpe 全为负"
                        "（−0.569 / −0.976 / −0.966 / −0.832 / −0.859），"
                        "方向没写反（rev_short = −rev_raw/vol）。与其混合的 45 个权重"
                        "格子里最优权重全是 0.0。注意措辞限定为「本实现下不成立」。",
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
                    "（低于 v3 的 1.937），10 个 arm 全部过不了验收判据。",
        })
    slack = get("portfolio.cap_width_slack")
    if slack is not None and float(slack) > 0:
        out.append({
            "sev": "high", "key": "portfolio.cap_width_slack",
            "title": "宽度自适应 cap 已被逐折检验否决",
            "body": "全样本看着该采纳（slack=1.00 时 1.937 → 1.966），"
                    "但逐年 Δ 里 2026 是 −0.976，15 个季度折只有 47% 为正，"
                    "锁定样本 CAGR 从 75.6% 掉到 44.4%。"
                    "反推出的结论是：在 3–7 个名字的横截面里，等权本身就是风控。",
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
                    "body": "与路线图预期反号：1.887 vs next_open 的 1.937。"})
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
        "--rebalance-days", str(cli.get("rebalance_days", 3.0)),
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
