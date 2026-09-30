"""Q43：空腿到底是不是被「崩跌式反弹」挤压的？—— 直接度量被做空标的的**当期**收益分布。

`artifacts/FAILURE_ATTRIBUTION.md` §十-1 把这条留成了「证据不足 / 无法确认」。
本脚本把它变成一个**可证伪的**问题：

    回撤期里被**选中的空头**，是不是比同期池子里任意一个合格标的表现得更好（涨得更多）？

判据（全部事前写死，不看结果再定）：
* `mw_ret_names` = 被空标的的市值加权收益（`−short_pnl/short_gross`）；
* `pool_mean`   = 同期**池内全部合格标的**的等权平均收益（基准）；
* `excess_mw`   = `mw_ret_names − pool_mean` —— **这才是「挤压」**。
  空腿亏钱只说明市场在涨（做空上涨的市场必然亏），**只有比池子涨得多才叫被挤**。
* 辅助：被空标的「涨超 +5%」的占比 vs 池子的同一占比（尾部是不是更肥）。

只读：不改任何参数、不碰执行路径、不做任何优化。**本阶段唯一目标是解释历史回撤。**

跑法
----
    python scripts/short_squeeze.py

产物
----
    artifacts/short_squeeze/
        config.json       本次检验的设定与「明确没做的事」
        tables/71_*.csv   逐调仓点、窗口汇总（stated / episode）、贡献者
        summary.json      机器可读摘要（含裁决）
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.analysis import failure_attribution as fa                # noqa: E402
from crypto_ls_research.config.settings import (ACCEPTED_FACTORS,                # noqa: E402
                                                ACCEPTED_REBALANCE_DAYS)
from crypto_ls_research.data.store import Panels, load_panels                    # noqa: E402

BASELINE_PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")
OUT = os.path.join(ROOT, "artifacts", "short_squeeze")
TAB = os.path.join(OUT, "tables")


def log(m: str) -> None:
    print(m, flush=True)


def _clean(o):
    """把 numpy / pandas 标量转成 JSON 可序列化的东西。"""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    return o


def _panels_for(res) -> Panels:
    """把面板截回存档网格（与 `test_failure_attribution._panels` 同一做法）。

    ⚠️ `load_panels` 会把网格延伸到缓存里最新的一根 ⇒ 不 `reindex` 就复现不出存档。
    """
    p = load_panels("1h", "2021-01-01", "2026-09-26", insts=list(res.insts))
    g = res.bars.index
    return Panels(open=p.open.reindex(g), high=p.high.reindex(g), low=p.low.reindex(g),
                  close=p.close.reindex(g), vol=p.vol.reindex(g),
                  vol_ccy=p.vol_ccy.reindex(g), amount=p.amount.reindex(g),
                  funding=p.funding.reindex(g), list_dt=p.list_dt)


def _reconcile(res, sb: pd.DataFrame) -> Dict[str, float]:
    """把逐期 `short_pnl` 与引擎自己的 `bars["short_ret"]` 对账。

    这是**价格口径对不对**的唯一硬证据：口径错一位，这里就会差出量级。
    """
    b = res.bars
    dec = np.searchsorted(b.index, res.reb_ts)
    t = dec + 1
    sr = b["short_ret"].to_numpy()
    lr = b["long_ret"].to_numpy()
    dif_s: List[float] = []
    dif_l: List[float] = []
    for i in range(len(res.reb_ts)):
        a = int(t[i])
        bb = int(t[i + 1]) if i + 1 < len(res.reb_ts) else len(b)
        dif_s.append(float(sr[a:bb].sum()) - float(sb.loc[i, "short_pnl"]))
        dif_l.append(float(lr[a:bb].sum()) - float(sb.loc[i, "long_pnl"]))
    ds, dl = np.asarray(dif_s), np.asarray(dif_l)
    px_s = (sb["short_pnl"] - sb["short_pnl_px"]).abs()
    px_l = (sb["long_pnl"] - sb["long_pnl_px"]).abs()
    return {"short_max_abs_diff": float(np.nanmax(np.abs(ds))),
            "long_max_abs_diff": float(np.nanmax(np.abs(dl))),
            "short_median_abs_diff": float(np.nanmedian(np.abs(ds))),
            # ⚠️ 上面两项只钉**区间分组**（`short_pnl` 就来自 `name_gross`）；
            # 下面两项用**价格独立算出的** `*_px` 钉**价格口径**（前向起点）。
            "short_px_max_abs_diff": float(np.nanmax(px_s)),
            "long_px_max_abs_diff": float(np.nanmax(px_l))}


def main() -> None:
    os.makedirs(TAB, exist_ok=True)
    log("=" * 78)
    log("Q43：空腿是不是被「崩跌式反弹」挤压的？（只读，不优化）")
    log("=" * 78)

    if not os.path.exists(BASELINE_PKL):
        raise SystemExit(f"缺少 {BASELINE_PKL}；先跑验收阶段")
    with open(BASELINE_PKL, "rb") as f:
        res = pickle.load(f)["result"]
    log(f"baseline: {len(res.bars):,} bars  {res.meta['n_rebalances']} rebalances  "
        f"factors={res.meta['factor_subset']}")

    pan = _panels_for(res)
    sb = fa.short_book_frame(res, pan)
    sb.to_csv(os.path.join(TAB, "71_short_book_points.csv"), index=False)

    # ---- 0. 自检：价格口径必须与引擎逐期吻合 ------------------------------
    rec = _reconcile(res, sb)
    log("\n[0] 自检：逐期 `short_pnl` vs 引擎 `bars['short_ret']`")
    log(f"    空腿 max|Δ| = {rec['short_max_abs_diff']:.3e}（float32 精度量级即合格）"
        f"  中位 {rec['short_median_abs_diff']:.3e}")
    log(f"    多腿 max|Δ| = {rec['long_max_abs_diff']:.3e}")
    log(f"    价格口径（独立算出）：空腿 {rec['short_px_max_abs_diff']:.3e}  "
        f"多腿 {rec['long_px_max_abs_diff']:.3e} —— 前向起点错一根 bar 这里会立刻炸")
    if max(rec["short_max_abs_diff"], rec["short_px_max_abs_diff"]) > 1e-6:
        log("    ⚠️ 对账不过 ⇒ 后面的分布数字不可信，先修口径。")
    n_un = int((~sb["available"]).sum())
    log(f"    不可用调仓点 {n_un}/{len(sb)}  原因 "
        f"{sb.loc[~sb['available'], 'reason'].value_counts().to_dict()}")

    # ---- 1 + 2. 两套切片：stated / episode --------------------------------
    ep = fa.episode_windows(res.bars)
    sb_ep = sb.copy()
    sb_ep["window"] = fa.retag_episode(sb_ep, "ts", ep)

    t_stated = fa.short_book_table(sb)
    t_ep = fa.short_book_table(sb_ep, windows=ep, rest_label="REST")
    t_stated.to_csv(os.path.join(TAB, "71b_short_book_stated.csv"), index=False)
    t_ep.to_csv(os.path.join(TAB, "71c_short_book_episode.csv"), index=False)

    show = ["window", "n_points", "n_unavailable", "n_short", "ew_median", "frac_big",
            "pool_frac_big", "mw_ret", "mw_ret_names", "pool_mean", "excess_select",
            "excess_weight", "welch_p", "perm_p", "short_pnl"]
    for name, t in (("stated", t_stated), ("episode", t_ep)):
        log(f"\n[{'1' if name == 'stated' else '2'}] 空腿收益分布 —— {name} 切片")
        log(t[show].round(4).to_string(index=False))

    # ---- 3. 裁决（判据事前写死，见 squeeze_verdict） -----------------------
    log("\n[3] 「空腿被挤压」三态裁决（episode 切片）")
    verdicts: List[dict] = []
    for _, r in t_ep.iterrows():
        v = fa.squeeze_verdict(r)
        verdicts.append({"window": r["window"], "verdict": v,
                         "excess_select": _clean(r["excess_select"]),
                         "excess_weight": _clean(r["excess_weight"]),
                         "perm_p": _clean(r.get("perm_p")),
                         "frac_big": _clean(r["frac_big"]),
                         "pool_frac_big": _clean(r["pool_frac_big"])})
        log(f"    {r['window']:>6}  {v}")

    # ---- 4. 均值层面的三分分解 + 亏损的 NAV 分解 --------------------------
    m = t_ep[["window", "mw_ret_names", "pool_mean", "excess_select",
              "excess_weight"]].copy()
    m["recomposed"] = m["pool_mean"] + m["excess_select"] + m["excess_weight"]
    m["resid"] = m["mw_ret_names"] - m["recomposed"]
    m.to_csv(os.path.join(TAB, "71d_mean_decomposition.csv"), index=False)
    log("\n[4a] 均值层面：被空标的的收益 = 市场 + 选股 + 加权（逐点恒等）")
    log(m.round(5).to_string(index=False))
    log(f"    残差 max|Δ| = {float(m['resid'].abs().max()):.3e}（应为 0）")

    d = t_ep[["window", "short_pnl", "short_pnl_market", "short_pnl_select",
              "short_pnl_weight"]].copy()
    d["resid"] = d["short_pnl"] - (d["short_pnl_market"] + d["short_pnl_select"]
                                   + d["short_pnl_weight"])
    d["market_share"] = np.where(d["short_pnl"] < 0,
                                 d["short_pnl_market"] / d["short_pnl"], np.nan)
    d.to_csv(os.path.join(TAB, "71e_short_loss_decomposition.csv"), index=False)
    log("\n[4b] NAV 层面：空腿损益 = 市场 + 选股 + 加权")
    log(d.round(4).to_string(index=False))
    log(f"    分解残差 max|Δ| = {float(d['resid'].abs().max()):.3e}（应为 0）")
    log("    ⚠️ 「市场」项偏小是因为 `short_gross` 与市场收益**负相关**（风控在上涨时缩仓）；")
    log("       要读「亏损主要是市场还是别的」，看 [4a] 的均值层面。")

    # ---- 5. 贡献者：亏损是少数名字还是普遍 --------------------------------
    c = fa.short_contributors(sb_ep, res, windows=ep, rest_label="REST", top=6)
    c.to_csv(os.path.join(TAB, "71f_short_contributors_episode.csv"), index=False)
    log("\n[5] 空腿亏损贡献者（episode，按 name_gross 累计）")
    log(c[["window", "rank", "inst", "pnl", "mean_ret", "share_of_loss",
           "top_share", "total_short_pnl", "n_names", "leg_is_loss"]]
        .round(4).to_string(index=False))
    log("    注：`top_share > 1` 合法 —— 其余名字在空腿上是赚钱的，净亏损全由少数名字造成。")

    # ---- 6. 对照：多头那侧是不是也一样 ------------------------------------
    log("\n[6] 对照：同一窗口里**多头**相对池子的超额（`excess_long`）")
    log(t_ep[["window", "long_mw_ret", "pool_mean", "excess_long", "long_pnl"]]
        .round(4).to_string(index=False))

    config = {
        "baseline_pkl": os.path.relpath(BASELINE_PKL, ROOT),
        "rebalance_days": ACCEPTED_REBALANCE_DAYS,
        "factors": list(ACCEPTED_FACTORS),
        "exec_price_mode": str(res.cfg.execution.exec_price),
        "big_move": 0.05,
        "big_move_note": "「大涨」阈值：单期收益 > +5%（1 天持有期）",
        "return_convention": "Σ_t (px[t+1]/px[t] − 1)，与引擎 name_gross 同源（相加不复合）",
        "excess_definition": ("判「挤压」用 excess_select = 空腿等权收益 − 池子等权收益；"
                              "excess_mw = excess_select + excess_weight（仓位加权）"),
        "squeeze_test": "Welch + 块置换作用在 excess_select（等权选股效应）上，不是 excess_mw",
        "windows": [{"label": w[0], "start": w[1], "end": w[2], "dd_stated": w[3]}
                    for w in fa.WINDOWS],
        "episode_windows": [{"label": l, "peak": str(pk), "trough": str(tr)}
                            for l, pk, tr, _dd in ep],
        "optimization_performed": False,
        "note": "只读检验；未调任何参数、未改因子/权重/TopK/止损/杠杆/换手限制、未加 regime filter。",
    }
    with open(os.path.join(OUT, "config.json"), "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    summary = {
        "headline": {
            "n_points": int(len(sb)),
            "n_unavailable": n_un,
            "unavailable_reasons": {str(k): int(v) for k, v in
                                    sb.loc[~sb["available"], "reason"].value_counts().items()},
            "reconciliation": _clean(rec),
            "verdicts": verdicts,
        },
        "stated": _clean(t_stated.to_dict(orient="records")),
        "episode": _clean(t_ep.to_dict(orient="records")),
        "mean_decomposition": _clean(m.to_dict(orient="records")),
        "loss_decomposition": _clean(d.to_dict(orient="records")),
        "contributors": _clean(c.to_dict(orient="records")),
    }
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log("\n[done] 产物 -> artifacts/short_squeeze/")


if __name__ == "__main__":
    main()
