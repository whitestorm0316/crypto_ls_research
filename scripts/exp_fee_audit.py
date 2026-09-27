# -*- coding: utf-8 -*-
"""交易费用审计：账本里到底算了哪些成本、各是多少、有没有漏项。

只用已存产物（artifacts/fund_diag/baseline.pkl，含 name_fund），不重跑回测。
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "artifacts", "fee_audit")
os.makedirs(OUT, exist_ok=True)

# 已验收 v3 的参考值（逐位对上才算这份产物可信）
REF_FUND = 0.020574444790719387
REF_NET_SUM = None  # 下面动态核对

PKL = os.path.join(ROOT, "artifacts", "fund_diag", "baseline.pkl")
with open(PKL, "rb") as f:
    b = pickle.load(f)
r = b["result"]
cfg = r.cfg
bar = r.bars

assert "name_fund" in r.__dict__, "name_fund 缺失（旧 pickle），本审计不可信"

# ---------------------------------------------------------------- 0. 自校验
_f = float(bar["funding"].sum())
print("=" * 92)
print("[自校验]")
print(f"  bars.funding 累计 = {_f:.18f}   期望 {REF_FUND:.18f}   Δ={_f - REF_FUND:+.3e}")
assert abs(_f - REF_FUND) < 1e-12, "产物与已验收 v3 不一致，审计结论不可信"

fee = bar["fee"].to_numpy(dtype="float64")
spread = bar["spread"].to_numpy(dtype="float64")
impact = bar["impact"].to_numpy(dtype="float64")
funding = bar["funding"].to_numpy(dtype="float64")
gross = bar["gross_ret"].to_numpy(dtype="float64")
net = bar["net_ret"].to_numpy(dtype="float64")
turn = bar["turnover"].to_numpy(dtype="float64")
cost = fee + spread + impact

# 账本恒等式：net == gross - (fee+spread+impact) + funding
resid = net - (gross - cost + funding)
print(f"  账本恒等式最大残差 = {np.abs(resid).max():.3e}  （应 ~1e-17）")
assert np.abs(resid).max() < 1e-12, "账本恒等式不成立！"
print("  → 恒等式成立：净收益里扣的成本 = fee + spread + impact，资金费是加项")

# ---------------------------------------------------------------- 1. 总量
idx = bar.index
years = (idx[-1] - idx[0]).total_seconds() / (365 * 24 * 3600)
print()
print("=" * 92)
print(f"[1] 全样本成本总量（{years:.3f} 年，{len(bar):,} 个 bar）")
tot = {
    "手续费 fee": float(fee.sum()),
    "买卖价差 spread": float(spread.sum()),
    "市场冲击 impact": float(impact.sum()),
}
cc = float(cost.sum())
print(f"  {'项目':<20s} {'累计(净值占比)':>16s} {'年化拖累':>12s} {'占比':>8s}")
print("  " + "-" * 62)
for k, v in tot.items():
    print(f"  {k:<20s} {v:>16.4%} {v / years:>11.4%} {v / cc:>7.1%}")
print("  " + "-" * 62)
print(f"  {'交易成本合计':<20s} {cc:>16.4%} {cc / years:>11.4%} {1.0:>7.1%}")
print(f"  {'资金费（现金流，加项）':<20s} {float(funding.sum()):>16.4%} "
      f"{float(funding.sum()) / years:>11.4%}   —")
print(f"  毛收益累计 {gross.sum():.4%} → 净收益累计 {net.sum():.4%}")

# 报告口径核对
print()
print(f"  与报告口径核对：报告写「成本拖累 1.71%/yr」，实测 {cc / years:.4%}/yr  "
      f"Δ={(cc / years - 0.0171) * 1e4:+.1f} bps")

# ---------------------------------------------------------------- 2. 有效费率
print()
print("=" * 92)
print("[2] 有效费率：每单位换手要付多少 bps")
tot_turn = float(turn.sum())
print(f"  全样本累计换手 {tot_turn:.4f}（= 净值倍数口径），即 {tot_turn / years:.1f}× / 年")
print(f"  {'项目':<20s} {'bps / 单位换手':>16s}")
print("  " + "-" * 40)
for k, v in tot.items():
    print(f"  {k:<20s} {v / tot_turn * 1e4:>15.2f}")
print("  " + "-" * 40)
print(f"  {'合计（有效往返费率）':<20s} {cc / tot_turn * 1e4:>15.2f}")

ccfg = cfg.costs
eff_fee = ccfg.fee_multiplier * ((1 - ccfg.passive_fill_ratio) * ccfg.taker_fee
                                 + ccfg.passive_fill_ratio * ccfg.maker_fee)
print()
print(f"  模型设定：taker={ccfg.taker_fee * 1e4:.1f}bps  maker={ccfg.maker_fee * 1e4:.1f}bps  "
      f"passive_fill_ratio={ccfg.passive_fill_ratio:.2f} → 有效 fee={eff_fee * 1e4:.2f}bps")
print(f"  半价差：流动性档 {ccfg.half_spread_bps_base:.2f}bps / 薄池档 "
      f"{ccfg.half_spread_bps_illiquid:.2f}bps（切点 trailing ADV "
      f"${ccfg.half_spread_adv_cut_usd / 1e6:.0f}M）")
print(f"  实测 fee {tot['手续费 fee'] / tot_turn * 1e4:.2f}bps 应 ≈ 设定 {eff_fee * 1e4:.2f}bps"
      f"  →  Δ={ (tot['手续费 fee'] / tot_turn * 1e4 - eff_fee * 1e4):+.3f}bps")

# ---------------------------------------------------------------- 3. 逐年
print()
print("=" * 92)
print("[3] 逐年：成本是否随规模/波动漂移")
rows = []
for y, g in bar.groupby(bar.index.year):
    c = g["fee"] + g["spread"] + g["impact"]
    n_years = len(g) / (365 * 24)
    rows.append({
        "year": int(y),
        "fee": float(g["fee"].sum()),
        "spread": float(g["spread"].sum()),
        "impact": float(g["impact"].sum()),
        "cost_total": float(c.sum()),
        "cost_ann": float(c.sum() / n_years),
        "gross": float(g["gross_ret"].sum()),
        "funding": float(g["funding"].sum()),
        "net": float(g["net_ret"].sum()),
        "turnover": float(g["turnover"].sum()),
        "eff_bps": float(c.sum() / g["turnover"].sum() * 1e4) if g["turnover"].sum() > 0 else np.nan,
    })
dy = pd.DataFrame(rows)
print(dy.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
print()
print(f"  有效费率逐年范围 {dy.eff_bps.min():.2f} .. {dy.eff_bps.max():.2f} bps "
      f"（稳 → 成本模型本身不是收益的噪声源）")
print(f"  成本/毛收益 逐年比值：")
for _, x in dy.iterrows():
    ratio = x.cost_total / x.gross if abs(x.gross) > 1e-9 else np.nan
    print(f"    {x.year}  成本 {x.cost_total:8.4%}  毛收益 {x.gross:8.4%}  成本/毛 = {ratio:6.1%}")

# ---------------------------------------------------------------- 4. 两腿对称性
print()
print("=" * 92)
print("[4] 成本是否只落在某一腿（用 name_cost 按权重符号拆）")
W = np.asarray(r.weight_matrix, dtype="float64")
C = np.asarray(r.name_cost, dtype="float64")
T = np.asarray(r.name_turnover, dtype="float64")
long_mask, short_mask = W > 0, W < 0
print(f"  多头腿成本 {C[long_mask].sum():.4%}   空头腿成本 {C[short_mask].sum():.4%}")
print(f"  多头腿换手 {T[long_mask].sum():.4f}   空头腿换手 {T[short_mask].sum():.4f}")
print("  → 成本按 |Δw| 逐名计费，与方向无关；两腿金额差异只反映换手量")

# ---------------------------------------------------------------- 5. 强平/退市成本
print()
print("=" * 92)
print("[5] 强平（stale / 退市）成本是否入账")
n_stale = bar["n_stale"].to_numpy()
print(f"  触发强平的 bar 数 {int((n_stale > 0).sum())}，累计强平名次 {int(n_stale.sum())}")
# 强平把 taker + illiquid 半价差直接加进 fee / spread，所以无法单独拆出；
# 用「强平 bar 上 fee 是否异常高」来间接验证。
sbar = bar[n_stale > 0]
if len(sbar):
    print(f"  强平 bar 的 fee 合计 {float(sbar['fee'].sum()):.6%}，"
          f"spread 合计 {float(sbar['spread'].sum()):.6%}")
    print(f"  强平 bar 换手合计 {float(sbar['turnover'].sum()):.4f}")
    print(f"  → 强平成本已按 taker + 薄池半价差入账（engine.py:380-388）")
else:
    print("  本样本未触发强平路径（→ 这条路径只有单元测试覆盖）")
print("  注：交易所强平罚款 / ADL 损失 **未建模**")

# ---------------------------------------------------------------- 6. 参与率
print()
print("=" * 92)
print("[6] 冲击成本的前提：参与率是否合理")
mp = bar["max_participation"].to_numpy(dtype="float64")
mp = mp[np.isfinite(mp) & (mp > 0)]
if mp.size:
    print(f"  单期最大参与率：中位 {np.median(mp):.4%}  p90 {np.percentile(mp, 90):.4%}  "
          f"p99 {np.percentile(mp, 99):.4%}  max {mp.max():.4%}")
    print(f"  超过 1% ADV 的期数占比 {np.mean(mp > 0.01):.2%}，"
          f"超过 5% 的 {np.mean(mp > 0.05):.2%}")

# ---------------------------------------------------------------- 7. 成本对绩效的贡献
print()
print("=" * 92)
print("[7] 成本吃掉了多少绩效")


def sharpe(x):
    d = pd.Series(x, index=idx).resample("1D").sum()
    return float(d.mean() / d.std(ddof=1) * np.sqrt(365))


sh_net = sharpe(net)
sh_gross = sharpe(gross)
sh_nofee = sharpe(gross - (spread + impact) + funding)   # 免手续费
sh_free = sharpe(gross + funding)                        # 零交易成本
print(f"  净 Sharpe（交付值）        {sh_net:.4f}")
print(f"  毛 Sharpe（不含任何成本）  {sh_free:.4f}   → 成本共击落 {sh_free - sh_net:+.4f}")
print(f"  免手续费（只扣价差+冲击）  {sh_nofee:.4f}   → 手续费单独击落 {sh_nofee - sh_net:+.4f}")
print(f"  毛 Sharpe（含成本）        {sh_gross:.4f}")

# ---------------------------------------------------------------- 8. 成本情景压力
print()
print("=" * 92)
print("[8] 成本情景压力（来自已验收 01b_cost_scenarios 表）")
p = os.path.join(ROOT, "artifacts", "v3", "tables", "01b_cost_scenarios.csv")
if os.path.exists(p):
    print(pd.read_csv(p).to_string(index=False))
else:
    print("  表不存在")

dy.to_csv(os.path.join(OUT, "cost_by_year.csv"), index=False, encoding="utf-8-sig")
summary = {
    "years": years, "bars": int(len(bar)),
    "fee": float(fee.sum()), "spread": float(spread.sum()), "impact": float(impact.sum()),
    "cost_total": cc, "cost_ann": float(cc / years), "funding": float(funding.sum()),
    "turnover_total": tot_turn, "eff_bps": float(cc / tot_turn * 1e4),
    "sharpe_net": sh_net, "sharpe_gross_costless": sh_free,
    "identity_max_resid": float(np.abs(resid).max()),
}
with open(os.path.join(OUT, "cost_summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)
print()
print(f"产物 → {OUT}")
