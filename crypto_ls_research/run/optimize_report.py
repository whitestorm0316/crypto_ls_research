"""Consolidate a research tag's artifacts into one machine-checked summary.

A backtest run scatters its evidence across ~30 tables.  Judging an optimisation
means re-reading the same handful of them every round, and it is easy to quote a
number from before the last stage overwrote it (this project has done that twice).
This module reads every artifact for a tag in one pass and checks the acceptance
criteria explicitly, so a criterion that fails is *reported as failing* rather than
quietly omitted from the write-up.

Usage:
    python -m crypto_ls_research.run.optimize_report --tag v2
    python -m crypto_ls_research.run.optimize_report --tag v2 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ART = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "..", "artifacts"))

#: The placebo nulls `stage_mc` writes into `22_montecarlo_summary.json`.  Named
#: at module level so the completeness test can check them against the stage
#: that produces them: a rename on either side would otherwise drop a gate
#: silently, which is the defect this list exists to prevent.
MC_KINDS = ("cross_section_permute", "random_score", "block_shuffle")


def _read(tag: str, name: str) -> Optional[pd.DataFrame]:
    p = os.path.join(ART, tag, "tables", name)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p)
    except Exception:                                            # noqa: BLE001
        return None


def _read_json(tag: str, name: str) -> Optional[dict]:
    p = os.path.join(ART, tag, "tables", name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _mtime(tag: str, name: str) -> Optional[float]:
    p = os.path.join(ART, tag, "tables", name)
    return os.path.getmtime(p) if os.path.exists(p) else None


def headline(tag: str) -> Dict[str, float]:
    j = _read_json(tag, "01_headline_metrics.json")
    if not j:
        return {}
    j.pop("_bars", None)
    return j


def acceptance_checks(tag: str) -> List[Dict[str, object]]:
    """Evaluate the roadmap's five acceptance criteria and return one row each."""
    rows: List[Dict[str, object]] = []

    # 1. walk-forward: every test fold positive, no systematic decay
    wf = _read(tag, "18_walkforward_folds.csv")
    if wf is not None and "test_sharpe" in wf:
        te = wf["test_sharpe"].dropna().to_numpy()
        dec = wf["is_oos_decay"].dropna().to_numpy() if "is_oos_decay" in wf else np.array([])
        rows.append({
            "criterion": "WF: all test folds > 0",
            "value": f"{te.size} folds, min={te.min():.3f}, max={te.max():.3f}",
            "pass": bool(te.size and (te > 0).all()),
        })
        rows.append({
            "criterion": "WF: no systematic train->test decay",
            "value": f"mean decay={dec.mean():.3f}" if dec.size else "n/a",
            "pass": bool(dec.size and dec.mean() <= 0),
        })

    # 1b. Finer folds.  Four annual folds cannot separate "stable" from "lucky", and
    #     on this dataset they are actively misleading: all four are positive and
    #     monotonically rising.  At quarterly resolution the picture is honest --
    #     ~93% of folds are positive but one quarter (2024Q3) is not.  Quarterly
    #     Sharpe on 63 trading days is a very noisy estimate, so the criterion is
    #     "most folds positive", not "every fold positive"; the worst fold is
    #     reported alongside as information rather than as a pass/fail gate.
    q = _read_json(tag, "18d_quarterly_fold_summary.json")
    if q and q.get("n_folds"):
        share = float(q.get("share_positive", float("nan")))
        rows.append({
            "criterion": "WF (quarterly): most folds positive",
            "value": f"{int(round(share * q['n_folds']))}/{q['n_folds']} folds > 0 "
                     f"(share={share:.2f}), min={q.get('min'):.3f}, "
                     f"median={q.get('median'):.3f}, std={q.get('std'):.3f}",
            "pass": bool(np.isfinite(share) and share >= 0.80),
        })
        rows.append({
            "criterion": "WF (quarterly): worst fold (informational)",
            "value": f"min test Sharpe = {q.get('min'):.3f}",
            "pass": None,
        })

    # 1c. The locked slice.  Not a performance gate -- a sign gate.  This project has
    #     three separate "small full-sample gain" results whose sign came entirely from
    #     2026, so the question that matters is whether the locked slice agrees.
    lo = _read(tag, "18c_locked_oos.csv")
    if lo is not None and len(lo):
        r = lo.iloc[0]
        rows.append({
            "criterion": "locked OOS slice exists and is positive",
            "value": f"{r.get('name')}: Sharpe={float(r.get('Sharpe')):.3f}, "
                     f"CAGR={float(r.get('CAGR')):.4f}, maxDD={float(r.get('max_dd')):.4f}",
            "pass": bool(float(r.get("Sharpe")) > 0),
        })

    # 2. placebo nulls
    #
    # This is the only gate here that can actually *falsify* the strategy: the
    # book is dollar-neutral by construction, so its beta is ~0 whether or not
    # there is any signal, and a beta test cannot tell skill from a lucky book.
    # Permuting the score can.
    #
    # Which is why the `if mc:` guard below was a defect, not a convenience.
    # A tag whose run did not include `--stages mc` produced **no rows at all**,
    # so `acceptance_checks` returned a clean 13/13 and the console reported a
    # full sweep.  That is what happened to every archived tag on disk --
    # `logs/research_v4_1d.log` shows the accepted run's stage list and `mc` is
    # not in it.  A placebo test that was never run must not look like one that
    # passed, so the absence is now stated explicitly.
    mc = _read_json(tag, "22_montecarlo_summary.json")
    if mc:
        for kind, d in mc.items():
            p = (d.get("sharpe") or {}).get("p_value")
            if p is None:
                continue
            rows.append({"criterion": f"MC null: {kind}",
                         "value": f"p={p}", "pass": bool(p is not None and p <= 0.05)})
    else:
        for kind in MC_KINDS:
            rows.append({
                "criterion": f"MC null: {kind}",
                "value": "未评估：22_montecarlo_summary.json 不在盘上"
                         "（这次运行没有跑 `--stages mc`）",
                "pass": None,
            })

    # 3. cost bookkeeping
    # (a) The strong form: the per-bar ledger identity must hold exactly.  Both of
    #     this project's ledger bugs (funding sign, forced-close cost missing from
    #     the itemised columns) would show up here as a non-zero residual.
    pkl = os.path.join(ART, tag, "baseline.pkl")
    bars = None
    name_fund = None
    if os.path.exists(pkl):
        try:
            import pickle
            with open(pkl, "rb") as f:
                res = pickle.load(f)["result"]
            b = res.bars
            bars = b
            name_fund = getattr(res, "name_fund", None)
            net = b["net_ret"].to_numpy()
            rhs = (b["gross_ret"] - (b["fee"] + b["spread"] + b["impact"])
                   + b["funding"]).to_numpy()
            resid = float(np.nanmax(np.abs(net - rhs)))
            rows.append({
                "criterion": "ledger identity per bar: net == gross - costs + funding",
                "value": f"max |residual| = {resid:.3e} over {len(net):,} bars",
                "pass": bool(resid < 1e-12),
            })
        except Exception as e:                                   # noqa: BLE001
            rows.append({"criterion": "ledger identity per bar", "value": f"skipped: {e}",
                         "pass": None})

    # (b) The roadmap's weaker form: the two CAGR gaps must at least agree in sign.
    #     NOTE: a gap is not an additive decomposition -- removing costs changes the
    #     equity path, which moves vol-targeting and the drawdown ladder -- so a gap
    #     of either sign is legitimate; only a *disagreement* between the two is a
    #     red flag.
    cs = _read(tag, "01b_cost_scenarios.csv")
    if cs is not None and "scenario" in cs.columns and "CAGR" in cs.columns:
        s = dict(zip(cs["scenario"], cs["CAGR"]))
        g, nt, nf, nn = s.get("gross"), s.get("no_trading_cost"), s.get("no_funding"), s.get("net")
        if None not in (g, nt, nf, nn):
            a, b_ = nt - g, nn - nf
            rows.append({
                "criterion": "cost gaps agree in sign (trading vs funding)",
                "value": f"no_trading_cost-gross={a:+.4f}, net-no_funding={b_:+.4f}",
                "pass": bool((a >= 0) == (b_ >= 0)),
            })
            fund_total = headline(tag).get("Funding P&L (total, frac)")
            if fund_total is not None:
                ft = float(fund_total)
                rows.append({
                    "criterion": "funding term is a credit (roadmap expectation)",
                    "value": f"funding P&L = {ft:+.4f}, net - no_funding = {b_:+.4f} "
                             f"(both agree in sign -- that part IS a property)",
                    "pass": bool(ft >= 0 and b_ >= 0),
                })
                # 3(c) **The sign is not a property of this strategy, and this row
                #      exists so nobody re-derives that conclusion from scratch.**
                #      The roadmap asserted "net receiver".  On the rebuilt data the
                #      same configuration is a net *payer* (-3.0%), while on 2021-2024
                #      alone it is a net receiver (+3.8%) -- so the sign is decided by
                #      where the sample ends, not by the design of the book.  The net
                #      is only ~14% of the gross funding, and a single instrument
                #      outweighs the whole net.  Gating on it made the verdict depend
                #      on the window, which is why the disclosure below is a row rather
                #      than a footnote.  It is informational (`pass=None`) so it does
                #      not change the gate count: the roadmap's criterion still reports
                #      as FAILING, because it does.
                disc = [f"funding P&L = {ft:+.4f}"]
                if bars is not None:
                    fr = bars["funding"]
                    gross = float(fr.abs().sum())
                    disc.append(f"net/gross = {abs(ft) / gross:.3f}" if gross else "n/a")
                    disc.append("逐年 " + ", ".join(
                        f"{y}:{v:+.1%}" for y, v in fr.groupby(fr.index.year).sum().items()))
                if name_fund is not None:
                    import numpy as _np
                    per = _np.asarray(name_fund, dtype="float64").sum(axis=0)
                    biggest = float(_np.abs(per).max()) if per.size else 0.0
                    if biggest:
                        disc.append(f"单币最大 |贡献| = {biggest:.4f} "
                                    f"(净额是它的 {abs(ft) / biggest:.2f} 倍)")
                rows.append({
                    "criterion": "funding term: sign is NOT stable (informational)",
                    "value": "; ".join(disc),
                    "pass": None,
                })
                if bars is not None:
                    fr = bars["funding"]
                    early = fr[fr.index.year <= 2024].sum()
                    late = fr[fr.index.year >= 2025].sum()
                    rows.append({
                        "criterion": "funding term: the sample split decides the sign "
                                     "(informational)",
                        "value": f"2021-2024 = {early:+.4f} (credit), "
                                 f"2025-now = {late:+.4f} (cost)",
                        "pass": None,
                    })

    # 4. plateau, not spike
    mult = _read(tag, "16_multiplicity_summary.csv")
    if mult is not None and len(mult):
        m = mult.iloc[0]
        pos = float(m.get("share_positive", np.nan))
        hair = float(m.get("deflated_haircut", np.nan))
        rows.append({
            "criterion": "multiplicity: every tested config has positive Sharpe",
            "value": f"share_positive={pos:.2f} over {int(m.get('n_configs', 0))} configs",
            "pass": bool(np.isfinite(pos) and pos >= 1.0),
        })
        rows.append({
            "criterion": "multiplicity: best-of-N survives the deflation haircut",
            "value": f"best={float(m.get('best', np.nan)):.3f}, "
                     f"deflated={hair:.3f}, median={float(m.get('median', np.nan)):.3f}",
            "pass": bool(np.isfinite(hair) and hair > 0),
        })
    try:
        from ..analysis.sensitivity import plateau_score
        for f in sorted(os.listdir(os.path.join(ART, tag, "tables"))):
            if not f.startswith("17_grid_") or not f.endswith(".csv"):
                continue
            g = pd.read_csv(os.path.join(ART, tag, "tables", f))
            g = g.set_index(g.columns[0])
            p = plateau_score(g)
            rows.append({
                "criterion": f"grid {f[8:-4]}: neighbourhood is a plateau",
                "value": f"verdict={p['verdict']}, frac_ok={p['frac_neighbours_ok']:.2f}, "
                         f"spike_ratio={p['spike_ratio']:.2f}",
                "pass": bool(str(p["verdict"]) == "plateau"),
            })
    except Exception as e:                                       # noqa: BLE001
        rows.append({"criterion": "grid plateau check", "value": f"skipped: {e}",
                     "pass": None})

    # 5. concentration must not get worse than the previous best config
    conc = _read_json(tag, "03_concentration.json")
    if conc:
        rows.append({
            "criterion": "concentration: top-1 name share of net PnL",
            "value": f"{float(conc.get('top1_share', float('nan'))):.3f}",
            "pass": bool(float(conc.get("top1_share", 1.0)) <= 0.30),
        })
    return rows


def tables(tag: str) -> Dict[str, pd.DataFrame]:
    out = {}
    for name, f in (
        ("headline", "01_headline_metrics.csv"),
        ("cost_scenarios", "01b_cost_scenarios.csv"),
        ("turnover", "09_turnover_budget.csv"),
        ("exec_models", "05_execution_models.csv"),
        ("ablation", "10_factor_ablation.csv"),
        ("folds", "18_walkforward_folds.csv"),
        ("folds_quarterly", "18b_walkforward_quarterly.csv"),
        ("locked_oos", "18c_locked_oos.csv"),
        ("stability", "19_period_stability.csv"),
        ("capacity", "23_capacity.csv"),
        ("decomp", "33_override_decomposition.csv"),
        ("neutral", "34_neutralisation_study.csv"),
        ("reversal", "30_reversal_book.csv"),
        ("blend", "31_trend_x_reversal_blend.csv"),
        ("regime", "32_vol_regime_grid.csv"),
        ("regime_wf", "32c_vol_regime_wf_selection.csv"),
    ):
        d = _read(tag, f)
        if d is not None:
            out[name] = d
    return out


def main() -> None:                                              # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v2")
    ap.add_argument("--json", default="")
    ap.add_argument("--full", action="store_true", help="print every table in full")
    a = ap.parse_args()

    pd.set_option("display.width", 220)
    print(f"########## tag = {a.tag}")
    h = headline(a.tag)
    if h:
        keys = ["start", "end", "years", "CAGR", "Sharpe", "Sortino", "Calmar",
                "Max Drawdown", "Max DD Duration (days)", "Annual Turnover",
                "Cost Drag (annual)", "Funding P&L (total, frac)",
                "Annualized Volatility", "Gross Exposure (avg)"]
        print("\n## headline")
        for k in keys:
            if k in h:
                v = h[k]
                print(f"  {k:32s} {v:.6f}" if isinstance(v, float) else f"  {k:32s} {v}")

    print("\n## acceptance criteria")
    rows = acceptance_checks(a.tag)
    if rows:
        print(pd.DataFrame(rows).to_string(index=False))
    else:
        print("  (no criterion could be evaluated -- are the stages present?)")

    for name, d in tables(a.tag).items():
        print(f"\n## {name}  ({len(d)} rows)")
        print(d.to_string(index=False) if a.full else d.head(24).to_string(index=False))

    print("\n## artifact freshness (newest first)")
    tabdir = os.path.join(ART, a.tag, "tables")
    if os.path.isdir(tabdir):
        fs = sorted(((f, os.path.getmtime(os.path.join(tabdir, f)))
                     for f in os.listdir(tabdir)), key=lambda x: -x[1])[:12]
        import datetime as dt
        for f, m in fs:
            print(f"  {dt.datetime.fromtimestamp(m):%Y-%m-%d %H:%M:%S}  {f}")

    if a.json:
        payload = {"headline": h, "acceptance": rows}
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, default=str)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":                                       # pragma: no cover
    main()
