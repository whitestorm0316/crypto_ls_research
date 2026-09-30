"""`analysis/exante_width.py` 的测试（Q42：事前宽度 → 下一期损益）。

纪律：期望值从 fixture 派生；新断言必须**变异测试**。
需要 `baseline.pkl` 的用例在文件缺失时 skip。
"""
from __future__ import annotations

import os
import pickle
import types

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")

_CACHE: dict = {}


def _res():
    if "res" not in _CACHE:
        if not os.path.exists(PKL):
            pytest.skip(f"缺少 {PKL}")
        with open(PKL, "rb") as f:
            _CACHE["res"] = pickle.load(f)["result"]
    return _CACHE["res"]


def _frame():
    if "df" not in _CACHE:
        from crypto_ls_research.analysis import exante_width as ew
        res = _res()
        _CACHE["df"] = ew.width_frame(res, roll=ew.ROLL)
    return _CACHE["df"]


def _truncated(res, k: int):
    """把回测结果截到前 k 个调仓点（用于**无未来函数**检验）。"""
    return types.SimpleNamespace(
        bars=res.bars, cfg=res.cfg,
        reb_ts=res.reb_ts[:k], rebalances=res.rebalances[:k],
    )


# ---------------------------------------------------------------------------
# 口径正确性
# ---------------------------------------------------------------------------
def test_width_frame_reconciles_to_the_bar_level_total():
    """逐期前向损益之和必须等于 bar 级损益之和（否则切片口径就错了）。"""
    df = _frame()
    res = _res()
    assert abs(float(df["fwd_net"].sum()) - float(res.bars["net_ret"].sum())) < 1e-9
    assert abs(float(df["fwd_gross"].sum()) - float(res.bars["gross_ret"].sum())) < 1e-9


def test_width_pct_uses_only_strictly_past_data():
    """`width_pct` 的切片必须**以 i 结尾**（不含 i、更不含未来）。"""
    from crypto_ls_research.analysis import exante_width as ew
    df = _frame()
    w = df["n_universe"].to_numpy(dtype="float64")
    pct = df["width_pct"].to_numpy(dtype="float64")
    checked = 0
    for i in range(len(df)):
        lo = max(0, i - ew.ROLL)
        past = w[lo:i]
        if past.size < max(10, ew.ROLL // 2):
            assert not np.isfinite(pct[i]), f"第 {i} 行样本不足却算出了 width_pct"
            continue
        assert abs(pct[i] - float(np.mean(past <= w[i]))) < 1e-12
        checked += 1
    assert checked > 1000


def test_width_pct_does_not_change_when_future_rows_are_removed():
    """真正的无未来函数检验：截断到前 k 期后，前 k-1 期的 `width_pct` 必须**逐位不变**。"""
    from crypto_ls_research.analysis import exante_width as ew
    res = _res()
    k = 900
    full = ew.width_frame(res, roll=ew.ROLL)
    cut = ew.width_frame(_truncated(res, k), roll=ew.ROLL)
    a = full["width_pct"].to_numpy(dtype="float64")[: k - 1]
    b = cut["width_pct"].to_numpy(dtype="float64")[: k - 1]
    assert np.array_equal(np.isfinite(a), np.isfinite(b))
    assert np.allclose(a[np.isfinite(a)], b[np.isfinite(b)], atol=0, rtol=0), \
        "截断未来数据后 width_pct 变了 ⇒ 用到了未来信息"


def test_untraded_rebalances_are_flagged_not_silently_kept():
    """宽度 0 / 无仓位的调仓点必须被标出来（否则『窄=不亏』的假象会进相关性）。"""
    df = _frame()
    assert (~df["traded"]).sum() > 0, "样本里应当存在无仓位的调仓点"
    assert (df.loc[~df["traded"], "n_side_max"] == 0).all()


# ---------------------------------------------------------------------------
# 机制检验：cap × n ≤ 1 是否真的退化成等权
# ---------------------------------------------------------------------------
def test_cap_binding_really_means_equal_weights():
    """实测（不是从代码推）：`cap×n ≤ 1` 的调仓点权重离散系数 ≈ 0。"""
    df = _frame()
    d = df[df["traded"]]
    bind = d[d["cap_binding"]]["w_long_cv"].dropna()
    free = d[~d["cap_binding"]]["w_long_cv"].dropna()
    assert len(bind) > 100 and len(free) > 100
    assert float(bind.max()) < 1e-9, "绑定时权重应当是**精确**等权"
    assert float(free.median()) > 0.1, "不绑定时权重应当明显不均匀"


def test_cap_binding_share_matches_the_side_count_rule():
    """绑定判据必须与 `cap × max(n_long, n_short) ≤ 1` 一致（不是别的判据）。"""
    from crypto_ls_research.analysis import exante_width as ew
    res = _res()
    cap = float(res.cfg.portfolio.max_weight_per_instrument)
    df = ew.width_frame(res, roll=ew.ROLL, cap=cap)
    expect = cap * df["n_side_max"] <= 1.0
    assert (df["cap_binding"].to_numpy() == expect.to_numpy()).all()


# ---------------------------------------------------------------------------
# 回归：本样本内的结论（含两个**被否定**的假设）
# ---------------------------------------------------------------------------
def test_width_has_no_predictive_power_for_next_period_pnl():
    """回归：本样本内「事前宽度 → 下一期损益」**没有**预测力（所有 |t| < 2）。

    钉住这个**否定结论**：若有人改了宽度的定义而突然出现「预测力」，先怀疑是未来函数。
    """
    from crypto_ls_research.analysis import exante_width as ew
    ic = ew.width_ic_table(_frame())
    assert len(ic) == 8
    assert (ic["t"].abs() < 2.0).all(), ic.to_string()


def test_narrowest_quintile_is_not_worse_than_widest():
    """回归：最窄五分位的下一期损益**不差于**最宽五分位（Welch p 很大）。"""
    from crypto_ls_research.analysis import exante_width as ew
    bt = ew.width_bucket_table(_frame(), q=5)
    assert list(bt["bucket"]) == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert bt.attrs["narrowest_minus_widest"]["welch_p"] > 0.2


def test_window_width_table_never_silently_drops_a_window():
    """回归：滚动百分位算不出来的窗口（W4 在样本第一个月）也必须出一行。

    第一版直接把 W4 从表里删掉了 —— 与 Q41 在 `momentum_reversal` 上犯的错同一类。
    """
    from crypto_ls_research.analysis import exante_width as ew
    from crypto_ls_research.analysis.failure_attribution import WINDOW_LABELS
    t = ew.window_width_table(_frame())
    assert set(WINDOW_LABELS) <= set(t["window"])
    w4 = t[t["window"] == "W4"].iloc[0]
    assert not bool(w4["width_pct_available"])
    assert int(w4["n_width_pct"]) == 0
    # 原始宽度仍必须报出来（它是描述量，不依赖历史）
    assert np.isfinite(w4["n_universe_mean"]) and w4["n_universe_mean"] > 0


def test_loss_per_exposure_identifies_w4_as_the_most_severe_window():
    """回归：按**每单位毛敞口**算，W4 是最严重的窗口（名义 DD 最小是假象）。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    legs = fa.leg_attribution(_res().bars, mode="episode")
    legs["lpe"] = legs["net_pnl"] / legs["avg_gross_exposure"]
    worst = legs.loc[legs["lpe"].idxmin(), "window"]
    assert worst == "W4", legs[["window", "net_pnl", "avg_gross_exposure", "lpe"]].to_string()
    # 而且 W4 的敞口确实是**最低**的（解释为什么名义 DD 看起来最小）
    assert legs.loc[legs["window"] == "W4", "avg_gross_exposure"].iloc[0] == \
        legs["avg_gross_exposure"].min()


def test_trend_check_detects_the_secular_width_trend():
    """回归：宽度与调仓点序号强相关 ⇒ 原始宽度不能直接当「市场状态」。"""
    from crypto_ls_research.analysis import exante_width as ew
    tc = ew.trend_check(_frame())
    assert tc["spearman_width_vs_index"] > 0.25
    assert len(tc["yearly_mean_width"]) >= 5
