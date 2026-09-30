"""审计账户的**保证金模式**（`cross` 全仓 / `isolated` 逐仓），以及它对爆仓距离的影响。

## 为什么需要这个脚本

对一个**对冲过的**（beta 中性）账本，"逐仓更安全" 是**反的**：

* **全仓 `cross`**：全账户权益给每一笔仓位兜底，多空腿的盈亏**互相抵消**。
  一笔仓位爆仓需要的是**整个账户**的净亏损，而这个账本的净敞口只有毛敞口的
  ~8%（`net/gross ≈ 0.0797`），所以需要的同向移动是 `(1 - mmr*G)/G` —— 以当前
  `G ≈ 0.44` 算是 **200%+**，结构性够不到。
* **逐仓 `isolated`**：每条腿只用自己的保证金，爆仓距离是 `1/L - mmr`
  —— `L=3` 是 **32.8%**，`L=5` 是 **19.5%**，`L=100` 是 **0.5%**。
  对冲腿的盈利**躺在另一个桶里，救不了它**。

所以逐仓会把一个对冲账本**拆成 N 笔独立的方向性押注**，每条腿的引信都短得多；
而且一条腿被强平后，**对冲就断了**，剩下的那条腿变成裸方向仓 ——
正是"一个爆了"的场景，但**是逐仓造成的**。

## 这个脚本量什么

1. 每个仓位的 `mgnMode` / `lever` / `liqPx`，以及**实测**爆仓距离。
2. **同一个合约同时存在两种模式**（混合账本）—— 这是最危险的状态，因为
   同一个方向、同一个合约的两条腿风险完全不同。
3. 实测距离 vs **风控闸门假设的距离**（`liquidation_ok` 用的是
   `1/rcfg.max_leverage - mmr`，即**逐仓**口径，且用的是**配置里的**杠杆，
   不是交易所上**实际生效**的杠杆）。
4. 假设市场整体移动 X%，**每种模式下会先死几条腿**。

## ⚠️ 三个"看不见"

* `td_mode` **不落盘**（不在 `LiveLimits`、不在 `store`）：它只是
  `LiveEngine` 的内存属性，`/api/live/execute` 每次请求现传。所以
  **控制台一重启就静默回到 `cross`**，而**成交记录里也没有 `tdMode`**
  —— 事后无法判断某一笔成交用的是哪种模式。
* `liquidation_ok` 对 `td_mode` **完全不敏感**，且读的是 `rcfg.max_leverage`
  而不是交易所上实际生效的 `lever`。所以它可以一边认为"19.5% 很安全"，
  一边账户上躺着一个 100× 的逐仓仓。
* **`lever` 字段是"配置"的镜像，不是持仓的杠杆。** 改 `set-leverage` 会改
  `(instId, mgnMode, posSide)` 的格子，也会让 `lever` 跟着变，但**在场那条腿的
  `margin`/`liqPx` 不会跟着走**（2026-09-30 实测 NEAR 3→5：`margin` 116.906、
  `liqPx` 3.317332 逐位不变，>65s 仍不变）。判断一条腿真正在几倍上，只能用
  `implied_leverage()` 从 `liqPx` 反推。

## 写 `set-leverage` 的四个坑（都是实测）

1. `mgnMode=isolated` **必须带 `posSide`**，否则 OKX 直接 **HTTP 400**。
2. `/api/v5/account/batch-set-leverage` **不存在**（**404**），只能一个一个来。
3. 一次只改**一个方向**：`leverage-info` 返回 long/short **两条**记录，
   设 `posSide=long` 不会动 short。⇒ 每个合约要**两次**调用。
4. **回读有几十秒延迟**：`set-leverage` 立刻回 200 并回显你请求的值，但
   `leverage-info` / 持仓端点的 `lever` 会在几十秒内继续显示旧值。
   ⇒ 写完立刻回读**不能作为验证**；必须等 ~60s，并用 `liqPx` 反推复核。

用法：`python scripts/audit_margin_mode.py [mode]`（默认 demo）。
产物：`artifacts/margin_mode/tables/41*.csv`。
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto_ls_research.config.settings import RiskConfig          # noqa: E402
from crypto_ls_research.execution.engine import LiveEngine         # noqa: E402
from crypto_ls_research.risk.engine import DEFAULT_MMR             # noqa: E402

OUT = "artifacts/margin_mode/tables"
MMR = DEFAULT_MMR


# ---------------------------------------------------------------------------
def liq_distance(pos: dict) -> Optional[float]:
    """Fractional adverse move that liquidates *this position*, from the venue's `liqPx`.

    Returns `None` when the venue gives no liquidation price -- which for a
    `cross` position is the *good* case: there is no per-position liquidation
    price because the whole account equity backs it.  A `cross` position can
    still carry a `liqPx`, and it is then far away (thousands of percent).
    """
    mp = pos.get("markPx")
    lp = pos.get("liqPx")
    sz = pos.get("pos") or 0.0
    if not mp or mp <= 0 or not lp or lp <= 0:
        return None
    return (mp - lp) / mp if sz > 0 else (lp - mp) / mp


def isolated_theory(lever: float) -> float:
    """`1/L - mmr`: the move that eats a leg's own isolated margin."""
    return 1.0 / max(float(lever or 0.0), 1e-9) - MMR


def cross_theory(gross_over_nav: float) -> float:
    """`(1 - mmr*G)/G`: the move that eats the whole account (all-cross, one side)."""
    g = max(float(gross_over_nav), 1e-9)
    return (1.0 - MMR * g) / g


def gate_theory(rcfg: RiskConfig) -> float:
    """What `liquidation_ok` *assumes*: `1/max_leverage - mmr` (isolated口径)."""
    return 1.0 / max(rcfg.max_leverage, 1e-9) - MMR


def implied_leverage(mark_px: float, liq_px: float, mmr: float = MMR) -> float:
    """The leverage a position is **actually margined at**, inverted from `liqPx`.

    Inverting `isolated_theory`:

        dist = 1/L - mmr   =>   L = 1 / (dist + mmr),  dist = |liqPx - markPx| / markPx

    Why this function has to exist: the venue's `lever` field (on both
    `/account/positions` and `/account/leverage-info`) mirrors the *account
    configuration* for `(instId, mgnMode, posSide)` -- **not** the leverage the
    open position is margined at.  Measured 2026-09-30 on the demo venue: setting
    NEAR `isolated` long from 3 to 5 moved `lever` to "5" but left the position's
    `margin` (116.906) and `liqPx` (3.317332) **bit-identical** for >65s.  So a
    reader who trusts `lever` will believe a leg was re-margined when it was not.

    `liqPx` is the only field that tells the truth, and this is how you read it.

    ⚠️ It is an **estimate**, because the venue's own liquidation formula uses a
    per-instrument maintenance-margin rate (and fees) while we assume a flat
    `mmr = 0.005`.  Measured on 9 legs on 2026-09-30: `config=3` came back as
    2.98–3.30 (±10 %), `config=100` came back as 93.6 (−6 %).  So treat this as a
    detector for *gross* divergence (the 20 % flag), not as a precise readout.
    """
    mark = float(mark_px)
    if mark <= 0:
        raise ValueError("mark_px must be positive")
    dist = abs(float(liq_px) - mark) / mark
    return 1.0 / (dist + mmr)


# ---------------------------------------------------------------------------
# 账户级杠杆配置（**与持仓无关**）
# ---------------------------------------------------------------------------
def leverage_config(eng: LiveEngine, insts: Sequence[str]) -> pd.DataFrame:
    """Configured leverage per `(instId, mgnMode)` straight from the venue.

    Why this is a separate section: leverage is **not** a property of the order.
    `place_order` sends no leverage field at all -- it only carries `tdMode`.  The
    venue opens the position at whatever leverage is already configured for
    `(instId, mgnMode, posSide)`.  So "why did this leg end up at 100x" is a
    question about *account configuration*, not about the order, and reading it
    back from `/api/v5/account/leverage-info` is the only honest way to answer it.

    `max_lever` is the instrument's ceiling from the contract spec, so a row where
    `configured == max` is a leg that will be liquidated on a ~`1/max` move.
    """
    from crypto_ls_research.execution.specs import load_specs
    cli = eng._private()
    specs = load_specs()
    out = []
    for inst in insts:
        spec = specs.get(inst)
        for mgn in ("cross", "isolated"):
            try:
                r = cli.request("GET", "/api/v5/account/leverage-info",
                                {"instId": inst, "mgnMode": mgn})
                lever = r[0]["lever"] if r else None
            except Exception:                                     # noqa: BLE001
                lever = None
            out.append({"instId": inst, "mgnMode": mgn,
                        "configured_lever": lever,
                        "max_lever": getattr(spec, "max_lever", None),
                        "at_max": (lever is not None and spec is not None
                                   and float(lever) >= float(spec.max_lever) - 1e-9),
                        "liq_distance_if_used": (
                            isolated_theory(float(lever))
                            if (lever and mgn == "isolated") else None)})
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
def main(mode: str = "demo") -> int:
    eng = LiveEngine(mode=mode)
    acct = eng.account()
    nav = float(acct.get("nav") or 0.0)
    positions = acct.get("positions") or []
    if not positions:
        print(f"[{mode}] 没有持仓（或读取失败）：{acct.get('equity_error')}")
        return 1

    rcfg = RiskConfig()
    os.makedirs(OUT, exist_ok=True)

    rows = []
    for p in positions:
        d = liq_distance(p)
        mp, lp = p.get("markPx"), p.get("liqPx")
        # Only meaningful for isolated: a cross leg's liqPx is account-level.
        imp = (implied_leverage(float(mp), float(lp))
               if (p.get("mgnMode") == "isolated" and mp and lp) else None)
        rows.append({
            "instId": p.get("instId"),
            "posSide": p.get("posSide"),
            "mgnMode": p.get("mgnMode"),
            "lever": pd.to_numeric(p.get("lever"), errors="coerce"),
            "implied_lever": imp,
            "sz": p.get("pos"),
            "notional": p.get("notional"),
            "markPx": p.get("markPx"),
            "liqPx": p.get("liqPx"),
            "dist_liq": d,
            "upl": p.get("upl"),
        })
    df = pd.DataFrame(rows)
    df["abs_notional"] = df["notional"].abs()
    df = df.sort_values(["mgnMode", "dist_liq"], na_position="last")
    df.to_csv(f"{OUT}/41_position_margin.csv", index=False)

    gross = float(df["abs_notional"].fillna(0.0).sum())
    G = gross / nav if nav else float("nan")
    signed = df.assign(sgn=df["posSide"].map({"long": 1.0, "short": -1.0}))
    net = float((signed["abs_notional"].fillna(0.0) * signed["sgn"]).sum())

    print(f"=== 保证金模式审计 · {mode} ===")
    print(f"NAV {nav:,.2f}  毛敞口 {gross:,.0f} ({G:.4f} x NAV)  "
          f"净敞口 {net:,.0f} ({net / gross:.4f} of gross)" if gross else "")
    print(f"引擎 td_mode = {eng.td_mode!r}   set_leverage = {eng.set_leverage!r}   "
          f"limits.max_leverage = {eng.limits.max_leverage}")

    # ---- per-mode summary ------------------------------------------------
    summ = []
    for m, sub in df.groupby("mgnMode", dropna=False):
        ds = sub["dist_liq"].dropna()
        summ.append({
            "mgnMode": m,
            "n": len(sub),
            "notional": float(sub["abs_notional"].fillna(0.0).sum()),
            "frac_of_gross": float(sub["abs_notional"].fillna(0.0).sum() / gross) if gross else None,
            "lever_min": float(sub["lever"].min()) if sub["lever"].notna().any() else None,
            "lever_max": float(sub["lever"].max()) if sub["lever"].notna().any() else None,
            "n_with_liqPx": int(len(ds)),
            "dist_min": float(ds.min()) if len(ds) else None,
            "dist_median": float(ds.median()) if len(ds) else None,
        })
    sm = pd.DataFrame(summ).sort_values("mgnMode")
    sm.to_csv(f"{OUT}/41b_mode_summary.csv", index=False)
    print("\n--- 按模式汇总 ---")
    for r in sm.itertuples():
        dd = (f"{r.dist_min * 100:.2f}%" if r.dist_min is not None else "n/a")
        dm = (f"{r.dist_median * 100:.2f}%" if r.dist_median is not None else "n/a")
        print(f"  {str(r.mgnMode):>9}: {r.n:>2} 名  名义 {r.notional:>10,.0f} "
              f"({r.frac_of_gross * 100:>5.1f}% of gross)  lever {r.lever_min:.0f}~{r.lever_max:.0f}  "
              f"最近爆仓 {dd:>9}  中位 {dm:>9}  (有 liqPx 的 {r.n_with_liqPx}/{r.n})")

    # ---- theory vs measured ---------------------------------------------
    thr = []
    for r in df.itertuples():
        if r.mgnMode == "isolated" and pd.notna(r.lever):
            thr.append({"instId": r.instId, "posSide": r.posSide, "mgnMode": r.mgnMode,
                        "lever": r.lever, "theory_dist": isolated_theory(r.lever),
                        "measured_dist": r.dist_liq})
    for r in df.itertuples():
        if r.mgnMode == "cross":
            thr.append({"instId": r.instId, "posSide": r.posSide, "mgnMode": r.mgnMode,
                        "lever": r.lever, "theory_dist": cross_theory(G),
                        "measured_dist": r.dist_liq})
    tdf = pd.DataFrame(thr)
    if len(tdf):
        tdf["residual"] = tdf["measured_dist"] - tdf["theory_dist"]
        tdf.to_csv(f"{OUT}/41c_liq_distance_by_mode.csv", index=False)

    print("\n--- 理论 vs 实测爆仓距离 ---")
    print(f"  逐仓理论 `1/L - mmr`   L=3 → {isolated_theory(3) * 100:.2f}%   "
          f"L=5 → {isolated_theory(5) * 100:.2f}%   L=100 → {isolated_theory(100) * 100:.2f}%")
    print(f"  全仓理论 `(1-mmr*G)/G` G={G:.4f} → {cross_theory(G) * 100:.2f}%")
    print(f"  风控闸门 `liquidation_ok` 假设的（= 逐仓口径 @ rcfg.max_leverage={rcfg.max_leverage:.0f}）"
          f" → {gate_theory(rcfg) * 100:.2f}%")

    # ---- the two "invisible" defects -------------------------------------
    print("\n--- 两个「看不见」 ---")
    dup = (df.groupby("instId")["mgnMode"].nunique() > 1)
    dups = sorted(dup[dup].index)
    print(f"① 同一合约**同时**存在两种模式：{len(dups)} 个 -> {', '.join(dups) if dups else '无'}")
    over = df[(df["lever"].notna()) & (df["lever"] > eng.limits.max_leverage)]
    print(f"② 实际 lever > limits.max_leverage({eng.limits.max_leverage:.0f}) 的仓位："
          f"{len(over)} 个" + (f" -> " + ", ".join(
              f"{r.instId}/{r.mgnMode}@{r.lever:.0f}x" for r in over.itertuples()) if len(over) else ""))
    if len(over):
        worst = over.assign(d=over["dist_liq"]).sort_values("d").iloc[0]
        print(f"   ⚠️ 最近的一个：{worst['instId']} {worst['posSide']} {worst['mgnMode']} "
              f"{worst['lever']:.0f}x  距爆仓 {worst['dist_liq'] * 100:.2f}%  "
              f"（闸门假设 {gate_theory(rcfg) * 100:.2f}%）")

    # ---- what a market-wide move does ------------------------------------
    print("\n--- 假设市场整体同向移动（对冲腿盈利救不了另一条腿）---")
    print(f"  {'移动':>7}{'逐仓先死':>12}{'全仓先死':>12}   说明")
    iso = df[df["mgnMode"] == "isolated"]["dist_liq"].dropna()
    crs = df[df["mgnMode"] == "cross"]["dist_liq"].dropna()
    mkt = []
    for x in (0.02, 0.05, 0.10, 0.20, 0.30):
        ni = int((iso <= x).sum())
        nc = int((crs <= x).sum())
        mkt.append({"market_move": x, "isolated_liquidated": ni,
                    "cross_liquidated": nc,
                    "isolated_total": int(len(iso)), "cross_total": int(len(crs)),
                    "net_pnl_frac_of_nav": abs(net) * x / nav if nav else None})
        print(f"  {x * 100:>6.0f}%{ni:>9} / {len(iso):<3}{nc:>9} / {len(crs):<3}"
              f"   账户净盈亏 ≈ {abs(net) * x / nav * 100:.2f}% of NAV（仍很小）")
    pd.DataFrame(mkt).to_csv(f"{OUT}/41d_market_move_liquidation.csv", index=False)

    # ---- account-level leverage configuration ----------------------------
    print("\n--- 账户级杠杆配置（**与订单无关**，订单里根本没有杠杆字段）---")
    try:
        cfg = leverage_config(eng, sorted(df["instId"].unique()))
    except Exception as e:                                        # noqa: BLE001
        print(f"  读不到杠杆配置：{type(e).__name__}: {e}")
        cfg = pd.DataFrame()
    if len(cfg):
        cfg.to_csv(f"{OUT}/41e_leverage_config.csv", index=False)
        for mgn in ("cross", "isolated"):
            sub = cfg[cfg["mgnMode"] == mgn]
            if not len(sub):
                continue
            vals = sorted(set(sub["configured_lever"].dropna()))
            print(f"  {mgn:>9}: 配置杠杆取值 {vals}")
            odd = sub[sub["at_max"]]
            if len(odd):
                for r in odd.itertuples():
                    dd = (f"{r.liq_distance_if_used * 100:.2f}%"
                          if r.liq_distance_if_used is not None else "n/a")
                    print(f"    ⚠️ {r.instId} {mgn} 杠杆 = 上限 {r.max_lever:.0f}× "
                          f"⇒ 该模式下爆仓距离只有 {dd}")
        print("  注：`place_order` 不带杠杆，持仓**继承**这里配置的值；"
              "`_apply_leverage` 是唯一能改它的地方，而它要求 `set_leverage` 非空。")

    # ---- 配置杠杆 vs 持仓实际杠杆（两者会脱钩）--------------------------
    print("\n--- 配置杠杆 vs 持仓实际杠杆（**改配置 ≠ 改持仓**）---")
    cmp = df[(df["mgnMode"] == "isolated") & df["implied_lever"].notna()][
        ["instId", "posSide", "lever", "implied_lever", "dist_liq"]].copy()
    if len(cmp):
        cmp["gap_ratio"] = cmp["implied_lever"] / cmp["lever"]
        cmp = cmp.sort_values("gap_ratio", ascending=False)
        cmp.to_csv(f"{OUT}/41f_config_vs_actual.csv", index=False)
        for r in cmp.itertuples():
            flag = "  ⚠️ 脱钩" if abs(r.gap_ratio - 1.0) > 0.20 else ""
            print(f"  {r.instId:18s} {r.posSide:5s} config={r.lever:>4.0f}x  "
                  f"实际≈{r.implied_lever:6.2f}x  距爆仓 {r.dist_liq * 100:5.2f}%{flag}")
        n_gap = int((cmp["gap_ratio"].sub(1.0).abs() > 0.20).sum())
        print(f"  脱钩 {n_gap}/{len(cmp)} 条。`lever` 只是**配置的镜像**；持仓真正的杠杆只能从 "
              f"`liqPx` 反推（`L = 1/(|liqPx/mark-1| + mmr)`）。")
        print("  ⚠️ 所以「把杠杆统一设成 3×」**不会**把已经在场的那条腿变成 3× —— "
              "只有平掉重开或直接给逐仓桶加保证金才会。")

    print(f"\n产物 -> {OUT}/41*.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "demo"))
