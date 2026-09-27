"""Regression tests for the trend x reversal blend (`analysis/combine.py`).

These exist because the blend silently failed in production for a whole run:
`combine_books` used to call

    compute_metrics(pd.DataFrame({"net_ret": net}))

while `compute_metrics` dereferences 14 columns, so every blend raised
`KeyError: 'equity'`.  The stage runner swallows stage exceptions and prints
`!! stage multi failed`, and because the run's exit code stayed 0 the gap was
indistinguishable from success -- the `31_*` tables were simply never written.
So the first two tests below pin the *contract* between the blend and the
metrics function, not just the numbers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.analysis.combine import (
    DEFAULT_WEIGHTS,
    align_returns,
    best_weight_is_a_plateau,
    blend_ledger,
    blend_weight_metrics,
    combine_books,
)
from crypto_ls_research.analysis.metrics import compute_metrics


def _legs(n: int = 6000, seed: int = 11, corr: float = 0.0):
    """Two synthetic 1h return legs with a controlled correlation."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq="1h")
    z1 = rng.normal(size=n)
    z2 = corr * z1 + np.sqrt(max(0.0, 1.0 - corr ** 2)) * rng.normal(size=n)
    a = pd.Series(3.0e-4 + 6.0e-3 * z1, index=idx)
    b = pd.Series(1.0e-4 + 9.0e-3 * z2, index=idx)
    return a, b


# --------------------------------------------------------------------------
# the contract that broke
# --------------------------------------------------------------------------
def test_blend_ledger_supplies_every_column_compute_metrics_needs():
    """The ledger must be metrics-ready, or the blend stage dies with a KeyError."""
    a, _ = _legs(500)
    bars = blend_ledger(a)
    for col in ("equity", "net_ret", "gross_ret", "long_ret", "short_ret",
                "turnover", "gross_exposure", "net_exposure", "beta_exposure",
                "fee", "spread", "impact", "funding", "max_participation"):
        assert col in bars.columns, f"blend ledger is missing {col!r}"
    # must not raise -- this is the regression
    m = compute_metrics(bars, name="blend")
    assert np.isfinite(m["Sharpe"])


def test_combine_books_runs_end_to_end_and_returns_a_finite_grid():
    a, b = _legs()
    out = combine_books(a, b, bars_per_day=24, name_a="trend", name_b="rev@4h")
    t = out["table"]
    assert list(t["overlay_weight"]) == list(DEFAULT_WEIGHTS)
    for col in ("Sharpe", "CAGR", "ann_vol", "max_dd", "Sortino", "Calmar"):
        assert np.isfinite(t[col]).all(), f"{col} has non-finite entries"
    assert -1.0 <= out["corr"] <= 1.0
    assert out["n_bars"] == len(a)


def test_blend_at_zero_weight_reproduces_the_primary_leg():
    """`w=0` is the trend-only book, so it must equal the primary leg's own metrics.

    Guards against an off-by-one in the weight grid and against the overlay
    leaking in through the trailing-vol scaling (which is only defined for w>0).
    """
    a, b = _legs(seed=5)
    out = combine_books(a, b, bars_per_day=24)
    row = out["table"].iloc[0]
    assert float(row["overlay_weight"]) == 0.0
    ref = compute_metrics(blend_ledger(a), name="trend only")
    assert float(row["Sharpe"]) == pytest.approx(ref["Sharpe"], rel=1e-9)
    assert float(row["CAGR"]) == pytest.approx(ref["CAGR"], rel=1e-9)
    assert float(row["max_dd"]) == pytest.approx(ref["Max Drawdown"], rel=1e-9)


# --------------------------------------------------------------------------
# honesty of the reported columns
# --------------------------------------------------------------------------
def test_equity_is_rebuilt_from_the_blend_never_sliced():
    """A blend has no equity curve of its own, so it must be rebuilt, not inherited.

    Slicing a *full-sample* equity curve and then annualising produces a
    spectacular, entirely fictitious CAGR (this repo measured 475% once).
    """
    a, b = _legs(seed=3)
    out = combine_books(a, b, weights=(0.5,), bars_per_day=24)
    net = (0.5 * out["aligned"]["a"] + 0.5 * out["b_scaled"].fillna(0.0))
    expect = float((1.0 + net).cumprod().iloc[-1])
    got = compute_metrics(blend_ledger(net), name="blend")["_equity"]
    assert float(got.iloc[-1]) == pytest.approx(expect, rel=1e-12)
    assert float(got.iloc[0]) == pytest.approx(1.0 + float(net.iloc[0]), rel=1e-12)


def test_unattributable_columns_are_nan_not_zero():
    """Filling these with 0 would read as 'costless' / 'never long a winner'.

    The blend adds no *incremental* cost (the legs' costs are already inside
    `net_ret`), but its turnover, exposures and long/short split are simply not
    attributable at blend level, so they must be NaN and surface as `n/a`.
    """
    a, _ = _legs(400)
    bars = blend_ledger(a)
    for col in ("long_ret", "short_ret", "turnover", "gross_exposure",
                "net_exposure", "beta_exposure", "max_participation"):
        assert bars[col].isna().all(), f"{col} should be NaN, not a placeholder"
    # ...while the incremental-cost columns are legitimately zero
    for col in ("fee", "spread", "impact", "funding"):
        assert (bars[col] == 0.0).all()


def test_blend_never_reports_a_zero_win_rate_for_the_long_leg():
    """`(NaN > 0).mean()` is 0.0 -- an all-NaN leg must not be reported as 0% wins.

    This is the subtle half of the `KeyError` bug: had the ledger been padded
    with zeros instead of NaNs, the stage would have *run* and quietly published
    "Long Win Rate = 0.0000" for every blend weight.
    """
    a, b = _legs(seed=9)
    out = combine_books(a, b, weights=(0.3,), bars_per_day=24)
    net = 0.7 * out["aligned"]["a"] + 0.3 * out["b_scaled"].fillna(0.0)
    m = blend_weight_metrics(net, 0.3)
    assert np.isnan(m["Long Win Rate (bar)"])
    assert np.isnan(m["Short Win Rate (bar)"])
    # and the metrics that *are* defined still come through
    assert np.isfinite(m["Sharpe"])
    assert np.isfinite(m["CAGR"])


def test_combine_books_rejects_a_thin_overlap():
    """Two clocks may barely overlap; blending 40 bars would be noise dressed as a result."""
    a, b = _legs(60, seed=2)
    with pytest.raises(ValueError, match="not enough overlapping bars"):
        combine_books(a, b, bars_per_day=24)


def test_align_returns_joins_on_timestamp_not_position():
    """A shifted index must shrink the overlap, never silently pair up bars."""
    a, b = _legs(500, seed=4)
    b_shifted = b.copy()
    b_shifted.index = b_shifted.index + pd.Timedelta(hours=7)
    df = align_returns(a, b_shifted)
    assert len(df) == len(a) - 7
    assert df.index.equals(a.index[7:])


def test_plateau_detection_flags_an_isolated_spike():
    """A single good weight among bad neighbours is the parameter-spike pattern."""
    spike = pd.DataFrame({"overlay_weight": [0.0, 0.2, 0.4, 0.6, 0.8],
                          "Sharpe": [0.5, 0.6, 3.0, 0.5, 0.4]})
    res = best_weight_is_a_plateau(spike)
    assert res["best_weight"] == 0.4 and res["plateau"] is False
    broad = pd.DataFrame({"overlay_weight": [0.0, 0.2, 0.4, 0.6, 0.8],
                          "Sharpe": [0.5, 1.9, 2.0, 1.8, 0.4]})
    assert best_weight_is_a_plateau(broad)["plateau"] is True


def test_plateau_detection_treats_a_boundary_optimum_as_not_a_plateau():
    """All five reversal blends peaked at `w=0` -- that means "reject the overlay".

    A one-sided neighbourhood cannot distinguish a broad top from a monotone
    decline, so calling it a plateau would dress up a rejection as a stable
    optimum.  This is a real false positive that was observed in `31b`.
    """
    lower = pd.DataFrame({"overlay_weight": [0.0, 0.1, 0.2, 0.3],
                          "Sharpe": [1.94, 1.78, 1.55, 1.22]})
    res = best_weight_is_a_plateau(lower)
    assert res["best_weight"] == 0.0
    assert res["at_boundary"] is True and res["plateau"] is False

    upper = pd.DataFrame({"overlay_weight": [0.0, 0.1, 0.2, 0.3],
                          "Sharpe": [0.4, 0.9, 1.6, 2.0]})
    res = best_weight_is_a_plateau(upper)
    assert res["at_boundary"] is True and res["plateau"] is False

    # the same broad top, moved to the interior, is genuinely a plateau
    interior = pd.DataFrame({"overlay_weight": [0.0, 0.1, 0.2, 0.3],
                             "Sharpe": [0.5, 1.9, 2.0, 1.85]})
    res = best_weight_is_a_plateau(interior)
    assert res["at_boundary"] is False and res["plateau"] is True
