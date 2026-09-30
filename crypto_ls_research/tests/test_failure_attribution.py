"""失效归因模块的测试（`analysis/failure_attribution.py` + `ic_series` 对齐修复）。

纪律：期望值从 fixture 派生；新断言必须**变异测试**（见 `scripts/_mutate_failure_attribution.py`
的运行记录，一次性脚本已移出仓库）。需要 `baseline.pkl` 的用例在文件缺失时 skip。
"""
from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PKL = os.path.join(ROOT, "artifacts", "v5_1d_all5", "baseline.pkl")

_CACHE: dict = {}


def _baseline():
    if "res" not in _CACHE:
        if not os.path.exists(PKL):
            pytest.skip(f"缺少 {PKL}")
        with open(PKL, "rb") as f:
            _CACHE["res"] = pickle.load(f)["result"]
    return _CACHE["res"]


def _panels(res):
    if "pan" not in _CACHE:
        from crypto_ls_research.analysis.failure_attribution import exec_price  # noqa: F401
        from crypto_ls_research.data.store import Panels, load_panels
        p = load_panels("1h", "2021-01-01", "2026-09-26", insts=list(res.insts))
        g = res.bars.index
        _CACHE["pan"] = Panels(
            open=p.open.reindex(g), high=p.high.reindex(g), low=p.low.reindex(g),
            close=p.close.reindex(g), vol=p.vol.reindex(g), vol_ccy=p.vol_ccy.reindex(g),
            amount=p.amount.reindex(g), funding=p.funding.reindex(g), list_dt=p.list_dt)
    return _CACHE["pan"]


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------
def test_window_mask_is_both_ends_inclusive():
    from crypto_ls_research.analysis.failure_attribution import window_mask
    idx = pd.date_range("2024-07-05", "2024-11-01 23:00", freq="1h", tz="UTC")
    m = window_mask(idx, "2024-07-05", "2024-11-01")
    assert m.all(), "start 与 end 两天都必须在窗口内"
    idx2 = pd.date_range("2024-07-04", "2024-11-02 23:00", freq="1h", tz="UTC")
    m2 = window_mask(idx2, "2024-07-05", "2024-11-01")
    assert not m2[0] and not m2[-1], "窗口外的 bar 不能进来"


def test_window_mask_boundaries_are_utc_midnight():
    from crypto_ls_research.analysis.failure_attribution import window_mask
    idx = pd.DatetimeIndex([pd.Timestamp("2024-07-05 00:00", tz="UTC"),
                            pd.Timestamp("2024-11-01 23:00", tz="UTC"),
                            pd.Timestamp("2024-11-02 00:00", tz="UTC")])
    m = window_mask(idx, "2024-07-05", "2024-11-01")
    assert list(m) == [True, True, False]


def test_any_window_mask_is_the_union():
    from crypto_ls_research.analysis import failure_attribution as fa
    idx = pd.date_range("2021-01-01", "2026-09-29", freq="1h", tz="UTC")
    u = fa.any_window_mask(idx)
    each = np.zeros(len(idx), dtype=bool)
    for _l, s, e, _d in fa.WINDOWS:
        each |= fa.window_mask(idx, s, e)
    assert (u == each).all()
    assert u.any() and not u.all()


# ---------------------------------------------------------------------------
# 用户给的 5 个窗口：必须能对上真实的回撤深度（可证伪的第一步）
# ---------------------------------------------------------------------------
def test_the_five_stated_windows_reproduce_the_stated_depths():
    """brief 里的 5 个深度数字必须在 baseline 上复现到 1e-4。"""
    from crypto_ls_research.analysis.failure_attribution import match_windows
    geo = match_windows(_baseline().bars)
    assert len(geo) == 5
    assert geo["depth_actual"].notna().all(), "有窗口配不到回撤段"
    d = (geo["depth_actual"] - geo["dd_stated"]).abs()
    assert float(d.max()) < 1e-4, f"深度对不上: {geo[['window','dd_stated','depth_actual']]}"


def test_window_boundary_conventions_are_not_uniform():
    """W2 的第二数是恢复日、W3 的第一数是局部高点 —— 这个不一致必须被量出来。"""
    from crypto_ls_research.analysis.failure_attribution import match_windows
    geo = match_windows(_baseline().bars).set_index("window")
    assert bool(geo.loc["W1", "end_is_trough"])
    assert not bool(geo.loc["W2", "end_is_trough"]), "W2 的 end 应是恢复日"
    assert not bool(geo.loc["W3", "start_is_peak"]), "W3 的 start 应是局部高点"


# ---------------------------------------------------------------------------
# 会计恒等式
# ---------------------------------------------------------------------------
def test_accounting_identity_holds_on_the_real_baseline():
    from crypto_ls_research.analysis.failure_attribution import cost_decomposition
    b = _baseline().bars
    resid = (b["net_ret"] - (b["gross_ret"] - b["fee"] - b["spread"]
                             - b["impact"] + b["funding"])).abs().max()
    assert float(resid) < 1e-12, "net = gross - fee - spread - impact + funding 不成立"
    t = cost_decomposition(b)
    assert float(t["recon_resid"].abs().max()) < 1e-12


def test_long_plus_short_equals_gross():
    b = _baseline().bars
    d = (b["gross_ret"] - (b["long_ret"] + b["short_ret"])).abs().max()
    assert float(d) == 0.0


# ---------------------------------------------------------------------------
# 分腿裁决 / 成本裁决
# ---------------------------------------------------------------------------
def test_leg_verdict_decision_rule():
    from crypto_ls_research.analysis.failure_attribution import leg_verdict
    row = lambda lp, sp: pd.Series({"long_pnl": lp, "short_pnl": sp})
    assert leg_verdict(row(-0.01, 0.02)).startswith("A")
    assert leg_verdict(row(0.02, -0.01)).startswith("B")
    assert leg_verdict(row(-0.01, -0.02)).startswith("C")
    assert leg_verdict(row(0.01, 0.02)).startswith("D")


def test_cost_verdict_distinguishes_signal_from_cost():
    from crypto_ls_research.analysis.failure_attribution import cost_decomposition
    t = cost_decomposition(_baseline().bars, mode="stated")
    assert t["verdict"].str.len().gt(0).all()
    # 至少有一个窗口是「毛收益为负」—— 否则这份归因的结论会完全不同
    assert t["verdict"].str.contains("信号失效").any()


# ---------------------------------------------------------------------------
# IC：与已有实现交叉验证 + 逐窗口统计
# ---------------------------------------------------------------------------
def test_ic_frame_matches_the_existing_rank_ic_implementation():
    """`ic_frame` 的 Rank IC 必须与 `analysis.ic.rank_ic_over_horizons` **逐位一致**。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    from crypto_ls_research.analysis.ic import ic_series, rank_ic_over_horizons
    res = _baseline()
    pan = _panels(res)
    H = int(round(res.cfg.bars_per_day))
    mine = fa.ic_frame(res, pan, fa.masked_factor(res, "momentum"), H)["rank_ic"]
    theirs = ic_series(rank_ic_over_horizons(res, pan, [H],
                                             matrix=fa.masked_factor(res, "momentum")), H)
    common = mine.index.intersection(theirs.index)
    assert len(common) > 1000
    assert float((mine.reindex(common) - theirs.reindex(common)).abs().max()) == 0.0


def test_ic_series_keeps_mid_sample_nans_on_the_right_dates():
    """回归：`ic_series` 曾把第 i 个存活值贴到第 i 个时刻上（中段 NaN 会整体错位）。"""
    from crypto_ls_research.analysis.ic import ic_series
    ts = pd.date_range("2021-01-31", periods=6, freq="1D", tz="UTC")
    vals = np.array([0.1, 0.2, np.nan, 0.4, 0.5, 0.6])          # 中段一个 NaN
    tab = pd.DataFrame({"horizon_bars": [24], "IC_mean": [np.nanmean(vals)]})
    tab.attrs["series"] = {24: [0.1, 0.2, 0.4, 0.5, 0.6]}
    tab.attrs["series_pos"] = {24: [0, 1, 3, 4, 5]}
    tab.attrs["series_index"] = [str(t) for t in ts]
    s = ic_series(tab, 24)
    assert list(s.index) == [ts[0], ts[1], ts[3], ts[4], ts[5]]
    assert ts[2] not in s.index, "NaN 那天不能被别的值占用"
    assert float(s.iloc[2]) == 0.4


def test_ic_summary_reports_icir_and_t_from_fixture():
    from crypto_ls_research.analysis.failure_attribution import ic_summary
    v = pd.Series([0.1, -0.2, 0.3, 0.05, -0.1, 0.2, 0.15, -0.05])
    s = ic_summary(v)
    assert s["n"] == 8
    assert s["ic_mean"] == pytest.approx(float(v.mean()))
    assert s["ic_std"] == pytest.approx(float(v.std(ddof=1)))
    assert s["icir"] == pytest.approx(s["ic_mean"] / s["ic_std"])
    assert s["t_stat"] == pytest.approx(s["icir"] * np.sqrt(8))
    assert s["pos_share"] == pytest.approx(float((v > 0).mean()))


def test_block_perm_separates_a_real_shift_from_noise():
    from crypto_ls_research.analysis.failure_attribution import block_perm
    rng = np.random.default_rng(7)
    base = pd.Series(rng.normal(0, 0.3, 400))
    same = pd.Series(rng.normal(0, 0.3, 60))
    shifted = pd.Series(rng.normal(-0.9, 0.3, 60))
    p_same = block_perm(same, base, n_perm=400)["perm_p"]
    p_shift = block_perm(shifted, base, n_perm=400)["perm_p"]
    assert p_shift < 0.01 < p_same


def test_block_perm_is_reproducible_under_the_same_seed():
    from crypto_ls_research.analysis.failure_attribution import block_perm
    rng = np.random.default_rng(11)
    a, b = pd.Series(rng.normal(size=50)), pd.Series(rng.normal(size=300))
    assert block_perm(a, b, n_perm=300)["perm_p"] == block_perm(a, b, n_perm=300)["perm_p"]


# ---------------------------------------------------------------------------
# 动量反转 / 轮动 / regime
# ---------------------------------------------------------------------------
def test_momentum_reversal_spread_is_top_minus_bottom():
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    mr = fa.momentum_reversal(res, _panels(res))
    assert len(mr) > 1000
    d = (mr["spread_1d"] - (mr["top_1d"] - mr["bot_1d"])).abs().max()
    assert float(d) < 1e-12


def test_momentum_reversal_table_covers_every_window_and_normal():
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    mr = fa.momentum_reversal(res, _panels(res))
    t = fa.momentum_reversal_table(mr)
    assert set(t["window"]) == set(fa.WINDOW_LABELS) | {"NORMAL"}
    assert set(t["horizon_days"]) == {1, 3, 5, 10}


def test_momentum_reversal_is_measurable_in_the_narrow_windows():
    """回归：第一版要求池子 ≥20 名才算，而 5 个回撤窗口的宇宙**恰好更窄**（W4 均值 7.4 名）
    ⇒ W1/W3/W4/W5 全部 0 个样本，会得出「这些窗口没有动量反转」的假结论。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    t = fa.momentum_reversal_table(fa.momentum_reversal(res, _panels(res)))
    one = t[t["horizon_days"] == 1].set_index("window")
    for label in fa.WINDOW_LABELS:
        assert int(one.loc[label, "n"]) > 0, f"{label} 的 Rank IC 样本数为 0"
        assert float(one.loc[label, "mean_pool"]) > 0
    assert one.loc["W4", "mean_pool"] < one.loc["NORMAL", "mean_pool"], \
        "W4 的池子应当明显窄于正常期（这正是第一版丢样本的原因）"
    assert one.loc["NORMAL", "n_top10"] > 0, "正常期仍应能算 Top10/Bottom10"


def test_regime_momentum_ic_is_trailing_not_contemporaneous():
    """回归：regime 标签若用**同期** IC，就是拿结果解释结果。必须 `.shift(1)`。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    pan = _panels(res)
    rot = fa.rotation_metrics(res, pan)
    ic = fa.ic_frame(res, pan, fa.masked_factor(res, "momentum"),
                     int(round(res.cfg.bars_per_day)))["rank_ic"]
    reg = fa.regime_frame(res, pan, rot, ic)
    exp = ic.rolling(5, min_periods=2).mean().shift(1)
    common = exp.dropna().index.intersection(reg["mom_ic_5d_trailing"].dropna().index)
    assert len(common) > 100
    assert np.allclose(reg["mom_ic_5d_trailing"].reindex(common), exp.reindex(common))
    assert (reg["mom_ic_5d_trailing"].isna().sum()
            >= reg["mom_ic_5d_contemp"].isna().sum() + 1), "trailing 至少多一个 NaN（shift 掉的）"


def test_regime_frame_takes_no_window_argument():
    """结构性断言：`regime_frame` 收不到窗口 ⇒ **不可能**按窗口调阈值。"""
    import inspect
    from crypto_ls_research.analysis.failure_attribution import regime_frame
    params = set(inspect.signature(regime_frame).parameters)
    assert "windows" not in params and "window" not in params


def test_regime_labels_are_one_per_day():
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    pan = _panels(res)
    rot = fa.rotation_metrics(res, pan)
    ic_mom = fa.ic_frame(res, pan, fa.masked_factor(res, "momentum"),
                         int(round(res.cfg.bars_per_day)))["rank_ic"]
    reg = fa.regime_frame(res, pan, rot, ic_mom)
    assert len(reg) == len(res.reb_ts)
    assert reg["regime"].notna().all()
    assert set(reg["regime"]).issubset(set(fa.REGIME_ORDER))


def test_rotation_metrics_read_overlap_from_rebalances():
    """`long_overlap` 必须等于「与上一期 Top10 的重叠率」，从 rebalances 直接算。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    rot = fa.rotation_metrics(res, _panels(res)).set_index("ts")
    rb = res.rebalances
    for i in (50, 900, 1500):
        prev, cur = set(rb[i - 1]["long"]), set(rb[i]["long"])
        assert rot.loc[rb[i]["ts"], "long_overlap"] == pytest.approx(
            len(prev & cur) / len(cur))


def test_overlap_is_undefined_not_zero_when_a_side_is_empty():
    """回归：极早的调仓点会出现**空侧** ⇒ 重叠率无定义，不能 ZeroDivisionError、也不能报 0。"""
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    rot = fa.rotation_metrics(res, _panels(res))
    assert rot["long_overlap"].notna().any()
    empty = [b for b in res.rebalances if len(b["long"]) == 0]
    if empty:  # 只有在样本里真的出现过空侧时才断言
        ts = pd.DatetimeIndex([b["ts"] for b in empty])
        sub = rot.set_index("ts").reindex(ts)
        assert sub["long_overlap"].isna().all(), "空侧的上一期重叠率必须是 NaN，不是 0"


def test_liquidity_table_reports_a_universe_per_window():
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    liq = fa.liquidity_diag(res)
    t = fa.liquidity_table(liq)
    assert set(t["window"]) == set(fa.WINDOW_LABELS) | {"NORMAL"}
    assert (t["universe"] > 0).all()


def test_rotation_and_liquidity_tables_honour_the_windows_argument():
    """回归：`windows` 参数曾被接受但**完全不生效**（表里永远只输出模块常量那 5 个窗口）。

    旧行为下传「只有 W1」也会返回 5 行 ⇒ 本断言变红。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    pan = _panels(res)
    rot = fa.rotation_metrics(res, pan)
    liq = fa.liquidity_diag(res)
    only_w1 = (fa.WINDOWS[0],)
    assert list(fa.rotation_table(rot, windows=only_w1)["window"]) == ["W1", "NORMAL"]
    assert list(fa.liquidity_table(liq, windows=only_w1)["window"]) == ["W1", "NORMAL"]
    # 默认（全部 5 个窗口）不受影响
    assert list(fa.rotation_table(rot)["window"]) == list(fa.WINDOW_LABELS) + ["NORMAL"]


def test_episode_windows_slice_is_the_same_bars_as_leg_attribution_episode():
    """episode 的 IC / 轮动 / 流动性必须与分腿归因的 episode P&L 落在**同一段 bar**。

    否则「空腿亏了多少」与「当期 IC 是多少」会是两个切片上的数字，放在同一行不可比。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    ep = fa.episode_windows(res.bars)
    assert [e[0] for e in ep] == list(fa.WINDOW_LABELS)
    legs = fa.leg_attribution(res.bars, mode="episode").set_index("window")
    for lbl, pk, tr, _dd in ep:
        m = (res.bars.index >= pk) & (res.bars.index <= tr)
        assert int(m.sum()) == int(legs.loc[lbl, "n_bars"]), f"{lbl} 切片与分腿归因不一致"


def test_retag_episode_labels_only_the_episode_and_calls_the_rest_rest():
    """落不进任何 episode 的点标 `REST`，**不能**标 `NORMAL`。

    episode 并集 ≠ 用户窗口并集；都叫 NORMAL 会把两个不同的基准混成一行。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    ep = fa.episode_windows(res.bars)
    rot = fa.rotation_metrics(res, _panels(res))
    w = fa.retag_episode(rot, "ts", ep)
    assert set(w.unique()) <= set(fa.WINDOW_LABELS) | {"REST"}
    assert (w == "REST").sum() > 0
    # 每个 episode 标签的条数与 bar 级切片推出来的调仓点数一致
    reb = pd.DatetimeIndex(res.reb_ts)
    for lbl, pk, tr, _dd in ep:
        assert int((w == lbl).sum()) == int(((reb >= pk) & (reb <= tr)).sum())


def test_tail_spread_is_top_minus_bottom_and_is_defined_where_ic_is():
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    pan = _panels(res)
    h = int(round(1.0 * res.cfg.bars_per_day))
    d = fa.tail_spread(res, pan, fa.masked_factor(res, "range_pos"), horizon_bars=h)
    ok = d["spread"].notna()
    assert ok.sum() > 100
    assert np.allclose((d.loc[ok, "top_fwd"] - d.loc[ok, "bot_fwd"]), d.loc[ok, "spread"])
    t = fa.tail_spread_table(d)
    assert list(t["window"]) == ["FULL"] + list(fa.WINDOW_LABELS) + ["NORMAL"]


def test_tail_spread_can_disagree_in_sign_with_rank_ic():
    """回归：`rev_short` 的**秩相关为正、尾部价差为负** —— 只看 IC 会漏掉它。

    把「IC 度量整个横截面、P&L 只取决于两端」钉住：谁把 `tail_spread` 换成 IC
    的实现（或把价差反过来），这里就变红。
    """
    from crypto_ls_research.analysis import failure_attribution as fa
    res = _baseline()
    pan = _panels(res)
    h = int(round(1.0 * res.cfg.bars_per_day))
    m = fa.masked_factor(res, "rev_short")
    ic = fa.ic_summary(fa.ic_frame(res, pan, m, h)["rank_ic"])["ic_mean"]
    sp = fa.tail_spread_table(fa.tail_spread(res, pan, m, horizon_bars=h)
                              ).set_index("window").loc["FULL", "spread"]
    assert ic > 0, f"rev_short 全样本秩相关应为正，实测 {ic}"
    assert sp < 0, f"rev_short 全样本尾部价差应为负，实测 {sp}"


def test_max_consecutive_true():
    from crypto_ls_research.analysis.failure_attribution import _max_consecutive_true
    assert _max_consecutive_true([True, True, False, True, True, True]) == 3
    assert _max_consecutive_true([]) == 0
    assert _max_consecutive_true([False, False]) == 0
