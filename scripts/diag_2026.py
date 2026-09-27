"""诊断：2026 为什么这么强？它是不是一个结构性不同的时期？

背景：本轮三个"小增益"——池子 scope（+0.285）、宽度自适应 cap（+0.029）、
以及锁定样本自身（Sharpe 3.686）——全部由 2026 主导。
在把 2026 的高 Sharpe 当作证据之前，必须回答：
它是"alpha 变强了"，还是"换了另一套风险/换手结构"？

只看 v3 baseline 的逐年结构量，不重跑。
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crypto_ls_research.analysis.metrics import compute_metrics    # noqa: E402

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)


def main(tag="v3"):
    path = os.path.join(ROOT, "artifacts", tag, "baseline.pkl")
    with open(path, "rb") as f:
        res = pickle.load(f)["result"]
    bars = res.bars

    rows = []
    for y, seg in bars.groupby(bars.index.year):
        # CRITICAL: bars["equity"] is the *full-sample* cumulative curve.  Passing a
        # slice of it straight to compute_metrics makes CAGR/Calmar/max_dd meaningless
        # (it annualises the whole-sample equity move over a 1-year window -- this
        # produced a spurious "2026 CAGR = 475%").  Sharpe and vol are safe because
        # they are built from daily sums of net_ret, but anything equity-based must be
        # rebased to the slice first.  walkforward.slice_bars() does exactly this.
        seg = seg.copy()
        seg["equity"] = (1.0 + seg["net_ret"]).cumprod()
        m = compute_metrics(seg, name=str(y))
        gm = compute_metrics(seg.assign(net_ret=seg["gross_ret"]).assign(
            equity=(1.0 + seg["gross_ret"]).cumprod()), name="gross")
        rows.append({
            "year": y,
            "Sharpe": m["Sharpe"],
            "Sharpe_gross": gm["Sharpe"],
            "CAGR": m["CAGR"],
            "ann_vol": m["Annualized Volatility"],
            "turnover": m["Annual Turnover"],
            "cost_drag": m["Cost Drag (annual)"],
            "funding": m["Funding P&L (total, frac)"],
            "avg_gross": seg["gross_exposure"].mean(),
            "avg_abs_beta": seg["beta_exposure"].abs().mean(),
            "scale_mean": seg["total_scale"].mean() if "total_scale" in seg else np.nan,
            "hit_rate": float((seg["net_ret"] > 0).mean()),
        })
    t = pd.DataFrame(rows).set_index("year")
    print(f"=== {tag} 逐年结构 ===")
    print(t.round(4).to_string())

    print("\n=== 相对 2021-2025 均值的变化（按标准差单位） ===")
    base = t.loc[2021:2025]
    for y in t.index:
        rel = (t.loc[y] - base.mean()) / base.std(ddof=1).replace(0, np.nan)
        top = rel.dropna().sort_values(key=np.abs, ascending=False).head(3)
        print(f"  {y}: " + ", ".join(f"{k}={v:+.2f}σ" for k, v in top.items()))

    print("\n=== 2026 的收益是否集中在少数几天 ===")
    r = bars.loc["2026-01-01":, "net_ret"]
    q = r.sort_values(ascending=False)
    tot = r.sum()
    for k in (5, 10, 20):
        print(f"  最好 {k} 天贡献 {q.head(k).sum():+.2%}，占全期净收益合计 {tot:+.2%} 的 "
              f"{q.head(k).sum()/tot:.0%}")
    print(f"  日胜率 {float((r>0).mean()):.3f}，日收益 std {r.std():.5f}")

    print("\n=== 全样本日胜率与 2026 对比 ===")
    allr = bars["net_ret"]
    print(f"  全样本日胜率 {float((allr>0).mean()):.3f}；2026 {float((r>0).mean()):.3f}")

    t.to_csv(os.path.join(ROOT, "artifacts", tag, "tables", "35_year_structure.csv"))
    print(f"\nwrote artifacts/{tag}/tables/35_year_structure.csv")


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["v3"]))
