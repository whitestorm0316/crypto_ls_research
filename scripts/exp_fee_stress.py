"""一次性实验：交易费用的敏感性 / 压力测试。

动机（本轮侦察结论）：
  * 交易成本 2.07%/yr 里 **手续费占 57.8%**（5.00bps/单位换手），是成本的最大单项。
  * 但 `15_sensitivity_ofat` 里 **fee / cost 相关行数 = 0** —— 手续费从未被扫过。
    只扫过 `funding_mult`（那是资金费现金流，不是成本）。
  * `01b_cost_scenarios` 只有 4 行（gross / no_trading_cost / no_funding / net），
    **没有手续费压力档**。

所以本实验回答两个决定"能不能实盘"的问题：
  Q1 手续费假设错了会怎样？（taker 5bps → 7.5 / 10bps）
  Q2 如果执行能拿到 maker 成交，能回收多少？（0% → 50% → 100% passive）

自校验：默认点必须**逐位复现** v3 的 Sharpe 1.9373269448000032。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from crypto_ls_research.analysis.sweep import run_sweep            # noqa: E402
from crypto_ls_research.config.settings import BARS_PER_DAY        # noqa: E402
from crypto_ls_research.data.asset_class import filter_insts, load_categories  # noqa: E402
from crypto_ls_research.data.store import list_cached_insts        # noqa: E402

BASE_SHARPE = 1.9373269448000032
BAR, RD, START, END = "1h", 3.0, "2021-01-01", "2026-09-26"
REBAL = max(1, int(round(RD * BARS_PER_DAY[BAR])))
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "artifacts", "fee_stress"))

BASE_OV = {
    "factors.subset": ["range_pos", "hitrate"],
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}

TAKER, MAKER = 5.0, 2.0   # bps，与 CostConfig 默认一致


def scoped_insts():
    pool = list_cached_insts(BAR)
    cats = load_categories()
    scoped, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify {len(unknown)} cached instrument(s): {unknown[:6]}")
    return scoped


def spec(label, note, **extra_ov):
    return {"label": label, "note": note,
            "overrides": {**BASE_OV, **extra_ov}, "rebalance_bars": REBAL}


def build_specs():
    specs = []
    # 自校验点
    specs.append(spec("base", "默认：100% taker @5bps"))

    # Q1 手续费假设偏乐观的风险
    for m in (1.5, 2.0, 3.0):
        specs.append(spec(f"fee_x{m:.1f}", f"手续费 ×{m:.1f}（taker {TAKER * m:.1f}bps）",
                          **{"costs.fee_multiplier": m}))

    # Q2 执行改善的上行空间（passive 成交占比）
    for p in (0.25, 0.50, 0.75, 1.0):
        eff = (1 - p) * TAKER + p * MAKER
        specs.append(spec(f"passive_{p:.2f}", f"passive 占比 {p:.0%}（有效 fee {eff:.2f}bps）",
                          **{"costs.passive_fill_ratio": p}))

    # 组合压力：手续费 ×2 + 价差翻倍（薄池 6bps / 主流 1.2bps）
    specs.append(spec("fee_x2_spread_x2", "手续费×2 且半价差×2",
                      **{"costs.fee_multiplier": 2.0,
                         "costs.half_spread_bps_base": 1.2,
                         "costs.half_spread_bps_illiquid": 6.0}))

    # Q3 最可疑的一项：薄池半价差 3bps 是「一刀切」而非实测报价值。
    # 实测 91% 的成交名次 / 79% 的成交金额落在薄池档，中位 ADV 仅 $2.46M
    # —— 对这种盘口，3bps 半价差可能偏乐观。单独加压力。
    specs.append(spec("spread_il6", "薄池半价差 3→6bps（主流不变）",
                      **{"costs.half_spread_bps_illiquid": 6.0}))
    specs.append(spec("spread_il10", "薄池半价差 3→10bps（主流 0.6→1.0bps）",
                      **{"costs.half_spread_bps_illiquid": 10.0,
                         "costs.half_spread_bps_base": 1.0}))
    specs.append(spec("impact_x2", "冲击系数 0.6→1.2",
                      **{"costs.impact_coef": 1.2}))
    specs.append(spec("all_x2", "fee×2 + 薄池价差 6bps + 冲击系数 1.2",
                      **{"costs.fee_multiplier": 2.0,
                         "costs.half_spread_bps_base": 1.2,
                         "costs.half_spread_bps_illiquid": 6.0,
                         "costs.impact_coef": 1.2}))
    return specs


def eff_fee_bps(ov):
    m = float(ov.get("costs.fee_multiplier", 1.0))
    p = float(ov.get("costs.passive_fill_ratio", 0.0))
    return m * ((1 - p) * TAKER + p * MAKER)


def drawdown(eq):
    v = np.asarray(eq, dtype="float64")
    return float((v / np.maximum.accumulate(v) - 1.0).min())


def year_sharpe(bars):
    d = bars["net_ret"].resample("1D").sum()
    out = {}
    for y, g in d.groupby(d.index.year):
        if len(g) < 60 or g.std(ddof=1) <= 0:
            out[int(y)] = np.nan
        else:
            out[int(y)] = float(g.mean() / g.std(ddof=1) * np.sqrt(365))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    insts = scoped_insts()
    print(f"池子 {len(insts)} 个合约；rebalance_bars={REBAL}")

    specs = build_specs()
    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=4, desc="fee")

    by_label = {r["label"]: r for r in res if "error" not in r}
    for r in res:
        if "error" in r:
            print(f"  !! {r['label']} failed: {r['error']}")

    rows, yrows = [], []
    for s in specs:
        lab = s["label"]
        if lab not in by_label:
            print(f"!! {lab} 缺失")
            continue
        r = by_label[lab]
        bars = r["bars"]
        m = r["metrics"]
        net = bars["net_ret"].astype("float64")
        eq = (1.0 + net).cumprod()

        # 成本分解（每个配置自己的账本）
        cost = (bars["fee"] + bars["spread"] + bars["impact"]).sum()
        turn = bars["turnover"].sum()
        years = (bars.index[-1] - bars.index[0]).total_seconds() / (365 * 24 * 3600)

        rows.append({
            "label": lab,
            "note": s["note"],
            "eff_fee_bps": eff_fee_bps(s["overrides"]),
            "sharpe": float(m.get("Sharpe", np.nan)),
            "cagr": float(m.get("CAGR", np.nan)),
            "max_dd": drawdown(eq),
            "ann_turnover": float(turn / years),
            "cost_total": float(cost),
            "cost_ann": float(cost / years),
            "allin_bps": float(cost / turn * 1e4) if turn > 0 else np.nan,
        })
        ys = year_sharpe(bars)
        yrows.append({"label": lab, **{f"sh_{k}": v for k, v in ys.items()}})

    df = pd.DataFrame(rows)
    yd = pd.DataFrame(yrows)

    # ---- 自校验 ----------------------------------------------------------
    base = df[df.label == "base"]
    if len(base) == 1:
        d = abs(float(base.iloc[0]["sharpe"]) - BASE_SHARPE)
        flag = "OK" if d < 1e-12 else "**不一致**"
        print(f"\n[自校验] base Sharpe={float(base.iloc[0]['sharpe']):.16f} "
              f"期望={BASE_SHARPE:.16f} Δ={d:.3e}  {flag}")
        assert d < 1e-12, "override 路径没接上，整张表作废"
    else:
        print("\n[自校验] base 缺失！")
        raise SystemExit(1)

    # ---- 报告 ------------------------------------------------------------
    pd.set_option("display.width", 200)
    print("\n" + "=" * 118)
    print("[A] 手续费 / 执行假设的敏感性")
    print("=" * 118)
    cols = ["label", "note", "eff_fee_bps", "sharpe", "cagr", "max_dd",
            "allin_bps", "cost_ann", "ann_turnover"]
    show = df[cols].copy()
    show["sharpe"] = show["sharpe"].map(lambda v: f"{v:.4f}")
    show["cagr"] = show["cagr"].map(lambda v: f"{v:.2%}")
    show["max_dd"] = show["max_dd"].map(lambda v: f"{v:.2%}")
    show["cost_ann"] = show["cost_ann"].map(lambda v: f"{v:.4%}")
    show["allin_bps"] = show["allin_bps"].map(lambda v: f"{v:.2f}")
    show["ann_turnover"] = show["ann_turnover"].map(lambda v: f"{v:.1f}")
    show["eff_fee_bps"] = show["eff_fee_bps"].map(lambda v: f"{v:.2f}")
    print(show.to_string(index=False))

    print("\n" + "=" * 118)
    print("[B] 基准对照：手续费假设错一档，绩效掉多少")
    print("=" * 118)
    b0 = df[df.label == "base"].iloc[0]
    for _, x in df.iterrows():
        if x.label == "base":
            continue
        ds = x.sharpe - b0.sharpe
        dc = x.cagr - b0.cagr
        print(f"  {x.label:<18s} fee {x.eff_fee_bps:5.2f}bps (Δ{x.eff_fee_bps - b0.eff_fee_bps:+5.2f})  "
              f"Sharpe {x.sharpe:.4f} ({ds:+.4f})   CAGR {x.cagr:6.2%} ({dc:+.2%})   "
              f"全包成本 {x.allin_bps:.2f}bps ({x.allin_bps - b0.allin_bps:+.2f})")

    print("\n" + "=" * 118)
    print("[C] 逐年 Sharpe（看压力下是否只是整体平移，而不是某一年崩塌）")
    print("=" * 118)
    print(yd.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    df.to_csv(os.path.join(OUT, "fee_stress.csv"), index=False, encoding="utf-8-sig")
    yd.to_csv(os.path.join(OUT, "fee_stress_by_year.csv"), index=False, encoding="utf-8-sig")
    print(f"\n产物 → {OUT}")


if __name__ == "__main__":
    main()
