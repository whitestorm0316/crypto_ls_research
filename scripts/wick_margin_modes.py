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

import numpy as np
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


def freq_table() -> pd.DataFrame:
    """How often a leg actually reaches a given liquidation distance.

    This is the *cost* side of `isolated`: the mode caps the loss per leg but
    puts every leg `1/L - mmr` from liquidation, so a low-leverage book and a
    high-leverage one buy the same protection at wildly different prices.
    Measured over the cached 1h panel; intraday extremes, so it is an UPPER
    bound on the forced-liquidation rate.
    """
    import glob

    from crypto_ls_research.config.settings import CACHE_DIR
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "candles/1h/*.parquet")))
    rows = []
    for f in files:
        d = pd.read_parquet(f)[["open", "high", "low"]]
        if len(d) < 240:
            continue
        g = d.groupby(d.index.tz_convert("UTC").normalize())
        dd = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                           "low": g["low"].min()}).dropna()
        if len(dd) < 30:
            continue
        o = dd["open"].to_numpy()
        for h in (1, 7):
            rmax = dd["high"].rolling(h).max().shift(-(h - 1)).to_numpy()
            rmin = dd["low"].rolling(h).min().shift(-(h - 1)).to_numpy()
            m = np.isfinite(o) & np.isfinite(rmax) & np.isfinite(rmin) & (o > 0)
            rows.append(pd.DataFrame({
                "hold": h,
                "short_adv": (rmax[m] - o[m]) / o[m],
                "long_adv": (o[m] - rmin[m]) / o[m],
            }))
    return pd.concat(rows, ignore_index=True)


def print_freq(df: pd.DataFrame) -> None:
    print("\n--- 逐仓的**频率代价**：腿真的会走到爆仓距离吗 ---")
    print("（用日内极值 ⇒ **上界**；K=10 的账本，按 1 天持有期折算）")
    print(f"  {'距离':>8}{'对应杠杆':>10}{'1d 空头':>10}{'1d 多头':>10}"
          f"{'7d 空头':>10}{'7d 多头':>10}{'K=10/年':>10}")
    out = []
    for thr in (0.328, 0.49, 0.66, 0.995):
        r = {}
        for h in (1, 7):
            for side in ("short", "long"):
                a = df.loc[df["hold"] == h, f"{side}_adv"].to_numpy()
                r[(h, side)] = float((a >= thr).mean())
        est = (r[(1, "short")] + r[(1, "long")]) * 10 * 365
        lev = 1.0 / (thr + MMR) if thr < 0.99 else 1.0
        out.append({"dist": thr, "lever": lev, "p_1d_short": r[(1, "short")],
                    "p_1d_long": r[(1, "long")], "p_7d_short": r[(7, "short")],
                    "p_7d_long": r[(7, "long")], "liquidations_per_year_k10": est})
        print(f"  {thr * 100:>7.1f}%{lev:>9.1f}x{r[(1, 'short')] * 100:>9.3f}%"
              f"{r[(1, 'long')] * 100:>9.3f}%{r[(7, 'short')] * 100:>9.3f}%"
              f"{r[(7, 'long')] * 100:>9.3f}%{est:>9.1f} 次")
    pd.DataFrame(out).to_csv(f"{OUT}/42c_liquidation_frequency.csv", index=False)
    print(f"产物 -> {OUT}/42c_liquidation_frequency.csv")


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

    if "--freq" in sys.argv:
        print_freq(freq_table())


if __name__ == "__main__":
    main()
