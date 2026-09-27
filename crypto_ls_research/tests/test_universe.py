"""PIT universe correctness: listing gate, age gate, liquidity gate, rank gate."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import UniverseConfig
from crypto_ls_research.data.store import Panels
from crypto_ls_research.universe.pit import listing_age_bars, min_history_bars, universe_mask

from .conftest import synth_cfg, synth_panels


def test_age_bars_are_exact():
    p = synth_panels(n_inst=6, n_bars=2000, seed=3, first_bar_offset=300)
    T, N = p.close.shape
    first = np.array([
        np.flatnonzero(np.isfinite(p.close[c].to_numpy(dtype="float64")))[0]
        for c in p.close.columns], dtype="float64")
    age = listing_age_bars(first, T)
    cols = np.arange(N)
    np.testing.assert_allclose(age[first.astype(int), cols], 0.0)
    # one bar before listing -> NaN, never "age 0"
    before = first.astype(int) - 1
    ok = before >= 0
    assert np.isnan(age[before[ok], cols[ok]]).all()
    # age increments by exactly one bar
    i = int(first.max()) + 5
    np.testing.assert_allclose(age[i] - age[i - 1], 1.0)


def test_listing_gate_blocks_young_names():
    p = synth_panels(n_inst=6, n_bars=2000, seed=3, first_bar_offset=300)
    close = p.close.to_numpy(dtype="float64")
    adv = np.full(close.shape[1], 1e9)
    age = np.full(close.shape[1], 1.0)
    u = UniverseConfig(min_history_days=1, min_avg_amount_usd=1.0, pit_topn=100)
    mask, _ = universe_mask(close[0], age, adv, u, min_history_bars=10)
    assert mask.sum() < close.shape[1], "young names were not gated out"
    # a name with no data yet at bar 0 must never be eligible
    assert not mask[np.isnan(close[0])].any()


def test_liquidity_gate_and_rank_are_point_in_time():
    close = np.full(4, 100.0)
    age = np.full(4, 1e6)
    adv = np.array([1e7, 5e7, 3e7, 2e7])
    u = UniverseConfig(min_history_days=1, min_avg_amount_usd=8e6, pit_topn=2)
    mask, rank = universe_mask(close, age, adv, u, min_history_bars=1)
    assert list(np.flatnonzero(mask)) == [1, 2]           # top-2 by ADV
    assert rank[1] == 1 and rank[2] == 2
    assert np.isnan(rank[0]) and np.isnan(rank[3])

    # raising the liquidity floor only removes names -- it can never add one
    u2 = UniverseConfig(min_history_days=1, min_avg_amount_usd=2.5e7, pit_topn=2)
    mask2, _ = universe_mask(close, age, adv, u2, min_history_bars=1)
    assert set(np.flatnonzero(mask2)) <= set(np.flatnonzero(mask))


def test_universe_is_monotone_in_adv_threshold():
    rng = np.random.default_rng(0)
    close = np.full(50, 10.0)
    age = np.full(50, 1e5)
    adv = rng.uniform(1e5, 1e9, 50)
    prev = None
    for thr in (1e5, 1e6, 1e7, 1e8, 1e9):
        u = UniverseConfig(min_history_days=1, min_avg_amount_usd=thr, pit_topn=100)
        mask, _ = universe_mask(close, age, adv, u, min_history_bars=1)
        if prev is not None:
            assert set(np.flatnonzero(mask)) <= prev
        prev = set(np.flatnonzero(mask))


def test_pool_size_never_exceeds_topn():
    rng = np.random.default_rng(5)
    close = np.full(80, 1.0)
    age = np.full(80, 1e6)
    adv = rng.uniform(1e7, 1e9, 80)
    u = UniverseConfig(min_history_days=1, min_avg_amount_usd=1.0, pit_topn=25)
    mask, rank = universe_mask(close, age, adv, u, min_history_bars=1)
    assert mask.sum() == 25
    assert np.nansum(np.isfinite(rank)) == 25
    # ranks inside the pool are a permutation of 1..25
    assert sorted(rank[mask]) == list(range(1, 26))
    # the highest ADV inside the pool must hold rank 1
    assert int(np.flatnonzero(mask)[np.argmax(adv[mask])]) == int(np.flatnonzero(rank == 1)[0])


def test_no_trade_before_listing_end_to_end():
    from crypto_ls_research.backtest.engine import run_backtest
    p = synth_panels(n_inst=10, n_bars=4000, seed=21, first_bar_offset=350)
    cfg = synth_cfg()
    res = run_backtest(p, cfg)
    firsts = {c: p.close[c].first_valid_index() for c in p.close.columns}
    for rb in res.rebalances:
        for n in rb["long"] + rb["short"]:
            assert rb["exec_ts"] > firsts[n], f"{n} traded before its listing"
