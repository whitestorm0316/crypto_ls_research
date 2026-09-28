"""Independent look-ahead audit on the REAL panel (not synthetic test data).

The repo's `tests/test_no_lookahead.py` proves causality on synthetic panels.
This script re-runs the same *idea* against the actual cached OKX panels and the
actual recommended config, so the guarantee is not lost in the gap between
"toy data" and "the thing we intend to trade".

Method (single direction, no statistics, no thresholds to tune):
    1. run the backtest on the real panel                     -> baseline
    2. corrupt EVERYTHING at and after bar t0 (prices * U[0.2,5],
       amounts * U[1e3,1e6], funding shifted)                  -> mutated
    3. assert, for all rebalance points STRICTLY BEFORE t0:
           - selected names identical
           - scores bit-identical
           - net_ret[:t0-1] identical to 1e-12
    4. assert the mutation actually BIT (otherwise step 3 is vacuous): the
       post-t0 returns must differ.

A pass means: no information at or after t0 can reach any decision made before
t0.  That is exactly what "no look-ahead" means, and it is checked end to end
(panels -> factors -> cross-section -> PIT -> sizing -> costs -> P&L).
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crypto_ls_research.config.settings import default_config   # noqa: E402
from crypto_ls_research.data.store import load_panels           # noqa: E402
from crypto_ls_research.backtest.engine import run_backtest     # noqa: E402

BAR = "1h"
START = "2021-01-01"
END = "2026-09-26"

# The recommended configuration (MEMORY.md "对外唯一交付"), expressed the way the
# research entrypoint expresses it.
OVERRIDES = {
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
    "factors.subset": ["range_pos", "hitrate"],
    "execution.exec_price": "next_open",
}


def make_cfg(rebalance_days: int):
    cfg = default_config(bar=BAR, rebalance_bars=rebalance_days * 24)
    cfg.start, cfg.end = START, END
    for k, v in OVERRIDES.items():
        parts = k.split(".")
        t = cfg
        for p in parts[:-1]:
            t = getattr(t, p)
        setattr(t, parts[-1], v)
    return cfg


def mutate_future(p, t0: int, seed: int = 20260926):
    """Corrupt everything at/after t0.  Past rows must stay byte-identical."""
    rng = np.random.default_rng(seed)
    T, N = p.close.shape
    n = T - t0
    pm = rng.uniform(0.2, 5.0, size=(n, N))
    am = rng.uniform(1e3, 1e6, size=(n, N))

    # Work on float64 numpy copies and rebuild DataFrames: the stored panels are
    # float32, and pandas refuses in-place `*=` of a float64 right-hand side on a
    # float32 block ("LossySetitemError").  Rebuilding also guarantees the past
    # rows are bit-identical (same dtype cast, same values).
    def arr(x):
        return x.to_numpy(dtype="float64").copy()

    close = arr(p.close)
    high = arr(p.high)
    low = arr(p.low)
    open_ = arr(p.open)
    amount = arr(p.amount)
    funding = arr(p.funding)

    for k in (close, high, low, open_):
        k[t0:] *= pm
    amount[t0:] = am
    funding[t0:] = funding[t0:] * 7.0 - 0.001

    def df(a, like, dtype=None):
        # Cast back to the ORIGINAL dtype.  The engine stores panels as float32;
        # feeding it a float64 close/high/low/open makes every downstream P&L
        # differ in the last bits (float32 eps ~ 1.2e-7), which would show up as a
        # spurious "look-ahead" of order 1e-7.  Matching dtype removes that
        # artefact without weakening the test.
        return pd.DataFrame(a.astype(like.to_numpy().dtype), index=like.index,
                            columns=like.columns)

    from crypto_ls_research.data.store import Panels
    # Keep the untouched series at their ORIGINAL dtype so every code path that
    # reads them sees exactly the same array it saw in the baseline run.
    return Panels(open=df(open_, p.open), high=df(high, p.high),
                  low=df(low, p.low), close=df(close, p.close),
                  vol=p.vol.copy(), vol_ccy=p.vol_ccy.copy(),
                  amount=df(amount, p.amount), funding=df(funding, p.funding),
                  list_dt=p.list_dt.copy())


def main() -> int:
    rebal = int(os.environ.get("AUDIT_REBAL_DAYS", "3"))
    t0_frac = float(os.environ.get("AUDIT_T0_FRAC", "0.55"))

    print(f"loading real panels  bar={BAR} {START}->{END} ...", flush=True)
    t_load = time.time()
    panels = load_panels(BAR, START, END)
    print(f"  {len(panels.index):,} bars x {len(panels.insts)} instruments "
          f"({panels.index[0]} -> {panels.index[-1]})  [{time.time()-t_load:.0f}s]",
          flush=True)

    cfg = make_cfg(rebal)
    T = len(panels.index)
    t0 = int(T * t0_frac)
    print(f"  rebalance_days={rebal}  T={T}  mutation starts at bar t0={t0} "
          f"({panels.index[t0]})", flush=True)

    print("\n[1/3] baseline backtest on the real panel ...", flush=True)
    t0c = time.time()
    base = run_backtest(panels, cfg)
    print(f"  done in {time.time()-t0c:.0f}s, {base.meta['n_rebalances']} rebalances",
          flush=True)

    print("\n[2/3] backtest on a panel whose future is destroyed ...", flush=True)
    mp = mutate_future(panels, t0)
    # The corruption must actually land on the past-visible boundary but leave the
    # past untouched -- that is the precondition for the whole experiment.
    same_past = np.array_equal(
        panels.close.to_numpy()[:t0], mp.close.to_numpy()[:t0], equal_nan=True)
    future_differs = not np.allclose(
        panels.close.to_numpy()[t0:], mp.close.to_numpy()[t0:], equal_nan=True)
    print(f"  past rows identical : {same_past}   (must be True)")
    print(f"  future rows differ  : {future_differs}   (must be True)")
    if not (same_past and future_differs):
        print("  !! mutation precondition failed -- audit is meaningless")
        return 1
    mut = run_backtest(mp, cfg)

    print("\n[3/3] comparing all decisions made BEFORE the mutation ...", flush=True)
    t0_ts = panels.index[t0]
    fails = []

    # --- rebalance selection -------------------------------------------------
    b_reb = [r for r in base.rebalances if r["ts"] < t0_ts]
    m_reb = [r for r in mut.rebalances if r["ts"] < t0_ts]
    print(f"  pre-t0 rebalances: base={len(b_reb)}  mutated={len(m_reb)}")
    if len(b_reb) != len(m_reb):
        fails.append(f"pre-t0 rebalance COUNT differs: {len(b_reb)} vs {len(m_reb)}")
    else:
        for i, (a, b) in enumerate(zip(b_reb, m_reb)):
            if a["ts"] != b["ts"]:
                fails.append(f"reb#{i}: ts differs {a['ts']} vs {b['ts']}")
                break
            if a["long"] != b["long"] or a["short"] != b["short"]:
                fails.append(f"reb#{i} @{a['ts']}: SELECTION differs\n"
                             f"      base L={a['long']}\n      mut  L={b['long']}")
                break
            if a["n_universe"] != b["n_universe"]:
                fails.append(f"reb#{i} @{a['ts']}: universe size differs "
                             f"{a['n_universe']} vs {b['n_universe']}")
                break

    # --- score / factor-z matrices ------------------------------------------
    n_pre = int(np.searchsorted(base.reb_ts, t0_ts, side="left")) \
        if hasattr(base, "reb_ts") else len(b_reb)
    for name in ("score_matrix",):
        A = getattr(base, name, None)
        B = getattr(mut, name, None)
        if A is None or B is None:
            continue
        A, B = np.asarray(A)[:n_pre], np.asarray(B)[:n_pre]
        if A.shape != B.shape:
            fails.append(f"{name}: shape {A.shape} vs {B.shape}")
        elif not np.allclose(A, B, rtol=0, atol=0, equal_nan=True):
            d = ~np.isclose(A, B, rtol=0, atol=0, equal_nan=True)
            fails.append(f"{name}: {int(d.sum())} cells changed before t0")

    # --- P&L ----------------------------------------------------------------
    n_ret = t0 - 1
    bb = base.bars["net_ret"].to_numpy(dtype="float64")[:n_ret]
    mb = mut.bars["net_ret"].to_numpy(dtype="float64")[:n_ret]
    worst = float(np.max(np.abs(bb - mb))) if bb.size else 0.0
    print(f"  net_ret[:{n_ret}] max abs diff = {worst:.3e}")
    if not np.allclose(bb, mb, rtol=1e-12, atol=1e-12):
        fails.append(f"net_ret differs before t0 (max abs {worst:.3e})")

    # --- the mutation must bite AFTER t0 (else the test proves nothing) ------
    post_b = base.bars["net_ret"].to_numpy(dtype="float64")[t0:]
    post_m = mut.bars["net_ret"].to_numpy(dtype="float64")[t0:]
    bite = float(np.max(np.abs(post_b - post_m))) if post_b.size else 0.0
    print(f"  net_ret[{t0}:] max abs diff = {bite:.3e}  (must be > 0)")
    if bite <= 0.0:
        fails.append("mutation did NOT change any post-t0 return -- "
                     "the invariance check above is vacuous")

    print()
    if fails:
        print("RESULT: FAIL -- look-ahead detected")
        for f in fails:
            print("  * " + f)
        return 1
    print("RESULT: PASS -- no information at/after t0 reaches any pre-t0 decision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
