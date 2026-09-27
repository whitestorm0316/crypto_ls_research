"""实验：cap 的邻域细化 + 宽度自适应 cap 的稳健性。

第一轮（`scripts/exp_cap_width.py`）结论：
    cap   0.10  0.20  0.34  0.50  off
    Sh    1.839 1.937 1.760 1.690 1.728
→ cap=0.20 明显好于两侧，但集合里只有 5 个点，**无法区分「峰」和「平台」**。
路线图明确要求：「不要因 cap=0.20 最高就锁定它（非单调）」→ 必须看邻域。

同时测 `cap_width_slack` 的稳健性：slack=1.0 时 Sharpe 1.966 > 1.937，
但 +0.029 落在噪声量级，必须看它是不是孤峰，以及和 cap 的交互。

退化占比用 `sel_n_long/sel_n_short`（**选币数**）算，不是 `weight_matrix` 非零数。
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
START, END = "2021-01-01", "2026-09-26"
OUT = os.path.join(ROOT, "artifacts", "capwidth")
os.makedirs(OUT, exist_ok=True)

research.BASE_OVERRIDES = {
    "execution.max_daily_turnover": 0.2,
    "factors.subset": ["range_pos", "hitrate"],
}


def degenerate(r: dict, cap: float, slack: float, k: int) -> dict:
    """退化占比 = 选币数 n_side 使 cap_eff*n_side <= 1 的调仓点占比。

    cap_eff == max(cap, (1+slack)/n_side) when slack > 0, so slack>0 makes it feasible
    by construction; we still report how often the *original* cap would have failed,
    because that is the number that describes the pool, not the remedy.
    """
    nl = np.asarray(r.get("sel_n_long", []), dtype=float)
    ns = np.asarray(r.get("sel_n_short", []), dtype=float)
    if nl.size == 0 or ns.size == 0:
        return {}
    n = np.maximum(nl, ns)
    n = n[np.isfinite(n)]
    if n.size == 0:
        return {}
    would = cap * n <= 1.0 + 1e-12
    eff = cap if slack <= 0 else np.maximum(cap, (1.0 + slack) / np.maximum(n, 1e-9))
    still = eff * n <= 1.0 + 1e-12
    top = np.sort(n)[::-1]
    return {"sel_side_med": float(np.median(n)),
            "sel_side_min": float(n.min()),
            "cap_infeasible_frac": float(would.mean()),
            "degen_after_remedy": float(still.mean()),
            "cap_eff_for_min_n": float(min(1.0, max(cap, (1.0 + slack) / max(n.min(), 1e-9))))}


def main():
    cats = load_categories()
    pool = list_cached_insts(BAR)
    insts, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify: {unknown[:8]}")
    print(f"crypto scope: {len(pool)} -> {len(insts)}")

    specs: list[dict] = []
    for cap in (1 / 9, 0.13, 0.15, 0.18, 0.20, 0.22, 0.25, 0.30):
        specs.append({"label": f"cap{cap:.4f}", "cap": cap, "slack": 0.0,
                      "overrides": {"portfolio.max_weight_per_instrument": round(cap, 4)}})
    for slack in (0.25, 0.50, 0.75, 1.00, 1.50, 2.00):
        specs.append({"label": f"cap0.20_slack{slack:.2f}", "cap": 0.20, "slack": slack,
                      "overrides": {"portfolio.max_weight_per_instrument": 0.20,
                                    "portfolio.cap_width_slack": slack}})
    for cap, slack in ((0.15, 1.00), (0.25, 1.00)):
        specs.append({"label": f"cap{cap:.2f}_slack{slack:.2f}", "cap": cap, "slack": slack,
                      "overrides": {"portfolio.max_weight_per_instrument": cap,
                                    "portfolio.cap_width_slack": slack}})

    for s in specs:
        s["rebalance_bars"] = REBAL
        s["bar"] = BAR
    sweep_specs = research.merge_ov([{k: v for k, v in s.items() if k in
                                      ("label", "overrides", "rebalance_bars")}
                                     for s in specs])
    res = run_sweep(sweep_specs, BAR, START, END, insts=insts, n_jobs=3, desc="caprefine")

    bad = [(r["label"], r.get("error")) for r in res if r.get("error")]
    if bad:
        raise SystemExit("specs failed:\n  " + "\n  ".join(f"{k}: {v}" for k, v in bad))

    by_label = {r["label"]: r for r in res}
    tbl = sweep_table(res)
    extra = []
    for s in specs:
        r = by_label.get(s["label"])
        if r is None:
            continue
        row = {"label": s["label"], "cap": s["cap"], "slack": s["slack"]}
        row.update(degenerate(r, s["cap"], s["slack"], 0))
        extra.append(row)
    ext = pd.DataFrame(extra)
    j = tbl.merge(ext, on="label", how="left")

    grp = np.where(j["slack"] > 0, "width_aware", "static_cap")
    j.insert(1, "kind", grp)
    cols = ["label", "kind", "cap", "slack", "Sharpe", "Sharpe_gross", "CAGR", "max_dd",
            "ann_vol", "ann_turnover", "cost_drag", "sel_side_med", "sel_side_min",
            "cap_infeasible_frac", "degen_after_remedy", "cap_eff_for_min_n"]
    j = j[[c for c in cols if c in j.columns]]

    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 60)
    print("\n=== cap neighbourhood + width-aware cap ===")
    print(j.to_string(index=False))
    j.to_csv(os.path.join(OUT, "cap_refine.csv"), index=False)
    with open(os.path.join(OUT, "cap_refine.json"), "w") as f:
        json.dump({"overrides": research.BASE_OVERRIDES, "rows": j.to_dict(orient="records")},
                  f, indent=1, default=str)

    stat = j[j["kind"] == "static_cap"].sort_values("cap")
    w = stat[["cap", "Sharpe"]].to_numpy(dtype=float)
    print("\n静态 cap 邻域（cap, Sharpe）:")
    for c, s in w:
        print(f"  {c:.4f}  {s:.4f}")
    if len(w) > 2:
        i = int(np.argmax(w[:, 1]))
        nb = w[max(0, i - 1):i + 2]
        print(f"  峰值 cap={w[i,0]:.4f} Sharpe={w[i,1]:.4f}；"
              f"邻域 [{nb[0,1]:.4f}, {nb[-1,1]:.4f}] → "
              f"{'平台' if (w[i,1]-min(nb[:,1])) < 0.15 else '孤峰（邻域落差大）'}")
    print(f"\nwrote {OUT}/cap_refine.csv")


if __name__ == "__main__":
    main()
