"""失效归因（failure attribution）：解释历史回撤 —— **只诊断，不调参、不优化**。

纪律（与 `artifacts/FINDINGS.md` Q41 对应）
------------------------------------------
* **本模块只读**：所有策略参数从 `config.settings.ACCEPTED_*` 读取，模块内**没有任何可调旋钮**，
  也不做参数搜索。窗口清单来自用户给定的 5 段，**不重定义、不按窗口调阈值**。
* 窗口切片一律 **UTC 日历日、双端含**（`[start 00:00, end 24:00)`）。
* 「回撤期 vs 正常期」的差异**一律给统计量**（Welch t + 块置换 p），不靠肉眼。
* 会计恒等式（引擎 `engine.py:433`）：**`net = gross − fee − spread − impact + funding`**。
  ⚠️ `funding` 是**带符号现金流**，要 **加**不要减 —— 写成减号会把每一笔资金费记成贷记。
  全样本实测 `max|resid| = 8.7e-19`。
* ⚠️ `long_ret` / `short_ret` 是**带符号 P&L**（`Σ held·(held≷0)·r`），不是绝对收益。

窗口边界的不一致（必须披露）
----------------------------
用户给的 5 个窗口**不是同一套边界约定**：W1/W4/W5 = 峰→谷；W2 的第二个数是**恢复日**
（真谷底 2023-12-18）；W3 的第一个数是**回撤内部的局部高点**（cummax 峰是 2023-01-25）。
5 个深度数字**逐位吻合**，所以是同一批事件。因此本模块**同时**报两种切片：
`stated`（用户原样）与 `episode`（真实的 cummax 峰→谷）。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResult, pick_exec_price
from ..data.store import Panels, panels_to_arrays
from ..factors.engine import FACTOR_NAMES
from .ic import _spearman

# ---------------------------------------------------------------------------
# 窗口定义：用户原样。`dd_stated` 是用户清单里的深度，用于对账。
# ---------------------------------------------------------------------------
WINDOWS: Tuple[Tuple[str, str, str, float], ...] = (
    ("W1", "2024-07-05", "2024-11-01", -0.1331),
    ("W2", "2023-11-09", "2024-03-03", -0.1082),
    ("W3", "2023-02-22", "2023-05-03", -0.0998),
    ("W4", "2021-02-01", "2021-04-10", -0.0959),
    ("W5", "2022-08-04", "2022-09-21", -0.0942),
)
WINDOW_LABELS = tuple(w[0] for w in WINDOWS)


# ---------------------------------------------------------------------------
# 基础：切片
# ---------------------------------------------------------------------------
def window_mask(index: pd.DatetimeIndex, start: str, end: str) -> np.ndarray:
    """布尔掩码：UTC 日历日 `[start, end]` **双端含**。"""
    a = pd.Timestamp(start, tz="UTC")
    b = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    return np.asarray((index >= a) & (index < b))


def any_window_mask(index: pd.DatetimeIndex,
                    windows: Sequence = WINDOWS) -> np.ndarray:
    m = np.zeros(len(index), dtype=bool)
    for _lbl, s, e, _dd in windows:
        m |= window_mask(index, s, e)
    return m


# ---------------------------------------------------------------------------
# 回撤段的真实几何
# ---------------------------------------------------------------------------
def episode_geometry(bars: pd.DataFrame) -> pd.DataFrame:
    """所有「水下」段的 (峰, 谷, 恢复, 深度)，按深度降序。

    峰 = 进入水下前最后一次触及 cummax 的时刻；谷 = 段内 dd 最低点；
    恢复 = 段内 dd 首次回到 0 的时刻。
    """
    eq = bars["equity"]
    dd = eq / eq.cummax() - 1.0
    under = (dd < -1e-12).to_numpy()
    grp = pd.Series(np.cumsum(np.r_[False, under[:-1]] != under), index=dd.index)
    rows: List[dict] = []
    for _g, seg in dd.groupby(grp):
        if not bool(under[dd.index.get_loc(seg.index[0])]):
            continue
        t0 = seg.index[0]
        pre = eq.loc[:t0]
        pk = pre.index[-1] if len(pre) == 0 else pre[pre >= pre.cummax() - 1e-15].index[-1]
        rows.append({"peak": pk, "trough": seg.idxmin(), "recovery": seg.index[-1],
                     "depth": float(seg.min()), "bars_underwater": int(len(seg))})
    if not rows:
        return pd.DataFrame(columns=["peak", "trough", "recovery", "depth", "bars_underwater"])
    return pd.DataFrame(rows).sort_values("depth").reset_index(drop=True)


def episode_windows(bars: pd.DataFrame,
                    windows: Sequence = WINDOWS) -> Tuple[Tuple[str, pd.Timestamp, pd.Timestamp, float], ...]:
    """把用户窗口换成**真实 cummax 峰→谷**的时间戳对（bar 级，双端含）。

    用的是与 `leg_attribution(mode="episode")` **完全同一对时间戳** ⇒ 分腿 P&L 与
    IC / 轮动 / 流动性各列落在**同一个切片**上，**不引入第二套边界约定**。
    这是刻意的：若这里改用日历日，就会出现「P&L 按 bar、IC 按日」的第三种口径，
    两个数字放同一行就不可比了。
    """
    geo = match_windows(bars, windows).set_index("window")
    out: List[Tuple[str, pd.Timestamp, pd.Timestamp, float]] = []
    for label, _s, _e, dd in windows:
        out.append((label, pd.Timestamp(geo.loc[label, "peak"]),
                    pd.Timestamp(geo.loc[label, "trough"]), float(dd)))
    return tuple(out)


def retag_episode(df: pd.DataFrame, ts_col: str,
                  ep: Sequence) -> pd.Series:
    """按 bar 级峰→谷把逐点表重新打标；落在任何 episode 之外的标 `"REST"`。

    标 `"REST"` 而不是 `"NORMAL"`：episode 窗口并集与用户窗口并集**不是同一块**，
    把两者都叫 NORMAL 会让两个不同的基准混成一行。汇总时只取 episode 标签。
    """
    w = pd.Series("REST", index=df.index, dtype=object)
    for lbl, pk, tr, _dd in ep:
        w[(df[ts_col] >= pk) & (df[ts_col] <= tr)] = lbl
    return w


def _window_labels(windows: Sequence) -> Tuple[str, ...]:
    """汇总表要输出的标签集合 = **由 `windows` 决定** + 一个 `NORMAL`。

    ⚠️ 这里曾经写死 `WINDOW_LABELS`，于是 `rotation_table(..., windows=...)` 与
    `liquidity_table(..., windows=...)` 的 `windows` 参数**被接受但完全不生效**
    （永远只输出模块常量那 5 个窗口）⇒ 传 episode 窗口会静默返回空表。
    """
    return tuple(w[0] for w in windows) + ("NORMAL",)


def match_windows(bars: pd.DataFrame, windows: Sequence = WINDOWS) -> pd.DataFrame:
    """把用户给的窗口对齐到真实的回撤段（用「谷底落在窗口内」配对）。"""
    eps = episode_geometry(bars)
    rows: List[dict] = []
    for label, s, e, dd in windows:
        hit = eps[window_mask(pd.DatetimeIndex(eps["trough"]), s, e)]
        r = hit.iloc[0] if len(hit) else None
        rows.append({
            "window": label, "start": s, "end": e, "dd_stated": float(dd),
            "peak": r["peak"] if r is not None else pd.NaT,
            "trough": r["trough"] if r is not None else pd.NaT,
            "recovery": r["recovery"] if r is not None else pd.NaT,
            "depth_actual": float(r["depth"]) if r is not None else np.nan,
            "bars_underwater": int(r["bars_underwater"]) if r is not None else 0,
            "end_is_trough": bool(r is not None and r["trough"].date() == pd.Timestamp(e).date()),
            "start_is_peak": bool(r is not None and r["peak"].date() == pd.Timestamp(s).date()),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 工作流 1 + 8：分腿归因 + 成本分解
# ---------------------------------------------------------------------------
def _max_consecutive_true(flags: Sequence[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return int(best)


def leg_attribution(bars: pd.DataFrame, windows: Sequence = WINDOWS,
                    mode: str = "stated") -> pd.DataFrame:
    """逐窗口 Long/Short 分腿 + 换手/资金费 + 日频胜率与最长连亏。

    `mode="stated"` 用用户给的窗口；`mode="episode"` 用真实 cummax 峰→谷。
    """
    if mode not in ("stated", "episode"):
        raise ValueError("mode must be 'stated' or 'episode'")
    geo = match_windows(bars, windows).set_index("window")
    rows: List[dict] = []
    for label, s, e, dd in windows:
        if mode == "stated":
            m = window_mask(bars.index, s, e)
        else:
            pk, tr = geo.loc[label, "peak"], geo.loc[label, "trough"]
            m = np.asarray((bars.index >= pk) & (bars.index <= tr))
        w = bars[m]
        daily = w["net_ret"].resample("1D").sum()
        gross = float(w["gross_ret"].sum())
        long_pnl = float(w["long_ret"].sum())
        short_pnl = float(w["short_ret"].sum())
        fee, spread = float(w["fee"].sum()), float(w["spread"].sum())
        impact, fund = float(w["impact"].sum()), float(w["funding"].sum())
        net = float(w["net_ret"].sum())
        rows.append({
            "window": label, "start": s, "end": e, "mode": mode,
            "n_bars": int(len(w)), "n_days": int(len(daily)),
            "dd_stated": float(dd),
            "net_pnl": net, "gross_pnl": gross,
            "long_pnl": long_pnl, "short_pnl": short_pnl,
            "fee": fee, "spread": spread, "impact": impact, "funding": fund,
            "cost_total": fee + spread + impact,
            "long_share_of_gross": long_pnl / gross if gross else np.nan,
            "short_share_of_gross": short_pnl / gross if gross else np.nan,
            "turnover": float(w["turnover"].sum()),
            "avg_gross_exposure": float(w["gross_exposure"].mean()),
            "win_rate_daily": float((daily > 0).mean()) if len(daily) else np.nan,
            "mean_daily": float(daily.mean()) if len(daily) else np.nan,
            "worst_day": float(daily.min()) if len(daily) else np.nan,
            "max_consec_loss_days": _max_consecutive_true((daily < 0).to_numpy()),
            "window_ret_compound": float((1 + w["net_ret"]).prod() - 1),
            "gross_ret_compound": float((1 + w["gross_ret"]).prod() - 1),
        })
    return pd.DataFrame(rows)


def leg_verdict(row: pd.Series) -> str:
    """A) Long 驱动 / B) Short 驱动 / C) 两者 / D) 都不是（成本或资金费）。

    判据（可证伪、无阈值）：看两条腿的**带符号** P&L 谁为负。
    """
    lp, sp = float(row["long_pnl"]), float(row["short_pnl"])
    if lp >= 0 and sp >= 0:
        return "D) 两腿都不亏（亏损来自成本/资金费）"
    if lp < 0 and sp >= 0:
        return "A) Long 驱动"
    if sp < 0 and lp >= 0:
        return "B) Short 驱动"
    return "C) 两腿同时亏"


def cost_decomposition(bars: pd.DataFrame, windows: Sequence = WINDOWS,
                       mode: str = "stated") -> pd.DataFrame:
    """`Gross − Fee − Spread − Impact + Funding = Net`，并判定信号失效 vs 成本问题。"""
    t = leg_attribution(bars, windows, mode=mode)
    t["recon_resid"] = t["net_pnl"] - (t["gross_pnl"] - t["cost_total"] + t["funding"])
    t["verdict"] = [
        ("信号失效：毛收益本身为负" if g < 0 else
         "成本问题：毛收益为正、净收益为负" if n < 0 else
         "该窗口净收益为正（窗口延伸过谷底）")
        for g, n in zip(t["gross_pnl"], t["net_pnl"])
    ]
    t["cost_share_of_gross"] = np.where(
        t["gross_pnl"] != 0, t["cost_total"] / t["gross_pnl"].abs(), np.nan)
    return t[["window", "mode", "gross_pnl", "fee", "spread", "impact", "funding",
              "cost_total", "net_pnl", "recon_resid", "verdict", "cost_share_of_gross"]]


# ---------------------------------------------------------------------------
# 工作流 2 + 4：逐因子 IC 与横截面动量反转
# ---------------------------------------------------------------------------
def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if int(ok.sum()) < 8:
        return np.nan
    x, y = a[ok].astype("float64"), b[ok].astype("float64")
    if x.std() < 1e-12 or y.std() < 1e-12:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def exec_price(panels: Panels, cfg, convention: str = "exec") -> np.ndarray:
    """执行价矩阵（`exec` = 引擎真正成交的那个价，`close` = 仅供对照）。"""
    arr = panels_to_arrays(panels)
    if convention == "exec":
        return pick_exec_price(arr, cfg.execution.exec_price).astype("float64")
    return arr["close"].astype("float64")


def ic_frame(result: BacktestResult, panels: Panels, matrix: np.ndarray,
             horizon_bars: int, convention: str = "exec") -> pd.DataFrame:
    """逐调仓点的 Rank IC 与 Pearson IC，索引 = `result.reb_ts`。

    与 `analysis.ic.rank_ic_over_horizons` 同一约定（`exec` 下前向收益从 `d+1` 起算），
    测试里与它**交叉验证**。
    """
    cfg = result.cfg
    px = exec_price(panels, cfg, convention)
    T = px.shape[0]
    dec = np.searchsorted(panels.index, result.reb_ts)
    shift = 1 if convention == "exec" else 0
    rk = np.full(len(dec), np.nan)
    pe = np.full(len(dec), np.nan)
    for i, d in enumerate(dec):
        a, b = int(d) + shift, int(d) + shift + int(horizon_bars)
        if a >= T or b >= T:
            continue
        fwd = px[b] / px[a] - 1.0
        fwd[~np.isfinite(fwd)] = np.nan
        rk[i] = _spearman(matrix[i], fwd)
        pe[i] = _pearson(matrix[i], fwd)
    return pd.DataFrame({"rank_ic": rk, "pearson_ic": pe}, index=result.reb_ts)


def masked_factor(result: BacktestResult, factor: str) -> np.ndarray:
    """因子 z 值，池外置 NaN（与 `analysis.ic.ic_by_factor` 一致）。"""
    m = result.factor_zs[factor].astype("float64").copy()
    m[~result.mask_matrix] = np.nan
    return m


def ic_summary(s: pd.Series) -> Dict[str, float]:
    v = s.dropna()
    n = len(v)
    if n < 3:
        return {"n": n, "ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan,
                "t_stat": np.nan, "pos_share": np.nan}
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    icir = mu / sd if sd > 0 else np.nan
    return {"n": n, "ic_mean": mu, "ic_std": sd, "icir": icir,
            "t_stat": icir * np.sqrt(n) if np.isfinite(icir) else np.nan,
            "pos_share": float((v > 0).mean())}


def welch(a: pd.Series, b: pd.Series) -> Dict[str, float]:
    """Welch t 检验（方差不齐）。⚠️ IC 序列自相关 ⇒ p 值**偏乐观**，配合 `block_perm` 读。"""
    from scipy import stats as _st
    x, y = a.dropna().to_numpy(), b.dropna().to_numpy()
    if len(x) < 3 or len(y) < 3:
        return {"welch_t": np.nan, "welch_p": np.nan, "mean_diff": np.nan}
    t, p = _st.ttest_ind(x, y, equal_var=False)
    return {"welch_t": float(t), "welch_p": float(p), "mean_diff": float(x.mean() - y.mean())}


def block_perm(a: pd.Series, b: pd.Series, block: int = 5, n_perm: int = 2000,
               seed: int = 20260930) -> Dict[str, float]:
    """块置换检验：保留自相关结构，问「这 5 段恰好拿到这么大的均值差」有多难。

    把 a、b 拼成一条序列，随机抽**同样长度**的若干块作为伪「窗口」，比较均值差。
    """
    x, y = a.dropna().to_numpy(), b.dropna().to_numpy()
    if len(x) < block or len(y) < block:
        return {"perm_p": np.nan, "perm_n": 0}
    pool = np.r_[x, y]
    n, k = len(pool), len(x)
    obs = abs(float(x.mean() - y.mean()))
    if n <= block:
        return {"perm_p": np.nan, "perm_n": 0}
    rng = np.random.default_rng(seed)
    nblk = int(np.ceil(k / block))
    hits = 0
    for _ in range(int(n_perm)):
        starts = rng.integers(0, n - block + 1, size=nblk)
        idx = np.concatenate([np.arange(s, s + block) for s in starts])[:k]
        picked = np.zeros(n, dtype=bool)
        picked[idx] = True
        if abs(float(pool[picked].mean() - pool[~picked].mean())) >= obs:
            hits += 1
    return {"perm_p": (hits + 1) / (n_perm + 1), "perm_n": int(n_perm)}


def ic_by_window(result: BacktestResult, panels: Panels,
                 windows: Sequence = WINDOWS, horizon_bars: int = 24,
                 perm: bool = True, factors: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """逐因子 × 逐窗口的 IC 表，含 `FULL` 与 `NORMAL`（不在任何窗口内的调仓点）。

    每个窗口一行；`delta_vs_normal` = 窗口 IC 均值 − 正常期 IC 均值，带 Welch t/p
    与块置换 p。
    """
    facs = tuple(factors) if factors is not None else tuple(FACTOR_NAMES)
    inwin = any_window_mask(result.reb_ts, windows)
    rows: List[dict] = []
    for f in facs:
        m = masked_factor(result, f)
        fr = ic_frame(result, panels, m, horizon_bars)
        rk = fr["rank_ic"]
        for label, s, e, _dd in windows:
            sel = window_mask(result.reb_ts, s, e)
            rows.append({"factor": f, "window": label, **ic_summary(rk[sel])})
        normal = rk[~inwin]
        rows.append({"factor": f, "window": "FULL", **ic_summary(rk)})
        rows.append({"factor": f, "window": "NORMAL", **ic_summary(normal)})
        for label, s, e, _dd in windows:
            sel = window_mask(result.reb_ts, s, e)
            d = welch(rk[sel], normal)
            if perm:
                d.update(block_perm(rk[sel], normal))
            rows.append({"factor": f, "window": label + "_vs_NORMAL", **ic_summary(rk[sel]), **d})
    return pd.DataFrame(rows)


def rolling_ic(result: BacktestResult, panels: Panels, factor: str,
               horizon_bars: int = 24, windows: Sequence = WINDOWS,
               spans: Sequence[int] = (5, 20)) -> pd.DataFrame:
    """5D / 20D 滚动 IC 序列 + 逐窗口均值（用同一份逐点 IC，不重算）。"""
    rk = ic_frame(result, panels, masked_factor(result, factor), horizon_bars)["rank_ic"]
    out = pd.DataFrame({"rank_ic": rk})
    for k in spans:
        out[f"roll{k}"] = rk.rolling(int(k), min_periods=max(2, int(k) // 2)).mean()
    out["window"] = ""
    for label, s, e, _dd in WINDOWS:
        out.loc[window_mask(result.reb_ts, s, e), "window"] = label
    out.loc[out["window"] == "", "window"] = "NORMAL"
    return out


def tail_spread(result: BacktestResult, panels: Panels, score: np.ndarray,
                horizon_bars: int = 24, top_k: int = 10, min_names: int = 20,
                windows: Sequence = WINDOWS) -> pd.DataFrame:
    """逐调仓点：池内 score 前 `top_k` 名与后 `top_k` 名**未来收益均值之差**。

    问的是「**尾部**能不能赚钱」。与 `ic_frame` 的**整体秩相关**是两个不同的量 ——
    两者可以**不同号**：`rev_short` 就是（IC 正、尾部价差负）。只看 IC 会漏掉它。

    池子 < `max(min_names, 2*top_k)` 时置 NaN：上下两组重叠时「价差」没有意义。
    """
    cfg = result.cfg
    px = exec_price(panels, cfg, "exec")
    T = px.shape[0]
    dec = np.searchsorted(panels.index, result.reb_ts)
    need = max(int(min_names), 2 * int(top_k))
    rows: List[dict] = []
    for i, d in enumerate(dec):
        a, b = int(d) + 1, int(d) + 1 + int(horizon_bars)
        s = score[i]
        n = int(np.isfinite(s).sum())
        top = bot = np.nan
        if a < T and b < T:
            fwd = px[b] / px[a] - 1.0
            j = np.flatnonzero(np.isfinite(s) & np.isfinite(fwd))
            if j.size >= need:
                srt = j[np.argsort(s[j])]
                bot = float(np.mean(fwd[srt[:top_k]]))
                top = float(np.mean(fwd[srt[-top_k:]]))
        rows.append({"ts": result.reb_ts[i], "n_pool": n,
                     "top_fwd": top, "bot_fwd": bot, "spread": top - bot})
    df = pd.DataFrame(rows)
    df["window"] = ""
    for label, s_, e_, _dd in windows:
        df.loc[window_mask(pd.DatetimeIndex(df["ts"]), s_, e_), "window"] = label
    df.loc[df["window"] == "", "window"] = "NORMAL"
    return df


def _tail_row(label: str, seg: pd.DataFrame) -> dict:
    return {"window": label, "n": int(seg["spread"].notna().sum()),
            "n_pool": float(seg["n_pool"].mean()),
            "top_fwd": float(seg["top_fwd"].mean()),
            "bot_fwd": float(seg["bot_fwd"].mean()),
            "spread": float(seg["spread"].mean())}


def tail_spread_table(ts_df: pd.DataFrame, windows: Sequence = WINDOWS,
                      full: bool = True) -> pd.DataFrame:
    rows: List[dict] = []
    if full:
        rows.append(_tail_row("FULL", ts_df))
    for label in _window_labels(windows):
        seg = ts_df[ts_df["window"] == label]
        if not seg.empty:
            rows.append(_tail_row(label, seg))
    return pd.DataFrame(rows)


def momentum_reversal(result: BacktestResult, panels: Panels,
                      windows: Sequence = WINDOWS, past_days: float = 5.25,
                      horizons_days: Sequence[float] = (1, 3, 5, 10),
                      top_k: int = 10, min_names: int = 8) -> pd.DataFrame:
    """横截面动量反转：**过去 5.25 天最强/最弱**的名字，未来 1/3/5/10 天收益如何。

    问的是「过去强 → 未来强」是否在某些窗口翻成「过去强 → 未来弱」。

    ⚠️ **必须同时报两个口径**：`top10/bot10` 需要池子 ≥ `2*top_k` 名才不重叠，
    而 5 个回撤窗口的宇宙**恰恰更窄**（W4 均值只有 7.4 名）⇒ 只报 Top10/Bottom10 会
    **恰好把最需要解释的窗口整段丢掉**（第一版就是这样：W1/W3/W4/W5 全 0 个样本）。
    因此再加一个**尺度无关**的「上/下三分位」价差，以及只要 ≥8 名就能算的 Rank IC。
    """
    cfg = result.cfg
    bpd = cfg.bars_per_day
    px = exec_price(panels, cfg, "exec")
    T = px.shape[0]
    dec = np.searchsorted(panels.index, result.reb_ts)
    pb = max(2, int(round(past_days * bpd)))
    inwin = any_window_mask(result.reb_ts, windows)
    rows: List[dict] = []
    for i, d in enumerate(dec):
        a = int(d) + 1
        if a - pb < 0 or a >= T:
            continue
        past = px[a] / px[a - pb] - 1.0
        past[~np.isfinite(past)] = np.nan
        past = np.where(result.mask_matrix[i], past, np.nan)
        ok = np.flatnonzero(np.isfinite(past))
        row: Dict[str, object] = {"ts": result.reb_ts[i], "n_pool": int(ok.size),
                                  "in_window": bool(inwin[i])}
        if ok.size < max(min_names, 3):
            rows.append(row)
            continue
        order = ok[np.argsort(-past[ok])]
        wide = ok.size >= 2 * top_k
        top = order[:top_k] if wide else None
        bot = order[-top_k:] if wide else None
        k3 = max(1, ok.size // 3)
        top3, bot3 = order[:k3], order[-k3:]
        for h in horizons_days:
            b = a + int(round(float(h) * bpd))
            if b >= T:
                for c in ("top", "bot", "spread", "ric", "t3", "b3", "s3"):
                    row[f"{c}_{h}d"] = np.nan
                continue
            fwd = px[b] / px[a] - 1.0
            fwd[~np.isfinite(fwd)] = np.nan
            if wide:
                t_, b_ = float(np.nanmean(fwd[top])), float(np.nanmean(fwd[bot]))
                row[f"top_{h}d"], row[f"bot_{h}d"] = t_, b_
                row[f"spread_{h}d"] = t_ - b_
            else:
                row[f"top_{h}d"] = row[f"bot_{h}d"] = row[f"spread_{h}d"] = np.nan
            t3, b3 = float(np.nanmean(fwd[top3])), float(np.nanmean(fwd[bot3]))
            row[f"t3_{h}d"], row[f"b3_{h}d"] = t3, b3
            row[f"s3_{h}d"] = t3 - b3
            row[f"ric_{h}d"] = _spearman(past, fwd)
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["window"] = ""
    for label, s, e, _dd in windows:
        df.loc[window_mask(pd.DatetimeIndex(df["ts"]), s, e), "window"] = label
    df.loc[df["window"] == "", "window"] = "NORMAL"
    return df


def momentum_reversal_table(mr: pd.DataFrame,
                            horizons_days: Sequence[float] = (1, 3, 5, 10),
                            windows: Sequence = WINDOWS) -> pd.DataFrame:
    """把逐点表压成「窗口 × 未来期限」的均值表。

    `n` = 该窗口能算 Rank IC 的点数（池子 ≥8）；`n_top10` = 其中池子 ≥20、能算
    Top10/Bottom10 的点数。**两列都要看**，否则会以为窄宇宙窗口「没有反转」。
    """
    rows: List[dict] = []
    for label in WINDOW_LABELS + ("NORMAL",):
        seg = mr[mr["window"] == label]
        if seg.empty:
            continue
        for h in horizons_days:
            rows.append({
                "window": label, "horizon_days": h,
                "n": int(seg[f"ric_{h}d"].notna().sum()),
                "n_top10": int(seg[f"spread_{h}d"].notna().sum()),
                "mean_pool": float(seg["n_pool"].mean()),
                "top_fwd": float(seg[f"top_{h}d"].mean()),
                "bot_fwd": float(seg[f"bot_{h}d"].mean()),
                "spread": float(seg[f"spread_{h}d"].mean()),
                "tercile_top_fwd": float(seg[f"t3_{h}d"].mean()),
                "tercile_bot_fwd": float(seg[f"b3_{h}d"].mean()),
                "tercile_spread": float(seg[f"s3_{h}d"].mean()),
                "rank_ic": float(seg[f"ric_{h}d"].mean()),
                "rank_ic_pos_share": float((seg[f"ric_{h}d"] > 0).mean()),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 工作流 5：横截面轮动
# ---------------------------------------------------------------------------
def rotation_metrics(result: BacktestResult, panels: Panels,
                     windows: Sequence = WINDOWS, horizon_bars: int = 24,
                     corr_window_bars: int = 168) -> pd.DataFrame:
    """逐调仓点：Top10/Bottom10 重叠、rank turnover、离散度、平均两两相关。

    * `long_overlap` / `short_overlap`：与**上一期** Top10/Bottom10 的重叠率
      —— 直接读 `result.rebalances[i]["long"]/["short"]`，不是重算。
    * `rank_turnover`：相邻两期 **全池 score 秩**的 1 − Spearman（0 = 完全不动）。
    * `dispersion`：池内**未来 1 天收益**的横截面标准差。
    * `avg_pair_corr`：池内标的**过去 7 天小时收益**的平均两两相关。
    """
    cfg = result.cfg
    px = exec_price(panels, cfg, "exec")
    T = px.shape[0]
    dec = np.searchsorted(panels.index, result.reb_ts)
    ret = px[1:] / px[:-1] - 1.0
    ret = np.vstack([np.full((1, px.shape[1]), np.nan), ret])
    prev_long: Optional[set] = None
    prev_short: Optional[set] = None
    prev_rank: Optional[np.ndarray] = None
    rows: List[dict] = []
    for i, d in enumerate(dec):
        a = int(d) + 1
        rb = result.rebalances[i]
        L, S = set(rb["long"]), set(rb["short"])
        row: Dict[str, object] = {
            "ts": result.reb_ts[i], "n_universe": int(rb["n_universe"]),
            "n_long": len(L), "n_short": len(S),
            # 空的一侧 ⇒ 重叠率无定义（不是 0）。极早的调仓点会出现空侧。
            "long_overlap": (len(L & prev_long) / len(L)
                             if (prev_long is not None and len(L)) else np.nan),
            "short_overlap": (len(S & prev_short) / len(S)
                              if (prev_short is not None and len(S)) else np.nan),
            "n_replaced": int(rb.get("n_replaced", 0)),
            "binding_adv": float(rb.get("binding_adv", 0.0)),
        }
        pool = result.mask_matrix[i]
        if a < T and a + horizon_bars < T and int(pool.sum()) >= 2:
            fwd = px[a + horizon_bars] / px[a] - 1.0
            fwd = np.where(pool, fwd, np.nan)
            row["dispersion"] = float(np.nanstd(fwd))
        # rank turnover on the pooled score
        sc = np.where(pool, result.score_matrix[i].astype("float64"), np.nan)
        r = pd.Series(sc).rank().to_numpy()
        if prev_rank is not None:
            ok = np.isfinite(r) & np.isfinite(prev_rank)
            if int(ok.sum()) >= 8:
                row["rank_turnover"] = 1.0 - _spearman(r[ok], prev_rank[ok])
        prev_rank = r
        # average pairwise correlation of trailing hourly returns, pool only
        j = np.flatnonzero(pool)
        if j.size >= 5:
            lo = max(0, a - corr_window_bars)
            sub = ret[lo:a][:, j]
            keep = np.isfinite(sub).all(axis=0)
            if int(keep.sum()) >= 5:
                cm = np.corrcoef(sub[:, keep], rowvar=False)
                iu = np.triu_indices_from(cm, k=1)
                vals = cm[iu]
                vals = vals[np.isfinite(vals)]
                if vals.size:
                    row["avg_pair_corr"] = float(vals.mean())
        rows.append(row)
        prev_long, prev_short = L, S
    df = pd.DataFrame(rows)
    df["window"] = ""
    for label, s, e, _dd in windows:
        df.loc[window_mask(pd.DatetimeIndex(df["ts"]), s, e), "window"] = label
    df.loc[df["window"] == "", "window"] = "NORMAL"
    return df


def rotation_table(rot: pd.DataFrame, windows: Sequence = WINDOWS) -> pd.DataFrame:
    rows: List[dict] = []
    for label in _window_labels(windows):
        seg = rot[rot["window"] == label]
        if seg.empty:
            continue
        rows.append({
            "window": label, "n": int(len(seg)),
            "universe": float(seg["n_universe"].mean()),
            "long_overlap": float(seg["long_overlap"].mean()),
            "short_overlap": float(seg["short_overlap"].mean()),
            "rank_turnover": float(seg["rank_turnover"].mean()),
            "n_replaced": float(seg["n_replaced"].mean()),
            "dispersion": float(seg["dispersion"].mean()),
            "avg_pair_corr": float(seg["avg_pair_corr"].mean()),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 工作流 6：regime 分类（全部只用**滚动历史**，阈值取全样本分位，不按窗口调）
# ---------------------------------------------------------------------------
REGIME_ORDER = ("MomentumReversal", "Rotation", "HighVol", "LowVol", "Transition",
                "StrongTrend", "WeakTrend", "Range", "Normal")


def regime_frame(result: BacktestResult, panels: Panels, rot: pd.DataFrame,
                 ic_mom: pd.Series, ic_col: str = "mom_ic_5d_trailing") -> pd.DataFrame:
    """调仓网格上的 regime 维度。每一列只用**该时刻及之前**的数据。

    `ic_col` 决定动量反转标签用哪一列：默认 `mom_ic_5d_trailing`（滞后 ⇒ 可交易口径）。
    传 `mom_ic_5d_contemp` 会得到**同期**标签 —— 那是「拿结果解释结果」，只能用作对照，
    量出这个陷阱有多大。
    """
    if ic_col not in ("mom_ic_5d_trailing", "mom_ic_5d_contemp"):
        raise ValueError(f"unknown ic_col: {ic_col!r}")
    cfg = result.cfg
    bpd = cfg.bars_per_day
    btc = panels.close[cfg.benchmark_inst].astype("float64")
    lr = np.log(btc.where(btc > 0)).diff()

    def _roll(s: pd.Series, days: float, fn: str) -> pd.Series:
        w = max(5, int(round(days * bpd)))
        return getattr(s.rolling(w, min_periods=max(3, w // 4)), fn)()

    def _mom(days: float) -> pd.Series:
        return btc / btc.shift(max(2, int(round(days * bpd)))) - 1.0

    ma20, ma60 = _roll(btc, 20, "mean"), _roll(btc, 60, "mean")
    vol_ann = _roll(lr, 10, "std") * np.sqrt(cfg.bars_per_year)
    vol_7d = _roll(lr, 7, "std") * np.sqrt(cfg.bars_per_year)

    out = pd.DataFrame(index=result.reb_ts)
    out["btc_ret20"] = _mom(20).reindex(result.reb_ts)
    out["btc_ret60"] = _mom(60).reindex(result.reb_ts)
    out["btc_px_ma20"] = (btc / ma20 - 1.0).reindex(result.reb_ts)
    out["btc_px_ma60"] = (btc / ma60 - 1.0).reindex(result.reb_ts)
    out["btc_vol_ann"] = vol_ann.reindex(result.reb_ts)
    out["btc_vol_7d"] = vol_7d.reindex(result.reb_ts)
    r = rot.set_index("ts")
    out["xs_dispersion"] = r["dispersion"].reindex(result.reb_ts)
    out["xs_corr"] = r["avg_pair_corr"].reindex(result.reb_ts)
    out["xs_rank_turnover"] = r["rank_turnover"].reindex(result.reb_ts)
    out["mom_ic"] = ic_mom.reindex(result.reb_ts)
    # ⚠️ 必须**只用已实现的过去**：`ic_mom[t]` 是用第 t 期的未来收益算的，与第 t 期的
    # 损益**同期**。直接滚动会得到「拿结果解释结果」的假强关系。`.shift(1)` 之后
    # 标签只看 t-5..t-1 期，是一个**可交易口径**的状态量。
    out["mom_ic_5d_trailing"] = out["mom_ic"].rolling(5, min_periods=2).mean().shift(1)
    out["mom_ic_5d_contemp"] = out["mom_ic"].rolling(5, min_periods=2).mean()

    # ---- tags：阈值 = 全样本分位（**与 5 个窗口无关**，不按窗口调） ----
    def q(c: str, p: float) -> float:
        v = out[c].to_numpy(dtype="float64")
        v = v[np.isfinite(v)]
        return float(np.quantile(v, p)) if v.size else np.nan

    v80, v20 = q("btc_vol_ann", 0.80), q("btc_vol_ann", 0.20)
    t80, d40 = q("xs_rank_turnover", 0.80), q("xs_dispersion", 0.40)
    ic20 = q(ic_col, 0.20)
    ic_cut = min(0.0, ic20 if np.isfinite(ic20) else 0.0)

    out["tag_mom_rev"] = out[ic_col] < ic_cut
    out["tag_highvol"] = out["btc_vol_ann"] >= v80
    out["tag_lowvol"] = out["btc_vol_ann"] <= v20
    out["tag_rotation"] = (out["xs_rank_turnover"] >= t80) & (out["xs_dispersion"] <= d40)
    tr = pd.Series(np.select(
        [out["btc_ret60"].abs() >= 0.15, out["btc_ret60"].abs() >= 0.05],
        ["StrongTrend", "WeakTrend"], default="Range"), index=out.index)
    tr = tr.where(out["btc_ret60"].notna(), "Range")
    out["tag_trend"] = tr
    out["tag_transition"] = (tr != tr.shift(5)).fillna(False)

    # 只做**一个**主标签（用户要求「每天一个」），优先级显式写死，不按结果挑。
    pri = [
        (out["tag_mom_rev"], "MomentumReversal"),
        (out["tag_rotation"], "Rotation"),
        (out["tag_highvol"], "HighVol"),
        (out["tag_lowvol"], "LowVol"),
        (out["tag_transition"], "Transition"),
        (tr == "StrongTrend", "StrongTrend"),
        (tr == "WeakTrend", "WeakTrend"),
        (tr == "Range", "Range"),
    ]
    label = pd.Series("Normal", index=out.index, dtype=object)
    for cond, name in reversed(pri):
        label[cond.fillna(False).to_numpy()] = name
    out["regime"] = label
    return out


def regime_table(result: BacktestResult, regime: pd.DataFrame) -> pd.DataFrame:
    """逐 regime 的 CAGR / Sharpe / MaxDD / 日均 / 胜率 / 两腿贡献。

    用**调仓周期收益**（`per_period_returns`）而不是小时收益 —— 一个调仓周期 = 一天。
    """
    from .metrics import per_period_returns
    pr = per_period_returns(result.bars, result.reb_ts)
    bars = result.bars
    bounds = sorted(set(int(np.searchsorted(bars.index, t)) for t in result.reb_ts)
                    | {len(bars)})
    g = regime.reindex(result.reb_ts)
    rows: List[dict] = []
    for name in REGIME_ORDER:
        idx = np.flatnonzero((g["regime"] == name).to_numpy())
        if idx.size < 5:
            continue
        r = pr.iloc[idx].dropna()
        pos = np.concatenate([np.arange(bounds[i], bounds[i + 1]) for i in idx])
        sub = bars.iloc[pos]
        ann_ret = float(r.mean() * 365.0)
        ann_vol = float(r.std(ddof=1) * np.sqrt(365.0)) if len(r) > 2 else np.nan
        eq = (1 + r).cumprod()
        rows.append({
            "regime": name, "n_days": int(len(r)), "n_bars": int(len(sub)),
            "share_of_time": float(len(r) / max(len(pr), 1)),
            "mean_daily": float(r.mean()), "ann_ret": ann_ret, "ann_vol": ann_vol,
            "sharpe": ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan,
            "win_rate": float((r > 0).mean()),
            "max_dd": float((eq / eq.cummax() - 1).min()),
            "long_pnl": float(sub["long_ret"].sum()),
            "short_pnl": float(sub["short_ret"].sum()),
            "gross_pnl": float(sub["gross_ret"].sum()),
            "net_pnl": float(sub["net_ret"].sum()),
            "avg_gross_exposure": float(sub["gross_exposure"].mean()),
        })
    return pd.DataFrame(rows).sort_values("share_of_time", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 工作流 7：流动性 / ADV 门槛诊断
# ---------------------------------------------------------------------------
def liquidity_diag(result: BacktestResult, windows: Sequence = WINDOWS) -> pd.DataFrame:
    """逐调仓点：宇宙宽度、流动性水平与离散、实际选中的名字数、换手/ADV 参与率。"""
    adv = result.adv_matrix.astype("float64")
    adv = np.where(result.mask_matrix, adv, np.nan)
    rows: List[dict] = []
    for i, t in enumerate(result.reb_ts):
        rb = result.rebalances[i]
        a = adv[i]
        rows.append({
            "ts": t, "n_universe": int(rb["n_universe"]),
            "n_pool": int(result.mask_matrix[i].sum()),
            "n_long": len(rb["long"]), "n_short": len(rb["short"]),
            "adv_median": float(np.nanmedian(a)) if np.isfinite(a).any() else np.nan,
            "adv_p10": float(np.nanquantile(a, 0.10)) if np.isfinite(a).any() else np.nan,
            "adv_dispersion": (float(np.nanstd(np.log(a[np.isfinite(a) & (a > 0)])))
                               if (np.isfinite(a) & (a > 0)).sum() > 2 else np.nan),
            "binding_adv": float(rb.get("binding_adv", 0.0)),
            "long_gross": float(rb.get("long_gross", np.nan)),
            "short_gross": float(rb.get("short_gross", np.nan)),
            "scale": float(rb.get("scale", np.nan)),
            "dd_scale": float(rb.get("dd_scale", np.nan)),
            "btc_vol_scale": float(rb.get("btc_vol_scale", np.nan)),
        })
    df = pd.DataFrame(rows)
    bars = result.bars
    pid = np.searchsorted(result.reb_ts, bars.index, side="right") - 1
    mp = bars["max_participation"].groupby(pd.Series(pid, index=bars.index)).max()
    df["max_participation"] = mp.reindex(range(len(df))).to_numpy()
    df["window"] = ""
    for label, s, e, _dd in windows:
        df.loc[window_mask(pd.DatetimeIndex(df["ts"]), s, e), "window"] = label
    df.loc[df["window"] == "", "window"] = "NORMAL"
    return df


def liquidity_table(liq: pd.DataFrame, windows: Sequence = WINDOWS) -> pd.DataFrame:
    rows: List[dict] = []
    for label in _window_labels(windows):
        seg = liq[liq["window"] == label]
        if seg.empty:
            continue
        rows.append({
            "window": label, "n": int(len(seg)),
            "universe": float(seg["n_universe"].mean()),
            "universe_p10": float(seg["n_universe"].quantile(0.10)),
            "pool": float(seg["n_pool"].mean()),
            "n_long": float(seg["n_long"].mean()), "n_short": float(seg["n_short"].mean()),
            "adv_median_usd": float(seg["adv_median"].mean()),
            "adv_dispersion": float(seg["adv_dispersion"].mean()),
            "binding_adv_share": float((seg["binding_adv"] > 0).mean()),
            "max_participation": float(seg["max_participation"].max()),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 工作流 9（Q43）：「空腿被反弹挤压」到底成不成立
# ---------------------------------------------------------------------------
# 报告 §十-1 把这条留成了「证据不足 / 无法确认」。这里做的是**直接度量**：
# 回撤期里**被选中的空头**是不是普遍在涨、是不是比同池的名字涨得多。
# 这是「挤压」的**必要条件**（不是充分条件）—— 若空腿并不比池子涨得多，
# 那就没有「挤压」，无论事后看 K 线多像一次逼空。
_SB_QUANTILES = (0.75, 0.90, 0.95)


def label_stated(index: pd.DatetimeIndex,
                 windows: Sequence = WINDOWS) -> pd.Series:
    """按用户窗口给调仓点打标；落在任何窗口之外的标 `"NORMAL"`。"""
    w = pd.Series("NORMAL", index=index, dtype=object)
    for label, s, e, _dd in windows:
        w[window_mask(index, s, e)] = label
    return w


def _bar_simple_returns(px: np.ndarray) -> np.ndarray:
    """逐 bar 简单收益，与引擎的 `ret_exec[t] = px[t+1]/px[t] − 1` 同形。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        g = px[1:] / px[:-1] - 1.0
    return np.where(np.isfinite(g), g, np.nan)


def _period_sum(g: np.ndarray, a: int, b: int) -> Tuple[np.ndarray, np.ndarray]:
    """把 bar 级收益 `g[a:b]` **按标的相加**（不是复合 —— 引擎也是相加）。

    返回 `(R, ok)`：`ok[k]` 表示该标的在这个持有期内价格**全程**有定义；
    有缺口时 `R[k] = nan`（不假装它涨了 0%）。
    """
    n = g.shape[1]
    win = g[a:b]
    if win.shape[0] == 0:
        return np.full(n, np.nan), np.zeros(n, dtype=bool)
    finite = np.isfinite(win)
    ok = finite.all(axis=0)
    R = np.where(ok, np.nansum(np.where(finite, win, 0.0), axis=0), np.nan)
    return R.astype("float64"), ok


def short_book_frame(result: BacktestResult, panels: Panels,
                     windows: Sequence = WINDOWS, big_move: float = 0.05,
                     quantiles: Sequence[float] = _SB_QUANTILES) -> pd.DataFrame:
    """逐调仓点：**被做空标的**在该持有期的收益分布 + 同池 / 多头对照。

    收益一律用**引擎口径**（`Σ_t (px[t+1]/px[t] − 1)`，相加不复合），因此
    `short_pnl == −Σ_k w_k·R_k` 与 `result.name_gross` 一致（测试钉住）。

    关键列：
    * `ew_*` —— 被空标的的**等权**分布（每名一票）：中位、上分位、`frac_big`（涨超 `big_move` 的占比）；
    * `mw_ret` —— 空腿的**市值加权损益**（`short_pnl / short_gross`，**正 = 空腿在亏**）；
      `mw_ret_names = −mw_ret` = 被空标的的市值加权**收益**（与 `pool_mean` 同号，可直接比）；
    * `pool_*` —— 同期**池内全部合格标的**的同一统计量（基准）；
    * `excess_ew` = `excess_select` —— 空腿**等权**收益减池子（**纯粹的选股效应**）；
    * `excess_weight` —— 同一批名字里「市值加权 vs 等权」（**纯粹的仓位加权效应**）；
    * `excess_mw` = `excess_select + excess_weight`（逐点恒等）；
    * `excess_long` —— 多头那侧的同类超额。
      **判「挤压」只看 `excess_select`**：它和 `pool_mean` 同为等权口径。
      `excess_mw` 混了加权效应，单看它会把「最大的空头仓位涨得最多」误读成「被挤压」。

    ⚠️ 每一行都会输出，**不可用的情况标 `available=False` + `reason`，绝不丢行**。
    """
    cfg = result.cfg
    px = exec_price(panels, cfg, "exec")
    T = int(px.shape[0])
    g = _bar_simple_returns(px)
    dec = np.searchsorted(panels.index, result.reb_ts)
    t = dec + 1
    D = len(result.reb_ts)
    Wm = np.asarray(result.weight_matrix, dtype="float64")
    Gm = np.asarray(result.name_gross, dtype="float64")
    qs = tuple(float(q) for q in quantiles)

    base: Dict[str, object] = {
        "available": False, "reason": "",
        "n_short": 0, "n_long": 0, "n_short_priced": 0, "n_pool_priced": 0,
        "short_gross": np.nan, "short_pnl": np.nan, "long_pnl": np.nan,
        "short_pnl_px": np.nan, "long_pnl_px": np.nan,
        "ew_mean": np.nan, "ew_median": np.nan, "frac_big": np.nan, "mw_ret": np.nan,
        "mw_ret_names": np.nan,
        "long_ew_median": np.nan, "long_frac_big": np.nan, "long_mw_ret": np.nan,
        "pool_mean": np.nan, "pool_median": np.nan, "pool_frac_big": np.nan,
        "excess_ew": np.nan, "excess_mw": np.nan, "excess_long": np.nan,
        "excess_select": np.nan, "excess_weight": np.nan,
    }
    for q in qs:
        base[f"ew_q{int(round(q * 100))}"] = np.nan

    rows: List[dict] = []
    for i in range(D):
        a = int(t[i])
        b = int(t[i + 1]) if i + 1 < D else T
        row: Dict[str, object] = dict(base)
        row.update({"ts": result.reb_ts[i], "i": i, "a": a, "b": b,
                    "n_pool": int(result.mask_matrix[i].sum())})
        if not (0 <= a < T) or b <= a:
            row["reason"] = "no_forward_window"
            rows.append(row)
            continue
        R, _ok = _period_sum(g, a, b)
        held = Wm[i]
        sh, lo = held < 0, held > 0
        w_sh, w_lo = -held[sh], held[lo]
        row["n_short"], row["n_long"] = int(sh.sum()), int(lo.sum())
        if not sh.any():
            row["reason"] = "no_shorts"
            rows.append(row)
            continue
        row["short_gross"] = float(w_sh.sum())
        row["short_pnl"] = float(Gm[i][sh].sum())
        row["long_pnl"] = float(Gm[i][lo].sum()) if lo.any() else 0.0
        Rs = R[sh]
        fin = np.isfinite(Rs)
        row["n_short_priced"] = int(fin.sum())
        if row["n_short_priced"] == 0:
            row["reason"] = "no_price"
            rows.append(row)
            continue
        r = Rs[fin]
        # ⚠️ **独立算一遍**：用价格推出的 `R` 复算空/多腿损益。`short_pnl` 那一列来自
        # 引擎的 `name_gross`，所以**改价格口径不会让它变** —— 没有这一列，
        # 前向起点错一根 bar 也测不出来（第一版就漏了这个变异）。
        if row["n_short_priced"] == row["n_short"]:
            row["short_pnl_px"] = float(-(w_sh * Rs).sum())
        if lo.any():
            Rl_all = R[lo]
            if np.isfinite(Rl_all).all():
                row["long_pnl_px"] = float((w_lo * Rl_all).sum())
        row["ew_mean"] = float(np.mean(r))
        row["ew_median"] = float(np.median(r))
        for q in qs:
            row[f"ew_q{int(round(q * 100))}"] = float(np.quantile(r, q))
        row["frac_big"] = float(np.mean(r > big_move))
        if row["short_gross"] > 0:
            row["mw_ret"] = float(row["short_pnl"]) / float(row["short_gross"])
            # ⚠️ 符号陷阱：`mw_ret` 是**空腿的损益**（名字涨 ⇒ 负）。要与池子比，
            # 必须先取负号换成「被空标的的平均收益」。直接写 `mw_ret − pool_mean`
            # 会把「市场在涨」当成「被挤压」—— 牛市里它**恒为负**，看起来永远「已否定」。
            row["mw_ret_names"] = -float(row["mw_ret"])
        if lo.any():
            Rl = R[lo]
            fl = np.isfinite(Rl)
            if fl.any():
                row["long_ew_median"] = float(np.median(Rl[fl]))
                row["long_frac_big"] = float(np.mean(Rl[fl] > big_move))
            gs = float(w_lo.sum())
            if gs > 0:
                row["long_mw_ret"] = float(row["long_pnl"]) / gs
        pool = np.asarray(result.mask_matrix[i], dtype=bool)
        Rp = R[pool]
        fp = np.isfinite(Rp)
        row["n_pool_priced"] = int(fp.sum())
        if fp.any():
            row["pool_mean"] = float(np.mean(Rp[fp]))
            row["pool_median"] = float(np.median(Rp[fp]))
            row["pool_frac_big"] = float(np.mean(Rp[fp] > big_move))
            row["excess_ew"] = row["ew_mean"] - row["pool_mean"]
            # 恒等式（逐点、精确）：`mw_ret_names = pool_mean + 选股 + 加权`
            #   `excess_select` = 等权口径下「被空标的 vs 池子」—— **纯粹的选股效应**
            #   `excess_weight` = 同一批名字里「市值加权 vs 等权」—— **纯粹的仓位加权效应**
            # 两个口径**不能混**：`mw_ret_names − pool_mean` 同时含这两项，
            # 单看它会以为「被挤压」，其实可能只是最大的那几笔空头涨得最多。
            row["excess_select"] = row["excess_ew"]
            if np.isfinite(row["mw_ret_names"]):
                row["excess_mw"] = row["mw_ret_names"] - row["pool_mean"]
                row["excess_weight"] = row["mw_ret_names"] - row["ew_mean"]
            if np.isfinite(row["long_mw_ret"]):
                row["excess_long"] = row["long_mw_ret"] - row["pool_mean"]
        row["available"] = True
        rows.append(row)

    df = pd.DataFrame(rows)
    # ⚠️ 必须 `.to_numpy()`：`label_stated` 的索引是 ts，而 `df` 是 RangeIndex ——
    # 直接赋值会**按索引对齐**，结果整列变成 NaN（而且不报错）。
    df["window"] = label_stated(pd.DatetimeIndex(df["ts"]), windows).to_numpy()
    return df


def short_book_table(sb: pd.DataFrame, windows: Sequence = WINDOWS,
                     key: str = "window", rest_label: str = "NORMAL",
                     perm: bool = True, n_perm: int = 2000) -> pd.DataFrame:
    """按窗口汇总空腿挤压度量，并与**正常期**做 Welch + 块置换。

    ⚠️ **每个声明的窗口都无条件出一行**，哪怕 0 个可用点 —— 这是 Q42 的第三次教训：
    把「算不出来」的窗口 `continue` 掉，最该看的那一行就消失了，而且看不出来。
    `n_points` = 可用点数，`n_unavailable` = 该窗口里不可用的点数（两者必须相加 = 窗口总点数）。

    还输出 `short_pnl` 的**归因分解**：`short_pnl_market`（做空一个上涨的市场）
    + `short_pnl_excess`（被空标的比池子涨得多）。两项之和恒等于 `short_pnl`。
    """
    labels = tuple(w[0] for w in windows) + (str(rest_label),)
    rest = sb[(sb[key] == rest_label) & sb["available"]]
    rows: List[dict] = []
    for label in labels:
        inw = sb[sb[key] == label]
        seg = inw[inw["available"]]
        mean = (lambda c: float(seg[c].mean()) if len(seg) else np.nan)
        row: Dict[str, object] = {
            "window": label,
            "n_points": int(len(seg)),
            "n_unavailable": int(len(inw) - len(seg)),
            "n_short": mean("n_short"),
            "short_gross": mean("short_gross"),
            "ew_mean": mean("ew_mean"), "ew_median": mean("ew_median"),
            "ew_q90": mean("ew_q90"), "frac_big": mean("frac_big"),
            "mw_ret": mean("mw_ret"), "mw_ret_names": mean("mw_ret_names"),
            "long_mw_ret": mean("long_mw_ret"),
            "pool_mean": mean("pool_mean"), "pool_median": mean("pool_median"),
            "pool_frac_big": mean("pool_frac_big"),
            "excess_ew": mean("excess_ew"), "excess_mw": mean("excess_mw"),
            "excess_select": mean("excess_select"), "excess_weight": mean("excess_weight"),
            "excess_long": mean("excess_long"),
            "short_pnl": float(seg["short_pnl"].sum()) if len(seg) else np.nan,
            "long_pnl": float(seg["long_pnl"].sum()) if len(seg) else np.nan,
            "short_pnl_market": np.nan, "short_pnl_select": np.nan,
            "short_pnl_weight": np.nan,
        }
        if len(seg):
            # 空腿亏损的**归因分解**（逐点恒等，相加精确等于 `short_pnl`）：
            #   `short_pnl_i = −gross_i·mw_ret_names_i`
            #              = −gross_i·pool_mean_i        （做空一个上涨的市场）
            #              − gross_i·excess_select_i     （选到的名字比池子强）
            #              − gross_i·excess_weight_i     （最大的空头仓位涨得最多）
            # ⚠️ 「市场」项会偏小：`short_gross` 与市场收益**负相关**（风控在上涨时缩仓），
            # 所以它不等于「市场涨了多少」，只等于「按本账本的实际毛敞口路径去做空池子」。
            # 要读「亏损主要是市场还是别的」，用**均值层面**的分解（表里那三列 excess_*）。
            sg = seg["short_gross"].to_numpy(dtype="float64")
            pm = seg["pool_mean"].to_numpy(dtype="float64")
            sel = seg["excess_select"].to_numpy(dtype="float64")
            wgt = seg["excess_weight"].to_numpy(dtype="float64")
            ok = (np.isfinite(sg) & np.isfinite(pm)
                  & np.isfinite(sel) & np.isfinite(wgt))
            row["short_pnl_market"] = float(-(sg[ok] * pm[ok]).sum())
            row["short_pnl_select"] = float(-(sg[ok] * sel[ok]).sum())
            row["short_pnl_weight"] = float(-(sg[ok] * wgt[ok]).sum())
        if perm and label != str(rest_label) and len(seg) >= 3 and len(rest) >= 3:
            # ⚠️ 检验对象是 **`excess_select`（等权选股效应）**，不是 `excess_mw`：
            # 后者混了仓位加权，会把「最大那几笔空头涨得最多」当成「被挤压」。
            d = welch(seg["excess_select"], rest["excess_select"])
            d.update(block_perm(seg["excess_select"], rest["excess_select"],
                                n_perm=n_perm))
            row.update(d)
        rows.append(row)
    return pd.DataFrame(rows)


def short_contributors(sb: pd.DataFrame, result: BacktestResult,
                       windows: Sequence = WINDOWS, key: str = "window",
                       rest_label: str = "NORMAL", top: int = 8) -> pd.DataFrame:
    """逐窗口：对**空腿亏损**贡献最大的标的（按 `name_gross` 累计）。

    `mean_ret` = 该标的被做空期间的平均收益（**正 = 它在涨 = 空腿在亏**）；
    `share_of_loss` = 它占该窗口空腿**净亏损**的份额；`top_share` = 前 `top` 名合计份额
    ⇒ 直接回答「亏损是**少数名字**还是**普遍**」。

    ⚠️ 空腿在该窗口**赚钱**时（`total_short_pnl >= 0`）份额没有意义 ⇒ 置 NaN，
    并用 `leg_is_loss` 标出来。`top_share > 1` 是**合法**的：它意味着其余名字在空腿上
    **是赚钱的**，净亏损全由少数几个名字造成。
    """
    Gm = np.asarray(result.name_gross, dtype="float64")
    Wm = np.asarray(result.weight_matrix, dtype="float64")
    insts = list(result.insts)
    labels = tuple(w[0] for w in windows) + (str(rest_label),)
    out: List[dict] = []
    for label in labels:
        sel = sb.loc[sb[key] == label, "i"].to_numpy(dtype=int)
        if sel.size == 0:
            out.append({"window": label, "rank": np.nan, "inst": "", "pnl": np.nan,
                        "n_periods": 0, "mean_ret": np.nan, "share_of_loss": np.nan,
                        "top_share": np.nan, "total_short_pnl": np.nan, "n_names": 0,
                        "leg_is_loss": False})
            continue
        sh = Wm[sel] < 0
        pnl = np.where(sh, Gm[sel], 0.0).sum(axis=0)
        cnt = sh.sum(axis=0)
        wt = np.where(sh, Wm[sel], 0.0).sum(axis=0)
        total = float(pnl.sum())
        n_names = int((cnt > 0).sum())
        is_loss = bool(total < 0)
        order = np.argsort(pnl)                       # 最负（最亏）在前
        top_share = (float(pnl[order[:top]].sum() / total) if is_loss else np.nan)
        for r in range(min(int(top), n_names)):
            k = int(order[r])
            out.append({
                "window": label, "rank": r + 1, "inst": insts[k],
                "pnl": float(pnl[k]), "n_periods": int(cnt[k]),
                "mean_ret": float(pnl[k] / wt[k]) if wt[k] != 0 else np.nan,
                "share_of_loss": (float(pnl[k] / total) if is_loss else np.nan),
                "top_share": top_share, "total_short_pnl": total, "n_names": n_names,
                "leg_is_loss": is_loss,
            })
    return pd.DataFrame(out)


def squeeze_verdict(row: pd.Series, alpha: float = 0.05) -> str:
    """「空腿被崩跌式反弹挤压」的三态判定 —— **判据显式、可证伪**。

    判的是**选股效应**（`excess_select` = 空腿等权收益 − 池子等权收益），因为它与
    `pool_mean` 同为等权口径。`excess_mw` 混了仓位加权，不能用来判「挤压」。

    * `已确认`：空腿显著跑赢池子（`excess_select > 0` 且置换 `p < alpha`），
      **且**被空标的「大涨（> +5%）」的占比也高于池子。
    * `已否定`：`excess_select <= 0` —— 被空标的并不比池子涨得多，没有「挤压」可言。
    * `证据不足`：其余（方向为正但统计上站不住）。
    """
    e = row.get("excess_select", np.nan)
    p = row.get("perm_p", np.nan)
    fb, pf = row.get("frac_big", np.nan), row.get("pool_frac_big", np.nan)
    if not np.isfinite(e):
        return "证据不足：无法计算"
    if e <= 0:
        return "已否定：被空标的并不比池子涨得多"
    if (np.isfinite(p) and p < alpha and np.isfinite(fb) and np.isfinite(pf)
            and fb > pf):
        return "已确认：空腿显著跑赢池子且大涨占比更高"
    return "证据不足：方向为正但不显著"
