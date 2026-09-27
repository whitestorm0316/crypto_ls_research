"""Capacity analysis: how much AUM can the signal carry before costs eat it?

The mechanism is explicit and mechanical: for a fixed weight vector, dollar notional
scales linearly with AUM, participation scales linearly with AUM, and the square-root
impact law makes the cost grow as sqrt(AUM).  Beyond the ADV participation cap the
book is additionally *truncated*, so realised gross exposure falls and alpha capture
degrades even before costs are counted.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

CAPITAL_LADDER: List[float] = [10_000, 50_000, 100_000, 500_000,
                               1_000_000, 5_000_000, 10_000_000]


def capital_specs(capitals: Optional[Sequence[float]] = None, base_overrides: Optional[dict] = None,
                  rebalance_bars: int = 96) -> List[dict]:
    caps = list(capitals or CAPITAL_LADDER)
    out = []
    for c in caps:
        ov = dict(base_overrides or {})
        ov["initial_capital"] = float(c)
        out.append({"label": f"capital={c:,.0f}", "overrides": ov,
                    "rebalance_bars": rebalance_bars, "keep_bars": True})
    return out


def capacity_table(results: Sequence[dict]) -> pd.DataFrame:
    rows = []
    for r in results:
        if "error" in r:
            rows.append({"label": r["label"], "capital": np.nan, "error": r["error"]})
            continue
        m = r["metrics"]
        cap = float(r["label"].split("=")[1].replace(",", ""))
        rows.append({
            "capital": cap,
            "CAGR": m["CAGR"], "Sharpe": m["Sharpe"], "ann_vol": m["Annualized Volatility"],
            "max_dd": m["Max Drawdown"], "ann_turnover": m["Annual Turnover"],
            "fee": m["Trading Fee (total, frac)"], "slippage": m["Slippage/Impact (total, frac)"],
            "spread": m["Spread Cost (total, frac)"], "funding": m["Funding P&L (total, frac)"],
            "cost_drag": m["Cost Drag (annual)"],
            "avg_gross": m["Gross Exposure (avg)"],
            "avg_max_participation": m["Avg Max Participation"],
        })
    df = pd.DataFrame(rows).sort_values("capital").reset_index(drop=True)
    if "CAGR" in df:
        gross_like = df["CAGR"] + df["cost_drag"]
        df["cost_share_of_gross"] = df["cost_drag"] / gross_like.replace(0, np.nan)
    return df


def capacity_breakeven(df: pd.DataFrame, metric: str = "Sharpe",
                       frac_of_peak: float = 0.5) -> Dict[str, float]:
    """AUM at which the metric falls to `frac_of_peak` of its small-capital peak."""
    d = df.dropna(subset=[metric, "capital"])
    if d.empty:
        return {"peak_metric": np.nan, "peak_capital": np.nan, "breakeven_capital": np.nan}
    peak = float(d[metric].max())
    peak_cap = float(d.loc[d[metric].idxmax(), "capital"])
    target = peak * frac_of_peak
    beyond = d[(d["capital"] > peak_cap) & (d[metric] <= target)]
    be = float(beyond["capital"].iloc[0]) if not beyond.empty else np.inf
    return {"peak_metric": peak, "peak_capital": peak_cap,
            f"capital_at_{int(frac_of_peak*100)}pct_of_peak": be,
            "note": "inf means the metric never decays that far within the tested ladder"}
