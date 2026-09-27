"""`17_grid_K_x_tvol` 里露出的 K=5 增益，必须复核。

`sens` 网格显示：`tvol=0.30` 时
    K=5  -> 2.0416
    K=10 -> 1.9373   (v3 用的就是这个)
    K=15 -> 1.9300
    K=20 -> 1.9279
即 **K=5 比 K=10 高 +0.105**，量级与 sizing 修复（+0.099）相当。

但注意一个关键细节：**K=5 时 `cap·n = 0.20 × 5 = 1.0`，正好落在 §3.4 的
sizing 退化边界上** —— 也就是说 K=5 实际跑的是"5 个名字的等权书"，
而不是"5 个名字的 |score|/vol 书"。这与 §3.4 的结论（窄池里等权更好）方向一致，
但必须按同一把尺子验证后再决定。

本脚本：K=5/8/10 在 tvol=0.30 上逐年 + 15 季度折 + 锁定样本对比。
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crypto_ls_research.analysis import walkforward as wf                 # noqa: E402
from crypto_ls_research.analysis.metrics import compute_metrics           # noqa: E402
from crypto_ls_research.analysis.sweep import run_sweep                   # noqa: E402
from crypto_ls_research.config.settings import BARS_PER_DAY               # noqa: E402
from crypto_ls_research.data.asset_class import filter_insts, load_categories  # noqa: E402
from crypto_ls_research.data.store import list_cached_insts               # noqa: E402
from crypto_ls_research.run import research                               # noqa: E402

BAR = "1h"
REBAL = int(round(3.0 * BARS_PER_DAY[BAR]))
START, END = "2021-01-01", "2026-09-26"
OUT = os.path.join(ROOT, "artifacts", "topkstab")
os.makedirs(OUT, exist_ok=True)

research.BASE_OVERRIDES = {
    "execution.max_daily_turnover": 0.2,
    "portfolio.max_weight_per_instrument": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}

ARMS = [("K5", 5), ("K8", 8), ("K10", 10)]


def book_path(k):
    return os.path.join(OUT, f"book_K{k}.pkl")


def main():
    specs = [{"label": f"K{k}", "rebalance_bars": REBAL, "bar": BAR,
              "overrides": {"portfolio.top_k": k},
              "save_result": book_path(k)} for _, k in ARMS]
    specs = research.merge_ov(specs)

    cats = load_categories()
    insts, unknown = filter_insts(list_cached_insts(BAR), cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify: {unknown[:8]}")

    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=2, desc="topk")
    bad = [(r["label"], r.get("error")) for r in res if r.get("error")]
    if bad:
        raise SystemExit("specs failed:\n  " + "\n  ".join(f"{k}: {v}" for k, v in bad))

    books = {}
    for label, k in ARMS:
        with open(book_path(k), "rb") as f:
            books[label] = pickle.load(f)

    # 每侧选币数 + 退化占比（用选币数，不是 weight_matrix 非零数）
    print("\n=== 每侧选币数与 sizing 退化占比 ===")
    for label, k in ARMS:
        nl = np.array([len(r["long"]) for r in books[label].rebalances], dtype=float)
        ns = np.array([len(r["short"]) for r in books[label].rebalances], dtype=float)
        mx = np.maximum(nl, ns)
        mx = mx[np.isfinite(mx)]
        deg = 0.20 * mx <= 1.0 + 1e-12
        print(f"  {label:4s} top_k={k:2d}  每侧中位={int(np.median(mx)):2d}  "
              f"min={int(mx.min()):2d}  cap*n<=1 退化占比={deg.mean():.2%}")

    print("\n=== 逐年 Sharpe ===")
    yr = {}
    for label, b in books.items():
        rows = {}
        for y, seg in b.bars.groupby(b.bars.index.year):
            seg = seg.copy()
            seg["equity"] = (1.0 + seg["net_ret"]).cumprod()
            rows[y] = compute_metrics(seg, name=str(y))["Sharpe"]
        yr[label] = pd.Series(rows)
    yt = pd.DataFrame(yr)
    yt["K5-K10"] = yt["K5"] - yt["K10"]
    print(yt.round(4).to_string())

    print("\n=== 15 个季度折 test Sharpe ===")
    q = {}
    for label, b in books.items():
        t = wf.fold_metrics_table([{"label": label, "bars": b.bars}], folds=wf.QUARTERLY_FOLDS)
        q[label] = t.set_index("fold")["test_sharpe"]
    qt = pd.DataFrame(q)
    qt["K5-K10"] = qt["K5"] - qt["K10"]
    print(qt.round(4).to_string())

    d = (qt["K5"] - qt["K10"]).dropna()
    dy = (yt["K5"] - yt["K10"]).dropna()
    big = dy.abs().idxmax()
    print(f"\nK5-K10 折级: 均值={d.mean():+.4f} 中位={d.median():+.4f} "
          f"正比例={float((d > 0).mean()):.2f}  min={d.min():+.3f}  max={d.max():+.3f}")
    print(f"K5-K10 年度: 正比例={float((dy > 0).mean()):.2f}  "
          f"最大单年={big} ({dy.loc[big]:+.4f})，占总 |Δ| 的 "
          f"{abs(dy.loc[big]) / dy.abs().sum():.1%}")

    print("\n=== 锁定样本 2026 ===")
    lo = wf.LOCKED_OOS
    rows = [{"arm": lb, "top_k": k,
             **wf.slice_metrics(b.bars, *lo["window"], name=lo["name"])}
            for (lb, k), b in ((a, books[a[0]]) for a in ARMS)]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 全样本 ===")
    full = []
    for lb, k in ARMS:
        m = compute_metrics(books[lb].bars, name=lb)
        full.append({"arm": lb, "top_k": k, "Sharpe": m["Sharpe"], "CAGR": m["CAGR"],
                     "max_dd": m["Max Drawdown"],
                     "ann_turnover": m["Annual Turnover"],
                     "ann_vol": m["Annualized Volatility"]})
    print(pd.DataFrame(full).to_string(index=False))

    yt.to_csv(os.path.join(OUT, "topk_yearly.csv"))
    qt.to_csv(os.path.join(OUT, "topk_quarterly.csv"))
    print(f"\nwrote {OUT}/topk_yearly.csv, topk_quarterly.csv")


if __name__ == "__main__":
    main()
