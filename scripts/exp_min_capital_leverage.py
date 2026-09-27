"""$70 with leverage: does it clear the granularity floor, and what does the
drawdown become?

Two facts drive this:
  1. Margin is NOT the constraint.  The delivered config runs at a median gross
     exposure of 0.468x NAV, so $70 needs ~$33 of notional -- trivially collateralised
     even at 10x isolated margin.
  2. Granularity IS the constraint.  $33 across ~13 effective names is ~$2.5 per
     name, while one BTC contract is $8.65 of notional.  The fix is to raise the
     notional, i.e. lever up.  Leverage multiplies the notional uniformly, so
     effective capital = principal * L, which is exactly the axis the granularity
     scan already uses.

The cost of leverage is path risk.  We recompute the equity curve on the real
1h net-return series as eq_L(t) = prod(1 + L * net_ret_t) -- exact under the
assumption of no liquidation and no extra funding cost (perp funding is already
inside net_ret).  Reported: MDD, CAGR, Sharpe, and whether the path would have
been liquidated.
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "artifacts", "min_capital")
os.makedirs(OUT, exist_ok=True)

LEV = [1, 2, 3, 4, 5, 6, 8, 10]
PRINCIPAL = 70.0


def metrics(net: pd.Series, bar_per_year: float) -> dict:
    eq = (1.0 + net).cumprod()
    dd = eq / eq.cummax() - 1.0
    n = len(net)
    years = n / bar_per_year
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0 if eq.iloc[-1] > 0 else -1.0
    sd = net.std(ddof=1)
    sharpe = net.mean() / sd * np.sqrt(bar_per_year) if sd > 0 else np.nan
    return {
        "final_equity": float(eq.iloc[-1]),
        "mdd": float(dd.min()),
        "mdd_date": str(dd.idxmin()),
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "worst_bar": float(net.min()),
        "min_equity": float(eq.min()),
        "wiped_out": bool(eq.min() <= 0.0),
    }


def main() -> None:
    res = pickle.load(open(os.path.join(ROOT, "artifacts", "v3", "baseline.pkl"), "rb"))["result"]
    bars = res.bars
    net1 = bars["net_ret"].astype(float)
    bar_per_year = 365.0 * 24.0                       # 1h bars

    base = metrics(net1, bar_per_year)
    print("=== 1x baseline (recomputed from bars.net_ret) ===")
    print(json.dumps(base, indent=2))
    print(f"gross exposure median {bars['gross_exposure'].median():.4f}, "
          f"net exposure median {np.abs(bars['net_exposure']).median():.4f}")

    rows = []
    for L in LEV:
        m = metrics(net1 * L, bar_per_year)
        rows.append({
            "leverage": L,
            "effective_capital_usd": PRINCIPAL * L,
            "notional_usd_at_gross_0468": PRINCIPAL * L * 0.468,
            "sharpe": m["sharpe"],
            "cagr": m["cagr"],
            "mdd": m["mdd"],
            "worst_bar": m["worst_bar"],
            "final_equity_x": m["final_equity"],
            "pnl_usd_on_70": PRINCIPAL * (m["final_equity"] - 1.0),
            "mdd_usd_on_70": PRINCIPAL * m["mdd"],
            "wiped_out": m["wiped_out"],
        })
        print(f"  L={L:>2}: eff ${PRINCIPAL*L:>6.0f}  Sharpe {m['sharpe']:.3f}  "
              f"CAGR {m['cagr']*100:>7.2f}%  MDD {m['mdd']*100:>7.2f}%  "
              f"worst bar {m['worst_bar']*100:>6.2f}%  "
              f"P&L on $70 {PRINCIPAL*(m['final_equity']-1.0):>8.2f}")

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, "leverage_scenarios.csv"), index=False)

    # --- merge with the granularity scan: what effective capital buys you -----
    gp = os.path.join(OUT, "granularity_scenarios.csv")
    if os.path.exists(gp):
        g = pd.read_csv(gp)
        eff = 70.0 * df["leverage"].to_numpy()
        cov = np.interp(eff, g["capital_usd"], g["signal_coverage_median"])
        names = np.interp(eff, g["capital_usd"], g["names_kept_median"])
        slip = np.interp(eff, g["capital_usd"], g["net_hedge_slip_median"])
        df["signal_coverage"] = cov
        df["names_kept"] = names
        df["net_hedge_slip"] = slip
        df.to_csv(os.path.join(OUT, "leverage_scenarios.csv"), index=False)
        print("\n=== leverage vs granularity (interpolated at 70*L) ===")
        print(df[["leverage", "effective_capital_usd", "names_kept", "signal_coverage",
                  "net_hedge_slip", "sharpe", "mdd", "mdd_usd_on_70"]].to_string(index=False))

        ok = df[(df["signal_coverage"] >= 0.95) & (df["mdd"] > -0.5)]
        print("\nsmallest leverage with coverage>=95% and MDD shallower than -50%:",
              float(ok["leverage"].min()) if len(ok) else None)
        json.dump({
            "principal_usd": PRINCIPAL,
            "gross_exposure_median": float(bars["gross_exposure"].median()),
            "baseline_1x": base,
            "recommended_leverage": float(ok["leverage"].min()) if len(ok) else None,
        }, open(os.path.join(OUT, "leverage_report.json"), "w"), indent=2, default=str)


if __name__ == "__main__":
    main()
