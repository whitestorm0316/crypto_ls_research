"""K=5 的增益：是「宽度」带来的，还是「cap·n=1.0 等权」带来的？

背景
----
`17_grid_K_x_tvol` 里 K=5 比 K=10 高 +0.105（2.0416 vs 1.9373），量级与 sizing
修复（+0.099）相当。但 `exp_topk_stability.py` 量出一个决定性的细节：

    K5  (cap 0.20): cap·n <= 1 退化占比 = 100.00%
    K8  (cap 0.20): 14.53%
    K10 (cap 0.20): 14.53%

因为 cap·n = 0.20 × 5 = 1.0 **正好**在边界上，K=5 的书**每一根调仓都是等权**，
`|score|/vol` sizing 从未生效。于是 K5 vs K10 实际上同时改变了**两件事**：

    (1) 书的宽度:        5 个名字      ->  ~8 个名字
    (2) sizing 是否生效: 完全失效(等权) ->  85% 的调仓里生效

**这个对比是被混淆的（confounded），不能直接用来选 K。** 本脚本把两件事分开：
固定 top_k、把 cap 抬到 `cap·n > 1`（sizing 强制生效），看 K=5 的增益是否还在。

判据（沿用 §11b：年度正比例 ≥ 2/3 + 无单年 > 35% + 锁定样本同号）
- 若 K5@cap0.25 ≈ 2.04：增益来自**宽度**，等权只是副产品 -> 值得采纳；
- 若 K5@cap0.25 回落到 ≈ 1.94：增益只存在于「5 个名字的等权书」里，
  本质是一个**集中度赌注**，不是选币宽度改进 -> 不采纳为头条配置。
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
OUT = os.path.join(ROOT, "artifacts", "topkcap")
os.makedirs(OUT, exist_ok=True)
REF = "K10_cap0.20"          # v3 的配置：所有 Δ 都相对它算

research.BASE_OVERRIDES = {
    "execution.max_daily_turnover": 0.2,
    "portfolio.max_weight_per_instrument": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}

ARMS = [
    ("K5_cap0.20", 5, 0.20),    # 退化 100% —— 参考的"原"K5
    ("K5_cap0.25", 5, 0.25),    # sizing 生效（0.25*5 = 1.25 > 1）
    ("K5_cap0.30", 5, 0.30),
    ("K5_cap0.40", 5, 0.40),
    ("K8_cap0.20", 8, 0.20),
    ("K8_cap0.25", 8, 0.25),
    ("K8_cap0.30", 8, 0.30),
    ("K10_cap0.20", 10, 0.20),  # = v3
    ("K10_cap0.25", 10, 0.25),
    ("K10_cap0.30", 10, 0.30),
]


def book_path(label: str) -> str:
    return os.path.join(OUT, f"book_{label}.pkl")


def _side_width(b):
    nl = np.array([len(r["long"]) for r in b.rebalances], dtype=float)
    ns = np.array([len(r["short"]) for r in b.rebalances], dtype=float)
    return np.maximum(nl, ns)


def main():
    specs = [{"label": lb, "rebalance_bars": REBAL, "bar": BAR,
              "overrides": {"portfolio.top_k": k,
                            "portfolio.max_weight_per_instrument": cap},
              "save_result": book_path(lb)} for lb, k, cap in ARMS]
    specs = research.merge_ov(specs)

    cats = load_categories()
    insts, unknown = filter_insts(list_cached_insts(BAR), cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify: {unknown[:8]}")

    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=2, desc="Kxcap")
    bad = [(r["label"], r.get("error")) for r in res if r.get("error")]
    if bad:
        raise SystemExit("specs failed:\n  " + "\n  ".join(f"{k}: {v}" for k, v in bad))

    books = {}
    for lb, _, _ in ARMS:
        with open(book_path(lb), "rb") as f:
            books[lb] = pickle.load(f)

    # ---- 1. 宽度与退化占比 -------------------------------------------------
    # n=0（空书）既不是等权也不是 sizing，单列出来，避免它把占比算虚。
    print("\n=== 每侧选币数 / sizing 退化占比 ===", flush=True)
    wid = []
    for lb, k, cap in ARMS:
        mx = _side_width(books[lb])
        nonempty = mx[mx > 0]
        deg_all = float((cap * mx <= 1.0 + 1e-12).mean())
        deg_ne = float((cap * nonempty <= 1.0 + 1e-12).mean()) if len(nonempty) else np.nan
        wid.append({"arm": lb, "top_k": k, "cap": cap,
                    "median_width": float(np.median(nonempty)) if len(nonempty) else np.nan,
                    "n_empty": int((mx == 0).sum()),
                    "deg_share_all": deg_all, "deg_share_nonempty": deg_ne})
    wt = pd.DataFrame(wid)
    print(wt.to_string(index=False), flush=True)
    wt.to_csv(os.path.join(OUT, "width_degeneracy.csv"), index=False)

    # ---- 2. 全样本 --------------------------------------------------------
    print("\n=== 全样本 ===", flush=True)
    full = []
    for lb, k, cap in ARMS:
        m = compute_metrics(books[lb].bars, name=lb)
        full.append({"arm": lb, "top_k": k, "cap": cap, "Sharpe": m["Sharpe"],
                     "CAGR": m["CAGR"], "max_dd": m["Max Drawdown"],
                     "ann_vol": m["Annualized Volatility"],
                     "ann_turnover": m["Annual Turnover"],
                     "cost_drag": m["Cost Drag (annual)"]})
    ft = pd.DataFrame(full)
    ft["d_Sharpe_vs_ref"] = ft["Sharpe"] - float(
        ft.loc[ft["arm"] == REF, "Sharpe"].iloc[0])
    print(ft.to_string(index=False), flush=True)
    ft.to_csv(os.path.join(OUT, "full_sample.csv"), index=False)

    # ---- 3. 逐年 + 季度折 + 锁定样本 --------------------------------------
    yr = {}
    for lb, _, _ in ARMS:
        rows = {}
        for y, seg in books[lb].bars.groupby(books[lb].bars.index.year):
            seg = seg.copy()
            # 必须重算 equity —— bars["equity"] 是全样本曲线，直接切片会把
            # 全样本涨幅年化到子窗口，曾算出"2026 CAGR = 475%"这种假数。
            seg["equity"] = (1.0 + seg["net_ret"]).cumprod()
            rows[int(y)] = compute_metrics(seg, name=str(y))["Sharpe"]
        yr[lb] = pd.Series(rows)
    yt = pd.DataFrame(yr)
    print("\n=== 逐年 Sharpe ===", flush=True)
    print(yt.round(4).to_string(), flush=True)
    yt.to_csv(os.path.join(OUT, "yearly.csv"))

    qt = {}
    for lb, _, _ in ARMS:
        t = wf.fold_metrics_table([{"label": lb, "bars": books[lb].bars}],
                                  folds=wf.QUARTERLY_FOLDS)
        qt[lb] = t.set_index("fold")["test_sharpe"]
    qdf = pd.DataFrame(qt)
    print("\n=== 15 个季度折 test Sharpe ===", flush=True)
    print(qdf.round(4).to_string(), flush=True)
    qdf.to_csv(os.path.join(OUT, "quarterly.csv"))

    lo = wf.LOCKED_OOS
    lock = pd.DataFrame([{"arm": lb, "top_k": k, "cap": cap,
                          **wf.slice_metrics(books[lb].bars, *lo["window"], name=lo["name"])}
                         for lb, k, cap in ARMS])
    print("\n=== 锁定样本 2026 ===", flush=True)
    print(lock.to_string(index=False), flush=True)
    lock.to_csv(os.path.join(OUT, "locked.csv"), index=False)

    # ---- 4. 按 §11b 判据给结论 -------------------------------------------
    print("\n=== §11b 判据（所有 Δ 相对 K10_cap0.20 = v3）===", flush=True)
    refy, refq = yt[REF], qdf[REF]
    ref_lock = float(lock.loc[lock["arm"] == REF, "Sharpe"].iloc[0])
    rules = []
    for lb, k, cap in ARMS:
        if lb == REF:
            continue
        dy = (yt[lb] - refy).dropna()
        dq = (qdf[lb] - refq).dropna()
        big = dy.abs().idxmax()
        lock_s = float(lock.loc[lock["arm"] == lb, "Sharpe"].iloc[0])
        rules.append({
            "arm": lb, "d_Sharpe_full": float(ft.loc[ft["arm"] == lb, "Sharpe"].iloc[0]),
            "ann_pos_share": float((dy > 0).mean()),
            "largest_year": big,
            "largest_year_share": float(abs(dy.loc[big]) / dy.abs().sum()),
            "locked_Sharpe": lock_s,
            "locked_same_sign": bool(np.sign(lock_s - ref_lock) == np.sign(dy.mean())),
            "qtr_pos_share_info_only": float((dq > 0).mean()),
        })
    rt = pd.DataFrame(rules)
    rt["annual_gate_2of3"] = rt["ann_pos_share"] >= 2 / 3
    rt["concentration_gate_35pct"] = rt["largest_year_share"] <= 0.35
    rt["pass"] = (rt["annual_gate_2of3"] & rt["concentration_gate_35pct"]
                  & rt["locked_same_sign"])
    print(rt.round(4).to_string(index=False), flush=True)
    rt.to_csv(os.path.join(OUT, "rules.csv"), index=False)

    print(f"\nwrote {OUT}/")


if __name__ == "__main__":
    main()
