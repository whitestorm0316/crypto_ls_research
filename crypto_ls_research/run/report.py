"""Assemble everything in artifacts/<tag>/ into a single REPORT.md.

The data sections are generated from the saved tables so the numbers can never drift
from the backtest; the interpretation section is authored (see `NARRATIVE`).
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional

import pandas as pd

ART_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "..", "artifacts"))


def rd(tab_dir: str, name: str, index: bool = False) -> Optional[pd.DataFrame]:
    p = os.path.join(tab_dir, name)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p, index_col=0 if index else None)
    except Exception:                                            # noqa: BLE001
        return None


def md_table(df: pd.DataFrame, floatfmt: str = "{:.4f}", max_rows: int = 40,
             pct_cols: Optional[List[str]] = None) -> str:
    if df is None or df.empty:
        return "_(no data)_\n"
    d = df.head(max_rows).copy()
    pct_cols = pct_cols or []
    for c in d.columns:
        if d[c].dtype.kind in "fc":
            if c in pct_cols:
                d[c] = d[c].map(lambda v: f"{v:.2%}" if pd.notna(v) else "")
            else:
                d[c] = d[c].map(lambda v: f"{v:,.4f}" if pd.notna(v) else "")
    lines = ["| " + " | ".join(str(c) for c in d.columns) + " |",
             "|" + "|".join("---" for _ in d.columns) + "|"]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(x) for x in r.tolist()) + " |")
    if len(df) > max_rows:
        lines.append(f"\n_({len(df) - max_rows} more rows in the CSV)_")
    return "\n".join(lines) + "\n"


def sec(title: str, body: str, level: int = 2) -> str:
    return f"\n{'#' * level} {title}\n\n{body}\n"


def build(tag: str = "") -> str:
    art = os.path.join(ART_ROOT, tag) if tag else ART_ROOT
    tab = os.path.join(art, "tables")
    chart = os.path.join(art, "charts")
    meta = json.load(open(os.path.join(tab, "00_run_meta.json"))) if \
        os.path.exists(os.path.join(tab, "00_run_meta.json")) else {}

    out: List[str] = []
    out.append("# Crypto Futures Liquidity-Flow Trend Market-Neutral Strategy\n")
    out.append(f"_Research artifacts: `{os.path.relpath(art, os.path.dirname(ART_ROOT))}`  "
               f"· {meta.get('n_rebalances', '?')} rebalances · "
               f"execution `{meta.get('exec_price', '?')}`_\n")

    out.append(sec("1. Headline", md_table(rd(tab, "01_headline_metrics.csv"),
                                           pct_cols=["CAGR", "Annualized Volatility",
                                                     "Max Drawdown", "Win Rate (daily)"])))
    out.append(sec("2. Cost decomposition (the decisive table)",
                   "The same position path valued under four cost assumptions.\n\n"
                   + md_table(rd(tab, "01b_cost_scenarios.csv"),
                              pct_cols=["CAGR", "ann_vol", "max_dd", "CAGR_gap_vs_gross"])))
    out.append(sec("3. Long / short attribution",
                   md_table(rd(tab, "02_long_short_attribution.csv"),
                            pct_cols=["total_return", "CAGR", "ann_vol", "max_dd",
                                      "win_rate_daily"])))
    out.append(sec("4. Beta neutralisation: Method A vs Method B",
                   md_table(rd(tab, "06_beta_neutralisation.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd"])))
    out.append(sec("5. Execution model comparison",
                   md_table(rd(tab, "05_execution_models.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd"])))
    out.append(sec("6. Funding sensitivity",
                   md_table(rd(tab, "07_funding_sensitivity.csv"),
                            pct_cols=["CAGR", "funding_pnl_total", "cost_drag_annual"])))
    out.append(sec("7. Rebalance frequency: is high frequency just fee generation?",
                   md_table(rd(tab, "08_rebalance_frequency.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd", "CAGR_gross",
                                      "CAGR_zero_fee"])))
    out.append(sec("8. Turnover budget ladder",
                   md_table(rd(tab, "09_turnover_budget.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd", "CAGR_gross"])))
    out.append(sec("9. Factor ablation (which factor carries the signal?)",
                   md_table(rd(tab, "10_factor_ablation.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd", "CAGR_gross"])))
    out.append(sec("10. Rank IC",
                   "**Score IC by horizon (executable convention)**\n\n"
                   + md_table(rd(tab, "11_rank_ic_by_horizon.csv"))
                   + "\n**IC by factor x horizon**\n\n"
                   + md_table(rd(tab, "12_rank_ic_by_factor.csv"), max_rows=60)
                   + "\n**Factor correlation (mean cross-sectional Spearman)**\n\n"
                   + md_table(rd(tab, "14_factor_correlation.csv", index=True))))
    out.append(sec("11. Parameter sensitivity",
                   md_table(rd(tab, "16_multiplicity_summary.csv"))))
    out.append(sec("12. Walk-forward / period stability",
                   md_table(rd(tab, "18_walkforward_folds.csv"),
                            pct_cols=["train_cagr", "test_cagr", "test_maxdd"])
                   + "\n"
                   + md_table(rd(tab, "19_period_stability.csv"),
                              pct_cols=["CAGR", "ann_vol", "max_dd"])))
    mc = None
    p = os.path.join(tab, "22_montecarlo_summary.json")
    if os.path.exists(p):
        mc = json.load(open(p))
    if mc:
        rows = []
        for k, v in mc.items():
            s = v.get("sharpe", {})
            rows.append({"null_hypothesis": k, "n_permutations": s.get("n"),
                         "placebo_mean_sharpe": s.get("placebo_mean"),
                         "placebo_std": s.get("placebo_std"),
                         "strategy_sharpe": s.get("real"),
                         "percentile": s.get("percentile"),
                         "p_value": s.get("p_value"),
                         "significant_5pct": s.get("significant_5pct")})
        out.append(sec("13. Monte-Carlo nulls", md_table(pd.DataFrame(rows))))
    out.append(sec("14. Capacity",
                   md_table(rd(tab, "23_capacity.csv"),
                            pct_cols=["CAGR", "ann_vol", "max_dd", "CAGR_gross"])
                   + "\n"
                   + md_table(rd(tab, "23b_capacity_breakeven.csv"))))
    out.append(sec("15. Hidden-bias audit",
                   md_table(rd(tab, "25_bias_audit.csv"), max_rows=20)))
    ds = rd(tab, "24_delisting_stress.csv")
    if ds is not None:
        out.append(sec("16. Delisting stress test",
                       md_table(ds.describe().T.reset_index().rename(columns={"index": "stat"}),
                                max_rows=20), level=3))

    out.append(sec("17. Charts", "\n".join(
        f"- `{f}`" for f in sorted(os.listdir(chart)) if f.endswith(".png"))
        if os.path.isdir(chart) else "_(none)_"))

    rp = os.path.join(art, "REPORT.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"wrote {rp}")
    return rp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    build(ap.parse_args().tag)
