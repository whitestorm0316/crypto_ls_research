"""Process-pool sweep harness.

A full backtest is a pure function of (panels, cfg, flags), so parameter grids,
ablations, walk-forward folds, Monte-Carlo shuffles and capacity curves are all just
"sweeps".  Panels are loaded once per worker process (they are far too large to ship
through the task queue); only compact result summaries travel back.

Override syntax: dotted paths into BacktestConfig, e.g.
    {"factors.mom_lookback_days": 7.5, "portfolio.top_k": 20}
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import fields, is_dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResult, run_backtest
from ..config.settings import BacktestConfig, default_config
from ..data.store import load_panels
from .metrics import compute_metrics

_G: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
def apply_overrides(cfg: BacktestConfig, ov: Dict[str, Any]) -> BacktestConfig:
    """Deep-copy `cfg` and apply `ov` (dotted keys).  Never mutates the input."""
    import copy
    c = copy.deepcopy(cfg)
    for path, val in ov.items():
        parts = path.split(".")
        target = c
        for p in parts[:-1]:
            target = getattr(target, p)
        if not hasattr(target, parts[-1]):
            raise KeyError(f"unknown config path: {path}")
        setattr(target, parts[-1], val)
    return c


def _worker_init(bar: str, start: str, end: str, insts: Optional[list], cache: Optional[str]):
    _G["panels"] = load_panels(bar, start, end, insts=insts,
                               **({"cache": cache} if cache else {}))


def _summarise(label: str, res: BacktestResult, keep_bars: bool = True) -> Dict[str, Any]:
    m = {k: v for k, v in compute_metrics(res.bars, name=label).items()
         if not k.startswith("_")}
    out: Dict[str, Any] = {"label": label, "metrics": m, "meta": res.meta,
                           "reb_ts": res.reb_ts, "factor_subset": res.meta.get("factor_subset")}
    # Per-side *selection* size, i.e. the `n` that `inverse_vol_weights` sees.  This is
    # NOT the number of names in the weight matrix: the turnover budget is hard-binding,
    # so the book rotates gradually and a name that left the target selection stays on
    # the books, at a residual weight, for several rebalances.  Measured on the v3 book:
    # selection median 7-8 names per side, weight-matrix nonzeros median 15-18, and
    # `bars["n_long"]` median 14 -- three different numbers for the "same" quantity.
    # Anything that reasons about the cap (cap*n <= 1) must use this one.
    out["sel_n_long"] = np.array([len(r.get("long", ())) for r in res.rebalances],
                                 dtype="int32")
    out["sel_n_short"] = np.array([len(r.get("short", ())) for r in res.rebalances],
                                  dtype="int32")
    if keep_bars:
        # every column the metrics / scenario helpers touch must travel back, otherwise
        # downstream analysis fails with a confusing KeyError inside compute_metrics
        cols = ["net_ret", "gross_ret", "long_ret", "short_ret", "fee", "spread",
                "impact", "funding", "turnover", "gross_exposure", "net_exposure",
                "beta_exposure", "drawdown", "equity", "total_scale", "regime_scale",
                "dd_scale", "max_participation", "n_long", "n_short", "n_stale"]
        out["bars"] = res.bars[[c for c in cols if c in res.bars.columns]].copy()
    return out


def _worker_run(spec: Dict[str, Any]) -> Dict[str, Any]:
    panels = _G["panels"]
    cfg = default_config(bar=spec["bar"], rebalance_bars=spec["rebalance_bars"])
    cfg = apply_overrides(cfg, spec.get("overrides", {}))
    kw = dict(spec.get("kwargs", {}))
    seed = spec.get("seed")
    if seed is not None:
        kw["rng"] = np.random.default_rng(seed)
    res = run_backtest(panels, cfg, **kw)
    out = _summarise(spec["label"], res, keep_bars=spec.get("keep_bars", True))
    if spec.get("save_result"):
        import pickle
        with open(spec["save_result"], "wb") as f:
            pickle.dump(res, f, protocol=5)
    return out


# ---------------------------------------------------------------------------
def run_sweep(specs: Sequence[Dict[str, Any]], bar: str, start: str, end: str,
              insts: Optional[list] = None, n_jobs: int = 5,
              cache: Optional[str] = None, desc: str = "sweep",
              progress: bool = True) -> List[Dict[str, Any]]:
    specs = [dict(s, bar=bar) for s in specs]
    if n_jobs <= 1:
        _worker_init(bar, start, end, insts, cache)
        out = []
        for i, s in enumerate(specs):
            out.append(_worker_run(s))
            if progress:
                print(f"  [{desc}] {i+1}/{len(specs)}", flush=True)
        return out

    results: List[Optional[Dict]] = [None] * len(specs)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=n_jobs, initializer=_worker_init,
                             initargs=(bar, start, end, insts, cache)) as ex:
        futs = {ex.submit(_worker_run, s): i for i, s in enumerate(specs)}
        done = 0
        for f in as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as e:                                   # noqa: BLE001
                results[i] = {"label": specs[i]["label"], "error": f"{type(e).__name__}: {e}"}
                # Never fail silently: a dropped config would otherwise vanish from the
                # output table and quietly shrink the parameter grid.
                print(f"  !! [{desc}] spec '{specs[i]['label']}' failed: "
                      f"{type(e).__name__}: {e}", flush=True)
            done += 1
            if progress and (done % 5 == 0 or done == len(specs)):
                el = time.time() - t0
                print(f"  [{desc}] {done}/{len(specs)}  {el:.0f}s "
                      f"(eta {el/done*(len(specs)-done):.0f}s)", flush=True)
    return [r for r in results if r is not None]


def sweep_table(results: Sequence[Dict]) -> pd.DataFrame:
    from .metrics import bars_variant, compute_metrics
    rows = []
    for r in results:
        if "error" in r:
            rows.append({"label": r["label"], "error": r["error"]})
            continue
        m = r["metrics"]
        row = {
            "label": r["label"],
            "CAGR": m["CAGR"], "ann_vol": m["Annualized Volatility"], "Sharpe": m["Sharpe"],
            "Sortino": m["Sortino"], "Calmar": m["Calmar"], "max_dd": m["Max Drawdown"],
            "ann_turnover": m["Annual Turnover"],
            "cost_drag": m["Cost Drag (annual)"],
            "funding": m["Funding P&L (total, frac)"],
            "fee": m["Trading Fee (total, frac)"],
            "avg_gross": m["Gross Exposure (avg)"],
            "avg_abs_beta": m["Beta Exposure (avg abs)"],
            "long_pnl": m["Long PnL (total)"], "short_pnl": m["Short PnL (total)"],
        }
        bars = r.get("bars")
        if bars is not None:
            for kind, tag in (("gross", "gross"), ("no_trading_cost", "zero_fee")):
                mm = compute_metrics(bars_variant(bars, kind), kind)
                row[f"Sharpe_{tag}"] = mm["Sharpe"]
                row[f"CAGR_{tag}"] = mm["CAGR"]
        rows.append(row)
    return pd.DataFrame(rows)
