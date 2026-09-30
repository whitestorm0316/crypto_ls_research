"""事前可见的「有效宇宙宽度」能不能预测策略的下一期损益？（Q42）

动机
----
Q41 发现 **4/5 个回撤窗口的宇宙比正常期窄**，但只把它写成「伴随现象 / 放大器」就停手了。
那是**没检验就下结论** —— 正是那份报告自己反对的做法。本模块把它当**可证伪的假设**来测。

为什么这个假设值得单独测
------------------------
它是报告里**唯一**同时满足三点的候选：
① 4/5 回撤窗口都成立；② **事前可见**（调仓那一刻就知道有几个名字）；
③ 能挂上一个**具体机制** —— 实测 `cap × n ≤ 1` 在 **14.65%** 的调仓点成立，
   此时 `inverse_vol_weights` **静默退化成等权**（权重不再按 `|score|/vol`）。

三个必须处理的坑
----------------
1. **宇宙宽度有长期趋势**：2021 年均 11.4 名 → 2024 年 23.1 名 → 2026 年 13.1 名（**非单调**）。
   原始相关可能是时间趋势的假象 ⇒ 必须用**滚动百分位**（只用 t **之前**的数据，事前可得）。
2. **未来损益也有趋势**（净值在涨）⇒ 除了原始损益，还要用**滚动去均值**的损益。
3. **宽度 0 的调仓点**（极早期）根本没有仓位，损益恒为 0 ⇒ 会把「窄 = 不亏」的假象
   喂进相关性。必须剔除，并把剔除条数报出来。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResult
from .failure_attribution import WINDOWS, any_window_mask, block_perm, welch, window_mask
from .ic import _spearman

# 滚动窗口（调仓点数）。180 个调仓点 ≈ 半年。
ROLL = 180


def width_frame(result: BacktestResult, roll: int = ROLL,
                cap: Optional[float] = None,
                windows: Sequence = WINDOWS) -> pd.DataFrame:
    """逐调仓点：**事前**宽度指标 + 下一期损益。

    前向损益用 `[dec[i]+1, dec[i+1]]`（调仓点之后成交，与引擎口径一致；实测
    `dec` 间隔恒为 `rebalance_bars`，所以这就是「持有这一期」的损益）。
    """
    bars = result.bars
    n = len(result.reb_ts)
    dec = np.searchsorted(bars.index, result.reb_ts)
    h = int(result.cfg.rebalance_bars)
    if cap is None:
        cap = float(result.cfg.portfolio.max_weight_per_instrument)

    net = bars["net_ret"].to_numpy(dtype="float64")
    gross = bars["gross_ret"].to_numpy(dtype="float64")

    rows: List[Dict[str, object]] = []
    for i in range(n):
        a = int(dec[i]) + 1
        if a >= len(bars):
            continue
        b = int(dec[i + 1]) if i + 1 < n else min(a + h, len(bars) - 1)
        rb = result.rebalances[i]
        wl = np.asarray(rb["w_long"], dtype="float64")
        ws = np.asarray(rb["w_short"], dtype="float64")
        nl, ns = len(rb["long"]), len(rb["short"])
        rows.append({
            "ts": result.reb_ts[i],
            "n_universe": int(rb["n_universe"]),
            "n_long": nl, "n_short": ns, "n_side_max": max(nl, ns),
            # cap 是否**绑住**（cap×n ≤ 1 ⇒ 打分/波动被忽略、静默等权）
            "cap_binding": bool(cap * max(nl, ns) <= 1.0),
            # 权重的离散系数：等权 ⇒ ≈0。用来**实测**退化是否真的发生。
            "w_long_cv": float(np.std(wl) / np.mean(wl)) if wl.size and np.mean(wl) else np.nan,
            "w_short_cv": float(np.std(ws) / np.mean(ws)) if ws.size and np.mean(ws) else np.nan,
            "exposure": float(rb["exposure"]),
            "fwd_net": float(net[a:b + 1].sum()),
            "fwd_gross": float(gross[a:b + 1].sum()),
            "traded": bool(max(nl, ns) > 0),
        })
    df = pd.DataFrame(rows).reset_index(drop=True)

    # 事前可得的两列：只用 **t 之前** 的 roll 个点（严格过去 ⇒ 无未来信息）
    w = df["n_universe"].to_numpy(dtype="float64")
    f = df["fwd_net"].to_numpy(dtype="float64")
    g = df["fwd_gross"].to_numpy(dtype="float64")
    pct = np.full(len(df), np.nan)
    fd = np.full(len(df), np.nan)
    gd = np.full(len(df), np.nan)
    for i in range(len(df)):
        lo = max(0, i - roll)
        past = w[lo:i]
        if past.size >= max(10, roll // 2):
            pct[i] = float(np.mean(past <= w[i]))
        fp, gp = f[lo:i], g[lo:i]
        fp = fp[np.isfinite(fp)]
        gp = gp[np.isfinite(gp)]
        if fp.size >= max(10, roll // 2):
            fd[i] = f[i] - float(np.mean(fp))
        if gp.size >= max(10, roll // 2):
            gd[i] = g[i] - float(np.mean(gp))
    df["width_pct"] = pct
    df["fwd_net_demean"] = fd
    df["fwd_gross_demean"] = gd

    df["window"] = ""
    for label, s, e, _dd in windows:
        df.loc[window_mask(pd.DatetimeIndex(df["ts"]), s, e), "window"] = label
    df.loc[df["window"] == "", "window"] = "NORMAL"
    return df


def _ic(x: pd.Series, y: pd.Series) -> Dict[str, float]:
    """Spearman 相关 + t 值（对 0 做检验）。两列都必须是**事前**可得的口径。"""
    ok = np.isfinite(x.to_numpy(dtype="float64")) & np.isfinite(y.to_numpy(dtype="float64"))
    a, b = x.to_numpy(dtype="float64")[ok], y.to_numpy(dtype="float64")[ok]
    if a.size < 10:
        return {"n": int(a.size), "ic": np.nan, "t": np.nan}
    r = float(_spearman(a, b))
    t = float(r * np.sqrt((a.size - 2) / max(1e-12, 1 - r * r))) if abs(r) < 1 else np.nan
    return {"n": int(a.size), "ic": r, "t": t}


def width_ic_table(df: pd.DataFrame, roll: int = ROLL) -> pd.DataFrame:
    """宽度指标 vs 下一期损益的秩相关。**同时给原始与去趋势两个版本**。

    `fwd_*` 是原始损益（有净值上涨趋势）；`fwd_*_demean` 是**减去滚动均值**后的。
    若两者符号不一致，说明「预测力」其实来自时间趋势。
    """
    d = df[df["traded"]].copy()
    rows = []
    for wcol in ("n_universe", "width_pct"):
        for fcol in ("fwd_net", "fwd_gross", "fwd_net_demean", "fwd_gross_demean"):
            r = _ic(d[wcol], d[fcol])
            rows.append({"width_metric": wcol, "target": fcol, **r})
    return pd.DataFrame(rows)


def width_bucket_table(df: pd.DataFrame, q: int = 5,
                       col: str = "width_pct") -> pd.DataFrame:
    """按事前宽度分位分组，看下一期损益的均值。"""
    d = df[df["traded"] & df[col].notna()].copy()
    if d.empty:
        return pd.DataFrame()
    try:
        d["bucket"] = pd.qcut(d[col], q, labels=[f"Q{i+1}" for i in range(q)],
                              duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    rows = []
    for b, seg in d.groupby("bucket", observed=True):
        rows.append({
            "bucket": str(b), "n": int(len(seg)),
            "n_universe_mean": float(seg["n_universe"].mean()),
            "width_pct_mean": float(seg[col].mean()),
            "fwd_net": float(seg["fwd_net"].mean()),
            "fwd_net_demean": float(seg["fwd_net_demean"].mean()),
            "win_rate": float((seg["fwd_net"] > 0).mean()),
            "cap_binding_share": float(seg["cap_binding"].mean()),
        })
    out = pd.DataFrame(rows).sort_values("bucket").reset_index(drop=True)
    # 最窄 vs 最宽 两组的 Welch + 块置换
    lo, hi = d[d["bucket"] == out["bucket"].iloc[0]], d[d["bucket"] == out["bucket"].iloc[-1]]
    out.attrs["narrowest_minus_widest"] = {
        **welch(lo["fwd_net"], hi["fwd_net"]), **block_perm(lo["fwd_net"], hi["fwd_net"]),
    }
    out.attrs["narrowest_minus_widest_demean"] = {
        **welch(lo["fwd_net_demean"], hi["fwd_net_demean"]),
        **block_perm(lo["fwd_net_demean"], hi["fwd_net_demean"]),
    }
    return out


def cap_binding_table(df: pd.DataFrame) -> pd.DataFrame:
    """`cap × n ≤ 1`（静默等权）成立 / 不成立 两组的下一期损益。

    这是那个**具体机制**的直接检验：如果静默等权真的有害，绑定时段的下一期损益应显著更差。
    """
    d = df[df["traded"]].copy()
    rows = []
    for flag, seg in d.groupby("cap_binding"):
        rows.append({
            "cap_binding": bool(flag), "n": int(len(seg)),
            "n_side_max_mean": float(seg["n_side_max"].mean()),
            "n_universe_mean": float(seg["n_universe"].mean()),
            "fwd_net": float(seg["fwd_net"].mean()),
            "fwd_net_demean": float(seg["fwd_net_demean"].mean()),
            "win_rate": float((seg["fwd_net"] > 0).mean()),
            "w_long_cv_mean": float(seg["w_long_cv"].mean()),
            "w_short_cv_mean": float(seg["w_short_cv"].mean()),
        })
    out = pd.DataFrame(rows)
    a = d[d["cap_binding"]]
    b = d[~d["cap_binding"]]
    out.attrs["binding_minus_free"] = {
        **welch(a["fwd_net"], b["fwd_net"]), **block_perm(a["fwd_net"], b["fwd_net"]),
    }
    out.attrs["binding_minus_free_demean"] = {
        **welch(a["fwd_net_demean"], b["fwd_net_demean"]),
        **block_perm(a["fwd_net_demean"], b["fwd_net_demean"]),
    }
    return out


def window_width_table(df: pd.DataFrame, windows: Sequence = WINDOWS) -> pd.DataFrame:
    """5 个回撤窗口 vs 正常期的**事前**宽度（滚动百分位），带 Welch + 块置换。

    ⚠️ **不许静默丢窗口。** `width_pct` 需要 180 期历史才可算，而 **W4 落在样本第一个月**
    ⇒ 它的 `width_pct` 全是 NaN。第一版直接把它从表里删掉了（与 Q41 在
    `momentum_reversal` 上犯过的错**同一类**）。现在每个窗口都出一行，用
    `n_width_pct` / `width_pct_available` 把「算不出来」**显式写出来**，
    原始 `n_universe_mean` 照报（它是描述量，不依赖历史）。
    """
    d = df[df["traded"]]
    base_wp = d[d["window"] == "NORMAL"]["width_pct"].dropna()
    rows = []
    for label, _s, _e, _dd in windows:
        seg = d[d["window"] == label]
        wp = seg["width_pct"].dropna()
        avail = bool(len(wp) >= 5)
        row = {
            "window": label, "n": int(len(seg)),
            "n_width_pct": int(len(wp)), "width_pct_available": avail,
            "n_universe_mean": float(seg["n_universe"].mean()) if len(seg) else np.nan,
            "width_pct_mean": float(wp.mean()) if avail else np.nan,
            "fwd_net_mean": float(seg["fwd_net"].mean()) if len(seg) else np.nan,
            "first_reb_index": int(seg.index.min()) if len(seg) else -1,
        }
        if avail and len(base_wp) >= 10:
            row.update(welch(wp, base_wp))
            row.update(block_perm(wp, base_wp))
        else:
            row.update({"welch_t": np.nan, "welch_p": np.nan, "mean_diff": np.nan,
                        "perm_p": np.nan, "perm_n": np.nan})
        rows.append(row)
    rows.append({
        "window": "NORMAL", "n": int(len(d[d["window"] == "NORMAL"])),
        "n_width_pct": int(len(base_wp)), "width_pct_available": bool(len(base_wp) >= 5),
        "n_universe_mean": float(d[d["window"] == "NORMAL"]["n_universe"].mean()),
        "width_pct_mean": float(base_wp.mean()) if len(base_wp) else np.nan,
        "fwd_net_mean": float(d[d["window"] == "NORMAL"]["fwd_net"].mean()),
        "first_reb_index": int(d[d["window"] == "NORMAL"].index.min()),
        "welch_t": np.nan, "welch_p": np.nan, "mean_diff": np.nan,
        "perm_p": np.nan, "perm_n": np.nan,
    })
    return pd.DataFrame(rows)


def warmup_table(df: pd.DataFrame, roll: int = ROLL) -> pd.DataFrame:
    """冷启动期（调仓点序号 < `roll`）vs 之后：损益与宽度是否不同？

    ⚠️ **W4 落在样本第一个月** ⇒ 它的「宇宙只有 7.4 名」可能只是**数据可用性**
    （2021 年整个市场在这个池子里只有 11 个合格名字），**不是**一个市场状态。
    若冷启动期与成熟期确实不同，Q41 的「正常期」基准也被它污染了。
    """
    d = df.copy()
    d["phase"] = np.where(np.arange(len(d)) < roll, "warmup", "mature")
    rows = []
    for ph, seg in d[d["traded"]].groupby("phase"):
        rows.append({
            "phase": ph, "n": int(len(seg)),
            "reb_index_max": int(seg.index.max()),
            "n_universe_mean": float(seg["n_universe"].mean()),
            "n_side_max_mean": float(seg["n_side_max"].mean()),
            "cap_binding_share": float(seg["cap_binding"].mean()),
            "exposure_mean": float(seg["exposure"].mean()),
            "fwd_net_mean": float(seg["fwd_net"].mean()),
            "fwd_net_sum": float(seg["fwd_net"].sum()),
            "win_rate": float((seg["fwd_net"] > 0).mean()),
        })
    out = pd.DataFrame(rows)
    a = d[d["traded"] & (d["phase"] == "warmup")]["fwd_net"]
    b = d[d["traded"] & (d["phase"] == "mature")]["fwd_net"]
    out.attrs["warmup_minus_mature"] = {**welch(a, b), **block_perm(a, b)}
    return out


def trend_check(df: pd.DataFrame) -> Dict[str, float]:
    """宇宙宽度有没有**长期趋势**？（决定「窄 = 危险」这个读数站不站得住）

    如果宽度与调仓点序号强相关，那么「回撤窗口宽度低」可能只是**回撤集中在样本早期**，
    与「窄宇宙」这个机制无关。这里量：原始宽度 vs 序号的相关，以及
    原始口径与去趋势口径**符号不一致**的窗口数。
    """
    d = df[df["traded"]].copy()
    idx = np.arange(len(df))[d.index].astype("float64")
    w = d["n_universe"].to_numpy(dtype="float64")
    ok = np.isfinite(w)
    r_idx = float(_spearman(idx[ok], w[ok]))
    d2 = d[d["width_pct"].notna()]
    r_two = float(_spearman(d2["n_universe"], d2["width_pct"]))
    return {"spearman_width_vs_index": r_idx, "n": int(ok.sum()),
            "spearman_raw_vs_pct": r_two, "n_pct": int(len(d2)),
            "yearly_mean_width": {
                str(k.year): float(v) for k, v in
                d.set_index("ts")["n_universe"].resample("YE").mean().items()}}



def binding_weight_check(df: pd.DataFrame, res: BacktestResult) -> Dict[str, float]:
    """实测：`cap_binding` 为真时，权重是否真的**退化成等权**。

    报告里「静默退化成等权」这句此前只从**代码**推出，没有在真实账本上验过。
    这里用权重离散系数直接量：绑定组的 `cv` 应显著低于非绑定组。
    """
    d = df[df["traded"]]
    a = d[d["cap_binding"]]["w_long_cv"].dropna()
    b = d[~d["cap_binding"]]["w_long_cv"].dropna()
    return {
        "binding_n": int(a.size), "free_n": int(b.size),
        "binding_cv_mean": float(a.mean()) if a.size else np.nan,
        "free_cv_mean": float(b.mean()) if b.size else np.nan,
        "binding_cv_median": float(a.median()) if a.size else np.nan,
        "free_cv_median": float(b.median()) if b.size else np.nan,
        "share_narrow_side_le_5": float((d["n_side_max"] <= 5).mean()),
        "n_untraded_rebalances": int((~df["traded"]).sum()),
    }
