"""实验：cap 的「宽度依赖」—— 静态 cap 在窄池下必然失效，测它有没有害。

背景
----
Tier-1 把 `max_weight_per_instrument` 从 0.10 提到 0.20，修好了 `cap*n == 1.0`
的静默等权。但 cap 是**静态**的，而池子宽度在 4..42 之间变动：
  n_universe=4  ->  每侧最多 2 个名字 -> cap*n=0.4 <= 1 -> 仍然等权。
而且此时**等权是唯一可行解**（5 个名字、cap=0.20 -> 每个至少 0.20 -> 正好铺满 1.0），
所以这不是 bug，是 cap 与池宽不匹配。

诊断结果（artifacts/v3/baseline.pkl）：688 个调仓点里 100 个（14.53%）退化，
全部集中在窄池年份 —— 2021 33.9% / 2022 30.3% / 2023 15.7% / 2026 6.7%，
而 2021-2023 恰好是 Sharpe 最弱的几年。

本实验回答：sizing 在窄池不生效，到底是不是一阶问题？
用 4 组对照：
  cap_degraded  : 保留静态 cap（v3 原样，含 14.5% 退化）
  cap_feasible  : 抬高静态 cap 让绝大多数调仓点可行
  cap_off       : 完全去掉 cap（sizing 恒生效，但失去集中度保护）
  widthslack    : 宽度自适应 cap = max(cap, (1+slack)/n)，保留 1.5x 等权上限

只读数据，结果只在内存里返回，不写任何 artifacts 目录。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crypto_ls_research.analysis.sweep import run_sweep, sweep_table   # noqa: E402
from crypto_ls_research.config.settings import BARS_PER_DAY            # noqa: E402
from crypto_ls_research.data.asset_class import filter_insts, load_categories  # noqa: E402
from crypto_ls_research.data.store import list_cached_insts            # noqa: E402
from crypto_ls_research.run import research                           # noqa: E402

BAR = "1h"
REBAL = int(round(3.0 * BARS_PER_DAY[BAR]))
START = "2021-01-01"
END = "2026-09-26"
OUT = os.path.join(ROOT, "artifacts", "capwidth")
os.makedirs(OUT, exist_ok=True)

# v3 的全局覆盖（换手预算 + 因子子集），cap 由各 spec 自己设
research.BASE_OVERRIDES = {
    "execution.max_daily_turnover": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}


def scoped_crypto_insts():
    cats = load_categories()
    pool = list_cached_insts(BAR)
    scoped, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify {len(unknown)} instruments: {unknown[:8]}")
    print(f"crypto scope: {len(pool)} -> {len(scoped)}")
    return scoped


def degenerate_frac(bars: pd.DataFrame, cap: float, slack: float = 0.0) -> dict:
    """Per-bar share of bars where the cap cannot accommodate the book."""
    if not {"n_long", "n_short"}.issubset(bars.columns):
        return {}
    n = np.maximum(bars["n_long"].to_numpy(dtype=float),
                   bars["n_short"].to_numpy(dtype=float))
    ok = np.isfinite(n)
    n = n[ok]
    if n.size == 0:
        return {}
    eff = cap
    if slack > 0:
        # width-aware: feasible whenever max(cap, (1+slack)/n) is applied
        deg = np.zeros_like(n, dtype=bool)
    else:
        deg = eff * n <= 1.0 + 1e-12
    return {"degen_bars_frac": float(deg.mean()),
            "n_side_med": float(np.median(n)),
            "n_side_min": float(n.min())}


def main():
    insts = scoped_crypto_insts()

    specs = [
        {"label": "cap0.10_old", "overrides": {"portfolio.max_weight_per_instrument": 0.10}},
        {"label": "cap0.20_v3", "overrides": {"portfolio.max_weight_per_instrument": 0.20}},
        {"label": "cap0.34", "overrides": {"portfolio.max_weight_per_instrument": 0.34}},
        {"label": "cap0.50", "overrides": {"portfolio.max_weight_per_instrument": 0.50}},
        {"label": "cap_off", "overrides": {"portfolio.max_weight_per_instrument": 1.0}},
        {"label": "widthslack0.50",
         "overrides": {"portfolio.max_weight_per_instrument": 0.20,
                       "portfolio.cap_width_slack": 0.50}},
        {"label": "widthslack1.00",
         "overrides": {"portfolio.max_weight_per_instrument": 0.20,
                       "portfolio.cap_width_slack": 1.00}},
    ]
    # run_sweep's worker builds its own cfg from the spec, so rebalance_bars must
    # travel with every spec -- forgetting it makes every spec die with a KeyError
    # that run_sweep catches and reports as a one-line warning.
    for s in specs:
        s["rebalance_bars"] = REBAL
    specs = research.merge_ov(specs)
    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=2, desc="capwidth")

    errs = [(r["label"], r.get("error") or r.get("error_x") or r.get("error_y"))
            for r in res if ("error" in r or "error_x" in r or "error_y" in r)]
    errs = [(l, e) for l, e in errs if e]
    if errs:
        raise SystemExit("sweep specs failed:\n  " +
                         "\n  ".join(f"{l}: {e}" for l, e in errs))

    tbl = sweep_table(res)
    caps = {"cap0.10_old": (0.10, 0.0), "cap0.20_v3": (0.20, 0.0), "cap0.34": (0.34, 0.0),
            "cap0.50": (0.50, 0.0), "cap_off": (1.0, 0.0),
            "widthslack0.50": (0.20, 0.50), "widthslack1.00": (0.20, 1.00)}
    extra = []
    for r in res:
        if "error" in r:
            extra.append({"label": r["label"], "sweep_error": r["error"]})
            continue
        cap, sl = caps.get(r["label"], (np.nan, 0.0))
        row = {"label": r["label"]}
        row.update(degenerate_frac(r.get("bars"), cap, sl))
        extra.append(row)
    ext = pd.DataFrame(extra)
    joined = tbl.merge(ext, on="label", how="left")

    col_order = [c for c in ["label", "Sharpe", "CAGR", "max_dd", "ann_vol", "ann_turnover",
                             "cost_drag", "degen_bars_frac", "n_side_med", "n_side_min",
                             "Sharpe_gross", "sweep_error"] if c in joined.columns]
    joined = joined[col_order]

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    print("\n=== cap width-dependence sweep (crypto pool, v3 overrides) ===")
    print(joined.to_string(index=False))
    joined.to_csv(os.path.join(OUT, "cap_width_sweep.csv"), index=False)

    with open(os.path.join(OUT, "cap_width_sweep.json"), "w") as f:
        json.dump({"bar": BAR, "rebalance_bars": REBAL, "start": START, "end": END,
                   "overrides": research.BASE_OVERRIDES,
                   "rows": joined.to_dict(orient="records")}, f, indent=1, default=str)
    print(f"\nwrote {OUT}/cap_width_sweep.csv")


if __name__ == "__main__":
    main()
