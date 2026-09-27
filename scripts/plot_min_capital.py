"""Figure for the small-capital question: granularity coverage + leverage paths."""
from __future__ import annotations

import os
import pickle
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "artifacts", "min_capital")
os.makedirs(OUT, exist_ok=True)

g = pd.read_csv(os.path.join(OUT, "granularity_scenarios.csv"))
lev = pd.read_csv(os.path.join(OUT, "leverage_scenarios.csv"))

res = pickle.load(open(os.path.join(ROOT, "artifacts", "v3", "baseline.pkl"), "rb"))["result"]
net = res.bars["net_ret"].astype(float)

fig, ax = plt.subplots(1, 2, figsize=(14.5, 5.4))

# ---- left: signal coverage vs effective capital ---------------------------
x = g["capital_usd"].to_numpy(dtype=float)
ax[0].plot(x, g["signal_coverage_median"] * 100, "o-", color="#1f5fa8", lw=2.2,
           label="median coverage of the book")
ax[0].plot(x, g["signal_coverage_p10"] * 100, "s--", color="#c0392b", lw=1.6, ms=4,
           label="10th percentile coverage")
ax[0].axhline(95, color="#555", ls=":", lw=1.2)
ax[0].text(78, 95.8, "95% acceptance", fontsize=9, color="#555")
ax[0].axvline(70, color="#e08a1e", lw=2.0)
ax[0].annotate("$70\n(L=1)", xy=(70, 30), xytext=(105, 36), fontsize=10, color="#b5720f",
               arrowprops=dict(arrowstyle="->", color="#e08a1e", lw=1.6))
ax[0].axvspan(70, 700, color="#e08a1e", alpha=0.06)
ax[0].set_xscale("log")
ax[0].set_xlabel("effective capital, USD (log scale)   —   $70 with L=1..10 spans 70..700")
ax[0].set_ylabel("% of intended book actually tradeable")
ax[0].set_title("Order granularity, not margin, is the small-account constraint")
ax[0].set_ylim(30, 103)
ax[0].legend(loc="lower right", fontsize=9)
ax[0].grid(alpha=0.25)

# ---- right: levered equity curves ----------------------------------------
for L, col in [(1, "#1f5fa8"), (2, "#2e8b57"), (4, "#e08a1e"), (6, "#c0392b"), (10, "#7d3c98")]:
    eq = (1.0 + net * L).cumprod()
    dd = (eq / eq.cummax() - 1.0).min() * 100
    ax[1].plot(eq.index, eq.to_numpy(), lw=1.8, color=col,
               label=f"L={L}x   MDD {dd:.1f}%")
ax[1].set_yscale("log")
ax[1].set_ylabel("equity (log, multiple of starting capital)")
ax[1].set_title("Leverage fixes granularity but scales the drawdown linearly")
ax[1].legend(loc="upper left", fontsize=9)
ax[1].grid(alpha=0.25, which="both")
ax[1].tick_params(axis="x", rotation=0)

# annotate the trade-off in the safe part of the axes
ax[1].text(eq.index[20], 45, "$70 at L=4 buys 96% coverage\nbut must survive a -42.9% drawdown",
           fontsize=9, color="#b5720f",
           bbox=dict(fc="#fdf3e3", ec="#e08a1e", alpha=0.9, boxstyle="round,pad=0.4"))

fig.suptitle("Can the delivered config run on $70?  OKX USDT perps, real contract specs "
             "(minSz/lotSz/ctVal) x real v3 weights x real prices", fontsize=11.5)
fig.tight_layout(rect=(0, 0, 1, 0.94))
p = os.path.join(OUT, "min_capital.png")
fig.savefig(p, dpi=145)
print("saved", p)
