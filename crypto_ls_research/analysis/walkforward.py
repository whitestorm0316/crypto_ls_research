"""Walk-forward / stability testing.

Two distinct tests live here, and they answer different questions:

1. `fold_stability` -- does the *same* parameter set keep working across sub-periods?
   This is the honest test for a non-parametric rule, because nothing is fitted.

2. `walk_forward_selection` -- pick the best parameter set on the training window only,
   then measure it on the untouched test window.  This is the real over-fitting test:
   the gap between in-sample-best and out-of-sample-realised is the over-fitting tax.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..config.settings import BacktestConfig
from .metrics import compute_metrics

# Anchored walk-forward, matching the spec's example.
DEFAULT_FOLDS: List[dict] = [
    {"name": "T2023", "train": ("2021-01-01", "2022-12-31"), "test": ("2023-01-01", "2023-12-31")},
    {"name": "T2024", "train": ("2021-01-01", "2023-12-31"), "test": ("2024-01-01", "2024-12-31")},
    {"name": "T2025", "train": ("2021-01-01", "2024-12-31"), "test": ("2025-01-01", "2025-12-31")},
    {"name": "T2026", "train": ("2021-01-01", "2025-12-31"), "test": ("2026-01-01", "2026-09-26")},
]

# Pure out-of-sample slices: same parameters, different eras.
STABILITY_WINDOWS: List[dict] = [
    {"name": "2021", "window": ("2021-01-01", "2021-12-31")},
    {"name": "2022", "window": ("2022-01-01", "2022-12-31")},
    {"name": "2023", "window": ("2023-01-01", "2023-12-31")},
    {"name": "2024", "window": ("2024-01-01", "2024-12-31")},
    {"name": "2025", "window": ("2025-01-01", "2025-12-31")},
    {"name": "2026H1+", "window": ("2026-01-01", "2026-09-26")},
]

# Quarterly folds: four annual folds are too few to tell "stable" from "lucky".
# Twelve rolling quarters give a distribution of out-of-sample Sharpe rather than
# four points, and the era mix inside each fold is more homogeneous.
# Anchored training windows (train always starts 2021-01-01), matching DEFAULT_FOLDS.
_QUARTER_ENDS = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
                 "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
                 "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
                 "2026-03-31", "2026-06-30", "2026-09-26"]
QUARTERLY_FOLDS: List[dict] = []
_prev_end = "2022-12-31"
for _end in _QUARTER_ENDS:
    _start = (pd.Timestamp(_prev_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    QUARTERLY_FOLDS.append({
        "name": f"{pd.Timestamp(_end).year}Q{pd.Timestamp(_end).quarter}",
        "train": ("2021-01-01", _prev_end),
        "test": (_start, _end),
    })
    _prev_end = _end

# The slice that never participates in any parameter choice.  Reported separately so
# the claim "the edge survives untouched data" is checkable rather than asserted.
LOCKED_OOS: dict = {"name": "2026_locked", "window": ("2026-01-01", "2026-09-26")}


def slice_bars(bars: pd.DataFrame, t0: str, t1: str) -> pd.DataFrame:
    lo, hi = pd.Timestamp(t0, tz="UTC"), pd.Timestamp(t1, tz="UTC") + pd.Timedelta(days=1)
    seg = bars.loc[(bars.index >= lo) & (bars.index < hi)].copy()
    if len(seg) < 10:
        return seg
    seg["equity"] = (1 + seg["net_ret"]).cumprod()
    seg["drawdown"] = seg["equity"] / seg["equity"].cummax() - 1.0
    return seg


def slice_metrics(bars: pd.DataFrame, t0: str, t1: str, name: str = "") -> Dict[str, float]:
    seg = slice_bars(bars, t0, t1)
    if len(seg) < 10:
        return {"name": name, "CAGR": np.nan, "Sharpe": np.nan, "max_dd": np.nan,
                "ann_vol": np.nan, "bars": len(seg)}
    m = compute_metrics(seg, name=name)
    return {"name": name, "bars": len(seg), "CAGR": m["CAGR"],
            "ann_vol": m["Annualized Volatility"], "Sharpe": m["Sharpe"],
            "Sortino": m["Sortino"], "max_dd": m["Max Drawdown"], "Calmar": m["Calmar"],
            "ann_turnover": m["Annual Turnover"], "cost_drag": m["Cost Drag (annual)"]}


def stability_table(results: Sequence[dict], windows=None) -> pd.DataFrame:
    """results = sweep output (each with a `bars` frame).  One row per window per config."""
    windows = windows or STABILITY_WINDOWS
    rows = []
    for r in results:
        if "error" in r or "bars" not in r:
            continue
        for w in windows:
            rows.append({"config": r["label"], "window": w["name"],
                         **slice_metrics(r["bars"], *w["window"], name=w["name"])})
    return pd.DataFrame(rows)


def fold_metrics_table(results: Sequence[dict], folds=None) -> pd.DataFrame:
    folds = folds or DEFAULT_FOLDS
    rows = []
    for r in results:
        if "error" in r or "bars" not in r:
            continue
        for f in folds:
            tr = slice_metrics(r["bars"], *f["train"], name="train")
            te = slice_metrics(r["bars"], *f["test"], name="test")
            rows.append({
                "config": r["label"], "fold": f["name"],
                "train_sharpe": tr["Sharpe"], "train_cagr": tr["CAGR"],
                "test_sharpe": te["Sharpe"], "test_cagr": te["CAGR"],
                "test_maxdd": te["max_dd"], "test_ann_vol": te["ann_vol"],
                "is_oos_decay": tr["Sharpe"] - te["Sharpe"],
            })
    return pd.DataFrame(rows)


def walk_forward_selection(results: Sequence[dict], folds=None,
                           metric: str = "Sharpe") -> pd.DataFrame:
    """Select the best config on train, report its test performance, per fold.

    A large positive mean `is_oos_decay` means the selection step is manufacturing
    performance that does not survive.
    """
    folds = folds or DEFAULT_FOLDS
    rows = []
    for f in folds:
        scored = []
        for r in results:
            if "error" in r or "bars" not in r:
                continue
            tr = slice_metrics(r["bars"], *f["train"], name="train")
            te = slice_metrics(r["bars"], *f["test"], name="test")
            if np.isfinite(tr.get(metric, np.nan)) and np.isfinite(te.get(metric, np.nan)):
                scored.append((tr[metric], te[metric], r["label"], tr, te))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        med = float(np.median([s[1] for s in scored]))
        rows.append({
            "fold": f["name"], "n_candidates": len(scored),
            "best_on_train": best[2],
            "train_sharpe": best[3]["Sharpe"], "test_sharpe": best[4]["Sharpe"],
            "train_cagr": best[3]["CAGR"], "test_cagr": best[4]["CAGR"],
            "test_maxdd": best[4]["max_dd"],
            "oos_decay": best[3]["Sharpe"] - best[4]["Sharpe"],
            "test_sharpe_median_of_all": med,
            "selection_edge_vs_median": best[4]["Sharpe"] - med,
        })
    return pd.DataFrame(rows)
