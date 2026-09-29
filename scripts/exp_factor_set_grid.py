"""Factor-set x rebalance-grid cross-product on the current acceptance dataset.

Why this exists
---------------
The console lets you tick all five factors, while the accepted configuration uses only
two (`range_pos`, `hitrate`).  The obvious question -- "aren't all five better?" --
cannot be answered from what is already on disk:

  * `artifacts/v4_1d/tables/34_neutralisation_study.csv` holds the three factor sets
    but **only at the 1-day grid**, and `sweep.sweep_table` carries **no win rate**,
    so the "胜率更高" half of the claim is unmeasurable from it;
  * the 3-day factor-set comparison exists in `OPTIMIZATION_RESULTS.md` §3.3/§4.1,
    but those numbers **predate the market-data rebuild** -- the same file records the
    `v2` row as 1.937 while `artifacts/v3/` measured 1.758 -- so they are not
    comparable to anything currently on disk.

So this runs the cross-product in one process, on the current dataset, with the
acceptance scope and overrides held fixed, and reports the win rate **next to** the
risk-adjusted metrics.  CAGR alone cannot settle it: the 1-day grid already buys its
CAGR with 2.22x the drawdown of the 3-day grid, so "CAGR is higher" is not by itself
evidence of anything (`backtest-validation` 闸门 22 / 闸门 5).

What is held fixed
------------------
`bar=1h`, `asset_class=crypto`, window `2021-01-01..2026-09-26`, and the accepted
overrides (`portfolio.max_weight_per_instrument=0.20`,
`execution.max_daily_turnover=0.20`).  **Only** the factor set and the rebalance grid
vary, so every difference below is attributable to one of those two knobs.

Usage
-----
    python scripts/exp_factor_set_grid.py            # all 5 arms
    python scripts/exp_factor_set_grid.py --n-jobs 5
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.analysis.sweep import run_sweep  # noqa: E402
from crypto_ls_research.config.settings import BARS_PER_DAY  # noqa: E402
from crypto_ls_research.data.asset_class import (  # noqa: E402
    filter_insts, load_categories)
from crypto_ls_research.data.store import list_cached_insts  # noqa: E402
from crypto_ls_research.run.research import merge_ov, save, set_tag  # noqa: E402

BAR = "1h"
START, END = "2021-01-01", "2026-09-26"

# The accepted overrides, verbatim from `webapp.spec.OPTIMAL_OVERRIDES`.  Held fixed
# so the factor set / grid is the only thing that moves.
ACCEPTED_OVERRIDES = {
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}

FACTOR_SETS = {
    "v2(rp,hr)": ("range_pos", "hitrate"),
    "spec4(mom,flow,rp,hr)": ("momentum", "flow", "range_pos", "hitrate"),
    "all5(+rev_short)": ("momentum", "flow", "range_pos", "hitrate", "rev_short"),
}

GRIDS_DAYS = (1.0, 3.0)

# `construct.inverse_vol_weights` degrades to **equal weight** whenever `cap * n <= 1`,
# which at the accepted `cap = 0.20` means any side selecting <= 5 names.  That matters
# here: this project already measured that in a narrow cross-section equal weight is
# itself a risk control ("等权本身就是风控"), so a factor set that merely *selects fewer
# names per side* would post a better Sharpe with no factor information in it.  The
# equal-weight share is therefore reported next to every arm -- if the 5-factor gain
# tracks the equal-weight share, the gain is a sizing artifact, not a factor.
CAP = ACCEPTED_OVERRIDES["portfolio.max_weight_per_instrument"]


def _eqw_stats(r: dict) -> dict:
    """Share of rebalance points where the cap is infeasible (book is equal-weighted)."""
    nl = np.asarray(r.get("sel_n_long", []), dtype="float64")
    ns = np.asarray(r.get("sel_n_short", []), dtype="float64")
    n = min(nl.size, ns.size)
    if n == 0:
        return {"sel_n_long_med": np.nan, "sel_n_short_med": np.nan,
                "frac_eqw_either": np.nan, "frac_eqw_both": np.nan}
    nl, ns = nl[:n], ns[:n]
    deg_l = CAP * nl <= 1.0 + 1e-12
    deg_s = CAP * ns <= 1.0 + 1e-12
    return {
        "sel_n_long_med": float(np.median(nl)),
        "sel_n_short_med": float(np.median(ns)),
        "frac_eqw_either": float((deg_l | deg_s).mean()),
        "frac_eqw_both": float((deg_l & deg_s).mean()),
    }


def _scope_crypto(bar: str):
    """Reproduce `research.main()`'s `--asset-class crypto` scoping exactly."""
    cats = load_categories()
    pool = list_cached_insts(bar)
    scoped, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify {len(unknown)} cached instrument(s): "
                         f"{unknown[:8]}...  refresh with "
                         f"`python -m crypto_ls_research.data.asset_class --build`")
    print(f"### scope: crypto keeps {len(scoped)}/{len(pool)} cached instruments",
          flush=True)
    return scoped


def _row(label: str, r: dict) -> dict:
    m = r["metrics"]
    row = {
        "label": label,
        "CAGR": m["CAGR"],
        "ann_vol": m["Annualized Volatility"],
        "Sharpe": m["Sharpe"],
        "Sortino": m["Sortino"],
        "Calmar": m["Calmar"],
        "max_dd": m["Max Drawdown"],
        "ann_turnover": m["Annual Turnover"],
        "cost_drag": m["Cost Drag (annual)"],
        # The fields `sweep_table` drops, and the reason this script exists:
        "win_rate_daily": m["Win Rate (daily)"],
        "profit_factor_daily": m["Profit Factor (daily)"],
        "long_win_rate_bar": m["Long Win Rate (bar)"],
        "short_win_rate_bar": m["Short Win Rate (bar)"],
        "avg_gross": m["Gross Exposure (avg)"],
    }
    row.update(_eqw_stats(r))
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=5)
    ap.add_argument("--tag", default="factor_set_grid")
    a = ap.parse_args()

    set_tag(a.tag)
    insts = _scope_crypto(BAR)

    specs = []
    for grid in GRIDS_DAYS:
        rebal = max(1, int(round(grid * BARS_PER_DAY[BAR])))
        for name, sub in FACTOR_SETS.items():
            # 5 arms x 2 grids = 6, but the 5-factor set at the 3-day grid and the
            # 2-factor set at both grids are the ones the claim actually needs; run
            # the full cross-product so no cell can be quietly missing.
            specs.append({
                "label": f"{name}@{grid:g}d",
                "overrides": dict(ACCEPTED_OVERRIDES),
                "kwargs": {"factor_subset": sub},
                "rebalance_bars": rebal,
            })
    specs = merge_ov(specs)

    print(f"### {len(specs)} arms x 1 dataset, n_jobs={a.n_jobs}", flush=True)
    t0 = time.time()
    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=a.n_jobs,
                    desc="factor_set_grid")
    print(f"### sweep took {time.time() - t0:.0f}s", flush=True)

    rows = [_row(r["label"], r) for r in res if "error" not in r]
    for r in res:
        if "error" in r:
            print(f"  !! {r['label']} FAILED: {r['error']}", flush=True)
    t = pd.DataFrame(rows).sort_values("Sharpe", ascending=False)
    save(t, "exp_factor_set_grid")

    pd.set_option("display.width", 250)
    print("\n=== factor set x grid (all columns from the same sweep) ===", flush=True)
    print(t[["label", "CAGR", "Sharpe", "max_dd", "ann_turnover", "cost_drag",
             "win_rate_daily"]].to_string(index=False), flush=True)

    print("\n=== the confound: is the gain just the equal-weight degeneracy? ===", flush=True)
    print(t[["label", "Sharpe", "win_rate_daily", "profit_factor_daily",
             "sel_n_long_med", "sel_n_short_med", "frac_eqw_either",
             "frac_eqw_both"]].to_string(index=False), flush=True)

    # The paired read the claim is actually about: same grid, 5 factors vs 2.
    print("\n=== paired delta (all5 minus v2, same grid) ===", flush=True)
    for grid in GRIDS_DAYS:
        try:
            five = t[t.label == f"all5(+rev_short)@{grid:g}d"].iloc[0]
            two = t[t.label == f"v2(rp,hr)@{grid:g}d"].iloc[0]
        except IndexError:
            print(f"  {grid:g}d: arm missing", flush=True)
            continue
        print(f"  {grid:g}d  dSharpe {five.Sharpe - two.Sharpe:+.4f}   "
              f"dCAGR {five.CAGR - two.CAGR:+.4f}   "
              f"dMDD {five.max_dd - two.max_dd:+.4f}   "
              f"dWinRate {five.win_rate_daily - two.win_rate_daily:+.4f}   "
              f"dProfitFactor {five.profit_factor_daily - two.profit_factor_daily:+.4f}   "
              f"dEqwEither {five.frac_eqw_either - two.frac_eqw_either:+.4f}", flush=True)


if __name__ == "__main__":
    main()
