"""Run the accepted strategy on Binance bars and compare against the OKX bars.

The question
------------
The accepted result (Sharpe 1.937 / CAGR 39.1% / MDD -13.3%) was measured on OKX
`history-candles`.  If that edge were partly an artefact of one venue's bar
construction -- tick rounding, volume accounting, the exact bar boundary -- then
swapping the venue would move the headline.  If it survives, the result is a
property of the *market*, not of the data vendor.

Design: move exactly ONE variable
---------------------------------
  * same instrument universe  (the intersection of both venues' listings)
  * same window, same grid, same 5-factor set, same cap, same turnover budget
  * same funding series       (`funding_hyb` is copied into the Binance cache)
  * **only the OHLCV source differs**

Anything else would confound "the venue differs" with "the sample differs".

Note on `amount`: the two venues' prices agree to ~4bp (same-bar return
correlation 0.9985), but reported USDT turnover differs by a factor of ~2.6.  That
matters, because `amount` is not decoration -- it feeds the ADV universe gate and
the `flow` factor.  So the comparison is reported with that split in mind: the
price-driven signal should barely move, and any real difference should be
attributable to the volume-driven part.

Usage
-----
  python scripts/binance_backtest.py                 # compare both caches
  python scripts/binance_backtest.py --bar 1h --rebalance-days 1.0
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crypto_ls_research.analysis import metrics as met                      # noqa: E402
from crypto_ls_research.analysis import walkforward as wf_mod               # noqa: E402
from crypto_ls_research.backtest.engine import run_backtest                 # noqa: E402
from crypto_ls_research.config.settings import (ACCEPTED_FACTORS,           # noqa: E402
                                               ACCEPTED_REBALANCE_DAYS,
                                               BARS_PER_DAY, default_config)
from crypto_ls_research.data.binance_client import BinanceClient, okx_base  # noqa: E402
from crypto_ls_research.data.store import list_cached_insts, load_panels    # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OKX_CACHE = os.path.join(ROOT, "data_cache")
BNB_CACHE = os.path.join(ROOT, "data_cache_binance")
ART = os.path.join(ROOT, "artifacts", "binance_vs_okx")
TAB = os.path.join(ART, "tables")

# The accepted recipe.  Read from settings, never re-typed as literals -- the same
# rule that governs `engine.DEFAULT_SIGNAL` and `webapp.spec.OPTIMAL_OVERRIDES`.
ACCEPTED_OVERRIDES: Dict[str, object] = {
    "factors.subset": list(ACCEPTED_FACTORS),
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}


def common_universe(bar: str) -> List[str]:
    """Instrument ids present in BOTH caches, in a stable order.

    Derived from what is actually on disk rather than from a saved list, so a
    partially-finished download cannot silently shrink one side of the comparison.
    """
    okx = set(list_cached_insts(bar, cache=OKX_CACHE))
    try:
        bnb = set(list_cached_insts(bar, cache=BNB_CACHE))
    except FileNotFoundError:
        raise SystemExit(f"no Binance cache at {BNB_CACHE}; run the downloader first")
    return sorted(okx & bnb)


def make_cfg(bar: str, rebalance_bars: int, start: str, end: str):
    cfg = default_config(bar=bar, rebalance_bars=rebalance_bars)
    cfg.start, cfg.end = start, end
    for k, v in ACCEPTED_OVERRIDES.items():
        parts = k.split(".")
        t = cfg
        for p in parts[:-1]:
            t = getattr(t, p)
        setattr(t, parts[-1], v)
    return cfg


def run_one(label: str, cache: str, bar: str, cfg, insts: List[str]):
    t0 = time.time()
    panels = load_panels(bar, cfg.start, cfg.end, insts=insts, cache=cache)
    res = run_backtest(panels, cfg)
    m = met.compute_metrics(res.bars, label)
    print(f"  [{label}] {len(panels.index):,} bars x {len(panels.insts)} insts, "
          f"{res.meta['n_rebalances']} rebalances, {time.time()-t0:.1f}s", flush=True)
    return panels, res, m


def headline_rows(ms: Dict[str, dict]) -> pd.DataFrame:
    keys = [("Sharpe", "Sharpe"), ("CAGR", "CAGR"), ("Annualized Volatility", "ann_vol"),
            ("Max Drawdown", "max_dd"), ("Sortino", "Sortino"), ("Calmar", "Calmar"),
            ("Annual Turnover", "ann_turnover"),
            ("Trading Fee (total, frac)", "fee_total"),
            ("Cost Drag (annual)", "cost_drag"),
            ("Funding P&L (total, frac)", "funding_pnl"),
            ("Long PnL (total)", "long_pnl"), ("Short PnL (total)", "short_pnl"),
            ("Net PnL (total)", "net_pnl")]
    rows = []
    for label, m in ms.items():
        row = {"venue": label}
        for src, dst in keys:
            row[dst] = m.get(src)
        rows.append(row)
    return pd.DataFrame(rows)


def n_tradable(res) -> float:
    """Mean number of instruments the universe gate admitted per rebalance.

    Read from the result, not re-derived: this is the number that says whether the
    two runs are even trading the same cross-section.
    """
    try:
        m = res.score_matrix
        return float(np.isfinite(m).sum(axis=1).mean())
    except Exception:                                            # noqa: BLE001
        return float("nan")


def adv_diagnostic(panels_okx, panels_bnb, cfg) -> pd.DataFrame:
    """How many names clear the ADV floor in each venue, and how far apart are they?

    `amount` is the one input that genuinely differs between venues (median ~2.9x),
    and it gates the universe -- so this is where a venue effect shows up first.
    """
    bpd = BARS_PER_DAY[cfg.bar]
    win = max(5, int(round(cfg.universe.liq_window_days * bpd)))
    floor = cfg.universe.min_avg_amount_usd
    out = []
    for label, p in (("okx", panels_okx), ("binance", panels_bnb)):
        adv = p.amount.rolling(win, min_periods=5).mean()
        out.append({"venue": label,
                    "mean_names_passing_ADV": float((adv >= floor).sum(axis=1).mean()),
                    "median_adv_usd": float(adv.stack().median()),
                    "adv_floor_usd": floor})
    return pd.DataFrame(out)


def selectivity_sweep(insts, bar, rebal, start, end, quick=False):
    """Sharpe as a function of *how many names the ADV floor admits*, per venue.

    This is the decisive experiment.  The gate-on comparison above is confounded:
    Binance reports ~3.7x the USDT turnover of OKX, so the SAME $3M floor admits 39
    names on Binance but only 16 on OKX.  The two runs are therefore not "one
    strategy on two venues" -- they are two different strategies, with different
    cross-section widths.

    Sweeping the floor along each venue's own ADV quantiles and plotting Sharpe
    against the *realised* universe size puts both venues on one x-axis.  If the two
    curves lie on top of each other, the venue is irrelevant and only selectivity
    matters; if they separate, the venue carries information the price path does not.
    """
    grid = [0.50, 0.70, 0.85, 0.93, 0.97, 0.99] if quick else [
        0.40, 0.55, 0.70, 0.80, 0.87, 0.92, 0.955, 0.98, 0.99]
    rows = []
    for label, cache in (("okx", OKX_CACHE), ("binance", BNB_CACHE)):
        cfg0 = make_cfg(bar, rebal, start, end)
        panels = load_panels(bar, start, end, insts=insts, cache=cache)
        bpd = BARS_PER_DAY[bar]
        win = max(2, int(round(cfg0.universe.liq_window_days * bpd)))
        adv = panels.amount.rolling(win, min_periods=max(2, win // 4)).mean()
        pooled = adv.stack().replace([np.inf, -np.inf], np.nan).dropna()
        print(f"  [{label}] ADV 面板就绪，{len(pooled):,} 个有效 ADV 观测", flush=True)
        for q in grid:
            floor = float(pooled.quantile(q))
            cfg = make_cfg(bar, rebal, start, end)
            cfg.universe.min_avg_amount_usd = floor
            res = run_backtest(panels, cfg)
            m = met.compute_metrics(res.bars, f"{label}_q{q}")
            uni = float(np.isfinite(res.score_matrix).sum(axis=1).mean())
            # Overlap between the long and short candidate lists.  `select_book` picks
            # them from `score > 0` and `score < 0` respectively, so this SHOULD be 0
            # at every width -- but a narrow universe (16 names against 2*top_k=20) is
            # exactly where an overlap bug would hide, and a peak that coincides with
            # that regime would be an artefact rather than an edge.  Measured, not
            # assumed: it is 0.00 everywhere, including at the OKX peak.
            ov = np.array([len(set(b["long"]) & set(b["short"])) for b in res.rebalances])
            rows.append({"venue": label, "quantile": q, "adv_floor_usd": floor,
                         "mean_universe": uni, "Sharpe": m["Sharpe"], "CAGR": m["CAGR"],
                         "max_dd": m["Max Drawdown"],
                         "ann_turnover": m["Annual Turnover"],
                         "cost_drag": m["Cost Drag (annual)"],
                         "mean_long_overlap": float(ov.mean()) if ov.size else 0.0})
            print(f"    q={q:<6} floor=${floor:>14,.0f}  universe={uni:6.1f}  "
                  f"Sharpe={m['Sharpe']:.3f}  CAGR={m['CAGR']:+.3%}  "
                  f"重叠={ov.mean() if ov.size else 0:.2f}", flush=True)
            del res, m
        # Free this venue's panels before loading the next: a 131x50k float32 panel
        # set is ~190 MB, and holding two of them alongside the sweep's temporaries is
        # what OOM-killed the first attempt at this experiment (exit 137).
        del panels, adv, pooled
        gc.collect()
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="1h")
    ap.add_argument("--rebalance-days", type=float, default=ACCEPTED_REBALANCE_DAYS)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-09-29")
    ap.add_argument("--gate-only", action="store_true",
                    help="skip the no-gate control runs (faster, less informative)")
    ap.add_argument("--selectivity", action="store_true",
                    help="also sweep the ADV floor per venue (the decisive test)")
    a = ap.parse_args()

    os.makedirs(TAB, exist_ok=True)
    rebal = max(1, int(round(a.rebalance_days * BARS_PER_DAY[a.bar])))
    insts = common_universe(a.bar)
    print(f"### Binance vs OKX: bar={a.bar} rebalance={rebal} bars "
          f"({a.rebalance_days}d) window={a.start}..{a.end}")
    print(f"### common universe = {len(insts)} instruments "
          f"(identical list fed to both runs)")
    print(f"### factors = {list(ACCEPTED_FACTORS)}")
    print(f"### overrides = {ACCEPTED_OVERRIDES}\n")

    # A 2x2 design, because a venue swap moves TWO things at once:
    #   (a) the price path (tiny: ~4bp, return corr 0.9985)
    #   (b) the *effective universe*, because the ADV floor is an absolute USDT
    #       threshold and Binance reports ~2.9x the turnover -- so more names clear it.
    # Running the gate off as well isolates (a) from (b); without that control, any
    # difference is unattributable.
    runs = [("okx", OKX_CACHE, True), ("binance", BNB_CACHE, True)]
    if not a.gate_only:
        runs += [("okx_nogate", OKX_CACHE, False), ("binance_nogate", BNB_CACHE, False)]

    panels: Dict[str, object] = {}
    results: Dict[str, object] = {}
    ms: Dict[str, dict] = {}
    for label, cache, gate in runs:
        cfg = make_cfg(a.bar, rebal, a.start, a.end)
        if not gate:
            cfg.universe.min_avg_amount_usd = 0.0
        p, r, m = run_one(label, cache, a.bar, cfg, insts)
        panels[label], results[label], ms[label] = p, r, m

    tbl = headline_rows(ms)
    tbl.to_csv(os.path.join(TAB, "50_headline_binance_vs_okx.csv"), index=False)
    print("\n=== headline ===")
    print(tbl.T.to_string(header=False))

    # effective cross-section size -- the number that proves whether (b) is real
    sizes = pd.DataFrame([{"run": k, "mean_names_with_score": n_tradable(v),
                           "gate": "off" if k.endswith("_nogate") else "on"}
                          for k, v in results.items()])
    sizes.to_csv(os.path.join(TAB, "51b_effective_universe.csv"), index=False)
    print("\n=== effective cross-section (mean names with a score) ===")
    print(sizes.to_string(index=False))

    adv = adv_diagnostic(panels["okx"], panels["binance"],
                         make_cfg(a.bar, rebal, a.start, a.end))
    adv.to_csv(os.path.join(TAB, "51_adv_gate_diagnostic.csv"), index=False)
    print("\n=== ADV gate (the one input that really differs) ===")
    print(adv.to_string(index=False))

    # ---- bar-level agreement ----------------------------------------------
    # `panels.close` is a WIDE frame (index=UTC bar, columns=instrument), so the
    # comparison is element-wise across that whole matrix -- not a single column.
    # The two caches can also end on different bars (Binance's newest close is not
    # OKX's), so align on the intersection instead of assuming a shared grid.
    c_okx, c_bn = panels["okx"].close.align(panels["binance"].close, join="inner", axis=0)
    cols = sorted(set(c_okx.columns) & set(c_bn.columns))
    c_okx, c_bn = c_okx[cols], c_bn[cols]

    # A constant denomination factor must be divided out BEFORE measuring price
    # agreement.  Binance lists several contracts in 1000x units (`1000PEPEUSDT`,
    # `1000SHIBUSDT`, ...) while OKX quotes the base asset, so the level ratio for
    # those is ~1000 by construction -- left in, it would report a "999x price
    # disagreement" that is purely a unit convention.  Returns are scale-free, so
    # this only affects the level comparison.
    ratio = c_bn / c_okx
    denom = ratio.median()
    rel = ((ratio / denom) - 1).abs().stack()
    scaled = denom[(denom - 1).abs() > 0.5]

    ret_okx = (panels["okx"].close / panels["okx"].open - 1)[cols].stack()
    ret_bn = (panels["binance"].close / panels["binance"].open - 1)[cols].stack()
    idx = ret_okx.index.intersection(ret_bn.index)
    a_okx, a_bn = ret_okx.loc[idx], ret_bn.loc[idx]
    ok = np.isfinite(a_okx.to_numpy()) & np.isfinite(a_bn.to_numpy())
    amt = (panels["binance"].amount / panels["okx"].amount)[cols].stack()
    agree = {
        "n_cells": int(len(rel)),
        "n_insts": len(cols),
        "close_rel_diff_median": float(rel.median()),
        "close_rel_diff_p99": float(rel.quantile(0.99)),
        "bar_return_corr": float(np.corrcoef(a_okx.to_numpy()[ok], a_bn.to_numpy()[ok])[0, 1]),
        "amount_ratio_median": float(amt.replace([np.inf, -np.inf], np.nan).dropna().median()),
        "n_denominated_1000x": int(len(scaled)),
    }
    with open(os.path.join(TAB, "52_bar_agreement.json"), "w") as f:
        json.dump(agree, f, indent=1)
    print("\n=== bar-level agreement ===")
    for k, v in agree.items():
        print(f"  {k:26s} {v:,.6g}")

    # ---- is the difference inside noise, or real? --------------------------
    # Align on the fold NAME, not on position: if one venue's panel is shorter, a
    # positional zip would compare fold k against fold k-1 and manufacture a
    # difference that is purely a bookkeeping artefact.
    stats: Dict[str, dict] = {}
    fold_frames = []
    for pair, (la, lb) in {"gate_on": ("okx", "binance"),
                           "gate_off": ("okx_nogate", "binance_nogate")}.items():
        if la not in results:
            continue
        fa = wf_mod.fold_metrics_table([{"label": la, "bars": results[la].bars}],
                                       folds=wf_mod.QUARTERLY_FOLDS)
        fb = wf_mod.fold_metrics_table([{"label": lb, "bars": results[lb].bars}],
                                       folds=wf_mod.QUARTERLY_FOLDS)
        sa = fa.set_index("fold")["test_sharpe"].dropna()
        sb = fb.set_index("fold")["test_sharpe"].dropna()
        common = sa.index.intersection(sb.index)
        sa, sb = sa.loc[common], sb.loc[common]
        if not len(common):
            continue
        d = (sb - sa).to_numpy()
        stats[pair] = {"n_folds": int(len(common)), "mean_diff": float(d.mean()),
                       "median_diff": float(np.median(d)), "std_diff": float(d.std(ddof=1)),
                       "max_abs_diff": float(np.abs(d).max()),
                       "n_folds_binance_better": int((d > 0).sum()),
                       "corr_okx_vs_binance": float(np.corrcoef(sa.to_numpy(), sb.to_numpy())[0, 1])}
        fold_frames.append(pd.DataFrame({"pair": pair, "fold": list(common),
                                         "okx": sa.to_numpy(), "binance": sb.to_numpy(),
                                         "diff": d}))
        print(f"\n=== quarterly folds [{pair}] (n={len(common)}) ===")
        print(f"  test-Sharpe 相关 = {stats[pair]['corr_okx_vs_binance']:.4f}")
        print(f"  差值 mean={stats[pair]['mean_diff']:+.4f} "
              f"median={stats[pair]['median_diff']:+.4f} "
              f"max|d|={stats[pair]['max_abs_diff']:.4f}")
        print(f"  币安更好的折数 = {stats[pair]['n_folds_binance_better']}/{len(common)}")
    if fold_frames:
        pd.concat(fold_frames, ignore_index=True).to_csv(
            os.path.join(TAB, "53_quarterly_fold_diffs.csv"), index=False)
    with open(os.path.join(TAB, "53b_fold_diff_stats.json"), "w") as f:
        json.dump(stats, f, indent=1)

    if a.selectivity:
        print("\n=== ADV-floor selectivity sweep (matched x-axis) ===", flush=True)
        sw = selectivity_sweep(insts, a.bar, rebal, a.start, a.end)
        sw.to_csv(os.path.join(TAB, "54_selectivity_sweep.csv"), index=False)
        # `pivot` raises on duplicate index values, and the top of the grid collapses
        # several quantiles to universe == 0 (the floor exceeds every ADV).  Bucket the
        # realised width so the table stays readable instead of blowing up.
        sw = sw[sw["mean_universe"] >= 1.0].copy()
        sw["universe_bucket"] = sw["mean_universe"].round(-1).astype(int)
        piv = sw.pivot_table(index="universe_bucket", columns="venue", values="Sharpe",
                             aggfunc="max")
        print("\n  Sharpe vs realised universe size (max within a 10-name bucket):")
        print(piv.round(3).to_string())
        piv.to_csv(os.path.join(TAB, "54b_sharpe_vs_universe.csv"))

    summary = {"metrics": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                           for k, v in ms.items()},
               "universe": insts, "bar_agreement": agree, "fold_stats": stats,
               "config": {"bar": a.bar, "rebalance_bars": rebal, "start": a.start,
                          "end": a.end, **ACCEPTED_OVERRIDES}}
    with open(os.path.join(TAB, "50b_summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(f"\nartifacts -> {os.path.relpath(TAB, ROOT)}")


if __name__ == "__main__":
    main()
