"""失效归因（failure attribution）—— 解释 5 段最大回撤，**只诊断，不调参**。

本阶段**严格禁止**：调参数、改因子权重、增删因子、改 TopK、改止损、改杠杆、改换手限制、
加 regime filter、优化进出场。唯一目标：解释历史回撤。

跑法
----
    python scripts/drawdown_attribution.py                # 全部（含单因子隔离回测）
    python scripts/drawdown_attribution.py --no-ablations # 只跑产物分析，跳过重跑回测

产出
----
    artifacts/drawdown_attribution/
        config.json                     本次实验配置（**存档，不改任何参数**）
        tables/*.csv                    各表
        summary.json                    关键结论的机器可读摘要

窗口
----
用户给的 5 个窗口原样使用（UTC 日历日，双端含）。真实回撤段几何另出一表 ——
因为 5 个窗口**不是同一套边界约定**（W2 的第二数是恢复日，W3 的第一数是局部高点）。
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.analysis import failure_attribution as fa          # noqa: E402
from crypto_ls_research.analysis.metrics import compute_metrics            # noqa: E402
from crypto_ls_research.analysis.sweep import apply_overrides              # noqa: E402
from crypto_ls_research.backtest.engine import run_backtest                # noqa: E402
from crypto_ls_research.config.settings import (                           # noqa: E402
    ACCEPTED_FACTORS, ACCEPTED_REBALANCE_DAYS, BARS_PER_DAY, default_config)
from crypto_ls_research.data.store import Panels, load_panels              # noqa: E402
from crypto_ls_research.factors.engine import FACTOR_NAMES                 # noqa: E402

BAR = "1h"
BASELINE_PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")
OUT = os.path.join(ROOT, "artifacts", "drawdown_attribution")
TAB = os.path.join(OUT, "tables")

# 验收配置：**从 settings 读，不写字面量**（与 engine.DEFAULT_SIGNAL / spec.OPTIMAL_OVERRIDES 同源）。
ACCEPTED_OVERRIDES: Dict[str, object] = {
    "factors.subset": list(ACCEPTED_FACTORS),
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_baseline() -> dict:
    if not os.path.exists(BASELINE_PKL):
        raise SystemExit(f"缺少 {BASELINE_PKL}；先跑验收阶段")
    with open(BASELINE_PKL, "rb") as f:
        return pickle.load(f)


def rebuild_panels(res, bar: str, start: str, end: str) -> Panels:
    """重建 baseline 用的那**同一块**面板（按存档 bar 网格截断）。

    `load_panels` 会把网格延伸到缓存里最新的一根，缓存每天都在长 ⇒ 不截断就不可复现。
    实测：重建后重跑，9 个损益列 `max|Δ| = 0`（逐位一致）。
    """
    pan = load_panels(bar, start, end, insts=list(res.insts))
    grid = res.bars.index
    return Panels(open=pan.open.reindex(grid), high=pan.high.reindex(grid),
                  low=pan.low.reindex(grid), close=pan.close.reindex(grid),
                  vol=pan.vol.reindex(grid), vol_ccy=pan.vol_ccy.reindex(grid),
                  amount=pan.amount.reindex(grid), funding=pan.funding.reindex(grid),
                  list_dt=pan.list_dt)


def single_factor_runs(panels: Panels, cfg, res) -> pd.DataFrame:
    """工作流 3：单因子隔离回测。**其余条件（风控/成本/换手/TopK/网格）全部不动。**

    只用 `factor_subset=(f,)` 把因子集缩到 1 个；`max_weight_per_instrument` 保持 0.20
    （⚠️ 单因子时每侧选名数可能 <5 ⇒ cap×n≤1 ⇒ 静默退化成等权，这一列要一起读）。

    额外报**回撤期**的净/分腿损益与 IC（把 5 个窗口的 bar 合起来），
    用于回答「momentum 是不是既给最多 alpha 也给最多回撤」。
    """
    ddmask = fa.any_window_mask(res.bars.index)
    inwin = fa.any_window_mask(res.reb_ts)
    rows: List[dict] = []
    for f in FACTOR_NAMES:
        t0 = time.time()
        r = run_backtest(panels, cfg, factor_subset=(f,))
        m = compute_metrics(r.bars, f)
        fr = fa.ic_frame(r, panels, fa.masked_factor(r, f), int(round(1.0 * cfg.bars_per_day)))
        ic = fa.ic_summary(fr["rank_ic"])
        ic_dd = fa.ic_summary(fr["rank_ic"][inwin])
        b = r.bars
        dd = b[ddmask]
        n_side = float(np.mean([len(x["long"]) for x in r.rebalances]))
        rows.append({
            "factor": f, "CAGR": m["CAGR"], "Sharpe": m["Sharpe"],
            "ann_vol": m["Annualized Volatility"], "max_dd": m["Max Drawdown"],
            "Sortino": m["Sortino"], "Calmar": m["Calmar"],
            # 键名是 'Profit Factor (daily)'，不是 'Profit Factor'（写错会静默变 None）
            "profit_factor": m["Profit Factor (daily)"],
            "ann_turnover": m["Annual Turnover"],
            "cost_drag_ann": m["Cost Drag (annual)"],
            "long_pnl": m["Long PnL (total)"], "short_pnl": m["Short PnL (total)"],
            "net_pnl": m["Net PnL (total)"],
            "gross_pnl": float(b["gross_ret"].sum()),
            "funding_pnl": m["Funding P&L (total, frac)"],
            "IC_mean": ic["ic_mean"], "ICIR": ic["icir"], "IC_t": ic["t_stat"],
            "IC_pos_share": ic["pos_share"],
            "IC_dd_mean": ic_dd["ic_mean"], "IC_dd_n": ic_dd["n"],
            "IC_delta": ic["ic_mean"] - ic_dd["ic_mean"],
            "dd_net_pnl": float(dd["net_ret"].sum()),
            "dd_long_pnl": float(dd["long_ret"].sum()),
            "dd_short_pnl": float(dd["short_ret"].sum()),
            "dd_gross_pnl": float(dd["gross_ret"].sum()),
            "avg_names_per_side": n_side,
            "secs": round(time.time() - t0, 1),
        })
        log(f"    [{f}] Sharpe {m['Sharpe']:.4f}  MDD {m['Max Drawdown']*100:.2f}%  "
            f"net {m['Net PnL (total)']:.4f}  dd_net {rows[-1]['dd_net_pnl']:.4f}  "
            f"({time.time()-t0:.1f}s)")
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ablations", action="store_true",
                    help="跳过单因子隔离回测（那一步要重跑 5 次回测）")
    ap.add_argument("--perm", type=int, default=2000, help="块置换次数")
    args = ap.parse_args()

    os.makedirs(TAB, exist_ok=True)
    log("=" * 78)
    log("失效归因（只诊断，不调参）")
    log("=" * 78)

    p = load_baseline()
    res = p["result"]
    bars = res.bars
    log(f"baseline: {len(bars):,} bars  {bars.index[0]} -> {bars.index[-1]}")
    log(f"          {res.meta['n_rebalances']} rebalances, {len(res.insts)} insts, "
        f"factors={res.meta['factor_subset']}")

    cfg = apply_overrides(default_config(bar=BAR, rebalance_bars=max(
        1, int(round(ACCEPTED_REBALANCE_DAYS * BARS_PER_DAY[BAR])))), ACCEPTED_OVERRIDES)
    cfg.start, cfg.end = p["start"], p["end"]

    # ---- 存档实验配置（本阶段不允许改参数） -------------------------------
    config = {
        "baseline_pkl": os.path.relpath(BASELINE_PKL, ROOT),
        "bar": BAR, "rebalance_days": ACCEPTED_REBALANCE_DAYS,
        "rebalance_bars": cfg.rebalance_bars,
        "start": cfg.start, "end": cfg.end,
        "accepted_overrides": ACCEPTED_OVERRIDES,
        "windows": [{"label": w[0], "start": w[1], "end": w[2], "dd_stated": w[3]}
                    for w in fa.WINDOWS],
        "slice_convention": "UTC calendar days, both ends inclusive",
        "optimization_performed": False,
        "note": "本阶段只诊断；未调任何参数、未改因子权重/TopK/止损/杠杆/换手限制、未加 regime filter。",
        "n_perm": args.perm,
    }
    with open(os.path.join(OUT, "config.json"), "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    log(f"\n[config] 已存档 -> artifacts/drawdown_attribution/config.json")

    # ---- 0. 窗口对齐（可证伪的第一步） ------------------------------------
    geo = fa.match_windows(bars)
    geo.to_csv(os.path.join(TAB, "60_window_geometry.csv"), index=False)
    log("\n[0] 窗口几何（用户窗口 vs 真实回撤段）")
    log(geo[["window", "start", "end", "dd_stated", "peak", "trough", "recovery",
             "depth_actual", "end_is_trough", "start_is_peak"]].to_string(index=False))

    # ---- 1 + 8. 分腿归因 / 成本分解 ---------------------------------------
    legs = fa.leg_attribution(bars, mode="stated")
    legs["verdict"] = [fa.leg_verdict(r) for _, r in legs.iterrows()]
    legs_ep = fa.leg_attribution(bars, mode="episode")
    legs_ep["verdict"] = [fa.leg_verdict(r) for _, r in legs_ep.iterrows()]
    cost = fa.cost_decomposition(bars, mode="stated")
    cost_ep = fa.cost_decomposition(bars, mode="episode")
    # 正常期基准（不在任何窗口内）
    nmask = ~fa.any_window_mask(bars.index)
    normal = bars[nmask]
    normal_row = {
        "window": "NORMAL", "mode": "rest-of-sample", "n_bars": int(len(normal)),
        "gross_pnl": float(normal["gross_ret"].sum()),
        "long_pnl": float(normal["long_ret"].sum()),
        "short_pnl": float(normal["short_ret"].sum()),
        "fee": float(normal["fee"].sum()), "spread": float(normal["spread"].sum()),
        "impact": float(normal["impact"].sum()), "funding": float(normal["funding"].sum()),
        "net_pnl": float(normal["net_ret"].sum()),
        "turnover": float(normal["turnover"].sum()),
    }
    legs.to_csv(os.path.join(TAB, "61_leg_attribution_stated.csv"), index=False)
    legs_ep.to_csv(os.path.join(TAB, "61b_leg_attribution_episode.csv"), index=False)
    cost.to_csv(os.path.join(TAB, "62_cost_decomposition_stated.csv"), index=False)
    cost_ep.to_csv(os.path.join(TAB, "62b_cost_decomposition_episode.csv"), index=False)
    log("\n[1+8] 分腿归因（用户窗口口径）")
    log(legs[["window", "dd_stated", "net_pnl", "gross_pnl", "long_pnl", "short_pnl",
              "fee", "spread", "impact", "funding", "turnover", "win_rate_daily",
              "max_consec_loss_days", "verdict"]].to_string(index=False))
    log("\n[8] 成本分解")
    log(cost[["window", "gross_pnl", "fee", "spread", "impact", "funding", "cost_total",
              "net_pnl", "recon_resid", "verdict"]].to_string(index=False))

    # ---- 2 + 4. 逐因子 IC + 动量反转 --------------------------------------
    log("\n[2] 重建面板（用于 IC / 前向收益）…")
    panels = rebuild_panels(res, BAR, cfg.start, cfg.end)
    log(f"    {len(panels.index):,} bars x {len(panels.insts)} insts")

    icw = fa.ic_by_window(res, panels, horizon_bars=int(round(1.0 * cfg.bars_per_day)),
                          perm=args.perm > 0)
    icw.to_csv(os.path.join(TAB, "63_factor_ic_by_window.csv"), index=False)
    log("\n[2] 逐因子 Rank IC（窗口 vs 正常期）")
    piv = icw[icw["window"].isin(list(fa.WINDOW_LABELS) + ["FULL", "NORMAL"])]
    log(piv.pivot_table(index="factor", columns="window", values="ic_mean")
        .round(4).to_string())
    log("\n[2] 窗口 vs 正常期的显著性（Welch + 块置换）")
    dv = icw[icw["window"].str.endswith("_vs_NORMAL")]
    log(dv[["factor", "window", "ic_mean", "mean_diff", "welch_t", "welch_p",
            "perm_p"]].round(4).to_string(index=False))

    # 2b. 汇总口径：把 5 个窗口**并起来**当一个样本，与正常期比（用户要的「回撤期 IC」）
    pooled_rows = []
    inwin_reb = fa.any_window_mask(res.reb_ts)
    for f in FACTOR_NAMES:
        rk = fa.ic_frame(res, panels, fa.masked_factor(res, f),
                         int(round(1.0 * cfg.bars_per_day)))["rank_ic"]
        a, b = rk[inwin_reb], rk[~inwin_reb]
        row = {"factor": f, "full_ic": fa.ic_summary(rk)["ic_mean"],
               "dd_ic": fa.ic_summary(a)["ic_mean"], "normal_ic": fa.ic_summary(b)["ic_mean"],
               "n_dd": int(a.notna().sum()), "n_normal": int(b.notna().sum()),
               "dd_icir": fa.ic_summary(a)["icir"], "full_icir": fa.ic_summary(rk)["icir"]}
        row.update(fa.welch(a, b))
        row.update(fa.block_perm(a, b, n_perm=args.perm))
        row["dd_minus_full"] = row["dd_ic"] - row["full_ic"]
        pooled_rows.append(row)
    pooled = pd.DataFrame(pooled_rows)
    pooled.to_csv(os.path.join(TAB, "63c_factor_ic_pooled.csv"), index=False)
    log("\n[2b] 汇总口径：5 个窗口合并 vs 正常期（用户要的「回撤期 IC」）")
    log(pooled[["factor", "full_ic", "dd_ic", "normal_ic", "dd_minus_full", "n_dd",
                "welch_t", "welch_p", "perm_p"]].round(4).to_string(index=False))

    roll = fa.rolling_ic(res, panels, "momentum",
                         horizon_bars=int(round(1.0 * cfg.bars_per_day)))
    roll.to_csv(os.path.join(TAB, "63b_momentum_rolling_ic.csv"), index_label="ts")

    # 2c. 尾部价差：**与 IC 是两个量，可以不同号**（报告 §三 那段 ⚠️ 的数据源）
    ts_rows = []
    for f in FACTOR_NAMES:
        d = fa.tail_spread(res, panels, fa.masked_factor(res, f),
                           horizon_bars=int(round(1.0 * cfg.bars_per_day)))
        t = fa.tail_spread_table(d)
        t.insert(0, "factor", f)
        ts_rows.append(t)
    tsd = pd.concat(ts_rows, ignore_index=True)
    tsd.to_csv(os.path.join(TAB, "63d_factor_tail_spread.csv"), index=False)
    log("\n[2c] 因子尾部价差（池内 top10 − bottom10，未来 1 天；池内 ≥20 名才计）")
    log(tsd[tsd["window"] == "FULL"].round(4).to_string(index=False))

    mr = fa.momentum_reversal(res, panels)
    mr.to_csv(os.path.join(TAB, "64_momentum_reversal_points.csv"), index=False)
    mrt = fa.momentum_reversal_table(mr)
    mrt.to_csv(os.path.join(TAB, "64b_momentum_reversal_table.csv"), index=False)
    log("\n[4] 横截面动量反转（过去 5.25 天 → 未来 1/3/5/10 天）")
    log(mrt.round(4).to_string(index=False))

    # ---- 5. 轮动 ----------------------------------------------------------
    rot = fa.rotation_metrics(res, panels)
    rot.to_csv(os.path.join(TAB, "65_rotation_points.csv"), index=False)
    rott = fa.rotation_table(rot)
    rott.to_csv(os.path.join(TAB, "65b_rotation_table.csv"), index=False)
    log("\n[5] 横截面轮动")
    log(rott.round(4).to_string(index=False))

    # ---- 6. regime --------------------------------------------------------
    ic_mom = fa.ic_frame(res, panels, fa.masked_factor(res, "momentum"),
                         int(round(1.0 * cfg.bars_per_day)))["rank_ic"]
    reg = fa.regime_frame(res, panels, rot, ic_mom)
    reg.to_csv(os.path.join(TAB, "66_regime_daily.csv"), index_label="ts")
    regt = fa.regime_table(res, reg)
    regt.to_csv(os.path.join(TAB, "66b_regime_table.csv"), index=False)
    log("\n[6] regime 表（9 类主标签，**动量标签用滞后 IC**）")
    log(regt.round(4).to_string(index=False))
    log("\n[6] regime 分布")
    log(reg["regime"].value_counts().to_string())

    # 6b. 对照：把动量标签换成**同期** IC（会「拿结果解释结果」）——整条优先级链重算一遍，
    # 不能在滞后标签上覆盖（那得到的是两者的并集，会稀释对照）。
    reg_c = fa.regime_frame(res, panels, rot, ic_mom, ic_col="mom_ic_5d_contemp")
    regt_c = fa.regime_table(res, reg_c)
    regt_c.to_csv(os.path.join(TAB, "66c_regime_table_contemp_ic.csv"), index=False)
    log("\n[6b] 对照：动量标签改用**同期** IC（同期口径 = 拿结果解释结果）")
    log(regt_c.round(4).to_string(index=False))

    # 6c. 每个回撤窗口的 regime 构成（回答「这些窗口当时是什么状态」）
    share_rows = []
    for label, s, e, _dd in fa.WINDOWS:
        m = fa.window_mask(reg.index, s, e)
        sub = reg[m]
        row = {"window": label, "n_days": int(len(sub))}
        vc = sub["regime"].value_counts(normalize=True)
        for name in fa.REGIME_ORDER:
            row[f"share_{name}"] = float(vc.get(name, 0.0))
        share_rows.append(row)
    m = ~fa.any_window_mask(reg.index)
    row = {"window": "NORMAL", "n_days": int(m.sum())}
    vc = reg[m]["regime"].value_counts(normalize=True)
    for name in fa.REGIME_ORDER:
        row[f"share_{name}"] = float(vc.get(name, 0.0))
    share_rows.append(row)
    shares = pd.DataFrame(share_rows)
    shares.to_csv(os.path.join(TAB, "66d_regime_share_by_window.csv"), index=False)
    log("\n[6c] 回撤窗口的 regime 构成（占比）")
    log(shares.round(4).to_string(index=False))

    # ---- 7. 流动性 --------------------------------------------------------
    liq = fa.liquidity_diag(res)
    liq.to_csv(os.path.join(TAB, "67_liquidity_points.csv"), index=False)
    liqt = fa.liquidity_table(liq)
    liqt.to_csv(os.path.join(TAB, "67b_liquidity_table.csv"), index=False)
    log("\n[7] 流动性 / ADV 门槛")
    log(liqt.round(4).to_string(index=False))

    # ---- 3. 单因子隔离回测 -------------------------------------------------
    ab = None
    if not args.no_ablations:
        log("\n[3] 单因子隔离回测（其余条件不动）…")
        ab = single_factor_runs(panels, cfg, res)
        ab.to_csv(os.path.join(TAB, "68_single_factor_runs.csv"), index=False)
        log(ab.round(4).to_string(index=False))
    else:
        log("\n[3] 跳过单因子隔离回测（--no-ablations）")

    # ---- 9. 一页式对比表（**每个格子都能追到上面的产物**） -------------------
    # 用户要的列：阶段 / DD / Long P&L / Short P&L / Momentum IC / Dispersion /
    #             Rank Turnover / Universe / Funding / Cost
    # 这里把两种切片（stated / episode）拼成一张表并**落盘**，报告 §二 逐字抄它 ——
    # 报告里不再出现任何「只在命令行里算过一次、产物里找不到」的数字。
    ep = fa.episode_windows(bars)
    pd.DataFrame([{"window": l, "peak": p, "trough": t, "dd_stated": d,
                   "convention": "bar-exact, both ends inclusive（与 61b 同一段）"}
                  for l, p, t, d in ep]
                 ).to_csv(os.path.join(TAB, "69b_episode_windows.csv"), index=False)

    reb = pd.DatetimeIndex(res.reb_ts)
    ep_ic = {}
    for lbl, pk, tr, _dd in ep:
        ep_ic[lbl] = fa.ic_summary(ic_mom[(reb >= pk) & (reb <= tr)])["ic_mean"]
    # episode 的轮动 / 流动性：把**逐点**表按 bar 级峰→谷重新打标，再汇总。
    # （不重算指标，只换标签 ⇒ 与 stated 口径逐点同源。）
    rot_ep = rot.copy()
    rot_ep["window"] = fa.retag_episode(rot_ep, "ts", ep)
    rott_ep = fa.rotation_table(rot_ep, windows=ep).set_index("window")
    liq_ep = liq.copy()
    liq_ep["window"] = fa.retag_episode(liq_ep, "ts", ep)
    liqt_ep = fa.liquidity_table(liq_ep, windows=ep).set_index("window")

    legs_i, legs_ep_i = legs.set_index("window"), legs_ep.set_index("window")
    rott_i = rott.set_index("window")
    ic_norm = float(icw[(icw["factor"] == "momentum")
                        & (icw["window"] == "NORMAL")]["ic_mean"].iloc[0])
    rows_tbl: List[dict] = []
    for lbl, _s, _e, _dd in fa.WINDOWS:
        for mode in ("stated", "episode"):
            L = (legs_i if mode == "stated" else legs_ep_i).loc[lbl]
            R = (rott_i if mode == "stated" else rott_ep).loc[lbl]
            icv = (float(icw[(icw["factor"] == "momentum")
                             & (icw["window"] == lbl)]["ic_mean"].iloc[0])
                   if mode == "stated" else float(ep_ic[lbl]))
            rows_tbl.append({
                "phase": lbl, "slice": mode, "dd": float(L["dd_stated"]),
                "net_pnl": float(L["net_pnl"]), "gross_pnl": float(L["gross_pnl"]),
                "long_pnl": float(L["long_pnl"]), "short_pnl": float(L["short_pnl"]),
                "momentum_ic": icv, "dispersion": float(R["dispersion"]),
                "rank_turnover": float(R["rank_turnover"]),
                "universe": float(R["universe"]),
                "funding": float(L["funding"]), "cost": float(L["cost_total"]),
            })
    Rn = rott_i.loc["NORMAL"]
    rows_tbl.append({
        "phase": "NORMAL", "slice": "rest-of-sample", "dd": np.nan,
        "net_pnl": normal_row["net_pnl"], "gross_pnl": normal_row["gross_pnl"],
        "long_pnl": normal_row["long_pnl"], "short_pnl": normal_row["short_pnl"],
        "momentum_ic": ic_norm, "dispersion": float(Rn["dispersion"]),
        "rank_turnover": float(Rn["rank_turnover"]), "universe": float(Rn["universe"]),
        "funding": normal_row["funding"],
        # 正常期成本 = fee + spread + impact（与 61/61b 的 cost_total 同一算法）
        "cost": normal_row["fee"] + normal_row["spread"] + normal_row["impact"],
    })
    wt = pd.DataFrame(rows_tbl)
    wt.to_csv(os.path.join(TAB, "69_window_table.csv"), index=False)
    log("\n[9] 一页式对比表（报告 §二 的数据源）")
    log(wt.round(4).to_string(index=False))

    # ---- summary.json ------------------------------------------------------
    summary = {
        "headline": {
            "n_bars": int(len(bars)), "n_rebalances": int(res.meta["n_rebalances"]),
            "mdd": float((bars["equity"] / bars["equity"].cummax() - 1).min()),
            "net_pnl_total": float(bars["net_ret"].sum()),
            "gross_pnl_total": float(bars["gross_ret"].sum()),
            "long_pnl_total": float(bars["long_ret"].sum()),
            "short_pnl_total": float(bars["short_ret"].sum()),
            "fee_total": float(bars["fee"].sum()),
            "spread_total": float(bars["spread"].sum()),
            "impact_total": float(bars["impact"].sum()),
            "funding_total": float(bars["funding"].sum()),
        },
        "accounting_identity": "net = gross - fee - spread - impact + funding",
        "recon_max_abs_resid": float(np.abs(
            bars["net_ret"] - (bars["gross_ret"] - bars["fee"] - bars["spread"]
                              - bars["impact"] + bars["funding"])).max()),
        "windows": legs.to_dict(orient="records"),
        "window_table": wt.replace({np.nan: None}).to_dict(orient="records"),
        "episode_windows": [{"window": l, "peak": str(p), "trough": str(t), "dd_stated": d}
                            for l, p, t, d in ep],
        "window_geometry": geo.astype(str).to_dict(orient="records"),
        "normal_period": normal_row,
        "cost_decomposition": cost.to_dict(orient="records"),
        "ic_by_window": icw.replace({np.nan: None}).to_dict(orient="records"),
        "rotation": rott.to_dict(orient="records"),
        "regime": regt.to_dict(orient="records"),
        "liquidity": liqt.to_dict(orient="records"),
        "single_factor": None if ab is None else ab.to_dict(orient="records"),
    }
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    log(f"\n[done] 产物 -> artifacts/drawdown_attribution/")


if __name__ == "__main__":
    main()
