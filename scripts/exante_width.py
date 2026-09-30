"""Q42：**事前**可见的「有效宇宙宽度」能不能预测策略的下一期损益？

Q41 把「宇宙变窄」写成「伴随现象 / 放大器」就停手了 —— 那是没检验就下结论。
本脚本把它当可证伪的假设来测，并直接检验那个具体机制（`cap × n ≤ 1` ⇒ 静默等权）。

只读：不改任何参数、不碰执行路径、不做任何优化。

跑法
----
    python scripts/exante_width.py

产物
----
    artifacts/exante_width/
        config.json      本次检验的设定（阈值 / 滚动窗口 / 剔除规则）
        tables/70_*.csv  逐调仓点、IC、分位、cap 绑定、窗口对照
        summary.json     关键结论的机器可读摘要
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from typing import Dict

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.analysis import exante_width as ew                       # noqa: E402
from crypto_ls_research.analysis import failure_attribution as fa                # noqa: E402
from crypto_ls_research.config.settings import ACCEPTED_FACTORS, ACCEPTED_REBALANCE_DAYS  # noqa: E402

BASELINE_PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")
OUT = os.path.join(ROOT, "artifacts", "exante_width")
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


def main() -> None:
    os.makedirs(TAB, exist_ok=True)
    log("=" * 78)
    log("Q42：事前可见的宇宙宽度 → 下一期损益？（只读，不优化）")
    log("=" * 78)

    if not os.path.exists(BASELINE_PKL):
        raise SystemExit(f"缺少 {BASELINE_PKL}；先跑验收阶段")
    with open(BASELINE_PKL, "rb") as f:
        res = pickle.load(f)["result"]
    log(f"baseline: {len(res.bars):,} bars  {res.meta['n_rebalances']} rebalances  "
        f"factors={res.meta['factor_subset']}")

    cap = float(res.cfg.portfolio.max_weight_per_instrument)
    df = ew.width_frame(res, roll=ew.ROLL, cap=cap)
    df.to_csv(os.path.join(TAB, "70_width_points.csv"), index=False)

    # 恒等式自检：逐调仓点前向损益之和 ≈ 全部 bar 损益之和（允许首尾差）
    tot_bars = float(res.bars["net_ret"].sum())
    tot_fwd = float(df["fwd_net"].sum())
    log(f"\n[自检] 逐期前向损益合计 {tot_fwd:.4f}  vs  bar 损益合计 {tot_bars:.4f}  "
        f"差 {tot_fwd - tot_bars:.4f}（首尾各缺一段，应很小）")

    config = {
        "baseline_pkl": os.path.relpath(BASELINE_PKL, ROOT),
        "rebalance_days": ACCEPTED_REBALANCE_DAYS,
        "rebalance_bars": int(res.cfg.rebalance_bars),
        "factors": list(ACCEPTED_FACTORS),
        "max_weight_per_instrument": cap,
        "roll": ew.ROLL,
        "roll_note": "滚动百分位/去均值只用 **t 之前** 的 180 个调仓点（严格过去，事前可得）",
        "exclusion": "宽度 0 / 无仓位的调仓点不进入相关性（否则『窄=不亏』的假象会进样本）",
        "windows": [{"label": w[0], "start": w[1], "end": w[2], "dd_stated": w[3]}
                    for w in fa.WINDOWS],
        "optimization_performed": False,
        "note": "只读检验；未调任何参数、未改因子/权重/TopK/止损/杠杆/换手限制、未加 regime filter。",
    }
    with open(os.path.join(OUT, "config.json"), "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    # ---- 1. 宽度 → 下一期损益：IC（原始 vs 去趋势） -----------------------
    ic = ew.width_ic_table(df)
    ic.to_csv(os.path.join(TAB, "70b_width_ic.csv"), index=False)
    log("\n[1] 事前宽度 vs 下一期损益（秩相关 + t）")
    log(ic.round(4).to_string(index=False))

    # ---- 2. 分位分组 ------------------------------------------------------
    bt = ew.width_bucket_table(df, q=5)
    bt.to_csv(os.path.join(TAB, "70c_width_buckets.csv"), index=False)
    log("\n[2] 按事前宽度百分位分 5 组 → 下一期损益")
    log(bt.round(4).to_string(index=False))
    log(f"    最窄−最宽（原始损益）  {_clean(bt.attrs.get('narrowest_minus_widest'))}")
    log(f"    最窄−最宽（去趋势损益）{_clean(bt.attrs.get('narrowest_minus_widest_demean'))}")

    # ---- 3. cap 绑定（静默等权）的直接检验 --------------------------------
    cb = ew.cap_binding_table(df)
    cb.to_csv(os.path.join(TAB, "70d_cap_binding.csv"), index=False)
    log("\n[3] `cap × n ≤ 1`（静默等权）成立 / 不成立")
    log(cb.round(4).to_string(index=False))
    log(f"    绑定−不绑定（原始损益）  {_clean(cb.attrs.get('binding_minus_free'))}")
    log(f"    绑定−不绑定（去趋势损益）{_clean(cb.attrs.get('binding_minus_free_demean'))}")

    wchk = ew.binding_weight_check(df, res)
    log("\n[3b] 实测：绑定时权重是否真的退化成等权（离散系数 cv，等权 ⇒ ≈0）")
    log("     " + "  ".join(f"{k}={v}" for k, v in wchk.items()))

    # ---- 4. 5 个回撤窗口 vs 正常期 的事前宽度 -----------------------------
    ww = ew.window_width_table(df)
    ww.to_csv(os.path.join(TAB, "70e_window_width.csv"), index=False)
    log("\n[4] 回撤窗口 vs 正常期的**事前**宽度（滚动百分位）")
    log(ww.round(4).to_string(index=False))

    # ---- 5. cap 绑定在各窗口的占比 ----------------------------------------
    rows = []
    for label, s, e, _dd in fa.WINDOWS:
        seg = df[df["window"] == label]
        rows.append({"window": label, "n": int(len(seg)),
                     "cap_binding_share": float(seg["cap_binding"].mean()),
                     "n_side_max_mean": float(seg["n_side_max"].mean()),
                     "n_universe_mean": float(seg["n_universe"].mean())})
    seg = df[df["window"] == "NORMAL"]
    rows.append({"window": "NORMAL", "n": int(len(seg)),
                 "cap_binding_share": float(seg["cap_binding"].mean()),
                 "n_side_max_mean": float(seg["n_side_max"].mean()),
                 "n_universe_mean": float(seg["n_universe"].mean())})
    cw = pd.DataFrame(rows)
    cw.to_csv(os.path.join(TAB, "70f_cap_binding_by_window.csv"), index=False)
    log("\n[5] cap 绑定在各窗口的占比")
    log(cw.round(4).to_string(index=False))

    # ---- 6. 冷启动期 vs 成熟期（W4 落在样本第一个月） ---------------------
    wu = ew.warmup_table(df)
    wu.to_csv(os.path.join(TAB, "70g_warmup.csv"), index=False)
    log("\n[6] 冷启动期（前 180 个调仓点）vs 成熟期")
    log(wu.round(4).to_string(index=False))
    log(f"    冷启动−成熟（原始损益）{_clean(wu.attrs.get('warmup_minus_mature'))}")

    # ---- 7. 趋势检查：原始宽度站不站得住 ----------------------------------
    tc = ew.trend_check(df)
    log("\n[7] 趋势检查（决定「窄 = 危险」这个读数站不站得住）")
    log(f"    宽度 vs 调仓点序号 的 Spearman = {tc['spearman_width_vs_index']:+.4f} (n={tc['n']})")
    log(f"    原始宽度 vs 滚动百分位 的 Spearman = {tc['spearman_raw_vs_pct']:+.4f} (n={tc['n_pct']})")
    log(f"    各年均宽度 = {tc['yearly_mean_width']}")

    # ---- 8. 每单位敞口的亏损（冷启动期账本只投了一半） --------------------
    legs_ep = fa.leg_attribution(res.bars, mode="episode")
    legs_ep["loss_per_exposure"] = legs_ep["net_pnl"] / legs_ep["avg_gross_exposure"]
    legs_ep["gross_per_exposure"] = legs_ep["gross_pnl"] / legs_ep["avg_gross_exposure"]
    exp_tbl = legs_ep[["window", "net_pnl", "avg_gross_exposure", "loss_per_exposure",
                       "gross_per_exposure"]].copy()
    exp_tbl.to_csv(os.path.join(TAB, "70h_loss_per_exposure.csv"), index=False)
    log("\n[8] 每单位毛敞口的亏损（冷启动期账本只投了一半 ⇒ 名义 DD 会低估严重度）")
    log(exp_tbl.round(4).to_string(index=False))
    log("    注：分子是 episode 口径净损益（占 NAV 的比例），分母是平均毛敞口占 NAV 的比例。")

    # ---- summary.json -----------------------------------------------------
    summary = {
        "headline": {
            "n_rebalances": int(len(df)),
            "n_untraded": int((~df["traded"]).sum()),
            "cap": cap,
            "cap_binding_share_all": float(df["cap_binding"].mean()),
            "cap_binding_share_traded": float(df[df["traded"]]["cap_binding"].mean()),
            "fwd_sum": tot_fwd, "bars_sum": tot_bars,
        },
        "width_ic": _clean(ic.to_dict(orient="records")),
        "buckets": _clean(bt.to_dict(orient="records")),
        "buckets_attrs": {
            "narrowest_minus_widest": _clean(bt.attrs.get("narrowest_minus_widest")),
            "narrowest_minus_widest_demean": _clean(bt.attrs.get("narrowest_minus_widest_demean")),
        },
        "cap_binding": _clean(cb.to_dict(orient="records")),
        "cap_binding_attrs": {
            "binding_minus_free": _clean(cb.attrs.get("binding_minus_free")),
            "binding_minus_free_demean": _clean(cb.attrs.get("binding_minus_free_demean")),
        },
        "binding_weight_check": _clean(wchk),
        "window_width": _clean(ww.to_dict(orient="records")),
        "cap_binding_by_window": _clean(cw.to_dict(orient="records")),
        "warmup": _clean(wu.to_dict(orient="records")),
        "warmup_attrs": _clean(wu.attrs.get("warmup_minus_mature")),
        "trend_check": _clean(tc),
        "loss_per_exposure": _clean(exp_tbl.to_dict(orient="records")),
    }
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log("\n[done] 产物 -> artifacts/exante_width/")


if __name__ == "__main__":
    main()
