"""插针（wick / flash crash）压力测试：这个策略遇到插针会怎样？

第一版把假设写成「回测看不见插针」，然后被自己的表打红了：只放大影线
（`high *= 1+w`、`low *= 1-w`，open/close 不动）就改了 32558 根 bar 的收益。
原因不是收益口径，是**信号**：

    range_pos = (close - low.rolling(R).min()) / (high.rolling(R).max() - low.rolling(R).min())

`high`/`low` 是 5 个验收因子之一 `range_pos` 的输入，也是 `atr_pct` 的输入。
所以插针**同时**是价格事件和信息事件。这一版按通道拆开量：

  L  末段影线 —— 只注入到「任何决策 bar 都看不到」的那几根上。
     这是**唯一**干净的盲性检验：收益路径（`ret_exec = open[t+1]/open[t]-1`）
     只读 open，所以必须**逐位相同**。若不为零，说明收益口径的判断错了。
  S  全样本影线 —— 走信号通道：改 `range_pos` / `atr_pct` ⇒ 改选股。
     量出「一根针能改变多少仓」。
  P  只动 open —— `open` 在 `factors/engine.py` 里**从不出现**（已核对），
     所以这只改 `ret_exec`，**书逐位不变**。这是纯价格冲击通道。
     注入点之前逐 bar 毛敞口必须完全相等；一 bar 的损益有解析预测
     `w × Σ held·(open[t+1]/open[t])`，实测残差 <2e-9（float32 权重矩阵的精度）。
     ⚠️ 预测**不是** `w × Σ held`：名义额按被冲击**后**的价格记，多一个价格比因子。
     一开始写成 `w × 净敞口`，差 0.03%（−0.0086916 vs −0.0086939），是这张表抓出来的。

另外三条独立证据：爆仓距离（解析）、`liquidation_ok` 的 ATR 门槛与通过率、
5.7 年真实最差单 bar，以及已有的退市压力测试（−50%~−90% 缺口 + 永久停牌）。

跑法：
    python scripts/exp_wick_stress.py
    python scripts/exp_wick_stress.py --n-jobs 4
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.analysis.metrics import compute_metrics  # noqa: E402
from crypto_ls_research.analysis.sweep import apply_overrides  # noqa: E402
from crypto_ls_research.backtest.engine import run_backtest  # noqa: E402
from crypto_ls_research.config.settings import (  # noqa: E402
    ACCEPTED_FACTORS, ACCEPTED_REBALANCE_DAYS, BARS_PER_DAY, default_config)
from crypto_ls_research.data.asset_class import (  # noqa: E402
    filter_insts, load_categories)
from crypto_ls_research.data.store import Panels, list_cached_insts, load_panels  # noqa: E402
from crypto_ls_research.risk.engine import DEFAULT_MMR, liquidation_ok  # noqa: E402
from crypto_ls_research.run.research import save, set_tag  # noqa: E402

BAR = "1h"
START, END = "2021-01-01", "2026-09-26"
BASELINE_PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")

ACCEPTED_OVERRIDES = {
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}
REBAL = max(1, int(round(ACCEPTED_REBALANCE_DAYS * BARS_PER_DAY[BAR])))

_G: dict = {}


# ---------------------------------------------------------------------------
# 面板变换：spec 必须是纯数据，才能过 ProcessPoolExecutor 的 pickle
# ---------------------------------------------------------------------------
def _df(a: np.ndarray, p: Panels) -> pd.DataFrame:
    return pd.DataFrame(a, index=p.index, columns=p.close.columns)


def _rebuild(p: Panels, out: dict) -> Panels:
    """只换价格字段；vol/amount 保持原样 ⇒ ADV 与 ADV 上限不受扰动。"""
    return Panels(open=out["open"], high=out["high"], low=out["low"], close=out["close"],
                  vol=p.vol, vol_ccy=p.vol_ccy, amount=p.amount, funding=p.funding,
                  list_dt=p.list_dt)


def _transform(p: Panels, spec: dict) -> Panels:
    kind = spec.get("kind")
    if not kind:
        return p
    w = float(spec["w"])
    idx = np.asarray(spec["idx"], dtype=int)
    cols = spec.get("cols")

    if kind == "wick":
        # 只动影线：high 抬、low 压，open/close 一位不动。
        # high 只升、low 只降 ⇒ high>=max(open,close)、low<=min(open,close) 仍成立。
        hi = p.high.to_numpy(dtype="float64").copy()
        lo = p.low.to_numpy(dtype="float64").copy()
        hi[idx, :] *= (1.0 + w)
        lo[idx, :] *= (1.0 - w)
        return _rebuild(p, {"open": p.open, "close": p.close,
                            "high": _df(hi, p), "low": _df(lo, p)})

    if kind == "open":
        # 只动 open ⇒ 只改 `ret_exec`，完全不碰因子 ⇒ 选股/权重逐位不变。
        a = p.open.to_numpy(dtype="float64").copy()
        t0 = int(idx[0])
        if cols is None:
            a[t0:, :] *= (1.0 + w)
        else:
            j = [p.close.columns.get_loc(c) for c in cols]
            a[t0:, j] *= (1.0 + w)
        return _rebuild(p, {"open": _df(a, p), "high": p.high,
                            "low": p.low, "close": p.close})

    raise ValueError(f"unknown transform kind: {kind!r}")


# ---------------------------------------------------------------------------
def _init(bar: str, start: str, end: str, insts: list, grid: pd.DatetimeIndex) -> None:
    p = load_panels(bar, start, end, insts=insts)
    # `load_panels` 会把网格延伸到**缓存里最新的一根**，`end` 只是建议值
    # （`stop = last + step if extend_to_last else last`）。缓存每天在长，
    # 所以不截断的话，任何存档回测都不可复现。截到存档的 bar 网格。
    p = Panels(open=p.open.reindex(grid), high=p.high.reindex(grid),
               low=p.low.reindex(grid), close=p.close.reindex(grid),
               vol=p.vol.reindex(grid), vol_ccy=p.vol_ccy.reindex(grid),
               amount=p.amount.reindex(grid), funding=p.funding.reindex(grid),
               list_dt=p.list_dt)
    _G["panels"] = p
    _G["cfg"] = apply_overrides(default_config(bar=bar, rebalance_bars=REBAL),
                                ACCEPTED_OVERRIDES)


def _run(spec: dict) -> dict:
    p = _transform(_G["panels"], spec)
    cfg = apply_overrides(_G["cfg"], spec.get("overrides", {}))
    t0 = time.time()
    res = run_backtest(p, cfg, factor_subset=ACCEPTED_FACTORS, **spec.get("kwargs", {}))
    m = compute_metrics(res.bars, name=spec["label"])
    b = res.bars
    return {
        "label": spec["label"],
        "metrics": {k: v for k, v in m.items() if not k.startswith("_")},
        "bars": b[["net_ret", "gross_ret", "long_ret", "short_ret", "turnover",
                   "gross_exposure", "net_exposure", "dd_scale", "total_scale",
                   "drawdown", "equity", "n_stale"]].copy(),
        "insts": list(res.insts),
        "weights": res.weight_matrix,
        "secs": time.time() - t0,
    }


# ---------------------------------------------------------------------------
def _scope_crypto(bar: str):
    cats = load_categories()
    pool = list_cached_insts(bar)
    scoped, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify {len(unknown)} cached instrument(s): "
                         f"{unknown[:8]}...")
    print(f"### scope: crypto keeps {len(scoped)}/{len(pool)} cached instruments",
          flush=True)
    return scoped


def _dev(a: pd.Series, b: pd.Series, upto: int | None = None) -> float:
    d = (a - b).abs()
    if upto is not None:
        d = d.iloc[:upto + 1]
    return float(d.max())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=4)
    ap.add_argument("--tag", default="wick_stress")
    a = ap.parse_args()

    set_tag(a.tag)
    insts = _scope_crypto(BAR)

    with open(BASELINE_PKL, "rb") as f:
        arch = pickle.load(f)
    arch_res = arch["result"]
    arch_s = compute_metrics(arch_res.bars, name="archived")["Sharpe"]
    arch_eq = float(arch_res.bars["equity"].iloc[-1])
    grid = arch_res.bars.index
    T = len(grid)
    print(f"### archived: bar={arch['bar']} {arch['start']}..{arch['end']} "
          f"bars={T} last={grid[-1]} Sharpe={arch_s:.6f} final_eq={arch_eq:.6f}",
          flush=True)

    rebs = arch_res.rebalances
    dts = pd.DatetimeIndex([r["ts"] for r in rebs])
    last_dec = int(grid.get_loc(dts[-1]))
    warmup = int(arch_res.meta.get("warmup_bars", 0))

    # ---- 冲击基准点：70% 处的调仓，冲击落在 exec_ts 的下一根 open ----
    # ret_exec[t] = open[t+1]/open[t]-1，所以改 open[t+1] 就是让 t 那一根的收益吃针。
    k = int(0.70 * len(rebs))
    rb = rebs[k]
    t_shock = int(grid.get_loc(pd.Timestamp(rb["exec_ts"]))) + 1
    long_w = sorted(zip(rb["long"], rb["w_long"]), key=lambda kv: -abs(kv[1]))
    name_big, w_big = long_w[0]
    print(f"### 冲击点: exec_ts={rb['exec_ts']} -> open[{t_shock}]="
          f"{grid[t_shock]}  最大多头腿={name_big} w={w_big:+.4f}", flush=True)
    print(f"### last decision idx={last_dec} (T-1={T - 1}), warmup={warmup}",
          flush=True)

    # ---- 盲性检验的注入点：任何决策 bar 都看不到的 bar ----
    # 决策 bar 从 `warmup` 开始；因子最长回看 = `universe.min_history_days`（30 天）
    # 是 warmup 里最大的一项，所以 warmup 之前 30 天以外的 bar 一定没被任何决策读过。
    maxlb = int(round(30.0 * BARS_PER_DAY[BAR]))
    early = np.arange(0, max(1, warmup - maxlb - 2))
    late = np.arange(last_dec + 1, T - 1)
    blind_idx = np.concatenate([early, late]) if len(late) else early
    print(f"### 盲性注入点: 前 {len(early)} 根 + 末 {len(late)} 根 = "
          f"{len(blind_idx)} 根（决策 bar 的回看窗覆盖不到）", flush=True)

    rng = np.random.default_rng(20260930)
    full_idx = np.sort(rng.choice(np.arange(24, T - 2), size=300, replace=False))

    specs = [
        {"label": "A0  基线（照做）", "kind": None},
        {"label": "A0o 基线·关风控", "kind": None,
         "kwargs": {"disable_risk_overlays": True}},
        # --- 通道 L：盲性（收益路径只读 open） ---
        {"label": "L  末段影线·关风控", "kind": "wick", "w": 1.00, "idx": blind_idx,
         "kwargs": {"disable_risk_overlays": True}},
        {"label": "Lb 末段影线·带风控", "kind": "wick", "w": 1.00, "idx": blind_idx},
        # --- 通道 S：全样本影线 -> range_pos / atr_pct -> 选股 ---
        {"label": "S  全样本影线30%", "kind": "wick", "w": 0.30, "idx": full_idx},
        {"label": "S3 全样本影线30%·ATR×3", "kind": "wick", "w": 2.00, "idx": full_idx},
        # --- 通道 P：只动 open -> 纯价格冲击，书逐位不变 ---
        {"label": "Pm10 全市场 open −10%", "kind": "open", "w": -0.10, "idx": [t_shock]},
        {"label": "Pm20 全市场 open −20%", "kind": "open", "w": -0.20, "idx": [t_shock]},
        {"label": f"Ps50 单名 {name_big} open −50%", "kind": "open", "w": -0.50,
         "idx": [t_shock], "cols": [name_big]},
        {"label": f"Ps90 单名 {name_big} open −90%", "kind": "open", "w": -0.90,
         "idx": [t_shock], "cols": [name_big]},
    ]

    print(f"### {len(specs)} arms, n_jobs={a.n_jobs}", flush=True)
    t_start = time.time()
    if a.n_jobs <= 1:
        _init(BAR, START, END, insts, grid)
        out = []
        for i, s in enumerate(specs):
            out.append(_run(s))
            print(f"  [wick] {i+1}/{len(specs)}  {s['label']}", flush=True)
    else:
        out = [None] * len(specs)
        with ProcessPoolExecutor(max_workers=a.n_jobs, initializer=_init,
                                 initargs=(BAR, START, END, insts, grid)) as ex:
            futs = {ex.submit(_run, s): i for i, s in enumerate(specs)}
            done = 0
            for f in as_completed(futs):
                i = futs[f]
                try:
                    out[i] = f.result()
                except Exception as e:                                # noqa: BLE001
                    out[i] = {"label": specs[i]["label"],
                              "error": f"{type(e).__name__}: {e}"}
                    print(f"  !! arm failed: {specs[i]['label']}: {e}", flush=True)
                done += 1
                print(f"  [wick] {done}/{len(specs)}", flush=True)
    print(f"### sweep took {time.time() - t_start:.0f}s", flush=True)

    bad = [r for r in out if "error" in r]
    for r in bad:
        print(f"!! arm failed: {r['label']}: {r['error']}", flush=True)
    ok = [r for r in out if "error" not in r]
    if len(ok) != len(specs):
        raise SystemExit(f"only {len(ok)}/{len(specs)} arms produced a result")
    by = {r["label"]: r for r in ok}
    A0 = by["A0  基线（照做）"]

    # ---- 自检：截断后重跑必须复现存档 --------------------------------------
    m0 = A0["metrics"]["Sharpe"]
    eq0 = float(A0["bars"]["equity"].iloc[-1])
    print(f"\n### 自检：Sharpe 重跑 {m0:.9f} vs 存档 {arch_s:.9f} "
          f"差 {m0 - arch_s:+.2e}；终值 {eq0:.9f} vs {arch_eq:.9f}", flush=True)
    repro = abs(m0 - arch_s) < 1e-9
    if not repro:
        print("!! 没有复现存档——下面的增量口径不可信", flush=True)

    # ---- 载入截断后的面板：通道 P 的解析预测与第 5 节都要用 ----------------
    _p = load_panels(BAR, START, END, insts=insts)
    _p = Panels(open=_p.open.reindex(grid), high=_p.high.reindex(grid),
                low=_p.low.reindex(grid), close=_p.close.reindex(grid), vol=_p.vol,
                vol_ccy=_p.vol_ccy, amount=_p.amount, funding=_p.funding,
                list_dt=_p.list_dt)

    # ---- 1. 通道 L：盲性 ---------------------------------------------------
    print("\n=== 1. 通道 L：收益路径是否读影线？（末段影线，决策看不到）===", flush=True)
    rows = []
    for lab, base_lab in (("L  末段影线·关风控", "A0o 基线·关风控"),
                          ("Lb 末段影线·带风控", "A0  基线（照做）")):
        r, b = by[lab], by[base_lab]
        for q in ("net_ret", "gross_ret", "gross_exposure", "turnover", "dd_scale"):
            d = (r["bars"][q] - b["bars"][q]).abs()
            rows.append({"arm": lab, "vs": base_lab, "quantity": q,
                         "max_abs_deviation": float(d.max()),
                         "n_bars_differing": int((d > 0).sum())})
    blind = pd.DataFrame(rows)
    save(blind, "40_wick_blindness")
    print(blind.to_string(index=False, float_format=lambda v: f"{v:.3e}"), flush=True)
    identical = bool((blind["max_abs_deviation"] == 0.0).all())
    print(f"  -> 逐位相同: {identical}", flush=True)

    # ---- 2. 通道 S：影线 -> 信号 ------------------------------------------
    print("\n=== 2. 通道 S：全样本影线（改 range_pos / atr_pct）===", flush=True)
    rows = []
    for lab in ("S  全样本影线30%", "S3 全样本影线30%·ATR×3"):
        r, b = by[lab], A0
        d = (r["bars"]["gross_exposure"] - b["bars"]["gross_exposure"]).abs()
        rows.append({"arm": lab,
                     "Sharpe": r["metrics"]["Sharpe"],
                     "d_Sharpe": r["metrics"]["Sharpe"] - A0["metrics"]["Sharpe"],
                     "d_final_equity": float(r["bars"]["equity"].iloc[-1]
                                             - A0["bars"]["equity"].iloc[-1]),
                     "d_final_equity_pct": float(r["bars"]["equity"].iloc[-1]
                                                 / A0["bars"]["equity"].iloc[-1] - 1.0),
                     "maxdev_gross_exposure": float(d.max()),
                     "n_bars_book_differs": int((d > 1e-12).sum()),
                     "pct_bars_book_differs": float((d > 1e-12).mean())})
    chanS = pd.DataFrame(rows)
    save(chanS, "40b_channel_signal")
    print(chanS.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)
    _c = apply_overrides(default_config(bar=BAR, rebalance_bars=REBAL),
                         ACCEPTED_OVERRIDES)
    _rd = int(round(_c.factors.range_days * BARS_PER_DAY[BAR]))
    print(f"  range_pos 回看 range_days={_c.factors.range_days:g} 天 = {_rd} 根 bar ⇒"
          f" 一根针会把 r_high/r_low 撑开、从而压扁 range_pos **整整 {_rd} 根**",
          flush=True)

    # ---- 3. 通道 P：只动 open = 纯价格冲击 ---------------------------------
    print("\n=== 3. 通道 P：只动 open（书必须逐位不变，一 bar 损益有解析预测）===",
          flush=True)
    held_row = A0["bars"].iloc[t_shock - 1]
    net_exp = float(held_row["net_exposure"])
    gross_exp = float(held_row["gross_exposure"])
    # `held` 在两次调仓之间是常数，所以 t_shock-1 那一根的持仓就是
    # `weight_matrix[k]`（k = 第 70% 个调仓点）。用它做解析预测，
    # 不能用 target 权重——`max_daily_turnover=0.20` 会把实际仓位压到目标以下。
    ix = {n: j for j, n in enumerate(A0["insts"])}
    held_at = np.asarray(A0["weights"][k], dtype="float64")
    print(f"  冲击 bar 前一根的敞口: 净 {net_exp:+.6f}  毛 {gross_exp:.6f} "
          f"(净/毛 {abs(net_exp) / gross_exp:.4f})；"
          f"Σheld_at={held_at.sum():+.6f}", flush=True)
    print(f"  实际/目标 BTC 权重: {held_at[ix[name_big]]:+.6f} / {w_big:+.6f} "
          f"= {held_at[ix[name_big]] / w_big:.4f}（换手节流的爬坡）", flush=True)
    rows = []
    for spec in specs:
        if spec.get("kind") != "open":
            continue
        r, b = by[spec["label"]], A0
        w = float(spec["w"])
        # `ret_exec[t] = open[t+1]/open[t]-1`，改 `open[t_shock]` 让
        # `net_ret[t_shock-1]` 多出 `w × Σ held·(open[t+1]/open[t])`。
        # ⚠️ 不是 `w × Σ held`：名义额是按**被冲击后**的价格记的，
        # 所以多一个 `open[t+1]/open[t]` 因子。全市场那一档可以整体化简成
        # `w × (净敞口 + 该 bar 毛收益)`（因为 `gross_ret = Σ held·(ratio-1)`）；
        # 单名那一档必须逐名字取 ratio。
        # 直接用 `w × 净敞口` 会差 0.03%（实测 −0.0086939 vs −0.0086916）。
        if spec.get("cols"):
            c = spec["cols"][0]
            oj = _p.open[c].to_numpy(dtype="float64")
            ratio = oj[t_shock] / oj[t_shock - 1]
            pred = w * float(held_at[ix[c]]) * ratio
        else:
            pred = w * (net_exp + float(b["bars"]["gross_ret"].iloc[t_shock - 1]))
        meas = float(r["bars"]["net_ret"].iloc[t_shock - 1]
                     - b["bars"]["net_ret"].iloc[t_shock - 1])
        rows.append({
            "arm": spec["label"], "shock_w": w,
            "predicted_1bar": pred, "measured_1bar": meas,
            "residual": meas - pred,
            "maxdev_gross_exposure_before_shock":
                _dev(r["bars"]["gross_exposure"], b["bars"]["gross_exposure"],
                     upto=t_shock - 1),
            "maxdev_net_before_shock":
                _dev(r["bars"]["net_ret"], b["bars"]["net_ret"], upto=t_shock - 2),
            "d_final_equity": float(r["bars"]["equity"].iloc[-1]
                                    - b["bars"]["equity"].iloc[-1]),
            "d_final_equity_pct": float(r["bars"]["equity"].iloc[-1]
                                        / b["bars"]["equity"].iloc[-1] - 1.0),
            "d_Sharpe": r["metrics"]["Sharpe"] - A0["metrics"]["Sharpe"],
            "d_max_dd": r["metrics"]["Max Drawdown"] - A0["metrics"]["Max Drawdown"],
        })
    chanP = pd.DataFrame(rows)
    save(chanP, "40c_channel_price")
    print(chanP.to_string(index=False, float_format=lambda v: f"{v:,.6f}"), flush=True)
    book_ok = bool((chanP["maxdev_gross_exposure_before_shock"] == 0.0).all())
    # 残差 <2e-9，正好是 `weight_matrix` 以 float32 存储的精度（预测里用了它）。
    # 相对残差 ~2e-7 = float32 eps，不是模型误差。
    id_ok = bool((chanP["residual"].abs() < 1e-8).all())
    print(f"  -> 注入点之前书逐位不变: {book_ok}", flush=True)
    print(f"  -> 一 bar 损益 = w×Σ held·(open[t+1]/open[t]) 恒等式成立: {id_ok}"
          f"（最大残差 {chanP['residual'].abs().max():.2e}，= float32 权重矩阵精度）",
          flush=True)

    # ---- 4. 爆仓距离（解析）----------------------------------------------
    print("\n=== 4. 爆仓距离 ===", flush=True)
    mmr = DEFAULT_MMR
    rows = []
    for g in (0.292, 0.468, 0.9636, 1.00, 2.00):
        rows.append({"gross_over_nav": g,
                     "liq_move_same_direction": (1.0 - mmr * g) / g})
    liq = pd.DataFrame(rows)
    save(liq, "40d_liquidation_distance")
    print(liq.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)
    print(f"  (mmr={mmr:.4f}；账户杠杆只改维持保证金，不改敞口)", flush=True)

    # ---- 5. ATR 门槛与通过率 ----------------------------------------------
    print("\n=== 5. liquidation_ok 的 ATR 门槛与通过率 ===", flush=True)
    p = _p
    cfg = apply_overrides(default_config(bar=BAR, rebalance_bars=REBAL),
                          ACCEPTED_OVERRIDES)
    liq_dist = 1.0 / max(cfg.risk.leverage_in_force, 1e-9) - mmr
    thr = liq_dist / cfg.risk.min_liquidation_atr_multiple
    atr = ((p.high - p.low) / p.close).rolling(24, min_periods=2).mean()
    rows = []
    for scale, tag in ((1.0, "实际"), (1.5, "ATR×1.5"), (2.0, "ATR×2.0"),
                       (3.0, "ATR×3.0")):
        a = atr.to_numpy(dtype="float64")[-1] * scale
        ok = liquidation_ok(a, cfg.risk)
        fin = np.isfinite(a)
        rows.append({"scenario": tag, "n_listed": int(fin.sum()),
                     "n_pass": int(ok.sum()),
                     "pass_rate": float(ok.sum() / max(fin.sum(), 1)),
                     "atr_median": float(np.nanmedian(a)),
                     "atr_p90": float(np.nanpercentile(a, 90)),
                     "atr_max": float(np.nanmax(a))})
    atrt = pd.DataFrame(rows)
    save(atrt, "40e_atr_filter")
    print(f"  门槛 3·ATR% <= 1/{cfg.risk.leverage_in_force:g} - {mmr} = {liq_dist:.4f} "
          f"⇒ ATR% <= {thr:.4f} ({thr:.2%})", flush=True)
    print(atrt.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)

    # ---- 6. 历史极端 -------------------------------------------------------
    print("\n=== 6. 5.7 年实际最差单 bar（1h）===", flush=True)
    bb = A0["bars"]
    rows = [{"quantity": c, "worst": float(bb[c].min()), "worst_ts": str(bb[c].idxmin()),
             "best": float(bb[c].max()), "std": float(bb[c].std(ddof=1)),
             "worst_in_sigma": float(bb[c].min() / bb[c].std(ddof=1))}
            for c in ("net_ret", "gross_ret", "long_ret", "short_ret")]
    ext = pd.DataFrame(rows)
    save(ext, "40f_historical_extremes")
    print(ext.to_string(index=False, float_format=lambda v: f"{v:,.5f}"), flush=True)

    # ---- 7. 已有退市压力测试（对照）---------------------------------------
    dl = os.path.join(ROOT, "artifacts", "v5_1d_all5", "tables",
                      "24_delisting_stress.csv")
    if os.path.exists(dl):
        d = pd.read_csv(dl)
        print(f"\n=== 7. 已有退市压力测试（{len(d)} 次 × 每次 "
              f"{int(d['n_injected'].iloc[0])} 个 −50%~−90% 缺口 + 永久停牌）===",
              flush=True)
        print(f"  Sharpe {d['Sharpe'].min():.4f}~{d['Sharpe'].max():.4f} "
              f"(中位 {d['Sharpe'].median():.4f})  基线 {arch_s:.4f}", flush=True)
        print(f"  max_dd {d['max_dd'].min():.4f}~{d['max_dd'].max():.4f} "
              f"(中位 {d['max_dd'].median():.4f})", flush=True)

    # ---- 结论 --------------------------------------------------------------
    print("\n=== 结论 ===", flush=True)
    print(f"  0. 基线可复现（截断到存档网格后）: {repro}", flush=True)
    print(f"  1. 通道 L 收益路径读不读影线（末段注入）逐位相同: {identical}",
          flush=True)
    print(f"  2. 通道 P 注入点前书逐位不变: {book_ok}；"
          f"一 bar 损益恒等式成立: {id_ok}", flush=True)
    print(f"  3. 通道 S 全样本影线改了 "
          f"{chanS['pct_bars_book_differs'].iloc[0]:.2%} 的 bar 的仓", flush=True)
    print(f"  4. 净敞口/毛敞口 均值 "
          f"{float((A0['bars']['net_exposure'].abs() / A0['bars']['gross_exposure'].replace(0, np.nan)).mean()):.4f}",
          flush=True)


if __name__ == "__main__":
    main()
