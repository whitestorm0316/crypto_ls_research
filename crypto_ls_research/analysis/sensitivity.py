"""Parameter sensitivity: grids, one-factor-at-a-time sweeps, and an over-fitting test.

The purpose is explicitly NOT to find the best parameter set.  It is to find whether
there is a *plateau*: a region of parameter space where neighbouring cells all work.
A single hot cell surrounded by losers is flagged as Potential Overfitting.
"""
from __future__ import annotations

from itertools import product
from typing import Dict, Iterable, List, Sequence

import numpy as np
import pandas as pd

# Spec-mandated grids, expressed in the spec's own units and converted to days
# (bars @15m / 96 == days).
GRIDS: Dict[str, Dict[str, list]] = {
    "factors.mom_lookback_days": {"label": "momentum_lookback_bars15m",
                                  "values": [252, 378, 504, 720, 1008],
                                  "to_days": lambda v: v / 96.0},
    "factors.flow_short_days": {"label": "flow_short_bars15m",
                                "values": [24, 48, 96, 192],
                                "to_days": lambda v: v / 96.0},
    "factors.flow_long_days": {"label": "flow_long_bars15m",
                               "values": [672, 1440, 2880],
                               "to_days": lambda v: v / 96.0},
    "factors.range_days": {"label": "range_bars15m",
                           "values": [360, 720, 1440],
                           "to_days": lambda v: v / 96.0},
    "factors.hitrate_days": {"label": "hitrate_bars15m",
                             "values": [96, 192, 504],
                             "to_days": lambda v: v / 96.0},
    "portfolio.top_k": {"label": "top_k", "values": [5, 10, 15, 20],
                        "to_days": lambda v: v},
    "portfolio.min_abs_score": {"label": "min_abs_score",
                                "values": [0.0, 0.25, 0.5, 0.75, 1.0],
                                "to_days": lambda v: v},
    "risk.target_vol_annual": {"label": "target_vol", "values": [0.20, 0.30, 0.40],
                               "to_days": lambda v: v},
    "execution.max_daily_turnover": {"label": "max_daily_turnover",
                                     "values": [0.20, 0.50, 1.00, 2.00],
                                     "to_days": lambda v: v},
    "costs.funding_multiplier": {"label": "funding_mult", "values": [0.0, 1.0, 1.5, 2.0],
                                 "to_days": lambda v: v},
}


def build_specs(key: str, cfg) -> List[dict]:
    g = GRIDS[key]
    return [{"label": f"{g['label']}={v}", "overrides": {key: g["to_days"](v)},
             "rebalance_bars": cfg.rebalance_bars}
            for v in g["values"]]


def build_pair_grid(key_a: str, key_b: str, cfg, name_a: str, name_b: str
                    ) -> Tuple[List[dict], List, List]:
    ga, gb = GRIDS[key_a], GRIDS[key_b]
    specs, xs, ys = [], [], []
    for va in ga["values"]:
        ys.append(va)
    for vb in gb["values"]:
        xs.append(vb)
    for va in ga["values"]:
        for vb in gb["values"]:
            specs.append({
                "label": f"{name_a}={va}|{name_b}={vb}",
                "overrides": {key_a: ga["to_days"](va), key_b: gb["to_days"](vb)},
                "rebalance_bars": cfg.rebalance_bars,
            })
    return specs, xs, ys


def grid_from_results(results: Sequence[dict], key_a: str, key_b: str,
                      metric: str = "Sharpe", name_a: str = "A", name_b: str = "B"
                      ) -> pd.DataFrame:
    rows = {}
    for r in results:
        if "error" in r:
            continue
        parts = dict(p.split("=") for p in r["label"].split("|"))
        va, vb = float(parts[name_a]), float(parts[name_b])
        rows.setdefault(vb, {})[va] = r["metrics"][metric]
    df = pd.DataFrame(rows).T
    df.index.name = name_b
    df.columns.name = name_a
    return df.sort_index().sort_index(axis=1)


def plateau_score(grid: pd.DataFrame, positive_is_good: bool = True) -> Dict[str, float]:
    """Quantify whether performance sits on a plateau rather than a spike.

    * `frac_neighbours_ok`  -- share of cells whose metric is within 50% of the best
    * `spike_ratio`         -- best / median ; >2.5 with a small ok-fraction => spike
    """
    v = grid.to_numpy(dtype="float")
    ok = np.isfinite(v)
    if ok.sum() < 4:
        return {"frac_neighbours_ok": np.nan, "spike_ratio": np.nan, "verdict": "insufficient"}
    f = v[ok]
    best = np.nanmax(f)
    med = np.nanmedian(f)
    thresh = best * 0.5 if positive_is_good else best * 1.5
    frac = float(np.mean(f >= thresh)) if positive_is_good else float(np.mean(f <= thresh))
    ratio = float(best / med) if med and abs(med) > 1e-9 else np.nan
    verdict = "plateau" if frac >= 0.5 else ("spike -> suspect overfitting" if frac < 0.3
                                             else "mixed")
    return {"best": float(best), "median": float(med), "frac_neighbours_ok": frac,
            "spike_ratio": ratio, "verdict": verdict}


def multiplicity_summary(table: pd.DataFrame, metric: str = "Sharpe") -> pd.DataFrame:
    """How much of the headline Sharpe survives a multiple-testing haircut?

    Reported: the best / median / 25th-percentile metric across ALL tested
    configurations.  If the median across a large grid is near zero, the headline is
    a selection artefact rather than an edge.
    """
    v = pd.to_numeric(table[metric], errors="coerce").dropna()
    return pd.DataFrame([{
        "n_configs": len(v), "best": v.max(), "q90": v.quantile(0.90),
        "median": v.median(), "q25": v.quantile(0.25), "worst": v.min(),
        "share_positive": float((v > 0).mean()),
        "sharpe_haircut_median": v.median(),
        "deflated_haircut": v.max() - (v.max() - v.median()) * 0.5,
    }])
