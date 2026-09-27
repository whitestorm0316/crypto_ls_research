"""补齐某个 tag 的走向前附加产物（18b 季度折 / 18c 锁定样本 / 18d 折汇总）。

用途：`stage_wf` 的季度折部分是后加的。如果某一轮回测是在这段代码写入**之前**
启动的，运行中的进程持有旧模块，产物里就不会有 18b/18c/18d —— 而报告要引用它们。

这三张表只依赖 `artifacts/<tag>/baseline.pkl` 的 `bars`（切片 + 指标），
不重训、不重选参数，所以直接补算与重跑 `wf` 阶段**结果等价**。

用法：python scripts/wf_extras.py v3
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crypto_ls_research.analysis import walkforward as wf    # noqa: E402

TAG = sys.argv[1] if len(sys.argv) > 1 else "v3"
ART = os.path.join(ROOT, "artifacts", TAG)
TBL = os.path.join(ART, "tables")


def main():
    picks = [p for p in (os.path.join(ART, "baseline.pkl"),
                         os.path.join(ART, "baseline", "baseline.pkl"))
             if os.path.exists(p)]
    if not picks:
        pp = os.path.join(ART, "baseline", "baseline.pkl")
        # run/research.py keep-baseline layout varies between tags
        raise SystemExit(f"no baseline.pkl under {ART} (also tried {pp})")
    with open(picks[0], "rb") as f:
        blob = pickle.load(f)
    res = blob["result"] if isinstance(blob, dict) and "result" in blob else blob
    bars = res.bars
    os.makedirs(TBL, exist_ok=True)
    print(f"{TAG}: {len(bars):,} bars  {bars.index[0].date()} -> {bars.index[-1].date()}")

    q = wf.fold_metrics_table([{"label": "baseline", "bars": bars}],
                              folds=wf.QUARTERLY_FOLDS)
    q.to_csv(os.path.join(TBL, "18b_walkforward_quarterly.csv"), index=False)
    print("\n=== 季度折 ===")
    print(q.to_string(index=False))

    te = q["test_sharpe"].dropna().to_numpy()
    summ = {"n_folds": int(te.size), "min": float(te.min()), "median": float(np.median(te)),
            "max": float(te.max()), "mean": float(te.mean()),
            "std": float(te.std(ddof=1)) if te.size > 1 else None,
            "share_positive": float((te > 0).mean()), "test_sharpe": te.tolist()}
    import json
    with open(os.path.join(TBL, "18d_quarterly_fold_summary.json"), "w") as f:
        json.dump(summ, f, indent=1)
    print(f"\n季度折汇总: n={summ['n_folds']}  min={summ['min']:.3f}  "
          f"median={summ['median']:.3f}  max={summ['max']:.3f}  "
          f"std={summ['std']:.3f}  正比例={summ['share_positive']:.2f}")

    lo = wf.LOCKED_OOS
    locked = wf.slice_metrics(bars, *lo["window"], name=lo["name"])
    pd.DataFrame([locked]).to_csv(os.path.join(TBL, "18c_locked_oos.csv"), index=False)
    print(f"\n锁定样本 {lo['name']}: Sharpe={locked['Sharpe']:.3f} "
          f"CAGR={locked['CAGR']:.4f} maxDD={locked['max_dd']:.4f} bars={locked['bars']}")
    print(f"\nwrote 18b/18c/18d -> {TBL}")


if __name__ == "__main__":
    main()
