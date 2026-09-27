"""换手预算选择的稳定性检验。

报告把「20% / 日」当作本轮唯一"调"出来的参数（关掉它 −0.201 Sharpe，
是第二大单项）。但本轮的教训是：**任何全样本 Δ 都要逐折复核**
（池子 scope +0.285、宽度自适应 cap +0.029 都已因此被降级/否掉）。

所以这里对预算本身做同样的检验：0.10 / 0.20 / 0.50 三档，
逐年 Sharpe + 15 个季度折 + 锁定样本。若 0.20 的优势只在某一年才出现，
那个 −0.201 的归因就站不住。
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
OUT = os.path.join(ROOT, "artifacts", "budgetstab")
os.makedirs(OUT, exist_ok=True)

research.BASE_OVERRIDES = {
    "portfolio.max_weight_per_instrument": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}

BUDGETS = [0.10, 0.20, 0.50]


def main():
    specs = []
    for b in BUDGETS:
        specs.append({"label": f"b{b:.2f}", "rebalance_bars": REBAL,
                      "overrides": {"execution.max_daily_turnover": b},
                      "save_result": os.path.join(OUT, f"book_b{b:.2f}.pkl")})
    for s in specs:
        s["bar"] = BAR
    specs = research.merge_ov(specs)

    cats = load_categories()
    insts, unknown = filter_insts(list_cached_insts(BAR), cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify: {unknown[:8]}")

    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=2, desc="budgetstab")
    bad = [(r["label"], r.get("error")) for r in res if r.get("error")]
    if bad:
        raise SystemExit("specs failed:\n  " + "\n  ".join(f"{k}: {v}" for k, v in bad))

    books = {}
    for b in BUDGETS:
        with open(os.path.join(OUT, f"book_b{b:.2f}.pkl"), "rb") as f:
            books[f"b{b:.2f}"] = pickle.load(f)

    print("\n=== 逐年 Sharpe ===")
    yr = {}
    for k, res_ in books.items():
        rows = {}
        for y, seg in res_.bars.groupby(res_.bars.index.year):
            seg = seg.copy()
            seg["equity"] = (1.0 + seg["net_ret"]).cumprod()
            rows[y] = compute_metrics(seg, name=str(y))["Sharpe"]
        yr[k] = pd.Series(rows)
    yt = pd.DataFrame(yr)
    yt["b0.20-b0.10"] = yt["b0.20"] - yt["b0.10"]
    print(yt.round(4).to_string())

    print("\n=== 15 个季度折 test Sharpe ===")
    q = {}
    for k, res_ in books.items():
        t = wf.fold_metrics_table([{"label": k, "bars": res_.bars}], folds=wf.QUARTERLY_FOLDS)
        q[k] = t.set_index("fold")["test_sharpe"]
    qt = pd.DataFrame(q)
    qt["b0.20-b0.10"] = qt["b0.20"] - qt["b0.10"]
    qt["b0.20-b0.50"] = qt["b0.20"] - qt["b0.50"]
    print(qt.round(4).to_string())

    for pair in (("b0.20", "b0.10"), ("b0.20", "b0.50")):
        d = (qt[pair[0]] - qt[pair[1]]).dropna()
        print(f"\n{pair[0]} - {pair[1]}: 折级均值={d.mean():+.4f} 中位={d.median():+.4f} "
              f"正比例={float((d > 0).mean()):.2f}  min={d.min():+.3f}  max={d.max():+.3f}")
        dy = (yt[pair[0]] - yt[pair[1]]).dropna()
        big = dy.abs().idxmax()
        print(f"  年度: 正比例={float((dy > 0).mean()):.2f}  "
              f"最大单年={big} ({dy.loc[big]:+.4f})，占总 |Δ| 的 "
              f"{abs(dy.loc[big]) / dy.abs().sum():.1%}")

    print("\n=== 锁定样本 2026 ===")
    lo = wf.LOCKED_OOS
    rows = [{"arm": k, **wf.slice_metrics(v.bars, *lo["window"], name=lo["name"])}
            for k, v in books.items()]
    print(pd.DataFrame(rows).to_string(index=False))

    yt.to_csv(os.path.join(OUT, "budget_yearly.csv"))
    qt.to_csv(os.path.join(OUT, "budget_quarterly.csv"))
    print(f"\nwrote {OUT}/budget_yearly.csv, budget_quarterly.csv")


if __name__ == "__main__":
    main()
