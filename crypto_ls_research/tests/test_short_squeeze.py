"""Q43 空腿挤压度量的测试（`analysis/failure_attribution.py` 的工作流 9）。

三条纪律（与 Q41/Q42 一致）：
* 期望值**从 fixture 派生**，不写死；
* 新断言必须**变异测试**（见 `.workbuddy-ai/memory/2026-09-30.md` 的记录）；
* **必须有一条正对照** —— 证明这个度量在「真的发生挤压」时能检出，否则
  「已否定」可能只是度量本身坏掉了。

需要 `baseline.pkl` 的用例在文件缺失时 skip；合成用例（正/负对照）不需要。
"""
from __future__ import annotations

import os
import pickle
import types
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")

_CACHE: Dict[str, object] = {}


def _baseline():
    if "res" not in _CACHE:
        if not os.path.exists(PKL):
            pytest.skip(f"缺少 {PKL}")
        with open(PKL, "rb") as f:
            _CACHE["res"] = pickle.load(f)["result"]
    return _CACHE["res"]


def _panels(res):
    if "pan" not in _CACHE:
        from crypto_ls_research.data.store import Panels, load_panels
        p = load_panels("1h", "2021-01-01", "2026-09-26", insts=list(res.insts))
        g = res.bars.index
        _CACHE["pan"] = Panels(
            open=p.open.reindex(g), high=p.high.reindex(g), low=p.low.reindex(g),
            close=p.close.reindex(g), vol=p.vol.reindex(g), vol_ccy=p.vol_ccy.reindex(g),
            amount=p.amount.reindex(g), funding=p.funding.reindex(g), list_dt=p.list_dt)
    return _CACHE["pan"]


def _sb():
    if "sb" not in _CACHE:
        from crypto_ls_research.analysis import failure_attribution as fa
        _CACHE["sb"] = fa.short_book_frame(_baseline(), _panels(_baseline()))
    return _CACHE["sb"]


# ---------------------------------------------------------------------------
# 合成账本：价格按（分段的）常数 μ 演化，`name_gross` 由价格推出 ⇒ 自洽
# ---------------------------------------------------------------------------
def _synthetic(mu_a: Sequence[float], mu_b: Sequence[float], held: Sequence[float],
               period: int = 24, n_reb: int = 60, warmup: int = 200,
               switch_at: int = 30):
    """造 `(panels, result-like)`：前 `switch_at` 个调仓点用 `mu_a`，其余用 `mu_b`。

    返回的对象只带 `short_book_frame` 真正读的字段（用 `SimpleNamespace`，避免
    为了一个测试去跑整条回测栈）。
    """
    from crypto_ls_research.data.store import Panels

    n = len(mu_a)
    insts = [f"I{k}" for k in range(n)]
    dec = warmup + np.arange(n_reb) * period
    T = int(dec[-1]) + 1 + period         # 让最后一个持有期也是满的 `period` 根
    idx = pd.date_range("2021-01-01", periods=T, freq="1h", tz="UTC")

    # 分段常数 μ：切换点之后换成 mu_b
    # ⚠️ 调仓点 i 用的是 `g[dec[i]+1 : dec[i]+1+period]`，即 bar `dec[i]+1 .. dec[i]+period`
    # 的收益。要让「前 switch_at 个调仓点全在 A 区、其余全在 B 区」，切换点必须落在
    # `dec[switch_at]+1`（否则最后一个 A 区调仓点会吃到 B 区的收益）。
    cut = int(dec[switch_at]) + 1
    mu = np.zeros((T, n))
    for k in range(n):
        mu[:cut, k] = mu_a[k]
        mu[cut:, k] = mu_b[k]
    px = np.empty((T, n))
    px[0] = 100.0
    for t in range(1, T):
        px[t] = px[t - 1] * (1.0 + mu[t - 1])

    def frame(a: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(a, index=idx, columns=insts)

    pan = Panels(open=frame(px), high=frame(px), low=frame(px), close=frame(px),
                 vol=frame(np.ones_like(px)), vol_ccy=frame(np.ones_like(px)),
                 amount=frame(np.ones_like(px)), funding=frame(np.zeros_like(px)),
                 list_dt=pd.Series([idx[0]] * n, index=insts))

    g = px[1:] / px[:-1] - 1.0
    W = np.tile(np.asarray(held, dtype="float64"), (n_reb, 1))
    G = np.zeros((n_reb, n))
    for i in range(n_reb):
        a = int(dec[i]) + 1
        G[i] = W[i] * g[a:a + period].sum(axis=0)

    cfg = types.SimpleNamespace(execution=types.SimpleNamespace(exec_price="next_open"))
    res = types.SimpleNamespace(
        insts=insts, cfg=cfg, reb_ts=pd.DatetimeIndex(idx[dec]),
        weight_matrix=W, name_gross=G,
        mask_matrix=np.ones((n_reb, n), dtype=bool),
        bars=pd.DataFrame({"net_ret": np.zeros(T)}, index=idx))
    return pan, res


# ---------------------------------------------------------------------------
# 口径：必须与引擎自己的账逐期吻合
# ---------------------------------------------------------------------------
def test_period_slicing_reconciles_with_the_engine():
    """`short_pnl` / `long_pnl` 必须等于引擎 `bars['short_ret']/['long_ret']` 的逐期和。

    口径错一位（例如前向从 `d` 而不是 `d+1` 起算、或把区间写成 `[t_i, t_{i+1}]`），
    这里就会差出量级 —— 这是价格口径唯一硬证据。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    sb = _sb()
    b = res.bars
    dec = np.searchsorted(b.index, res.reb_ts)
    t = dec + 1
    for col, engine in (("short_pnl", "short_ret"), ("long_pnl", "long_ret")):
        e = b[engine].to_numpy()
        d = []
        for i in range(len(res.reb_ts)):
            a = int(t[i])
            bb = int(t[i + 1]) if i + 1 < len(res.reb_ts) else len(b)
            d.append(float(e[a:bb].sum()) - float(sb.loc[i, col]))
        assert float(np.nanmax(np.abs(d))) < 1e-6, f"{col} 与引擎 {engine} 对不上"


def test_price_derived_pnl_matches_the_engine_name_gross():
    """**价格口径**的唯一硬证据：用价格推出的逐期损益 == 引擎的 `name_gross`。

    ⚠️ 上面那条对账只钉住了**区间分组**（`t_i → t_{i+1}`），因为 `short_pnl` 那一列
    本身就来自 `result.name_gross`；**把前向起点挪一根 bar 它不会变**。
    这一条用 `short_pnl_px`（由价格独立算出）来钉，两者必须逐期相等。
    """
    sb = _sb()
    ok = np.isfinite(sb["short_pnl_px"]) & np.isfinite(sb["short_pnl"])
    assert ok.sum() > 1000
    assert float((sb.loc[ok, "short_pnl"] - sb.loc[ok, "short_pnl_px"]).abs().max()) < 1e-6
    ok2 = np.isfinite(sb["long_pnl_px"]) & np.isfinite(sb["long_pnl"])
    assert ok2.sum() > 1000
    assert float((sb.loc[ok2, "long_pnl"] - sb.loc[ok2, "long_pnl_px"]).abs().max()) < 1e-6


def test_money_weighted_return_is_minus_pnl_over_gross():
    """`mw_ret = short_pnl / short_gross`，且 `mw_ret_names = −mw_ret`（符号约定）。"""
    sb = _sb()
    ok = sb["available"] & (sb["short_gross"] > 0)
    assert ok.sum() > 1000
    assert np.allclose(sb.loc[ok, "mw_ret"],
                       sb.loc[ok, "short_pnl"] / sb.loc[ok, "short_gross"])
    assert np.allclose(sb.loc[ok, "mw_ret_names"], -sb.loc[ok, "mw_ret"])


def test_excess_decomposition_is_an_exact_identity():
    """`mw_ret_names = pool_mean + excess_select + excess_weight`（逐点、精确）。

    这是**整件事的关键**：`excess_mw`（市值加权减池子）混了「选股」与「仓位加权」
    两件事，直接拿它判「挤压」会把「最大的空头仓位涨得最多」误读成「被挤压」。
    """
    sb = _sb()
    ok = (sb["available"] & np.isfinite(sb["excess_mw"])
          & np.isfinite(sb["excess_select"]) & np.isfinite(sb["excess_weight"]))
    assert ok.sum() > 1000
    a = sb.loc[ok, "mw_ret_names"]
    b = (sb.loc[ok, "pool_mean"] + sb.loc[ok, "excess_select"]
         + sb.loc[ok, "excess_weight"])
    assert float((a - b).abs().max()) < 1e-12
    # 且 excess_mw 就是这两项之和
    c = sb.loc[ok, "excess_select"] + sb.loc[ok, "excess_weight"]
    assert float((sb.loc[ok, "excess_mw"] - c).abs().max()) < 1e-12


def test_short_loss_decomposition_sums_to_the_short_pnl():
    """NAV 层面三分（市场 / 选股 / 加权）之和必须等于 `short_pnl`。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    sb = _sb()
    ep = fa.episode_windows(res.bars)
    sb_ep = sb.copy()
    sb_ep["window"] = fa.retag_episode(sb_ep, "ts", ep)
    t = fa.short_book_table(sb_ep, windows=ep, rest_label="REST")
    got = t["short_pnl_market"] + t["short_pnl_select"] + t["short_pnl_weight"]
    assert float((t["short_pnl"] - got).abs().max()) < 1e-9


# ---------------------------------------------------------------------------
# 规则 6：每个声明的窗口都要有一行；不可用要显式标出来，不许丢
# ---------------------------------------------------------------------------
def test_every_declared_window_gets_a_row_even_a_ghost_one():
    """回归（Q42 的第三次教训）：窗口**一行都不许少**。

    旧行为下「算不出来」的窗口被 `continue` 掉，于是表看起来是完整的，
    只是最该看的那一行消失了 —— 而且看不出来。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    sb = _sb()
    ghost = (("GHOST", "1990-01-01", "1990-01-02", -0.1),)
    t = fa.short_book_table(sb, windows=ghost)
    assert list(t["window"]) == ["GHOST", "NORMAL"], "幽灵窗口也必须有一行"
    assert int(t.loc[0, "n_points"]) == 0
    only_w1 = (fa.WINDOWS[0],)
    assert list(fa.short_book_table(sb, windows=only_w1)["window"]) == ["W1", "NORMAL"]
    assert list(fa.short_book_table(sb)["window"]) == list(fa.WINDOW_LABELS) + ["NORMAL"]
    # episode 切片：rest 标签是 REST，不是 NORMAL
    ep = fa.episode_windows(_baseline().bars)
    sb_ep = sb.copy()
    sb_ep["window"] = fa.retag_episode(sb_ep, "ts", ep)
    t2 = fa.short_book_table(sb_ep, windows=ep, rest_label="REST")
    assert list(t2["window"]) == list(fa.WINDOW_LABELS) + ["REST"]


def test_window_row_counts_add_up_and_unavailable_is_flagged():
    """`n_points + n_unavailable` 必须等于该窗口的**总**调仓点数；不可用要带原因。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    sb = _sb()
    bad = sb[~sb["available"]]
    assert len(bad) > 0, "样本里应当存在不可用的调仓点（空侧）"
    assert set(bad["reason"]).issubset({"no_shorts", "no_forward_window", "no_price"})
    assert (bad["reason"].astype(str).str.len() > 0).all(), "不可用必须写清原因"
    t = fa.short_book_table(sb).set_index("window")
    for label in fa.WINDOW_LABELS + ("NORMAL",):
        total = int((sb["window"] == label).sum())
        assert int(t.loc[label, "n_points"]) + int(t.loc[label, "n_unavailable"]) == total


# ---------------------------------------------------------------------------
# 正 / 负对照：度量必须能检出真的挤压
# ---------------------------------------------------------------------------
def test_measurement_detects_a_real_squeeze():
    """**正对照**：故意做空「涨得最多」的两名 ⇒ 空腿亏、且等权选股效应为正、尾部更肥。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    mu = [0.005, -0.005, 0.004, -0.004, 0.0, 0.001]
    held = [-0.5, 0.0, -0.5, 0.0, 0.0, 0.0]        # 空 I0 / I2 —— 正是涨最多的两个
    pan, res = _synthetic(mu, mu, held)
    sb = fa.short_book_frame(res, pan)
    assert sb["available"].all()
    r = sb.iloc[0]
    assert r["mw_ret"] < 0, "做空上涨的标的 ⇒ 空腿损益必须为负"
    assert r["mw_ret_names"] > r["pool_mean"], "被空标的应比池子涨得多"
    assert r["excess_select"] > 0
    assert r["frac_big"] > r["pool_frac_big"], "尾部：被空标的「大涨」占比应更高"
    assert r["ew_median"] > r["pool_median"]


def test_measurement_rejects_a_non_squeeze():
    """**负对照**：做空「跌得最多」的两名 ⇒ 空腿赚钱、等权选股效应为负。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    mu = [0.005, -0.005, 0.004, -0.004, 0.0, 0.001]
    held = [0.0, -0.5, 0.0, -0.5, 0.0, 0.0]        # 空 I1 / I3 —— 正在跌
    pan, res = _synthetic(mu, mu, held)
    sb = fa.short_book_frame(res, pan)
    r = sb.iloc[0]
    assert r["mw_ret"] > 0, "做空下跌的标的 ⇒ 空腿应赚钱"
    assert r["excess_select"] < 0
    assert r["frac_big"] < r["pool_frac_big"]


def test_measurement_is_significant_when_the_squeeze_is_real():
    """正对照（统计版）：窗口内被空标的猛涨、窗口外不动 ⇒ 置换检验必须显著。

    这条是为了防「度量本身太钝 ⇒ 什么都说『已否定』」。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    mu_a = [0.006, -0.006, 0.006, -0.006, 0.0, 0.0]   # 窗口内：空头猛涨
    mu_b = [0.0] * 6                                    # 窗口外：全平
    held = [-0.5, 0.0, -0.5, 0.0, 0.0, 0.0]
    pan, res = _synthetic(mu_a, mu_b, held, switch_at=30)
    d0, d1 = res.reb_ts[0], res.reb_ts[29]
    win = (("T", str(d0.date()), str(d1.date()), -0.1),)
    sb = fa.short_book_frame(res, pan, windows=win)
    t = fa.short_book_table(sb, windows=win)
    row = t.set_index("window").loc["T"]
    assert row["excess_select"] > 0
    assert row["perm_p"] < 0.05, f"真挤压必须显著，实测 perm_p={row['perm_p']}"
    assert fa.squeeze_verdict(row).startswith("已确认")


def test_squeeze_verdict_three_states():
    """裁决规则：方向 + 显著性 + 尾部，三者都要满足才算「已确认」。"""
    from crypto_ls_research.analysis.failure_attribution import squeeze_verdict
    base = {"frac_big": 0.20, "pool_frac_big": 0.10}
    assert squeeze_verdict(pd.Series({**base, "excess_select": -0.001,
                                      "perm_p": 0.001})).startswith("已否定")
    assert squeeze_verdict(pd.Series({**base, "excess_select": 0.0,
                                      "perm_p": 0.001})).startswith("已否定")
    assert squeeze_verdict(pd.Series({**base, "excess_select": 0.004,
                                      "perm_p": 0.001})).startswith("已确认")
    # 方向对、显著，但尾部不更肥 ⇒ 不能叫「已确认」
    assert squeeze_verdict(pd.Series({"excess_select": 0.004, "perm_p": 0.001,
                                      "frac_big": 0.10, "pool_frac_big": 0.20})
                           ).startswith("证据不足")
    # 方向对但不显著 ⇒ 证据不足
    assert squeeze_verdict(pd.Series({**base, "excess_select": 0.004,
                                      "perm_p": 0.40})).startswith("证据不足")
    assert squeeze_verdict(pd.Series({"excess_select": np.nan})).startswith("证据不足")


# ---------------------------------------------------------------------------
# 贡献者 / 切片标签
# ---------------------------------------------------------------------------
def test_contributors_are_sorted_and_share_is_nan_when_the_leg_made_money():
    """空腿**赚钱**的窗口（W3/W5）没有「亏损份额」可言 ⇒ 必须是 NaN，不是负数份额。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    sb = _sb()
    ep = fa.episode_windows(res.bars)
    sb_ep = sb.copy()
    sb_ep["window"] = fa.retag_episode(sb_ep, "ts", ep)
    c = fa.short_contributors(sb_ep, res, windows=ep, rest_label="REST", top=6)
    for label, seg in c.groupby("window"):
        seg = seg.dropna(subset=["pnl"])
        assert list(seg["pnl"]) == sorted(seg["pnl"]), f"{label} 未按最亏排序"
        made_money = not bool(seg["leg_is_loss"].iloc[0])
        if made_money:
            assert seg["share_of_loss"].isna().all(), f"{label} 空腿赚钱时不应有亏损份额"
            assert seg["top_share"].isna().all()
        else:
            assert seg["share_of_loss"].notna().all()
            assert float(seg["total_short_pnl"].iloc[0]) < 0


def test_label_stated_marks_points_outside_all_windows_as_normal():
    from crypto_ls_research.analysis import failure_attribution as fa
    idx = pd.date_range("2021-01-01", "2026-09-29", freq="1D", tz="UTC")
    w = fa.label_stated(idx, windows=(fa.WINDOWS[0],))
    assert set(w.unique()) == {"W1", "NORMAL"}
    inside = fa.window_mask(idx, fa.WINDOWS[0][1], fa.WINDOWS[0][2])
    assert (w.to_numpy()[inside] == "W1").all()
    assert (w.to_numpy()[~inside] == "NORMAL").all()
