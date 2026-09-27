"""Main research orchestrator.

Stages
------
  base       baseline backtest + full metric set + saved result object
  exec       execution-model comparison (next_open / vwap / twap / close)
  beta       Method A (gross matched) vs Method B (beta neutral)
  funding    funding = 0 / 1.0 / 1.5 / 2.0
  freq       rebalance frequency ladder (15m / 1h / 4h / 12h / 1d)
  turnover   turnover-budget ladder
  ablation   factor ablation (single, pairwise, leave-one-out, full)
  ic         rank-IC by horizon, per factor, IC decay, factor correlation
  sens       parameter sensitivity grids + multiplicity summary
  wf         walk-forward folds + stability windows
  mc         permutation / random-score / block-shuffle Monte Carlo
  capacity   AUM ladder + capacity curve
  bias       hidden-bias audit + delisting stress
  charts     all figures
  multi      short-horizon reversal book + blend with the trend book (Tier-2 §3.2)
  regime     BTC-vol regime grid, scored by walk-forward selection (Tier-2 §3.3)
  report     the written report

Run:  python -m crypto_ls_research.run.research --bar 1h --stages base exec ic ... --n-jobs 4
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..analysis import attribution as att
from ..analysis import bias as bias_mod
from ..analysis import capacity as cap_mod
from ..analysis import combine as combine_mod
from ..analysis import ic as ic_mod
from ..analysis import metrics as met
from ..analysis import montecarlo as mc_mod
from ..analysis import plots
from ..analysis import sensitivity as sens_mod
from ..analysis import walkforward as wf_mod
from ..analysis.sweep import run_sweep, sweep_table
from ..backtest.engine import run_backtest
from ..config.settings import BARS_PER_DAY, BacktestConfig, default_config
from ..data.asset_class import ASSET_CLASSES, filter_insts, load_categories, scope_summary
from ..data.store import CACHE, list_cached_insts, load_panels
from ..factors.engine import FACTOR_NAMES

ART = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "..", "artifacts"))
TAB = os.path.join(ART, "tables")
CHART = os.path.join(ART, "charts")


def set_tag(tag: str) -> None:
    """Namespace all artifacts under artifacts/<tag>/ so multi-frequency studies coexist."""
    global ART, TAB, CHART
    if tag:
        ART = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "..", "artifacts", tag))
        TAB = os.path.join(ART, "tables")
        CHART = os.path.join(ART, "charts")
    for d in (ART, TAB, CHART):
        os.makedirs(d, exist_ok=True)


set_tag("")

REBAL = {
    "1h": {"1h": 1, "4h": 4, "12h": 12, "1d": 24, "3d": 72, "7d": 168},
    "15m": {"15m": 1, "1h": 4, "4h": 16, "12h": 48, "1d": 96, "3d": 288},
    "4h": {"4h": 1, "12h": 3, "1d": 6, "3d": 18},
}


def gross_stats(bars: pd.DataFrame) -> Dict[str, float]:
    """Gross-of-all-cost view of the same position path."""
    g = met.compute_metrics(met.bars_variant(bars, "gross"), "gross")
    n = met.compute_metrics(met.bars_variant(bars, "no_trading_cost"), "no_trading")
    return {"sharpe_gross": g["Sharpe"], "cagr_gross": g["CAGR"],
            "sharpe_no_trading_cost": n["Sharpe"], "cagr_no_trading_cost": n["CAGR"]}

# IC horizons in days
IC_HORIZONS_DAYS = [1 / 24, 4 / 24, 12 / 24, 1.0, 3.0, 7.0]


def save(df: pd.DataFrame, name: str, index: bool = False) -> None:
    if df is None:
        return
    p = os.path.join(TAB, name if name.endswith(".csv") else f"{name}.csv")
    df.to_csv(p, index=index)
    print(f"    -> {os.path.relpath(p, ART)}", flush=True)


def save_json(obj, name: str) -> None:
    p = os.path.join(TAB, name if name.endswith(".json") else f"{name}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    print(f"    -> {os.path.relpath(p, ART)}", flush=True)


def base_cfg(bar: str, rebal: int, start: str, end: str, capital: float = 100_000.0,
             **ov) -> BacktestConfig:
    cfg = default_config(bar=bar, rebalance_bars=rebal)
    cfg.start, cfg.end = start, end
    cfg.initial_capital = capital
    merged = {**BASE_OVERRIDES, **ov}
    for k, v in merged.items():
        parts = k.split(".")
        t = cfg
        for p in parts[:-1]:
            t = getattr(t, p)
        setattr(t, parts[-1], v)
    return cfg


# Global config overrides (--override key.path=value).  They are merged into EVERY
# backtest this process launches, including the ones executed inside sweep workers,
# so a variant study is guaranteed to be internally consistent.
BASE_OVERRIDES: Dict[str, object] = {}


def merge_ov(specs: List[dict]) -> List[dict]:
    for s in specs:
        s["overrides"] = {**BASE_OVERRIDES, **s.get("overrides", {})}
    return specs


def parse_override(s: str):
    if "=" not in s:
        raise ValueError(f"--override expects key=value, got {s!r}")
    k, v = s.split("=", 1)
    try:
        val = json.loads(v)
    except json.JSONDecodeError:
        val = v
    return k.strip(), val


# ===========================================================================
def stage_base(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [base] baseline backtest ===", flush=True)
    cfg = base_cfg(bar, rebal, start, end)
    panels = load_panels(bar, start, end, insts=insts)
    print(f"  panels: {len(panels.index):,} bars x {len(panels.insts)} instruments "
          f"({panels.index[0].date()} -> {panels.index[-1].date()})", flush=True)
    t0 = time.time()
    res = run_backtest(panels, cfg)
    print(f"  backtest took {time.time()-t0:.1f}s, {res.meta['n_rebalances']} rebalances, "
          f"{res.meta['n_stale_marks']} stale marks", flush=True)
    with open(os.path.join(ART, "baseline.pkl"), "wb") as f:
        pickle.dump({"result": res, "cfg": cfg, "bar": bar, "start": start, "end": end}, f,
                    protocol=5)
    m = met.compute_metrics(res.bars, "baseline")
    save(met.fmt_metrics(m), "01_headline_metrics")
    save_json({k: v for k, v in m.items() if not k.startswith("_")}, "01_headline_metrics")
    print(met.fmt_metrics(m).to_string(index=False), flush=True)

    cs = met.cost_scenario_table(res.bars)
    save(cs, "01b_cost_scenarios")
    save_json(res.meta | {"bars": len(res.bars.index)}, "00_run_meta")
    print("\n  cost scenarios:", flush=True)
    print(cs.to_string(index=False), flush=True)

    ls = att.long_short_attribution(res.bars)
    save(ls, "02_long_short_attribution")
    print(ls.to_string(index=False), flush=True)

    save(att.coin_attribution(res), "03_coin_attribution")
    save_json(att.concentration_stats(res), "03_concentration")
    save(att.btc_eth_dependence(res), "03b_btc_eth_dependence")

    lab = att.regime_labels(panels, cfg)
    reg = att.regime_attribution(res.bars, lab)
    save(reg, "04_regime_attribution")
    save_json(res.meta, "00_run_meta")
    return res, cfg, panels


def stage_exec(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [exec] execution model comparison ===", flush=True)
    specs = [{"label": m, "overrides": {"execution.exec_price": m},
              "rebalance_bars": rebal} for m in
             ("next_open", "next_vwap", "next_twap", "next_close")]
    _, by_inst = _inst_list(insts)
    res = run_sweep(merge_ov(specs), bar, start, end, insts=by_inst, n_jobs=n_jobs, desc="exec")
    tbl = bias_mod.execution_model_comparison({r["label"]: r for r in res})
    save(tbl, "05_execution_models")
    print(tbl.to_string(index=False), flush=True)
    return res


def stage_beta(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [beta] gross-matched vs beta-neutral ===", flush=True)
    specs = [{"label": m, "overrides": {"portfolio.beta_neutral_mode": m},
              "rebalance_bars": rebal} for m in ("A_gross_matched", "B_beta_neutral")]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="beta")
    rows = []
    for r in res:
        if "error" in r:
            continue
        m = r["metrics"]
        rows.append({"mode": r["label"], "CAGR": m["CAGR"], "Sharpe": m["Sharpe"],
                     "ann_vol": m["Annualized Volatility"], "max_dd": m["Max Drawdown"],
                     "beta_exp_mean": m["Beta Exposure (avg)"],
                     "beta_exp_mean_abs": m["Beta Exposure (avg abs)"],
                     "beta_exp_std": m["Beta Exposure (std)"]})
    save(pd.DataFrame(rows), "06_beta_neutralisation")
    print(pd.DataFrame(rows).to_string(index=False), flush=True)
    return res


def stage_funding(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [funding] funding-cost sensitivity ===", flush=True)
    specs = [{"label": f"funding_x{v}", "overrides": {"costs.funding_multiplier": v},
              "rebalance_bars": rebal} for v in (0.0, 1.0, 1.5, 2.0)]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="funding")
    rows = []
    for r in res:
        if "error" in r:
            continue
        m = r["metrics"]
        rows.append({"scenario": r["label"], "CAGR": m["CAGR"], "Sharpe": m["Sharpe"],
                     "funding_pnl_total": m["Funding P&L (total, frac)"],
                     "cost_drag_annual": m["Cost Drag (annual)"],
                     "net_pnl": m["Net PnL (total)"]})
    save(pd.DataFrame(rows), "07_funding_sensitivity")
    print(pd.DataFrame(rows).to_string(index=False), flush=True)
    return res


def stage_freq(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [freq] rebalance frequency ladder ===", flush=True)
    presets = REBAL.get(bar, {"1d": rebal})
    specs = [{"label": k, "overrides": {}, "rebalance_bars": v} for k, v in presets.items()]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="freq")
    rows = []
    for r in res:
        if "error" in r:
            continue
        m = r["metrics"]
        row = {"rebalance": r["label"], "CAGR": m["CAGR"], "Sharpe": m["Sharpe"],
               "ann_vol": m["Annualized Volatility"], "max_dd": m["Max Drawdown"],
               "ann_turnover": m["Annual Turnover"],
               "fee_total": m["Trading Fee (total, frac)"],
               "cost_drag": m["Cost Drag (annual)"],
               "funding_pnl": m["Funding P&L (total, frac)"]}
        if "bars" in r:
            row.update(gross_stats(r["bars"]))
        rows.append(row)
    t = pd.DataFrame(rows)
    save(t, "08_rebalance_frequency")
    print(t.to_string(index=False), flush=True)
    return res


def stage_turnover(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [turnover] turnover budget ladder ===", flush=True)
    specs = [{"label": f"budget_{v}", "overrides": {"execution.max_daily_turnover": v},
              "rebalance_bars": rebal} for v in (0.10, 0.20, 0.50, 1.00, 2.00, None)]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="turnover")
    t = sweep_table(res)
    save(t, "09_turnover_budget")
    print(t.to_string(index=False), flush=True)
    return res


def stage_ablation(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [ablation] factor ablation ===", flush=True)
    from itertools import combinations
    F = FACTOR_NAMES
    subsets = ([(k,) for k in F] + [tuple(c) for c in combinations(F, 2)]
               + [tuple(sorted(set(F) - {k})) for k in F] + [tuple(F)])
    seen, uniq = set(), []
    for s in subsets:
        key = tuple(sorted(s))
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    specs = [{"label": "+".join(s), "overrides": {}, "rebalance_bars": rebal,
              "kwargs": {"factor_subset": s}} for s in uniq]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="ablation")
    rows = []
    for r in res:
        if "error" in r:
            continue
        m = r["metrics"]
        row = {"subset": r["label"], "n_factors": len(r["label"].split("+")),
               "CAGR": m["CAGR"], "ann_vol": m["Annualized Volatility"],
               "Sharpe": m["Sharpe"], "Sortino": m["Sortino"],
               "max_dd": m["Max Drawdown"], "ann_turnover": m["Annual Turnover"],
               "cost_drag": m["Cost Drag (annual)"],
               "long_pnl": m["Long PnL (total)"], "short_pnl": m["Short PnL (total)"]}
        if "bars" in r:
            row.update(gross_stats(r["bars"]))
        rows.append(row)
    t = pd.DataFrame(rows)
    key = "Sharpe_gross" if "Sharpe_gross" in t.columns else "Sharpe"
    t = t.sort_values(key, ascending=False)
    save(t, "10_factor_ablation")
    print(t.to_string(index=False), flush=True)
    return res


def stage_ic(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [ic] rank IC analysis ===", flush=True)
    d = load_baseline()
    res, cfg = d["result"], d["cfg"]
    # Pin the panel to the instrument set the baseline actually traded: the candle
    # cache can grow between stages (a download finishing mid-run), and a wider
    # panel would silently mis-align against result.score_matrix.
    panels = load_panels(bar, start, end, insts=res.insts)
    hz = sorted({max(1, cfg.days_to_bars(h)) for h in IC_HORIZONS_DAYS})
    tab = ic_mod.rank_ic_over_horizons(res, panels, hz, convention="exec")
    save(tab.drop(columns=["p_value"], errors="ignore"), "11_rank_ic_by_horizon")
    print(tab.to_string(index=False), flush=True)
    ftab = ic_mod.ic_by_factor(res, panels, hz)
    save(ftab, "12_rank_ic_by_factor")
    print(ftab.to_string(index=False), flush=True)
    decay = ic_mod.ic_decay(res, panels, max_days=8.0, n_points=20)
    save(decay, "13_ic_decay")
    fcorr = ic_mod.factor_correlation(res)
    save(fcorr, "14_factor_correlation", index=True)
    print("\n  factor correlation (mean cross-sectional Spearman):")
    print(fcorr.to_string(), flush=True)
    return tab, ftab, decay, fcorr


def stage_sens(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [sens] parameter sensitivity ===", flush=True)
    by = _inst_list(insts)[1]
    all_res: List[dict] = []
    keys = ["factors.mom_lookback_days", "factors.flow_short_days", "factors.flow_long_days",
            "factors.range_days", "factors.hitrate_days", "portfolio.top_k",
            "portfolio.min_abs_score", "risk.target_vol_annual",
            "execution.max_daily_turnover", "costs.funding_multiplier"]
    if quick:
        keys = keys[:4]
    for k in keys:
        specs = sens_mod.build_specs(k, type("C", (), {"rebalance_bars": rebal})())
        r = run_sweep(merge_ov(specs), bar, start, end, insts=by, n_jobs=n_jobs, desc=k.split(".")[-1])
        all_res += r
    t = sweep_table(all_res)
    save(t, "15_sensitivity_ofat")
    ms = sens_mod.multiplicity_summary(t, "Sharpe")
    save(ms, "16_multiplicity_summary")
    print(ms.to_string(index=False), flush=True)

    # 2-D grids
    pair_keys = [("factors.mom_lookback_days", "portfolio.top_k", "mom", "K"),
                 ("factors.flow_short_days", "factors.flow_long_days", "flowS", "flowL"),
                 ("portfolio.top_k", "risk.target_vol_annual", "K", "tvol")]
    for ka, kb, na, nb in pair_keys:
        specs, _, _ = sens_mod.build_pair_grid(ka, kb, type("C", (), {"rebalance_bars": rebal})(), na, nb)
        rr = run_sweep(merge_ov(specs), bar, start, end, insts=by, n_jobs=n_jobs, desc=f"{na}x{nb}")
        all_res += rr
        g = sens_mod.grid_from_results(rr, ka, kb, "Sharpe", na, nb)
        save(g, f"17_grid_{na}_x_{nb}", index=True)
        all_res += rr
        print(f"\n  grid {na} x {nb} (Sharpe):\n{g.to_string()}", flush=True)
        print("  plateau:", sens_mod.plateau_score(g), flush=True)
    return all_res


def stage_multi(bar, rebal, start, end, insts, n_jobs, quick):
    """Short-horizon reversal book, and its blend with the trend book (brief §38/§3.2).

    The rank-IC study finds the same cross-section to be a *reversal* signal at 4h
    and a *trend* signal at 7d.  Rather than average the two horizons inside one 3d
    book, we run the reversal bet as its own book with its own clock and then ask
    whether the two return streams combine well.  The horizon and the blend weight
    are both choices, so both are reported as full surfaces plus fold-by-fold
    stability -- never as a single "best" number.
    """
    print("\n=== [multi] short-horizon reversal book + blend ===", flush=True)
    by = _inst_list(insts)[1]
    horizons = {"1h": 1, "4h": 4, "12h": 12, "1d": 24, "3d": 3 * 24}
    specs = [{"label": f"rev@{k}", "overrides": {}, "rebalance_bars": v,
              "kwargs": {"factor_subset": ("rev_short",)}} for k, v in horizons.items()]
    specs += [{"label": "rev+trend@3d", "overrides": {}, "rebalance_bars": 3 * 24,
               "kwargs": {"factor_subset": ("rev_short", "range_pos", "hitrate")}}]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=by, n_jobs=n_jobs, desc="rev")
    t = sweep_table(res)
    save(t, "30_reversal_book")
    print(t.to_string(index=False), flush=True)
    folds = wf_mod.fold_metrics_table(res)
    save(folds, "30b_reversal_book_folds")
    sel = wf_mod.walk_forward_selection(res)
    save(sel, "30c_reversal_horizon_wf_selection")
    print("\n  walk-forward horizon selection:", flush=True)
    print(sel.to_string(index=False), flush=True)

    d = load_baseline()
    trend = d["result"].bars["net_ret"]
    rows, summaries = [], {}
    for r in res:
        if "error" in r or "bars" not in r or r["label"].startswith("rev+trend"):
            continue
        out = combine_mod.combine_books(trend, r["bars"]["net_ret"], bars_per_day=BARS_PER_DAY[bar],
                                        name_a="trend", name_b=r["label"])
        bt = combine_mod.blend_summary(out)
        bt.insert(0, "reversal_book", r["label"])
        rows.append(bt)
        summaries[r["label"]] = {"corr": out["corr"],
                                 **combine_mod.best_weight_is_a_plateau(out["table"])}
    if rows:
        bt = pd.concat(rows, ignore_index=True)
        save(bt, "31_trend_x_reversal_blend")
        save_json(summaries, "31b_blend_summary")
        print("\n  blend surface (Sharpe):", flush=True)
        piv = bt.pivot(index="reversal_book", columns="overlay_weight", values="Sharpe")
        print(piv.to_string(), flush=True)
        print("\n  correlation / plateau:", flush=True)
        print(pd.DataFrame(summaries).T.to_string(), flush=True)
    return res, rows


def stage_regime(bar, rebal, start, end, insts, n_jobs, quick):
    """BTC-volatility regime gating, evaluated the only way that can be trusted.

    §3.3 of the roadmap warns this is the highest-overfitting-risk idea in the
    project: the regime *thresholds* are themselves free parameters fitted on the
    same sample that judges them.  So the grid is scored by
    `walk_forward_selection` -- pick the best cell on each fold's train window,
    then read its untouched test window -- and the per-fold winners are printed.
    Drifting winners mean the rule was fitted, not found.
    """
    print("\n=== [regime] BTC-vol regime grid (walk-forward scored) ===", flush=True)
    by = _inst_list(insts)[1]
    specs = [{"label": "vol_cap_off", "overrides": {"risk.btc_vol_cap_enabled": False},
              "rebalance_bars": rebal}]
    for soft in (0.50, 0.60, 0.70):
        for hard in (1.00, 1.20, 1.60):
            for mins in (0.35, 0.60):
                specs.append({
                    "label": f"s{soft}_h{hard}_m{mins}",
                    "overrides": {"risk.btc_vol_soft": soft, "risk.btc_vol_hard": hard,
                                  "risk.btc_vol_min_scale": mins},
                    "rebalance_bars": rebal})
    if quick:
        specs = specs[:7]
    res = run_sweep(merge_ov(specs), bar, start, end, insts=by, n_jobs=n_jobs, desc="regime")
    t = sweep_table(res)
    save(t, "32_vol_regime_grid")
    print(t.sort_values("Sharpe", ascending=False).to_string(index=False), flush=True)
    folds = wf_mod.fold_metrics_table(res)
    save(folds, "32b_vol_regime_folds")
    sel = wf_mod.walk_forward_selection(res)
    save(sel, "32c_vol_regime_wf_selection")
    print("\n  walk-forward selection (thresholds re-picked per fold):", flush=True)
    print(sel.to_string(index=False), flush=True)
    stab = bands = None
    try:
        from ..analysis.sensitivity import plateau_score
        piv = t.set_index("label")["Sharpe"]
        bands = piv.describe().to_dict()
    except Exception as e:                                          # noqa: BLE001
        print(f"  regime plateau helper skipped: {type(e).__name__}: {e}", flush=True)
    if bands:
        save_json(bands, "32d_vol_regime_distribution")
        print("  Sharpe distribution across the grid:", bands, flush=True)
    return res, sel


def _cfg_get(cfg, dotted: str):
    t = cfg
    for p in dotted.split("."):
        t = getattr(t, p)
    return t


def stage_decomp(bar, rebal, start, end, insts, n_jobs, quick):
    """Leave-one-out attribution of the global overrides currently in force.

    A bundle of four changes is not evidence that any of them helped.  This stage
    re-runs the whole sweep with exactly one override reverted to its code default
    at a time, so the table reads "what did this change buy?" rather than "does the
    bundle work?".  It is driven by `--override`, so it always decomposes whatever
    recipe the run was launched with.

    Caveat printed with the table: leaving one override out also changes the other
    values incidentally, so if the effects interact the single-arm deltas will not
    sum to the bundle's total effect.  That is the honest reading, not a defect.
    """
    print("\n=== [decomp] leave-one-out attribution of the active overrides ===", flush=True)
    if not BASE_OVERRIDES:
        print("  no --override in force -> nothing to decompose", flush=True)
        return None
    ref = default_config(bar=bar, rebalance_bars=rebal)
    keys = sorted(BASE_OVERRIDES)
    specs = [{"label": "all_on", "overrides": {}, "rebalance_bars": rebal}]
    for k in keys:
        specs.append({"label": f"without[{k}]", "overrides": {k: _cfg_get(ref, k)},
                      "rebalance_bars": rebal})
    print(f"  reverting one at a time: {keys}", flush=True)
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1],
                    n_jobs=n_jobs, desc="decomp")
    t = sweep_table(res)
    ref_sharpe = float(t.loc[t["label"] == "all_on", "Sharpe"].iloc[0]) if len(t) else np.nan
    t["delta_vs_all_on"] = t["Sharpe"] - ref_sharpe
    t = t.sort_values("Sharpe", ascending=False)
    save(t, "33_override_decomposition")
    print(t[["label", "CAGR", "Sharpe", "max_dd", "ann_turnover", "cost_drag",
             "delta_vs_all_on"]].to_string(index=False), flush=True)
    return res


def stage_neutral(bar, rebal, start, end, insts, n_jobs, quick):
    """Factor-neutralisation study (brief §38's "Factor Neutralization").

    Neutralisation matters in proportion to how redundant the input factors are --
    momentum <-> range_pos correlate 0.71 -- so the informative experiment is a
    cross-product of *factor set* and *neutralise on/off*, not neutralisation on its
    own.  If orthogonalising the four-factor book beats simply dropping the
    redundant factors, neutralisation earns its complexity; if not, the smaller
    factor set is the better answer (Occam, and one less fitted degree of freedom).
    """
    print("\n=== [neutral] factor neutralisation vs factor pruning ===", flush=True)
    sets = {
        "spec4(mom,flow,rp,hr)": ("momentum", "flow", "range_pos", "hitrate"),
        "v2(rp,hr)": ("range_pos", "hitrate"),
        "all5+rev_short": ("momentum", "flow", "range_pos", "hitrate", "rev_short"),
    }
    specs = []
    for name, sub in sets.items():
        for neu in (False, True):
            specs.append({
                "label": f"{name}|neutralize={neu}",
                "overrides": {"factors.neutralize": neu},
                "kwargs": {"factor_subset": sub},
                "rebalance_bars": rebal,
            })
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1],
                    n_jobs=n_jobs, desc="neutral")
    t = sweep_table(res)
    save(t, "34_neutralisation_study")
    print(t[["label", "CAGR", "Sharpe", "max_dd", "ann_turnover", "cost_drag"]]
          .sort_values("Sharpe", ascending=False).to_string(index=False), flush=True)
    folds = wf_mod.fold_metrics_table(res)
    save(folds, "34b_neutralisation_folds")
    print("\n  per-fold test Sharpe:", flush=True)
    print(folds.pivot(index="config", columns="fold", values="test_sharpe").to_string(),
          flush=True)
    return res


def stage_wf(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [wf] walk-forward ===", flush=True)
    res = load_baseline()["result"]
    fold_tbl = wf_mod.fold_metrics_table([{"label": "baseline", "bars": res.bars}])
    save(fold_tbl, "18_walkforward_folds")
    print(fold_tbl.to_string(index=False), flush=True)
    stab = wf_mod.stability_table([{"label": "baseline", "bars": res.bars}])
    save(stab, "19_period_stability")
    print(stab.to_string(index=False), flush=True)

    # ---- finer folds + the locked out-of-sample slice ------------------------
    # Four annual folds cannot separate "stable" from "lucky".  These are extra
    # files (18b/18c) rather than replacements for 18, so an existing 4-fold report
    # stays comparable.
    q = wf_mod.fold_metrics_table([{"label": "baseline", "bars": res.bars}],
                                  folds=wf_mod.QUARTERLY_FOLDS)
    save(q, "18b_walkforward_quarterly")
    te = q["test_sharpe"].dropna().to_numpy()
    if te.size:
        print(f"\n  quarterly folds: n={te.size}, min={te.min():.3f}, median={np.median(te):.3f}, "
              f"max={te.max():.3f}, share_positive={float((te > 0).mean()):.2f}", flush=True)
        save_json({"n_folds": int(te.size), "min": float(te.min()),
                   "median": float(np.median(te)), "max": float(te.max()),
                   "mean": float(te.mean()), "std": float(te.std(ddof=1)) if te.size > 1 else None,
                   "share_positive": float((te > 0).mean()),
                   "test_sharpe": te.tolist()}, "18d_quarterly_fold_summary")
    lo = wf_mod.LOCKED_OOS
    locked = wf_mod.slice_metrics(res.bars, *lo["window"], name=lo["name"])
    save(pd.DataFrame([locked]), "18c_locked_oos")
    print(f"  locked OOS {lo['name']}: Sharpe={locked['Sharpe']:.3f} "
          f"CAGR={locked['CAGR']:.4f} maxDD={locked['max_dd']:.4f}", flush=True)
    return fold_tbl, stab


def stage_mc(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [mc] Monte-Carlo nulls ===", flush=True)
    by = _inst_list(insts)[1]
    real = met.compute_metrics(load_baseline()["result"].bars)
    real_sharpe = real["Sharpe"]
    real_cagr = real["CAGR"]
    n_perm = 40 if quick else 200

    out = {}
    for kind in ("cross_section_permute", "random_score"):
        specs = mc_mod.permute_specs(n_perm, kind, rebalance_bars=rebal)
        r = run_sweep(merge_ov(specs), bar, start, end, insts=by, n_jobs=n_jobs, desc=kind)
        sh = [x["metrics"]["Sharpe"] for x in r if "error" not in x]
        cg = [x["metrics"]["CAGR"] for x in r if "error" not in x]
        out[kind] = {"sharpe": mc_mod.placebo_summary(real_sharpe, sh),
                     "cagr": mc_mod.placebo_summary(real_cagr, cg)}
        save(pd.DataFrame({"Sharpe": sh, "CAGR": cg}), f"20_mc_{kind}")
        print(f"  {kind}: {out[kind]['sharpe']}", flush=True)
        plots.montecarlo_dist(real_sharpe, sh, f"Placebo: {kind}", CHART,
                              f"20_mc_{kind}.png")

    # surrogate price paths need new panels per iteration -> load once, shuffle locally
    n_blk = 10 if quick else 30
    panels = load_panels(bar, start, end, insts=by)
    cfg = base_cfg(bar, rebal, start, end)
    df = mc_mod.run_block_shuffle_mc(panels, cfg, lambda p, c: run_backtest(p, c), n=n_blk)
    save(df, "21_mc_block_shuffle")
    out["block_shuffle"] = {"sharpe": mc_mod.placebo_summary(
        real_sharpe, df.get("Sharpe", pd.Series(dtype=float)).dropna().tolist())}
    print(f"  block_shuffle: {out['block_shuffle']['sharpe']}", flush=True)
    plots.montecarlo_dist(real_sharpe, df.get("Sharpe", pd.Series(dtype=float)).dropna().tolist(),
                          "Surrogate price paths (block shuffle)", CHART,
                          "21_mc_block_shuffle.png")
    save_json(out, "22_montecarlo_summary")
    return out


def stage_capacity(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [capacity] AUM ladder ===", flush=True)
    caps = [1e4, 5e4, 1e5, 5e5, 1e6, 5e6, 1e7]
    specs = cap_mod.capital_specs(caps, rebalance_bars=rebal)
    res = run_sweep(merge_ov(specs), bar, start, end, insts=_inst_list(insts)[1], n_jobs=n_jobs,
                    desc="capacity")
    t = cap_mod.capacity_table(res)
    gs = [gross_stats(r["bars"]) for r in res if "bars" in r]
    if gs:
        t = pd.concat([t.reset_index(drop=True), pd.DataFrame(gs)], axis=1)
    save(t, "23_capacity")
    be = cap_mod.capacity_breakeven(t, "Sharpe", 0.5)
    save_json(be, "23b_capacity_breakeven")
    print(t.to_string(index=False), flush=True)
    print("  breakeven:", be, flush=True)
    return t


def load_baseline():
    p = os.path.join(ART, "baseline.pkl")
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"{p} not found -- run the 'base' stage first (it also produces the "
            f"position path every other stage reads)")
    with open(p, "rb") as f:
        return pickle.load(f)


def stage_bias(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [bias] hidden-bias audit ===", flush=True)
    by = _inst_list(insts)[1]
    panels = load_panels(bar, start, end, insts=by)
    cfg = base_cfg(bar, rebal, start, end)
    n = 8 if quick else 30
    df = bias_mod.run_delisting_stress(panels, cfg, lambda p, c: run_backtest(p, c), n_iter=n)
    save(df, "24_delisting_stress")
    res = load_baseline()["result"]
    audit = bias_mod.bias_audit_summary(panels, res, df)
    save(audit, "25_bias_audit")
    print(audit.to_string(index=False), flush=True)
    lt = bias_mod.listing_table(panels, res)
    save(lt, "26_listing_table")
    churn = bias_mod.universe_churn(res)
    # Save the monthly summary as the primary artifact: the raw daily table begins in
    # the warm-up window where the pool holds only a few names, so a truncated view is
    # misleading.  Keep the full daily series alongside it for auditability.
    save(bias_mod.churn_summary(churn), "27_universe_churn")
    save(churn, "27b_universe_churn_daily")
    print(bias_mod.churn_summary(churn).head(12).to_string(), flush=True)
    return df, audit


def stage_charts(bar, rebal, start, end, insts, n_jobs, quick):
    print("\n=== [charts] figures ===", flush=True)
    d = load_baseline()
    res, cfg = d["result"], d["cfg"]
    bars = res.bars

    def guard(label, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except Exception as e:                               # noqa: BLE001
            print(f"  chart '{label}' skipped: {type(e).__name__}: {e}", flush=True)

    guard("equity", plots.equity_curve, bars, CHART)
    guard("long_short", plots.long_short_equity, bars, CHART)
    guard("drawdown", plots.drawdown_curve, bars, CHART)
    guard("monthly", plots.monthly_heatmap, met.monthly_returns(bars), CHART)
    guard("rolling", plots.rolling_panels, met.rolling_stats(bars, 30), bars, CHART)
    guard("costs", plots.cost_curves, bars, CHART)

    def read(name):
        p = os.path.join(TAB, name)
        return pd.read_csv(p) if os.path.exists(p) else None

    tab = read("11_rank_ic_by_horizon.csv")
    if tab is not None:
        guard("ic", plots.ic_charts, tab, read("13_ic_decay.csv"), read("12_rank_ic_by_factor.csv"),
              CHART)

    for f, t, n in (("17_grid_mom_x_K.csv", "Momentum lookback x TopK (Sharpe)", "17_grid_mom_x_K.png"),
                    ("17_grid_flowS_x_flowL.csv", "Flow short x long (Sharpe)", "17b_grid_flow.png"),
                    ("17_grid_K_x_tvol.csv", "TopK x target vol (Sharpe)", "17c_grid_K_vol.png")):
        g = read(f)
        if g is not None:
            guard(f, plots.sensitivity_heatmap, g.set_index(g.columns[0]), CHART, t, n)

    c = read("23_capacity.csv")
    if c is not None:
        guard("capacity", plots.capacity_curve, c, CHART)

    fa = read("10_factor_ablation.csv")
    reg = read("04_regime_attribution.csv")
    if fa is not None and reg is not None:
        guard("attribution", plots.attribution_charts,
              att.long_short_attribution(bars),
              fa.rename(columns={"subset": "run"}), reg, CHART)
        guard("regime_heatmap", plots.regime_heatmap, reg, CHART)
    guard("coin", plots.coin_bar, att.coin_attribution(res), CHART)
    fr = read("08_rebalance_frequency.csv")
    if fr is not None:
        fr["label"] = fr["rebalance"]
        guard("freq_tradeoff", plots.turnover_frequency_chart, fr, CHART)
    print("  charts written to artifacts/charts/", flush=True)


# ---------------------------------------------------------------------------
_INSTS_CACHE: Dict[str, Optional[List[str]]] = {}


def _inst_list(insts):
    return "all", insts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="1h")
    ap.add_argument("--rebalance-days", type=float, default=1.0)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-09-26")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--n-jobs", type=int, default=4)
    ap.add_argument("--max-insts", type=int, default=0)
    ap.add_argument("--asset-class", default="all", choices=list(ASSET_CLASSES),
                    help="scope the traded pool; 'crypto' drops OKX's tokenised "
                         "equity/ETF and commodity perps (instCategory 3/4)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--override", action="append", default=[],
                    help="dotted config override, e.g. execution.max_daily_turnover=0.1")
    ap.add_argument("--stages", nargs="*", default=[
        "base", "exec", "beta", "funding", "freq", "turnover", "ablation", "ic",
        "sens", "wf", "mc", "capacity", "bias", "charts"])
    a = ap.parse_args()

    set_tag(a.tag)
    for o in a.override:
        k, v = parse_override(o)
        BASE_OVERRIDES[k] = v
    if BASE_OVERRIDES:
        print(f"### global overrides: {BASE_OVERRIDES}", flush=True)

    from ..config.settings import BARS_PER_DAY as _BPD
    rebal = max(1, int(round(a.rebalance_days * _BPD[a.bar])))
    insts = None
    if a.max_insts:
        p = os.path.join(CACHE, "meta", "candidate_pool.json")
        with open(p) as f:
            insts = sorted(set(json.load(f)["by_volume"][: a.max_insts] + ["BTC-USDT-SWAP",
                                                                          "ETH-USDT-SWAP"]))
    if a.asset_class != "all":
        cats = load_categories()
        pool = insts if insts is not None else list_cached_insts(a.bar)
        scoped, unknown = filter_insts(pool, cats, a.asset_class)
        if unknown:
            raise SystemExit(
                f"asset-class scope '{a.asset_class}' cannot classify {len(unknown)} "
                f"cached instrument(s): {unknown[:8]}{'...' if len(unknown) > 8 else ''}\n"
                f"  refresh the frozen snapshot: "
                f"python -m crypto_ls_research.data.asset_class --build")
        dropped = [i for i in pool if i not in set(scoped)]
        detail = scope_summary(dropped, cats)
        print(f"### asset_class={a.asset_class}: {len(pool)} instruments -> {len(scoped)} "
              f"(dropped {len(dropped)}: {detail})", flush=True)
        save_json({"asset_class": a.asset_class, "requested": len(pool),
                   "kept": len(scoped), "dropped": dropped,
                   "dropped_by_class": detail, "kept_insts": scoped},
                  "00b_universe_scope")
        insts = scoped
    t0 = time.time()
    print(f"### research run: bar={a.bar} rebalance={rebal} bars "
          f"({a.rebalance_days}d) window={a.start}..{a.end} jobs={a.n_jobs}", flush=True)

    summary = {}
    for s in a.stages:
        fn = {
            "base": stage_base, "exec": stage_exec, "beta": stage_beta,
            "funding": stage_funding, "freq": stage_freq, "turnover": stage_turnover,
            "ablation": stage_ablation, "ic": stage_ic, "sens": stage_sens,
            "wf": stage_wf, "mc": stage_mc, "capacity": stage_capacity,
            "bias": stage_bias, "charts": stage_charts,
            "multi": stage_multi, "regime": stage_regime, "decomp": stage_decomp,
            "neutral": stage_neutral,
        }.get(s)
        if fn is None:
            print(f"  unknown stage: {s}", flush=True)
            continue
        st = time.time()
        try:
            summary[s] = fn(a.bar, rebal, a.start, a.end, insts, a.n_jobs, a.quick)
        except Exception as e:                                # noqa: BLE001
            import traceback
            print(f"  !! stage {s} failed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        print(f"  [{s}] {time.time()-st:.0f}s", flush=True)

    print(f"\n### total {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
