"""Guards for two "silent mechanism" failure modes.

1. **Universe scope** -- the OKX perp cache contains tokenised equities/ETFs and
   commodities next to crypto.  Scoping must be explicit, auditable, and must
   fail loudly on an unclassified instrument instead of quietly shrinking the pool.

2. **Factor-subset plumbing** -- narrowing `factors.subset` has to reach the
   backtest engine.  A config change that never binds is indistinguishable from
   "the alternative is exactly as good", which is how this project once concluded
   that a factor was worthless while the sizing mechanism was silently disabled.
   So the test asserts the *output changed*, not merely that the knob exists.
"""
from __future__ import annotations

import numpy as np
import pytest

from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.data.asset_class import (
    CATEGORY_FILE, build_category_snapshot, filter_insts, load_categories, scope_summary)
from crypto_ls_research.portfolio.construct import inverse_vol_weights, reset_degradation_cache

from .conftest import synth_cfg, synth_panels

KNOWN_EQUITY = ["NVDA-USDT-SWAP", "TSLA-USDT-SWAP", "QQQ-USDT-SWAP", "SOXL-USDT-SWAP"]
KNOWN_COMMODITY = ["XAU-USDT-SWAP", "XAG-USDT-SWAP", "CL-USDT-SWAP", "BZ-USDT-SWAP"]
KNOWN_CRYPTO = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP"]


# ---------------------------------------------------------------------------
# 1. universe scope
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not CATEGORY_FILE or not __import__("os").path.exists(CATEGORY_FILE),
                    reason="instrument-class snapshot not frozen yet")
def test_crypto_scope_excludes_equities_and_commodities():
    cats = load_categories()
    pool = KNOWN_EQUITY + KNOWN_COMMODITY + KNOWN_CRYPTO
    kept, unknown = filter_insts(pool, cats, "crypto")
    assert unknown == []
    assert set(kept) == set(KNOWN_CRYPTO)
    # and the exchange really does label them differently, i.e. the filter is not
    # passing merely because the names are missing from the snapshot
    assert all(cats[i] == "1" for i in KNOWN_CRYPTO)
    assert all(cats[i] in ("3", "4") for i in KNOWN_EQUITY + KNOWN_COMMODITY)


def test_scope_summary_counts_by_class():
    cats = {**{i: "3" for i in KNOWN_EQUITY}, **{i: "4" for i in KNOWN_COMMODITY},
            **{i: "1" for i in KNOWN_CRYPTO}}
    assert scope_summary(KNOWN_EQUITY + KNOWN_COMMODITY + KNOWN_CRYPTO, cats) == {
        "crypto": 4, "equity": 4, "commodity": 4}


def test_unclassified_instrument_is_reported_not_dropped_silently():
    cats = {i: "1" for i in KNOWN_CRYPTO}
    kept, unknown = filter_insts(KNOWN_CRYPTO + ["FOO-USDT-SWAP"], cats, "crypto")
    assert unknown == ["FOO-USDT-SWAP"]
    assert set(kept) == set(KNOWN_CRYPTO)


def test_unknown_asset_class_rejected():
    with pytest.raises(ValueError):
        filter_insts(KNOWN_CRYPTO, {i: "1" for i in KNOWN_CRYPTO}, "equities")


def test_snapshot_is_reproducible_from_the_exchange(tmp_path, monkeypatch):
    """The frozen file must be a *snapshot of a live query*, not a hand-written list."""
    pytest.importorskip("crypto_ls_research.data.okx_client")
    dest = tmp_path / "inst_category.json"
    try:
        payload = build_category_snapshot(str(dest))
    except Exception as e:                                     # noqa: BLE001  (offline CI)
        pytest.skip(f"OKX unreachable: {type(e).__name__}: {e}")
    assert dest.exists()
    cats = payload["categories"]
    assert payload["n_instruments"] > 100
    assert cats["BTC-USDT-SWAP"] == "1"
    assert cats["NVDA-USDT-SWAP"] == "3"
    assert cats["XAU-USDT-SWAP"] == "4"
    # the frozen copy on disk must agree with the live query on every name it holds
    frozen = load_categories()
    shared = set(frozen) & set(cats)
    assert shared, "snapshot and live query share no instruments"
    mismatch = {k for k in shared if frozen[k] != cats[k]}
    assert not mismatch, f"classification drifted since the snapshot: {sorted(mismatch)[:8]}"


# ---------------------------------------------------------------------------
# 2. factor subset reaches the engine
# ---------------------------------------------------------------------------
def test_factor_subset_default_comes_from_config():
    p = synth_panels(n_inst=10, n_bars=2000, seed=3)
    cfg = synth_cfg()
    res = run_backtest(p, cfg)
    assert res.meta["factor_subset"] == list(cfg.factors.subset)


def test_narrowing_the_factor_subset_changes_the_result():
    """A config-only factor change must move the score AND the P&L."""
    p = synth_panels(n_inst=10, n_bars=2000, seed=3)
    full = run_backtest(p, synth_cfg())
    narrow = run_backtest(p, synth_cfg(**{"factors.subset": ("range_pos", "hitrate")}))
    assert narrow.meta["factor_subset"] == ["range_pos", "hitrate"]
    assert not np.allclose(full.score_matrix, narrow.score_matrix,
                           equal_nan=True), "subset never reached composite_score"
    assert not np.allclose(full.bars["net_ret"].to_numpy(),
                           narrow.bars["net_ret"].to_numpy()), "subset never reached the book"


def test_factor_subset_keeps_relative_profile_weights():
    """A 2-factor subset of a 4-factor profile must be re-normalised, not zeroed."""
    from crypto_ls_research.signals.cross_section import normalize_weights
    p = synth_panels(n_inst=10, n_bars=2000, seed=3)
    cfg = synth_cfg(**{"factors.subset": ("range_pos", "hitrate")})
    res = run_backtest(p, cfg)
    assert res.score_matrix.shape[0] > 0
    prof = cfg.factors.profiles[cfg.factors.default_profile]
    from crypto_ls_research.factors.engine import FACTOR_NAMES
    sub = np.array([prof[FACTOR_NAMES.index(k)] for k in cfg.factors.subset])
    assert np.isclose(normalize_weights(sub).sum(), 1.0)
    assert not np.allclose(normalize_weights(sub), np.array([0.5, 0.5])), \
        "subset weights must follow the profile, not fall back to equal weighting"


def test_unknown_factor_in_subset_fails_loudly():
    p = synth_panels(n_inst=8, n_bars=1000, seed=5)
    cfg = synth_cfg(**{"factors.subset": ("range_pos", "not_a_factor")})
    with pytest.raises(ValueError, match="not_a_factor"):
        run_backtest(p, cfg)


# ---------------------------------------------------------------------------
# 3. infeasible weight cap is never silent
# ---------------------------------------------------------------------------
def test_infeasible_cap_warns_instead_of_degrading_silently():
    reset_degradation_cache()
    cfg = synth_cfg(**{"portfolio.max_weight_per_instrument": 0.10,
                       "portfolio.top_k": 10})
    score = np.linspace(-2.0, 2.0, 10)
    vol = np.linspace(0.2, 0.8, 10)
    with pytest.warns(UserWarning, match="cap is infeasible"):
        w = inverse_vol_weights(score, vol, np.arange(10), cfg.portfolio)
    assert np.allclose(w, 0.1), "fallback allocation is still equal weight"


def test_feasible_cap_does_not_warn():
    reset_degradation_cache()
    cfg = synth_cfg(**{"portfolio.max_weight_per_instrument": 0.20,
                       "portfolio.top_k": 10})
    score = np.linspace(-2.0, 2.0, 10)
    vol = np.linspace(0.2, 0.8, 10)
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("error")
        w = inverse_vol_weights(score, vol, np.arange(10), cfg.portfolio)
    assert w.max() <= 0.20 + 1e-12
    assert len(np.unique(np.round(w, 8))) > 1, "sizing must actually vary with score/vol"


# ---------------------------------------------------------------------------
# 4. the cap fix is itself pool-width dependent
# ---------------------------------------------------------------------------
# Raising cap 0.10 -> 0.20 fixes `cap*n == 1.0` for a 10-name side, but this pool's
# width is not constant: the PIT universe runs 4..42 names, so a side can hold as few
# as 2.  With cap=0.20 any side of <=5 names again has cap*n <= 1, and there equal
# weight is the *only* feasible allocation.  Measured on the v3 book: 100 of 688
# rebalances (14.5%) still degrade, entirely in the narrow years (2021 33.9%,
# 2022 30.3%, 2023 15.7%).  `cap_width_slack` keeps sizing alive there.
def _narrow_book(slack: float, n: int = 5, cap: float = 0.20):
    reset_degradation_cache()
    cfg = synth_cfg(**{"portfolio.max_weight_per_instrument": cap,
                       "portfolio.cap_width_slack": slack})
    score = np.linspace(-2.0, 2.0, n)
    vol = np.linspace(0.2, 0.8, n)
    return inverse_vol_weights(score, vol, np.arange(n), cfg.portfolio)


def test_width_aware_cap_keeps_sizing_alive_in_a_narrow_book():
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("error")            # no degradation warning allowed
        w = _narrow_book(slack=0.50, n=5, cap=0.20)
    assert np.isclose(w.sum(), 1.0)
    assert len(np.unique(np.round(w, 8))) > 1, (
        "with slack>0 a 5-name book at cap=0.20 must no longer be equal-weighted")
    assert w.max() <= 1.5 / 5 + 1e-9, "relaxed cap must not exceed (1+slack)/n"


def test_width_aware_cap_never_exceeds_one_plus_slack_times_equal_weight():
    for n in (2, 3, 4, 5, 6, 8, 12, 20):
        w = _narrow_book(slack=1.00, n=n, cap=0.20)
        bound = max(0.20, 2.0 / n)
        assert w.max() <= bound + 1e-9, f"n={n}: {w.max():.4f} > {bound:.4f}"


def test_width_aware_cap_is_a_noop_when_the_cap_already_binds():
    a = _narrow_book(slack=0.0, n=10, cap=0.20)
    b = _narrow_book(slack=0.50, n=10, cap=0.20)
    assert np.allclose(a, b), (
        "cap*n=2.0 > 1 already, so the width-aware relaxation must not change anything")


def test_width_aware_cap_disabled_by_default_still_degrades_and_warns():
    reset_degradation_cache()
    cfg = synth_cfg(**{"portfolio.max_weight_per_instrument": 0.20,
                       "portfolio.top_k": 10})          # cap_width_slack defaults to 0.0
    with pytest.warns(UserWarning, match="cap is infeasible"):
        w = inverse_vol_weights(np.linspace(-2, 2, 5), np.linspace(0.2, 0.8, 5),
                                np.arange(5), cfg.portfolio)
    assert np.allclose(w, 0.2), "default behaviour is unchanged: equal weight + warning"
