"""Factor neutralisation (brief §38) and the short-horizon reversal factor.

Two things are being defended here:

* the neutralisation really removes the linear exposure to the other factors,
  and really reaches the traded book (a config flag that never binds is how this
  project previously mistook "the mechanism is off" for "the alternative is no
  better");
* adding a fifth factor must not move any result produced with the spec-default
  four-factor subset.
"""
from __future__ import annotations

import numpy as np
import pytest

from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.config.settings import default_config
from crypto_ls_research.factors.engine import FACTOR_NAMES
from crypto_ls_research.signals.cross_section import residualize

from .conftest import synth_cfg, synth_panels

SPEC_FACTORS = ("momentum", "flow", "range_pos", "hitrate")


# ---------------------------------------------------------------------------
# residualize()
# ---------------------------------------------------------------------------
def test_residualize_removes_the_linear_exposure_to_the_other_factor():
    rng = np.random.default_rng(0)
    a = rng.normal(size=200)
    b = 0.7 * a + rng.normal(scale=0.2, size=200)      # b is mostly a
    out = residualize({"a": a, "b": b}, ["a", "b"], rescale=False)
    assert abs(np.corrcoef(out["b"], a)[0, 1]) < 1e-8
    # ... while the residual keeps the part of b that a cannot explain
    assert np.corrcoef(out["b"], b)[0, 1] > 0.2


def test_residualize_is_order_invariant():
    rng = np.random.default_rng(1)
    z = {"a": rng.normal(size=150), "b": rng.normal(size=150), "c": rng.normal(size=150)}
    fwd = residualize(z, ["a", "b", "c"], rescale=False)
    rev = residualize(z, ["c", "b", "a"], rescale=False)
    for k in z:
        assert np.allclose(fwd[k], rev[k]), f"{k} residual depends on factor order"


def test_residualize_preserves_nan_mask():
    a = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0, 8.0])
    b = np.array([2.0, 1.0, 3.0, np.nan, 5.0, 4.0, 8.0, 7.0])
    out = residualize({"a": a, "b": b}, ["a", "b"], rescale=False)
    assert np.isnan(out["a"][2]) and np.isnan(out["a"][3])
    assert np.isfinite(out["a"][0])


def test_residualize_degenerate_cross_section_returns_input_not_noise():
    """Too few names to fit: neutralising must be a no-op, never a fabrication."""
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([3.0, 1.0, 2.0])
    c = np.array([2.0, 3.0, 1.0])
    d = np.array([1.0, 3.0, 2.0])
    out = residualize({"a": a, "b": b, "c": c, "d": d}, ["a", "b", "c", "d"], rescale=False)
    assert np.allclose(out["a"], a, equal_nan=True)
    assert np.allclose(out["d"], d, equal_nan=True)


def test_rescale_puts_residuals_on_the_same_scale_as_the_inputs():
    rng = np.random.default_rng(2)
    a = rng.normal(size=300)
    b = 0.95 * a + rng.normal(scale=0.1, size=300)
    out = residualize({"a": a, "b": b}, ["a", "b"], rescale=True)
    assert abs(float(np.std(out["b"])) - 1.0) < 1e-9
    assert abs(float(np.std(out["a"])) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# neutralisation inside the backtest
# ---------------------------------------------------------------------------
def test_neutralisation_reaches_the_score_and_the_pnl():
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    raw = run_backtest(p, synth_cfg())
    neu = run_backtest(p, synth_cfg(**{"factors.neutralize": True}))
    assert raw.meta["neutralize"] is False and neu.meta["neutralize"] is True
    assert not np.allclose(raw.score_matrix, neu.score_matrix, equal_nan=True), \
        "factors.neutralize never reached composite_score"
    assert not np.allclose(raw.bars["net_ret"].to_numpy(),
                           neu.bars["net_ret"].to_numpy()), \
        "factors.neutralize never reached the book"


def test_neutralising_a_single_factor_is_a_no_op():
    p = synth_panels(n_inst=12, n_bars=2000, seed=4)
    kw = {"factors.subset": ("range_pos",)}
    a = run_backtest(p, synth_cfg(**kw))
    b = run_backtest(p, synth_cfg(**dict(kw, **{"factors.neutralize": True})))
    assert np.allclose(a.bars["net_ret"].to_numpy(), b.bars["net_ret"].to_numpy())


# ---------------------------------------------------------------------------
# the fifth factor must not move the spec-default four-factor book
# ---------------------------------------------------------------------------
def test_rev_short_exists_and_is_not_in_the_default_subset():
    assert FACTOR_NAMES == SPEC_FACTORS + ("rev_short",)
    assert tuple(default_config().factors.subset) == SPEC_FACTORS


def test_default_result_is_unchanged_by_adding_rev_short():
    """Explicit 4-factor subset == config default, byte for byte."""
    p = synth_panels(n_inst=10, n_bars=2000, seed=6)
    implicit = run_backtest(p, synth_cfg())
    explicit = run_backtest(p, synth_cfg(), factor_subset=SPEC_FACTORS)
    assert np.array_equal(implicit.bars["net_ret"].to_numpy(),
                          explicit.bars["net_ret"].to_numpy())


def test_profile_weights_cover_every_factor():
    for name, prof in default_config().factors.profiles.items():
        assert len(prof) == len(FACTOR_NAMES), f"profile {name} does not cover every factor"
        assert sum(abs(w) for w in prof) > 0


@pytest.mark.parametrize("subset", [("rev_short",), ("range_pos", "rev_short")])
def test_rev_short_subset_runs_and_trades(subset):
    p = synth_panels(n_inst=12, n_bars=2500, seed=8)
    res = run_backtest(p, synth_cfg(**{"factors.subset": subset}))
    assert res.meta["factor_subset"] == list(subset)
    assert res.bars["turnover"].sum() > 0
    assert np.isfinite(res.bars["net_ret"]).all()
