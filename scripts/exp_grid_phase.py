"""Measure how sensitive v3 is to the rebalance-grid PHASE (the anchor clock).

Why this exists
---------------
The delivered v3 grid is anchored at **UTC 02:00 = Beijing 10:00**.  That is
not a parameter: `dec_idx = np.arange(warmup, T - 1, R)` with a warmup driven
by factor lookbacks on a panel that starts 2021-01-01 00:00 UTC.  Measured on
the real v3 run: the first decision bar is 2021-01-31 02:00 UTC and every one
of the 689 decision points sits on hour 02, minute 00.  There is no config key
for it (checked: no `anchor` / `phase` / `rebalance_hour` anywhere).

The request was "place orders at 23:30 Beijing".  Answering that honestly needs
to know what the anchor is WORTH.  Two very different worlds:

  * **Flat curve** -> the anchor is free; move it and keep quoting the Sharpe.
  * **Spike at k=0** -> the Sharpe was partly a lucky draw.  The honest
    out-of-sample expectation is then the *mean* of the curve, and the 23:30
    curve is a different strategy to be re-verified, not a re-scheduling.

Method
------
Shift the entire grid by `k` bars: `dec_idx = arange(warmup + k, T-1, R)`.
k sweeps 0..24 (one full day at 1h), so the 23:30 offset (13.5h = 13 or 14
bars) is inside the sweep.  Everything else is fixed.

The config comes from `signal.build_config`, i.e. **the same constructor the
live engine uses**, so the k=0 point is the same strategy the daemon will
actually trade.  A hard-coded expected Sharpe was deliberately NOT used: this
project has been bitten repeatedly by comparing against a stale archived pkl
(`artifacts/main/baseline.pkl` is the *spec-default* config -- 1-day grid,
4 factors, cap 0.10, turnover 0.50 -- and its Sharpe is ~0.59, not 1.93).  The
self-check is therefore "k=0 must equal the single-run reference computed in
this same process", which cannot drift.

Judged by the project's §11b rule, not by the full-sample delta:
  full-year share >= 2/3  ·  single-year contribution <= 35% of sum|delta|
  ·  the locked sample (2026) has the same sign

Run
---
    python scripts/exp_grid_phase.py
"""
from __future__ import annotations

import csv
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

import warnings                                                # noqa: E402
warnings.filterwarnings("ignore")

from crypto_ls_research.analysis.metrics import compute_metrics  # noqa: E402
from crypto_ls_research.backtest.engine import run_backtest      # noqa: E402
from crypto_ls_research.data.asset_class import (                # noqa: E402
    filter_insts, load_categories)
from crypto_ls_research.data.store import list_cached_insts, load_panels  # noqa: E402
from crypto_ls_research.execution.signal import build_config     # noqa: E402

BAR, RD, START, END = "1h", 3.0, "2021-01-01", "2026-09-26"
OUT = os.path.join(ROOT, "artifacts", "grid_phase")

# Exactly `engine.DEFAULT_SIGNAL["overrides"]` -- imported rather than retyped
# so a future change to the verified config cannot leave this script measuring
# a strategy nobody runs.
from crypto_ls_research.execution.engine import DEFAULT_SIGNAL   # noqa: E402

OV = dict(DEFAULT_SIGNAL["overrides"])
# Beijing 23:30 == UTC 15:30.  Anchor is UTC 02:00 -> offset = +13.5h
# = 13 or 14 bars.  Both are in the sweep.
TARGET_BJ = "23:30"


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    print("=" * 72)
    print("  调仓网格相位敏感度：锚点平移 k 根 1h bar（k=0..24，覆盖一整天）")
    print(f"  配置来自 engine.DEFAULT_SIGNAL（守护进程实际执行的那个）")
    print(f"  {OV}")
    print("=" * 72)

    cfg = build_config(BAR, RD, OV, start=START, end=END)
    pool = list_cached_insts(BAR)
    kept, unknown = filter_insts(pool, load_categories(), "crypto")
    if unknown:
        print(f"!! 有 {len(unknown)} 个合约无法分类，拒绝缩池：{unknown[:5]}")
        return 1
    panels = load_panels(bar=BAR, start=START, end=END, insts=kept)
    print(f"  池 {len(pool)} → crypto {len(kept)}；面板 "
          f"{len(panels.index):,} bars x {len(panels.insts)} insts")
    print(f"  rebalance_bars = {cfg.rebalance_bars}"
          f"（= {RD:g} 天 × {cfg.bars_per_day} bar/天）\n")

    rows = []
    ref = None
    for k in range(0, 25):
        t0 = time.time()
        r = run_backtest(panels, cfg, dec_offset_bars=k)
        m = compute_metrics(r.bars, name=f"k={k}")
        anchor = pd.Timestamp(r.reb_ts[0])
        sh = float(m["Sharpe"])
        if k == 0:
            ref = sh
        rows.append({
            "k_bars": k,
            "anchor_utc": str(anchor),
            "anchor_bj": str(anchor.tz_convert("Asia/Shanghai"))[:16],
            "n_reb": int(len(r.reb_ts)),
            "sharpe": sh,
            "cagr": float(m.get("CAGR", np.nan)),
            "mdd": float(m.get("Max Drawdown", np.nan)),
            "dd_days": float(m.get("Max DD Duration (days)", np.nan)),
        })
        x = rows[-1]
        tag = "  ← 基准" if k == 0 else f"  Δ {x['sharpe']-ref:+.4f}"
        print(f"  k={k:2d}  锚点(北京) {x['anchor_bj'][11:]}  "
              f"Sharpe {x['sharpe']:.4f}{tag}  "
              f"CAGR {x['cagr']*100:6.2f}%  MDD {x['mdd']*100:7.2f}%  "
              f"n={x['n_reb']}  ({time.time()-t0:.0f}s)", flush=True)

    p = os.path.join(OUT, "phase_sweep.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)

    sh = np.array([r["sharpe"] for r in rows])
    sh0 = rows[0]["sharpe"]
    print()
    print("=" * 72)
    print(f"  自校验：k=0（本次实测基准）     {sh0:.4f}")
    print(f"  全相位 均值 / 中位              {sh.mean():.4f} / {np.median(sh):.4f}")
    print(f"  全相位 std / min / max          {sh.std():.4f} / "
          f"{sh.min():.4f} / {sh.max():.4f}")
    print(f"  基准排名（1=最好）              {int((sh > sh0).sum())+1} / {len(sh)}")
    print(f"  基准 z 分数                     {(sh0 - sh.mean()) / (sh.std() or 1):+.2f}σ")
    print(f"  基准 − 全相位均值               {sh0 - sh.mean():+.4f}")

    tgt = [r for r in rows if r["anchor_bj"][11:] == TARGET_BJ]
    if tgt:
        x = tgt[0]
        print(f"  用户要的 {TARGET_BJ}(北京)          k={x['k_bars']}  "
              f"Sharpe {x['sharpe']:.4f}  Δ {x['sharpe']-sh0:+.4f}")
        print(f"                                    CAGR {x['cagr']*100:.2f}%  "
              f"MDD {x['mdd']*100:.2f}%")
    print("=" * 72)
    print(f"  → {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
