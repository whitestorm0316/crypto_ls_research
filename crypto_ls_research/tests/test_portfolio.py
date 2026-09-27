"""Portfolio construction, sizing caps, and beta neutralisation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import PortfolioConfig
from crypto_ls_research.portfolio.beta_neutral import side_beta, side_gross_targets
from crypto_ls_research.portfolio.construct import (
    build_units, inverse_vol_weights, rank_order, select_book)
from crypto_ls_research.signals.cross_section import (
    composite_score, normalize_weights, winsorize, zscore)


# --------------------------- selection -------------------------------------
def test_select_book_picks_extremes():
    score = np.array([3.0, -1.0, 0.5, -4.0, 2.0, 1.0])
    mask = np.ones(6, dtype=bool)
    cfg = PortfolioConfig(top_k=2, min_abs_score=0.0)
    sel = select_book(score, mask, cfg)
    assert set(sel.long_idx.tolist()) == {0, 4}
    assert set(sel.short_idx.tolist()) == {3, 1}


def test_min_abs_score_filters_weak_names():
    score = np.array([3.0, -1.0, 0.5, -0.2, 2.0, 0.1])
    mask = np.ones(6, dtype=bool)
    cfg = PortfolioConfig(top_k=3, min_abs_score=0.75)
    sel = select_book(score, mask, cfg)
    assert set(sel.long_idx.tolist()) == {0, 4}
    assert set(sel.short_idx.tolist()) == {1}


def test_excluded_names_are_never_selected():
    score = np.array([9.0, 8.0, -9.0, -8.0])
    mask = np.array([False, True, True, False])
    sel = select_book(score, mask, PortfolioConfig(top_k=2))
    assert set(sel.long_idx.tolist()) == {1}
    assert set(sel.short_idx.tolist()) == {2}


def test_hold_stability_limits_replacements():
    score = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    mask = np.ones(5, dtype=bool)
    cfg = PortfolioConfig(top_k=2, n_drop=1, hold_rank_buffer=0)
    first = select_book(score, mask, cfg)
    assert set(first.long_idx.tolist()) == {0, 1}

    # incumbents {0,1} fall to the bottom of the ranking but are still valid candidates
    score2 = np.array([3.0, 2.5, 9.0, 8.0, 7.0])
    second = select_book(score2, mask, cfg, prev_long=first.long_idx,
                         prev_short=first.short_idx)
    replaced = len(set(second.long_idx.tolist()) - set(first.long_idx.tolist()))
    assert replaced <= 1, f"n_drop=1 violated: {replaced} names replaced"
    assert len(second.long_idx) == 2, "the book must still hold K names"

    cfg_unlimited = PortfolioConfig(top_k=2, n_drop=999, hold_rank_buffer=0)
    third = select_book(score2, mask, cfg_unlimited, prev_long=first.long_idx,
                        prev_short=first.short_idx)
    assert set(third.long_idx.tolist()) == {2, 3}
    assert third.n_replaced_long == 2


def test_hold_buffer_keeps_marginal_incumbents():
    """With a buffer, an incumbent that slips just outside top-K is retained."""
    score = np.array([5.0, 4.0, 3.0, 2.0])
    mask = np.ones(4, dtype=bool)
    cfg = PortfolioConfig(top_k=2, n_drop=999, hold_rank_buffer=2)
    first = select_book(score, mask, cfg)
    assert set(first.long_idx.tolist()) == {0, 1}
    score2 = np.array([1.0, 2.0, 5.0, 4.0])       # incumbents now rank 4 and 3
    second = select_book(score2, mask, cfg, prev_long=first.long_idx)
    # ranks 3 and 4 are within k + buffer == 4, so both are held
    assert set(second.long_idx.tolist()) == {0, 1}


def test_incumbent_dropped_when_it_fails_min_abs_score():
    """A held name whose score collapses below the entry threshold must be dropped."""
    score = np.array([5.0, 4.0, 1.0])
    mask = np.ones(3, dtype=bool)
    cfg = PortfolioConfig(top_k=1, min_abs_score=0.0)
    first = select_book(score, mask, cfg)
    assert first.long_idx.tolist() == [0]
    score2 = np.array([-2.0, -3.0, -4.0])          # nothing qualifies for the long side
    second = select_book(score2, mask, cfg, prev_long=first.long_idx)
    assert second.long_idx.size == 0


def test_rank_order_is_dense_and_descending():
    score = np.array([1.0, np.nan, 5.0, 3.0])
    mask = np.array([True, False, True, True])
    r = rank_order(score, mask)
    assert r[2] == 1 and r[3] == 2 and r[0] == 3 and np.isnan(r[1])


# --------------------------- weighting -------------------------------------
def test_weights_sum_to_one_and_cap_respected():
    score = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    vol = np.array([0.5, 0.5, 0.5, 0.5, 0.5, 0.5])
    idx = np.arange(6)
    cfg = PortfolioConfig(max_weight_per_instrument=0.40, top_k=6)
    w = inverse_vol_weights(score, vol, idx, cfg)
    np.testing.assert_allclose(w.sum(), 1.0, rtol=1e-12)
    assert w.max() <= 0.40 + 1e-12


def test_infeasible_cap_degrades_gracefully():
    score = np.ones(10)
    vol = np.ones(10)
    cfg = PortfolioConfig(max_weight_per_instrument=0.05, top_k=10)   # 0.05*10 < 1
    w = inverse_vol_weights(score, vol, np.arange(10), cfg)
    np.testing.assert_allclose(w.sum(), 1.0, rtol=1e-12)
    np.testing.assert_allclose(w, 0.1)


def test_boundary_cap_equals_one_over_k_also_degrades_to_equal_weight():
    """The *boundary* `cap * k == 1` is degenerate too, and it is the shipped default.

    `construct.py` guards with `cap * n <= 1.0 + 1e-12`, so `k=10, cap=0.10` -- the
    specification's default pair -- does NOT run the inverse-vol/|score| tilting at all:
    every weight is exactly `1/k`.  The strictly-infeasible case is covered above; this
    test pins the equality case so nobody re-discovers it by shadowing a backtest again.

    Regression note: this was found by comparing two backtests that returned
    byte-identical results under different caps.  If the sizing is ever made to
    interpolate instead of degrade, this assertion must change deliberately.
    """
    rng = np.random.default_rng(7)
    n = 10
    score = rng.normal(0, 1, n)
    vol = rng.uniform(0.02, 0.10, n)          # deliberately very unequal
    cfg = PortfolioConfig(top_k=n, max_weight_per_instrument=1.0 / n)   # cap * n == 1

    w = inverse_vol_weights(score, vol, np.arange(n), cfg)
    np.testing.assert_allclose(w, 1.0 / n, rtol=1e-12)

    # and the very same book as explicitly turning the tilting off
    w_off = inverse_vol_weights(score, vol, np.arange(n),
                                PortfolioConfig(top_k=n, max_weight_per_instrument=1.0 / n,
                                                vol_weight_power=0.0, score_weight_power=0.0))
    np.testing.assert_allclose(w, w_off, rtol=1e-12)


def test_cap_above_one_over_k_activates_real_tilting():
    """Just above the boundary the tilting must engage, otherwise §2.2 has no fix."""
    rng = np.random.default_rng(7)
    n = 10
    score = rng.normal(0, 1, n)
    vol = rng.uniform(0.02, 0.10, n)
    cfg = PortfolioConfig(top_k=n, max_weight_per_instrument=0.20)     # cap * n == 2

    w = inverse_vol_weights(score, vol, np.arange(n), cfg)
    assert np.unique(np.round(w, 10)).size > 1, "sizing should not be degenerate here"
    np.testing.assert_allclose(w.sum(), 1.0, rtol=1e-12)
    assert w.max() <= 0.20 + 1e-12


def test_inverse_vol_tilts_toward_low_vol_and_strong_score():
    score = np.array([1.0, 1.0])
    vol = np.array([0.2, 0.8])
    cfg = PortfolioConfig(max_weight_per_instrument=0.9, top_k=2, vol_weight_power=1.0)
    w = inverse_vol_weights(score, vol, np.arange(2), cfg)
    assert w[0] > w[1], "lower-vol name should get more weight"

    score2 = np.array([3.0, 0.5])
    w2 = inverse_vol_weights(score2, np.array([0.5, 0.5]), np.arange(2), cfg)
    assert w2[0] > w2[1], "higher-score name should get more weight"


def test_negative_volatility_cannot_create_negative_weight():
    score = np.array([2.0, -2.0, 1.0])
    vol = np.array([0.3, -0.3, 0.0])          # pathological inputs
    w = inverse_vol_weights(np.abs(score), vol, np.arange(3),
                            PortfolioConfig(max_weight_per_instrument=0.9, top_k=3))
    assert (w >= 0).all()
    np.testing.assert_allclose(w.sum(), 1.0, rtol=1e-12)


# --------------------------- beta neutralisation ---------------------------
def test_beta_neutral_zeroes_portfolio_beta():
    score = np.array([2.0, 2.0, -2.0, -2.0])
    vol = np.ones(4)
    sel = select_book(score, np.ones(4, dtype=bool), PortfolioConfig(top_k=2))
    u_long, u_short = build_units(score, vol, sel, PortfolioConfig(top_k=2))
    beta = np.array([0.6, 1.8, 2.0, 0.4])       # longs low-beta, shorts high-beta
    bu = side_beta(beta, sel.long_idx, u_long)
    bv = side_beta(beta, sel.short_idx, u_short)
    cfg = PortfolioConfig(beta_neutral_mode="B_beta_neutral", beta_ratio_cap=(0.3, 3.0))
    g_long, g_short = side_gross_targets(bu, bv, 1.0, cfg, "B_beta_neutral")
    np.testing.assert_allclose(g_long + g_short, 1.0, rtol=1e-12)
    net_beta = g_long * bu - g_short * bv
    np.testing.assert_allclose(net_beta, 0.0, atol=1e-9)


def test_gross_matched_mode_is_not_beta_neutral_when_betas_differ():
    beta = np.array([0.6, 1.8, 2.0, 0.4])
    g_long, g_short = side_gross_targets(0.6, 1.8, 1.0, PortfolioConfig(), "A_gross_matched")
    assert g_long == g_short == 0.5
    net = 0.5 * 0.6 - 0.5 * 1.8
    assert abs(net) > 0.3, "method A should leave a visible residual beta"


def test_beta_ratio_cap_binds():
    cfg = PortfolioConfig(beta_neutral_mode="B_beta_neutral", beta_ratio_cap=(0.5, 2.0))
    g_long, g_short = side_gross_targets(1.0, 0.01, 1.0, cfg, "B_beta_neutral")
    assert g_short / g_long <= 2.0 + 1e-9


# --------------------------- cross-section ---------------------------------
def test_winsorisation_limits_outlier_influence():
    x = np.concatenate([np.linspace(0, 1, 100), [500.0]])
    raw = winsorize(x, "none")
    mad = winsorize(x, "mad", mad_k=3.0)
    pct = winsorize(x, "percentile", pct_clip=0.02)
    assert raw[-1] == 500.0, "method 'none' must be a no-op"
    # median 0.5, MAD ~0.25 -> bound = 0.5 + 3*1.4826*0.25 ~ 1.61
    assert 1.0 < mad[-1] < 2.0, "MAD winsorisation should have pulled the outlier in"
    assert pct[-1] < 5.0
    # winsorising must never reorder the cross-section
    assert np.all(np.diff(mad) >= -1e-12)
    # and z-scores are additionally hard-clipped to +/-4
    z = zscore(x, "mad")
    assert np.nanmax(np.abs(z)) <= 4.0 + 1e-9


def test_zscore_preserves_nan_positions():
    x = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0])
    z = zscore(x, "mad")
    assert np.isnan(z[2])
    assert np.isfinite(z[[0, 1, 3, 4, 5, 6]]).all()


def test_composite_score_is_a_weighted_average():
    # `factors` is passed explicitly: the point of the test is the weighting rule,
    # not how many factors the engine happens to ship with today.  Relying on the
    # module-level default made this test break the moment a fifth factor was added.
    F = ("momentum", "flow", "range_pos", "hitrate")
    zs = {"momentum": np.array([1.0, -1.0]), "flow": np.array([1.0, -1.0]),
          "range_pos": np.array([-1.0, 1.0]), "hitrate": np.array([0.0, 0.0])}
    s = composite_score(zs, (1.0, 0.0, 0.0, 0.0), factors=F)
    np.testing.assert_allclose(s, [1.0, -1.0])
    s_eq = composite_score(zs, (0.25, 0.25, 0.25, 0.25), factors=F)
    np.testing.assert_allclose(s_eq, [0.25, -0.25])
    # profile normalisation is explicit and total is 1
    np.testing.assert_allclose(normalize_weights((1.0, 0.4, 0.3, 0.2)).sum(), 1.0)


def test_composite_score_handles_missing_factors():
    F = ("momentum", "flow", "range_pos", "hitrate")
    zs = {"momentum": np.array([2.0, np.nan]), "flow": np.array([np.nan, 2.0]),
          "range_pos": np.array([np.nan, np.nan]), "hitrate": np.array([np.nan, np.nan])}
    s = composite_score(zs, (1.0, 1.0, 1.0, 1.0), factors=F)
    np.testing.assert_allclose(s, [2.0, 2.0])
