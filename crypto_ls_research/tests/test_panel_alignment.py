"""Regression tests for instrument-axis alignment between a backtest result and a
freshly loaded panel.

Why this file exists
--------------------
The candle cache can grow between the baseline stage and a later analysis stage --
e.g. a download that was retrying failed instruments finishes mid-run and adds a
few names.  The baseline result then carries per-instrument matrices of width N
while the re-loaded panel has width N+k, and any positional use of
`result.score_matrix` against that panel silently correlates the wrong columns
(or crashes with a broadcast error).

`ic.align_panels` makes the analysis align on instrument IDENTITY.  These tests
pin that behaviour so the defect cannot come back.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.analysis import ic as ic_mod
from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.data.store import Panels

from .conftest import synth_cfg, synth_panels

FIELDS = ("open", "high", "low", "close", "vol", "vol_ccy", "amount", "funding")


def _extend(p: Panels, extra: int = 3, seed: int = 99) -> Panels:
    """Append `extra` synthetic instruments, i.e. simulate cache growth."""
    rng = np.random.default_rng(seed)
    n_bars = len(p.index)
    kw = {}
    for f in FIELDS:
        base = getattr(p, f)
        new_cols = []
        for j in range(extra):
            name = f"X{j:02d}-USDT-SWAP"
            if f == "close":
                s = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n_bars))),
                              index=p.index)
            else:
                s = base.iloc[:, 0] * rng.uniform(0.8, 1.2)
            new_cols.append(s.rename(name))
        kw[f] = pd.concat([base] + new_cols, axis=1)
    for f in FIELDS:
        assert kw[f].columns.is_unique, f"helper produced duplicate labels in {f}"
    list_dt = pd.concat([p.list_dt, pd.Series({f"X{j:02d}-USDT-SWAP": p.index[0]
                                               for j in range(extra)})])
    return Panels(list_dt=list_dt, **kw)


def _reorder(p: Panels, order) -> Panels:
    kw = {f: getattr(p, f)[list(order)] for f in FIELDS}
    return Panels(list_dt=p.list_dt.reindex(list(order)), **kw)


def test_align_panels_reorders_to_requested_order():
    p = synth_panels()
    want = list(p.insts)[::-1]
    a = ic_mod.align_panels(p, want)
    assert a.insts == want
    # values must follow their names, not their positions
    pd.testing.assert_frame_equal(a.close, p.close[want])


def test_align_panels_drops_extra_instruments():
    p = synth_panels()
    big = _extend(p, 3)
    assert len(big.insts) == len(p.insts) + 3
    a = ic_mod.align_panels(big, p.insts)
    assert a.insts == p.insts
    pd.testing.assert_frame_equal(a.close, p.close)


def test_align_panels_raises_on_missing_instrument():
    p = synth_panels()
    with pytest.raises(ValueError, match="missing"):
        ic_mod.align_panels(p, list(p.insts) + ["GHOST-USDT-SWAP"])


def test_align_panels_rejects_duplicate_labels():
    """Label selection on a frame with duplicate names silently returns extra
    columns, which would re-introduce the mis-alignment.  It must fail loudly."""
    p = synth_panels()
    bad = p.close.copy()
    bad.columns = list(p.close.columns[:-1]) + [p.close.columns[0]]     # duplicate
    with pytest.raises(ValueError, match="duplicate instrument columns"):
        ic_mod.align_panels(
            Panels(open=p.open, high=p.high, low=p.low, close=bad, vol=p.vol,
                   vol_ccy=p.vol_ccy, amount=p.amount, funding=p.funding,
                   list_dt=p.list_dt),
            list(p.insts)[:5])


def test_ic_is_identical_when_panel_has_extra_instruments():
    """The core regression: a wider panel must not change the IC."""
    p = synth_panels()
    cfg = synth_cfg()
    res = run_backtest(p, cfg)
    hz = [cfg.rebalance_bars, 2 * cfg.rebalance_bars]

    base = ic_mod.rank_ic_over_horizons(res, p, hz)
    wider = ic_mod.rank_ic_over_horizons(res, _extend(p, 3), hz)
    assert np.allclose(base["IC_mean"].to_numpy(dtype="float64"),
                       wider["IC_mean"].to_numpy(dtype="float64"), equal_nan=True)


def test_ic_is_identical_when_panel_columns_are_permuted():
    """Column order must be irrelevant: alignment is by name."""
    p = synth_panels()
    cfg = synth_cfg()
    res = run_backtest(p, cfg)
    hz = [cfg.rebalance_bars, 2 * cfg.rebalance_bars]

    base = ic_mod.rank_ic_over_horizons(res, p, hz)
    perm = _reorder(p, list(p.insts)[::-1])
    got = ic_mod.rank_ic_over_horizons(res, perm, hz)
    assert np.allclose(base["IC_mean"].to_numpy(dtype="float64"),
                       got["IC_mean"].to_numpy(dtype="float64"), equal_nan=True)


def test_ic_raises_clearly_when_result_instrument_disappeared():
    p = synth_panels()
    cfg = synth_cfg()
    res = run_backtest(p, cfg)
    shrunk = _reorder(p, list(p.insts)[:-2])          # drop two names entirely
    with pytest.raises(ValueError, match="candle cache changed"):
        ic_mod.rank_ic_over_horizons(res, shrunk, [cfg.rebalance_bars])
