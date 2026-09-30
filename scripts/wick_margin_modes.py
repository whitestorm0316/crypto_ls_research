#!/usr/bin/env python
"""Single-name wick tolerance on the LIVE book: `isolated` vs `cross`.

Why this exists
---------------
`audit_margin_mode.py` stresses a **uniform** adverse move applied to every leg
at once (`41d_market_move_liquidation.csv`).  That is *not* what "插针" means.  A
wick is a sharp, **idiosyncratic** move in ONE instrument while the rest of the
book is unchanged.  The two stresses are different questions and can rank the
margin modes differently, so the untested case is measured here rather than
argued about.

Model
-----
* The venue's own `liqPx` is used wherever it exists -- for `isolated` legs AND
  for `cross` legs.  OKX publishes a liquidation price in both modes; in `cross`
  it sits far away precisely because the whole account equity backs the leg.
  Using the venue's own number beats re-deriving one.
* Where a leg has no `liqPx` the theory is used and **labelled**:
  `isolated: 1/L - mmr`, `cross: (1 - mmr*G)/G` with `G = gross / NAV`.
* A wick of size `d` on instrument `j` costs `|notional_j| * d`; every other leg
  is unchanged.  That single line is the whole difference from a market-wide
  move -- and it is why the hedge does not help here.
* Cross account solvency is also checked directly:
  `NAV - |notional_j| * d < gross * mmr` (maintenance margin is ~1% of gross).

Read-only.  Writes `42_wick_tolerance.csv` and `42b_single_name_wick.csv`.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto_ls_research.execution.engine import LiveEngine          # noqa: E402
from crypto_ls_research.risk.engine import (                        # noqa: E402
    DEFAULT_MMR, isolated_liq_distance)

OUT = "artifacts/margin_mode/tables"
MMR = DEFAULT_MMR


def liq_distance(pos: dict) -> Optional[float]:
    """Fractional adverse move that liquidates *this position*, from the venue's `liqPx`.

    `None` means the venue published no liquidation price.  For a `cross` leg
    that is the benign case, not a missing measurement -- there is no per-leg
    liquidation because the account backs it.
    """
    mp = pos.get("markPx")
    lp = pos.get("liqPx")
    sz = pos.get("pos") or 0.0
    if not mp or mp <= 0 or not lp or lp <= 0:
        return None
    return (mp - lp) / mp if sz > 0 else (lp - mp) / mp


def cross_theory(gross_over_nav: float) -> float:
    """`(1 - mmr*G)/G`: the move that eats the whole account (all-cross, one side)."""
    g = max(float(gross_over_nav), 1e-9)
    return (1.0 - MMR * g) / g


def build(mode: str = "demo") -> tuple[dict, pd.DataFrame]:
    eng = LiveEngine(mode=mode)
    acct = eng.account()
    nav = float(acct.get("nav") or 0.0)
    rows = []
    for p in acct.get("positions") or []:
        notional = p.get("notional")
        if notional is None:
            continue
        d = liq_distance(p)
        lev = float(p.get("lever") or 0.0)
        rows.append({
            "instId": p.get("instId"),
            "posSide": p.get("posSide"),
            "mgnMode": p.get("mgnMode"),
            "lever": lev,
            "abs_notional": abs(float(notional)),
            "dist_liq": d,
            "dist_theory": (isolated_liq_distance(lev) if lev else None),
            # What the leg would lose if *its own* bucket were wiped: notional / L.
            "bucket_margin": (abs(float(notional)) / lev if lev else None),
        })
    return {"nav": nav, "gross": float(sum(r["abs_notional"] for r in rows))}, pd.DataFrame(rows)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    acct, df = build(mode)
    nav, gross = acct["nav"], acct["gross"]
    G = gross / nav if nav else float("nan")
    mm_total = gross * MMR

    print(f"=== 单标的插针容忍度 · {mode} ===")
    print(f"NAV {nav:,.2f}   毛敞口 {gross:,.0f} ({G:.4f} x NAV)   "
          f"维持保证金 ≈ 毛敞口 × mmr({MMR}) = {mm_total:,.2f}")

    # ---- per-leg wick tolerance -------------------------------------------
    df = df.sort_values(["mgnMode", "dist_liq"], na_position="last")
    df.to_csv(f"{OUT}/42_wick_tolerance.csv", index=False)

    print("\n--- 每条腿「单独被插针」多远才爆仓（取交易所自己的 liqPx）---")
    for m, sub in df.groupby("mgnMode"):
        ds = sub["dist_liq"].dropna()
        if not len(ds):
            continue
        print(f"  {m:>9}: 最近 {ds.min() * 100:>9.2f}%   中位 {ds.median() * 100:>9.2f}%   "
              f"最远 {ds.max() * 100:>12.2f}%   (有 liqPx {len(ds)}/{len(sub)})")
        worst = sub.dropna(subset=["dist_liq"]).iloc[0]
        print(f"             最近的那条：{worst['instId']} {worst['posSide']} "
              f"{worst['lever']:.0f}x  名义 ${worst['abs_notional']:,.0f}  "
              f"若爆仓损失其保证金 ≈ ${worst['bucket_margin']:,.2f}")

    # ---- the sweep ---------------------------------------------------------
    print("\n--- 单标的插针扫描（只动一个标的，其余腿不变）---")
    print(f"  {'插针幅度':>8}{'逐仓被打掉的腿':>16}{'全仓被打掉的腿':>16}"
          f"{'逐仓损失':>12}{'全仓账户损失':>14}")
    sweep = []
    iso = df[df["mgnMode"] == "isolated"]
    crs = df[df["mgnMode"] == "cross"]
    for d in (0.02, 0.05, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 1.0, 2.0, 4.0):
        hit_i = iso[iso["dist_liq"].notna() & (iso["dist_liq"] <= d)]
        hit_c = crs[crs["dist_liq"].notna() & (crs["dist_liq"] <= d)]
        # Worst-case single leg: the largest position taking the whole move.
        worst_loss = float(df["abs_notional"].max()) * d
        sweep.append({
            "wick": d,
            "isolated_liquidated": int(len(hit_i)),
            "isolated_total": int(len(iso)),
            "cross_liquidated": int(len(hit_c)),
            "cross_total": int(len(crs)),
            "isolated_loss_usd": float(hit_i["bucket_margin"].fillna(0.0).sum()),
            "cross_worst_leg_loss_usd": worst_loss,
            "cross_worst_leg_loss_frac_nav": worst_loss / nav if nav else None,
            "cross_account_liquidated": bool(nav - worst_loss < mm_total),
        })
        s = sweep[-1]
        print(f"  {d * 100:>7.0f}%{s['isolated_liquidated']:>10} / {len(iso):<4}"
              f"{s['cross_liquidated']:>10} / {len(crs):<4}"
              f"{s['isolated_loss_usd']:>12,.2f}{worst_loss:>14,.2f}"
              + ("   ⚠️ 全仓账户爆仓" if s["cross_account_liquidated"] else ""))
    pd.DataFrame(sweep).to_csv(f"{OUT}/42b_single_name_wick.csv", index=False)

    # ---- the account-level cross check -------------------------------------
    print("\n--- 全仓的账户级判据（毛敞口 × mmr 为维持保证金）---")
    print(f"  账户爆仓需要的同向移动（全部仓位同一方向）= (1 - mmr*G)/G = "
          f"{cross_theory(G) * 100:,.1f}%")
    print(f"  单个最大标的（${df['abs_notional'].max():,.0f}）吃掉全部 NAV 需要 "
          f"{nav / df['abs_notional'].max() * 100:,.1f}%")
    print(f"  对照：逐仓每条腿 `1/L - mmr`，L=3 → {isolated_liq_distance(3) * 100:.2f}%")

    print(f"\n产物 -> {OUT}/42_wick_tolerance.csv, 42b_single_name_wick.csv")


if __name__ == "__main__":
    main()
