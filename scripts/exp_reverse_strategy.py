"""Run the accepted strategy **backwards** -- the falsification test for direction.

Why this exists
---------------
The user asked: "if I take my strategy and do the opposite -- buy what it wants to
short, short what it wants to buy -- what happens?"

That is not a curiosity, it is the sharpest single test of whether the book's
return comes from **direction** at all:

  * if the signal carries real directional information, the mirrored book must
    **lose**, and by construction it must lose *more* than the original gains,
    because the cost is paid on the same turnover in both directions;
  * if the mirrored book also makes money, then whatever the original earned was
    not directional -- it was exposure, drift, or a sizing artifact.

The arithmetic is exact and can be predicted before running anything.  With
`net = gross - cost + funding` per bar (the ledger identity this project already
verifies to ~1e-18), and with the mirrored book holding **the same names at the
same |weights| with every side flipped**:

    gross_flip   = -gross           (identical book, opposite direction)
    cost_flip    =  cost            (identical turnover)
    funding_flip = -funding         (short legs pay what the long legs earned)

so

    net_flip = -gross - cost - funding = -(net + 2 * cost)

i.e. the mirror should lose the original's net P&L **plus twice the cost**.
`36b_reverse_identity.csv` measures each of those equalities separately, so a
deviation can be attributed rather than hand-waved.  One row is expected to be
non-zero: `impact` cost is computed as `impact_rate(delta, adv, **equity**, vol)`,
and the two arms end up with different equity (one compounds up, one down), so the
impact term is *not* mirrored.  `fee` and `spread` are proportional to `|delta|` and
mirror exactly.  That residual is a property of the equity-dependent impact model,
not of the book.

Note on the side rows: `long_ret`/`short_ret` in the ledger are *signed* P&L
contributions keyed on the sign of the held position
(`long_ret = sum(held*(held>0)*r)`).  Flipping the book therefore maps
`long_flip -> -short_normal`, not `long_flip -> +short_normal`.  Getting that sign
wrong is exactly the kind of thing this table exists to catch -- it caught it here.

What "flip" means here (and what it does NOT)
---------------------------------------------
`run_backtest(flip_book=True)` negates the **assembled target**, after
`select_book`, `build_units` and `side_gross_targets` have run.  So the selected
set and the sizes are bit-identical to the normal arm; only the direction of each
leg changes.  This is deliberately *not* `score = -score`, which would also move
the selection (the incumbency buffer in `_stable_book` is path-dependent) and make
the comparison confounded.

Two levels of "backwards", and they are NOT the same thing
----------------------------------------------------------
* `A/B`  -- as traded, risk overlays ON.  This is the honest answer to "what if I
  actually run it backwards": the reversed book has its own equity path, so
  `dd_scale` / `vol_target_scale` / `turnover_budget_scale` throttle it
  differently.  In the smoke run the reversed book's average gross exposure came
  out at **0.34 vs 0.65** for the normal book -- it gets shrunk as it bleeds.
  So `A` vs `B` is *not* an exact mirror, and the identity table quantifies that.
* `A1/B1` -- overlays OFF.  Here nothing path-dependent is left, so
  `gross_flip == -gross_normal` must hold to machine precision.  **This is the arm
  that proves the two books are the same book.**  Without it, "exact mirror" would
  just be an assertion about my own arithmetic.
* `A2/B2` -- overlays OFF and costs OFF, i.e. the pure gross mirror.

Usage
-----
    python scripts/exp_reverse_strategy.py
    python scripts/exp_reverse_strategy.py --n-jobs 4
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
from crypto_ls_research.analysis.walkforward import stability_table  # noqa: E402
from crypto_ls_research.config.settings import (  # noqa: E402
    ACCEPTED_FACTORS, ACCEPTED_REBALANCE_DAYS, BARS_PER_DAY)
from crypto_ls_research.data.asset_class import (  # noqa: E402
    filter_insts, load_categories)
from crypto_ls_research.data.store import list_cached_insts  # noqa: E402
from crypto_ls_research.run.research import merge_ov, save, set_tag  # noqa: E402

BAR = "1h"
START, END = "2021-01-01", "2026-09-26"

# The accepted overrides, verbatim from `webapp.spec.OPTIMAL_OVERRIDES` -- the same
# constants `exp_factor_set_grid.py` holds fixed.  Nothing here may drift from
# `config.settings`, or the "normal" arm stops reproducing the accepted headline.
ACCEPTED_OVERRIDES = {
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}

COST_COLS = ("fee", "spread", "impact")

# The arms, as (label, extra run_backtest kwargs, extra config overrides).
ARMS = [
    ("A  正常（照做）",              {}, {}),
    ("B  反过来（照做）",            {"flip_book": True}, {}),
    ("A1 正常·关风控",              {"disable_risk_overlays": True}, {}),
    ("B1 反过来·关风控",            {"disable_risk_overlays": True, "flip_book": True}, {}),
    ("A2 正常·关风控·零成本",        {"disable_risk_overlays": True, "disable_costs": True}, {}),
    ("B2 反过来·关风控·零成本",      {"disable_risk_overlays": True, "disable_costs": True,
                                     "flip_book": True}, {}),
    # Diagnostic pair: same as A2/B2 but with the ADV participation cap pushed out of
    # reach.  `adv_cap_delta` scales each name by `min(1, budget/(|delta|*equity))` --
    # equity-dependent, hence NOT sign-symmetric.  If the mirror becomes exact here and
    # not in A2/B2, the cap is proven to be the only thing breaking it.
    ("A3 正常·关风控·零成本·无ADV上限",
     {"disable_risk_overlays": True, "disable_costs": True},
     {"risk.max_adv_participation": 1e9}),
    ("B3 反过来·关风控·零成本·无ADV上限",
     {"disable_risk_overlays": True, "disable_costs": True, "flip_book": True},
     {"risk.max_adv_participation": 1e9}),
]

# (normal label, flip label) pairs the identity table is computed on.
PAIRS = [("A  正常（照做）", "B  反过来（照做）", "as-traded (overlays ON)"),
         ("A1 正常·关风控", "B1 反过来·关风控", "pure mirror (overlays OFF)"),
         ("A2 正常·关风控·零成本", "B2 反过来·关风控·零成本",
          "pure mirror, zero cost (overlays OFF, costs OFF)"),
         ("A3 正常·关风控·零成本·无ADV上限", "B3 反过来·关风控·零成本·无ADV上限",
          "pure mirror, zero cost, ADV cap disabled")]


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
    b = r["bars"]
    cost = b[list(COST_COLS)].sum(axis=1)
    return {
        "label": label,
        "CAGR": m["CAGR"],
        "ann_vol": m["Annualized Volatility"],
        "Sharpe": m["Sharpe"],
        "Sortino": m["Sortino"],
        "max_dd": m["Max Drawdown"],
        "dd_days": m["Max DD Duration (days)"],
        "ann_turnover": m["Annual Turnover"],
        "trading_cost": m["Trading Cost (total, frac)"],
        "cost_drag": m["Cost Drag (annual)"],
        "funding": m["Funding P&L (total, frac)"],
        "avg_gross": m["Gross Exposure (avg)"],
        "avg_net_exp": m["Net Exposure (avg)"],
        # Additive, unlike CAGR: the mirror prediction `net_flip = -(net + 2*cost)`
        # sums linearly over bars, so this column is directly checkable.
        "net_sum": float(b["net_ret"].sum()),
        "cost_sum": float(cost.sum()),
        # Peak ADV participation.  The ADV cap scales `delta` by
        # `min(1, budget / (|delta| * equity))`, which is equity-dependent and
        # therefore NOT sign-symmetric: once an arm's equity grows enough for the cap
        # to engage, its positions stop being an exact mirror of the other arm's.
        # This column is how we tell "the book is the same book" apart from
        # "the cap engaged on one side only".
        "peak_adv_part": float(b["max_participation"].max()),
    }


def _identity(normal: pd.DataFrame, flip: pd.DataFrame, pair: str) -> list:
    """Per-bar check of the equalities the mirror is supposed to satisfy.

    Reported as *max absolute deviation* so a single broken bar cannot hide inside a
    mean.  A near-zero value on every row is what licenses the phrase "exact mirror";
    without it, `net_flip == -(net + 2*cost)` would just be an assertion about my own
    arithmetic.
    """
    idx = normal.index.intersection(flip.index)
    a, b = normal.loc[idx], flip.loc[idx]
    cost_a = a[list(COST_COLS)].sum(axis=1)
    cost_b = b[list(COST_COLS)].sum(axis=1)
    rows = [
        ("gross_flip == -gross_normal",
         float((b["gross_ret"] + a["gross_ret"]).abs().max())),
        ("long_flip == -short_normal",
         float((b["long_ret"] + a["short_ret"]).abs().max())),
        ("short_flip == -long_normal",
         float((b["short_ret"] + a["long_ret"]).abs().max())),
        ("turnover_flip == turnover_normal",
         float((b["turnover"] - a["turnover"]).abs().max())),
        ("funding_flip == -funding_normal",
         float((b["funding"] + a["funding"]).abs().max())),
        ("gross_exposure_flip == gross_exposure_normal",
         float((b["gross_exposure"] - a["gross_exposure"]).abs().max())),
        # Cost decomposition.  `fee` and `spread` are proportional to |delta| and so
        # mirror exactly; `impact` is NOT, because `impact_rate(delta, adv, **equity**,
        # ...)` reads the equity level, and the two arms have different equity paths
        # (one compounds up, the other down).  This row is where the tiny residual in
        # the `net_flip` prediction below comes from -- it is a property of the
        # equity-dependent impact model, not of the book.
        ("fee_flip == fee_normal",
         float((b["fee"] - a["fee"]).abs().max())),
        ("spread_flip == spread_normal",
         float((b["spread"] - a["spread"]).abs().max())),
        ("impact_flip == impact_normal",
         float((b["impact"] - a["impact"]).abs().max())),
        # Ledger identity, both arms: net == gross - cost + funding.
        ("ledger residual (normal)",
         float((a["net_ret"] - (a["gross_ret"] - cost_a + a["funding"])).abs().max())),
        ("ledger residual (flip)",
         float((b["net_ret"] - (b["gross_ret"] - cost_b + b["funding"])).abs().max())),
        # The prediction itself.  Deviation here means the *overlays* moved (the
        # reversed book's drawdown path throttles it differently) -- read it together
        # with the gross_exposure row above.
        ("net_flip vs -(net_normal + 2*cost_normal)",
         float((b["net_ret"] + (a["net_ret"] + 2.0 * cost_a)).abs().max())),
    ]
    return [{"pair": pair, "identity": k, "max_abs_deviation": v} for k, v in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=4)
    ap.add_argument("--tag", default="reverse_strategy")
    a = ap.parse_args()

    set_tag(a.tag)
    insts = _scope_crypto(BAR)
    rebal = max(1, int(round(ACCEPTED_REBALANCE_DAYS * BARS_PER_DAY[BAR])))

    specs = [{"label": lbl, "overrides": {**ACCEPTED_OVERRIDES, **ov},
              "kwargs": dict(kw, factor_subset=ACCEPTED_FACTORS),
              "rebalance_bars": rebal}
             for lbl, kw, ov in ARMS]
    specs = merge_ov(specs)

    print(f"### {len(specs)} arms (2 directions x 3 overlay/cost settings), "
          f"n_jobs={a.n_jobs}", flush=True)
    t0 = time.time()
    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=a.n_jobs,
                    desc="reverse")
    print(f"### sweep took {time.time() - t0:.0f}s", flush=True)

    bad = [r for r in res if "error" in r]
    for r in bad:
        print(f"!! arm failed: {r['label']}: {r['error']}", flush=True)
    res = [r for r in res if "error" not in r]
    if len(res) != len(ARMS):
        raise SystemExit(f"only {len(res)}/{len(ARMS)} arms produced a result -- "
                         f"refusing to report a partial comparison")

    by_label = {r["label"]: r for r in res}

    # ---- 1. headline metrics -------------------------------------------------
    tbl = pd.DataFrame([_row(r["label"], r) for r in res])
    save(tbl, "36_reverse_strategy")
    print("\n=== 六个 arm（方向 x 风控/成本）===", flush=True)
    print(tbl.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)

    # ---- 2. the mirror identity ---------------------------------------------
    ident = pd.concat([pd.DataFrame(_identity(by_label[n]["bars"], by_label[f]["bars"],
                                              pair))
                       for n, f, pair in PAIRS], ignore_index=True)
    save(ident, "36b_reverse_identity")
    print("\n=== 镜像恒等式（逐 bar 最大绝对偏差）===", flush=True)
    for pair in ident["pair"].unique():
        sub = ident[ident["pair"] == pair]
        print(f"\n  [{pair}]", flush=True)
        print(sub[["identity", "max_abs_deviation"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3e}"), flush=True)

    # ---- 3. per-year, both directions ---------------------------------------
    per = stability_table([{"label": r["label"], "bars": r["bars"]}
                           for r in res if "零成本" not in r["label"]])
    save(per, "36c_reverse_period")
    piv = per.pivot(index="window", columns="config", values="Sharpe")
    print("\n=== 逐年 Sharpe ===", flush=True)
    print(piv.to_string(float_format=lambda v: f"{v:,.3f}"), flush=True)

    # ---- 4. the headline answer ---------------------------------------------
    print("\n=== 结论 ===", flush=True)
    for lbl, _, _ in ARMS:
        m = by_label[lbl]["metrics"]
        print(f"  {lbl:26s} Sharpe {m['Sharpe']:+.4f}  CAGR {m['CAGR']:+.4%}  "
              f"MDD {m['Max Drawdown']:+.4%}  毛敞口 {m['Gross Exposure (avg)']:.4f}  "
              f"换手 {m['Annual Turnover']:,.1f}", flush=True)

    ma = by_label[ARMS[0][0]]["metrics"]
    mb = by_label[ARMS[1][0]]["metrics"]
    m1a = by_label[ARMS[2][0]]["metrics"]
    m1b = by_label[ARMS[3][0]]["metrics"]
    m2a = by_label[ARMS[4][0]]["metrics"]
    m2b = by_label[ARMS[5][0]]["metrics"]
    print(f"\n  去掉成本带来的 Sharpe 改善：正常 {m2a['Sharpe'] - m1a['Sharpe']:+.4f} / "
          f"反过来 {m2b['Sharpe'] - m1b['Sharpe']:+.4f}", flush=True)
    print(f"  关掉风控后的 Sharpe 变化：正常 {m1a['Sharpe'] - ma['Sharpe']:+.4f} / "
          f"反过来 {m1b['Sharpe'] - mb['Sharpe']:+.4f}", flush=True)

    # ---- 5. the additive prediction, checked at the aggregate level ----------
    rows = {r["label"]: r for r in res}
    print("\n=== 可加总检验（纯镜像对：Σnet 与预测）===", flush=True)
    for n_lbl, f_lbl, pair in PAIRS[1:]:
        na, fb = rows[n_lbl], rows[f_lbl]
        nsum = float(na["bars"]["net_ret"].sum())
        fsum = float(fb["bars"]["net_ret"].sum())
        csum = float(na["bars"][list(COST_COLS)].sum(axis=1).sum())
        pred = -(nsum + 2.0 * csum)
        print(f"  [{pair}]", flush=True)
        print(f"    Σnet 正常 {nsum:+.6f}   Σnet 反过来 {fsum:+.6f}   "
              f"预测 -(Σnet+2Σcost) {pred:+.6f}   差 {fsum - pred:+.3e}", flush=True)

    if mb["Sharpe"] > 0:
        print("\n  !! 反过来也是正 Sharpe —— 收益不是方向性的，必须查暴露/漂移",
              flush=True)
    else:
        print("\n  -> 反过来是亏的：方向信息成立（数额见 36b 的 net 行）", flush=True)


if __name__ == "__main__":
    main()
