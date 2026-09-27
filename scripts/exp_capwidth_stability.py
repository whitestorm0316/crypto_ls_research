"""`cap_width_slack` 的稳定性检验。

邻域细化（`cap_refine.csv`）给出：
    cap   0.15  0.18  0.20  0.22  0.25  0.30
    Sh    1.917 1.931 1.937 1.898 1.831 1.808   -> 平台峰
    slack 0.50  0.75  1.00  1.50  2.00  (cap=0.20)
    Sh    1.935 1.961 1.966 1.949 1.939   -> 也是平台
峰值增益只有 **+0.029 Sharpe**，落在噪声量级。

本项目刚被"池子 scope 的 +0.285 其实 100% 来自 2026"教训过，所以这里必须回答：
增益是**逐年/逐折都成立**，还是某一段的假象？

做法：把两个配置的完整回测存成 pickle（`save_result`），
再对两本账做 (a) 逐年 Sharpe 对比、(b) 15 个季度折 test Sharpe 对比、
(c) 锁定样本。**不重跑任何分析阶段。**
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
OUT = os.path.join(ROOT, "artifacts", "capwidth")
os.makedirs(OUT, exist_ok=True)

research.BASE_OVERRIDES = {
    "execution.max_daily_turnover": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}

ARMS = {
    "static0.20": {"portfolio.max_weight_per_instrument": 0.20},
    "slack1.00": {"portfolio.max_weight_per_instrument": 0.20,
                  "portfolio.cap_width_slack": 1.00},
}


def run_arms():
    specs = []
    for label, ov in ARMS.items():
        pickle_path = os.path.join(OUT, f"book_{label}.pkl")
        specs.append({"label": label, "overrides": ov, "rebalance_bars": REBAL,
                      "save_result": pickle_path, "keep_bars": True})
    specs = research.merge_ov(specs)
    cats = load_categories()
    insts, unknown = filter_insts(list_cached_insts(BAR), cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify: {unknown[:8]}")
    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=2, desc="slack-stab")
    bad = [(r["label"], r.get("error")) for r in res if r.get("error")]
    if bad:
        raise SystemExit("specs failed:\n  " + "\n  ".join(f"{k}: {v}" for k, v in bad))
    return res


def load_book(label):
    with open(os.path.join(OUT, f"book_{label}.pkl"), "rb") as f:
        return pickle.load(f)


def yearly(bars: pd.DataFrame) -> pd.Series:
    out = {}
    for y, seg in bars.groupby(bars.index.year):
        m = compute_metrics(seg, name=str(y))
        out[y] = m["Sharpe"]
    return pd.Series(out, name="Sharpe").rename_axis("year")


def main():
    run_arms()
    books = {k: load_book(k) for k in ARMS}

    print("\n=== 逐年 Sharpe ===")
    yy = pd.DataFrame({k: yearly(b.bars) for k, b in books.items()})
    yy["delta"] = yy["slack1.00"] - yy["static0.20"]
    print(yy.to_string())

    print("\n=== 15 个季度折 test Sharpe ===")
    q = {}
    for k, b in books.items():
        t = wf.fold_metrics_table([{"label": k, "bars": b.bars}], folds=wf.QUARTERLY_FOLDS)
        q[k] = t.set_index("fold")["test_sharpe"]
    qq = pd.DataFrame(q)
    qq["delta"] = qq["slack1.00"] - qq["static0.20"]
    print(qq.to_string())
    d = qq["delta"].dropna()
    print(f"\n折级 delta: n={d.size}  均值={d.mean():+.4f}  中位={d.median():+.4f}  "
          f"正比例={float((d > 0).mean()):.2f}  min={d.min():+.3f}  max={d.max():+.3f}")

    print("\n=== 年度 delta 与折级 delta 的集中度 ===")
    yd = yy["delta"].dropna()
    print(f"年度 delta 正比例 = {float((yd > 0).mean()):.2f}；"
          f"最大单年 = {yd.abs().idxmax()} ({yd.loc[yd.abs().idxmax()]:+.4f})，"
          f"占总 |delta| 的 {abs(yd.loc[yd.abs().idxmax()]) / yd.abs().sum():.1%}")

    print("\n=== 锁定样本（不参与任何选择） ===")
    lo = wf.LOCKED_OOS
    rows = []
    for k, b in books.items():
        rows.append({"arm": k, **wf.slice_metrics(b.bars, *lo["window"], name=lo["name"])})
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 全样本 ===")
    full = []
    for k, b in books.items():
        m = compute_metrics(b.bars, name=k)
        full.append({"arm": k, "Sharpe": m["Sharpe"], "CAGR": m["CAGR"],
                     "max_dd": m["Max Drawdown"],
                     "ann_turnover": m["Annual Turnover"],
                     "cost_drag": m["Cost Drag (annual)"]})
    print(pd.DataFrame(full).to_string(index=False))

    yy.to_csv(os.path.join(OUT, "slack_yearly.csv"))
    qq.to_csv(os.path.join(OUT, "slack_quarterly.csv"))
    print(f"\nwrote {OUT}/slack_yearly.csv, slack_quarterly.csv")


if __name__ == "__main__":
    main()
