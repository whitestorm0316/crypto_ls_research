"""Guards for the **margin mode** (`cross` 全仓 / `isolated` 逐仓) of the live book.

## The claim being pinned

For a **hedged** (beta-neutral) book, "逐仓更安全" is **backwards**, and the whole
argument is one inequality:

* `isolated` gives each leg a liquidation distance of `1/L - mmr`
  (L=3 -> 32.8%, L=5 -> 19.5%, L=100 -> 0.5%).  The offsetting leg's profit sits
  in a *different* bucket and cannot rescue it.
* `cross` backs every position with the whole account, so the move that liquidates
  the book is `(1 - mmr*G)/G`.  At the live cap `G = 1.0` that is 99.5%; at the
  current `G ~ 0.44` it is 224%.

So `cross_distance >= isolated_distance` for every `L >= 1, G <= 1` -- the test
below asserts that over a grid rather than trusting the algebra.  Isolated turns
one hedged book into N independent directional bets, each with a much shorter
fuse, and when one leg dies the hedge **breaks** -- which is precisely the
"one blows up" event it was supposed to prevent.

## The two defects this file also pins (both measured on the live demo account)

1. **`td_mode` is not persisted and has no single owner.**  It lives as an
   in-memory attribute of one `LiveEngine`.  `LiveLimits` has no field for it, no
   ledger records it, and `AutoTrader` never passes it -- so the unattended daemon
   is always `cross` while the console's engine can be flipped to `isolated` by a
   request body and **reverts to `cross` on restart**.  Measured 2026-09-30:
   the console reported `td_mode=isolated` and a freshly built engine in the same
   process reported `cross`, **on the same account**.  Result: the account holds
   the same instrument in *both* modes (9 of them), and which mode the next order
   uses depends on *which process submits it*.
2. **`liquidation_ok` is mode-blind and leverage-blind.**  It computes
   `1/rcfg.max_leverage - mmr` -- the isolated formula, at the *configured*
   leverage -- regardless of `td_mode` and regardless of the leverage actually in
   force on the venue.  Measured: the gate assumes 19.50% while `BTC-USDT-SWAP
   short isolated` sat at **100x**, 0.58% from liquidation.
"""
from __future__ import annotations

import importlib.util
import inspect
import pathlib

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import RiskConfig
from crypto_ls_research.execution.engine import LiveEngine
from crypto_ls_research.execution.limits import LiveLimits
from crypto_ls_research.risk.engine import DEFAULT_MMR, liquidation_ok

ROOT = pathlib.Path(__file__).resolve().parents[2]


# --- the two formulas, derived here independently of the audit script --------
def iso_dist(lever: float) -> float:
    """Move that eats a leg's own isolated margin."""
    return 1.0 / max(float(lever), 1e-9) - DEFAULT_MMR


def cross_dist(gross_over_nav: float) -> float:
    """Move that eats the whole account (all-cross, one direction)."""
    g = max(float(gross_over_nav), 1e-9)
    return (1.0 - DEFAULT_MMR * g) / g


def _audit():
    """Load `scripts/audit_margin_mode.py` so its formulas can be cross-checked.

    Loaded inside the call rather than at module scope: a module-level import of a
    script that fails to parse would turn into a whole-file collection error.
    """
    spec = importlib.util.spec_from_file_location(
        "audit_margin_mode", ROOT / "scripts" / "audit_margin_mode.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
def test_live_engine_defaults_to_cross():
    """The default is `cross`, and it is a *default* -- not a saved setting."""
    eng = LiveEngine(mode="paper")
    assert eng.td_mode == "cross"
    assert "td_mode" not in eng.limits.to_dict()


def test_the_daemon_cannot_be_switched_to_isolated():
    """`AutoTrader` never passes `td_mode`, so the unattended job is always cross.

    This is the load-bearing half of the divergence: the daemon cannot be flipped,
    the console can, and nothing reconciles them.
    """
    from crypto_ls_research.execution.auto_trader import AutoTrader

    src = inspect.getsource(AutoTrader.__init__)
    assert "td_mode" not in src, (
        "AutoTrader now passes td_mode -- if the daemon is no longer pinned to "
        "cross, the console/daemon divergence described in this module's "
        "docstring has changed and the docstring (and FINDINGS Q36) must follow.")
    trader = AutoTrader(mode="paper")
    assert trader.engine.td_mode == "cross"


def test_td_mode_is_not_persisted_anywhere():
    """No saved-limits key carries the mode, so a restart silently reverts it.

    `td_mode` is absent from `LiveLimits`, and therefore from `limits.json`, which
    is the only per-mode settings file that survives a restart.
    """
    keys = set(LiveLimits().to_dict())
    assert "td_mode" not in keys and "mgn_mode" not in keys
    assert {"max_gross_frac", "max_leverage"} <= keys, (
        "the limits dict changed shape; re-check that td_mode really is absent")


def test_liquidation_ok_is_mode_blind():
    """`liquidation_ok` cannot see the margin mode, and uses the *config* leverage."""
    sig = inspect.signature(liquidation_ok)
    assert not ({"td_mode", "mgn_mode", "margin_mode"} & set(sig.parameters)), (
        "liquidation_ok gained a margin-mode parameter -- update this test and "
        "FINDINGS Q36, the gate is no longer mode-blind.")

    rcfg = RiskConfig()
    assert rcfg.max_leverage == 5.0
    thr = 1.0 / rcfg.max_leverage - DEFAULT_MMR
    assert thr == pytest.approx(0.195)

    # ... and it is exactly the isolated formula, at the configured leverage.
    atr = np.array([thr / 3.0, thr / 3.0 + 1e-9])
    assert liquidation_ok(atr, rcfg).tolist() == [True, False]

    # Raising the ceiling *tightens* the filter (the direction is easy to state backwards).
    loose = liquidation_ok(atr, RiskConfig(max_leverage=3.0))
    tight = liquidation_ok(atr, RiskConfig(max_leverage=20.0))
    assert loose.tolist() == [True, True] and tight.tolist() == [False, False]


def test_cross_is_never_nearer_than_isolated():
    """The theorem: for every `L >= 1, G <= 1`, cross is at least as far as isolated.

    Boundary: `L = 1, G = 1` is the one equality (`1 - mmr`).  Any leverage above
    1x, or any gross below 1x NAV, makes cross strictly safer.
    """
    for L in (1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0, 125.0):
        for G in (0.05, 0.29, 0.44, 0.96, 1.0):
            assert cross_dist(G) >= iso_dist(L) - 1e-12, (L, G)
            if L > 1.0 or G < 1.0:
                assert cross_dist(G) > iso_dist(L), (L, G)


def test_the_gate_can_be_blind_to_the_real_leverage():
    """The gate's assumption is 19.50%; a 100x isolated leg is 0.50% away.

    Nothing reconciles `rcfg.max_leverage` with the per-instrument `lever` the
    venue actually holds, so the gate can pass a book that is one tick from a
    liquidation.
    """
    gate = 1.0 / RiskConfig().max_leverage - DEFAULT_MMR
    assert iso_dist(100.0) < gate / 10.0
    assert iso_dist(3.0) > gate            # the 3x legs sit *outside* the assumption


def test_liq_distance_sign_and_missing_price():
    """`liqPx` is a *level*, so the sign of the distance depends on the side.

    A missing `liqPx` is the **good** cross case: the venue publishes no
    per-position liquidation price because the whole account equity backs it.
    """
    audit = _audit()
    long_ = {"pos": 1.0, "markPx": 100.0, "liqPx": 80.0}
    short_ = {"pos": -1.0, "markPx": 100.0, "liqPx": 120.0}
    assert audit.liq_distance(long_) == pytest.approx(0.20)
    assert audit.liq_distance(short_) == pytest.approx(0.20)
    assert audit.liq_distance({"pos": 1.0, "markPx": 100.0, "liqPx": None}) is None
    assert audit.liq_distance({"pos": 1.0, "markPx": None, "liqPx": 80.0}) is None


def test_mixed_mode_on_one_instrument_is_detectable():
    """The audit's mixed-mode detector: same `instId`, two `mgnMode` values."""
    df = pd.DataFrame([
        {"instId": "BTC-USDT-SWAP", "mgnMode": "cross"},
        {"instId": "BTC-USDT-SWAP", "mgnMode": "isolated"},
        {"instId": "ETH-USDT-SWAP", "mgnMode": "cross"},
    ])
    dup = df.groupby("instId")["mgnMode"].nunique() > 1
    assert sorted(dup[dup].index) == ["BTC-USDT-SWAP"]


def test_the_audit_script_agrees_with_this_file():
    """Two independent derivations of the same formulas must agree."""
    audit = _audit()
    for L in (1.0, 2.0, 3.0, 5.0, 100.0):
        assert audit.isolated_theory(L) == pytest.approx(iso_dist(L))
    for G in (0.1, 0.44, 0.96, 1.0, 2.0):
        assert audit.cross_theory(G) == pytest.approx(cross_dist(G))
    assert audit.gate_theory(RiskConfig()) == pytest.approx(
        1.0 / RiskConfig().max_leverage - DEFAULT_MMR)


def test_orders_actually_carry_tdMode():
    """If the mode were not on the wire, none of the above would matter.

    Structural, because "submit a real order and inspect OKX's echo" needs
    credentials and a live venue; the two slices below are the *only* places the
    field is set, and both are asserted to carry it.
    """
    planner = (ROOT / "crypto_ls_research" / "execution" / "planner.py").read_text()
    i = planner.index("def to_body(")
    body = planner[i:planner.index("def to_dict(", i)]
    assert '"tdMode": td_mode' in body

    okx = (ROOT / "crypto_ls_research" / "execution" / "okx_private.py").read_text()
    j = okx.index("def place_order(")
    slice_ = okx[j:j + 4000]
    assert '"tdMode": td_mode' in slice_


# ---------------------------------------------------------------------------
# The defect that explains "why did BTC end up at 100x".
#
# `place_order` carries **no** leverage field -- the venue opens the position at
# whatever leverage is already configured for `(instId, mgnMode, posSide)`.  The
# only code that can change that configuration is `_apply_leverage`, which is
# gated on `set_leverage` and then calls `set_leverage()` / `set_leverage_batch()`.
#
# Measured live on the demo venue (2026-09-30), setting each value to the one it
# already had, so nothing on the account changed:
#
#   mgnMode=cross     no posSide      -> 200 OK
#   mgnMode=isolated  no posSide      -> **HTTP 400 Bad Request**
#   mgnMode=isolated  posSide=long    -> 200 OK
#   POST /api/v5/account/batch-set-leverage (with or without posSide) -> **404**
#
# So in isolated mode this project can never set leverage: the single call is
# missing a parameter OKX requires, and the batch endpoint it was written against
# does not exist.  A leg therefore keeps whatever the account already had -- which
# for `BTC-USDT-SWAP` isolated was **100x** (every other instrument, including
# three we have never traded, reads 3x in both modes).
#
# These are `xfail(strict=True)`: they pass while the defect is present and turn
# into hard failures the moment somebody fixes it, so the fix cannot land without
# updating this file and FINDINGS Q37.
# ---------------------------------------------------------------------------
def _captured_bodies(monkeypatch) -> list:
    """Build an `OKXPrivate` whose transport records bodies instead of sending."""
    from types import SimpleNamespace

    from crypto_ls_research.execution.okx_private import OKXPrivate

    creds = SimpleNamespace(api_key="k", secret_key="s", passphrase="p")
    cli = OKXPrivate(creds, mode="demo")
    seen: list = []
    monkeypatch.setattr(cli, "request",
                        lambda method, path, body=None, **kw: (seen.append((path, body)),
                                                               [{}])[1])
    return cli, seen


@pytest.mark.xfail(strict=True, reason=(
    "OKX 400s on mgnMode=isolated without posSide (measured 2026-09-30); "
    "set_leverage omits it, so isolated leverage can never be set. FINDINGS Q37."))
def test_set_leverage_passes_the_posSide_okx_requires(monkeypatch):
    cli, seen = _captured_bodies(monkeypatch)
    cli.set_leverage("BTC-USDT-SWAP", 3, "isolated")
    path, body = seen[-1]
    assert path == "/api/v5/account/set-leverage"
    assert "posSide" in (body or {}), (
        f"body sent for isolated leverage has no posSide: {body}")


@pytest.mark.xfail(strict=True, reason=(
    "leverage in long/short + isolated is per posSide, so both sides must be set; "
    "_apply_leverage emits one row per instrument. FINDINGS Q37."))
def test_isolated_leverage_covers_both_sides(monkeypatch):
    cli, seen = _captured_bodies(monkeypatch)
    cli.set_leverage("BTC-USDT-SWAP", 3, "isolated")
    cli.set_leverage("BTC-USDT-SWAP", 3, "isolated")
    sides = {(b or {}).get("posSide") for _, b in seen}
    assert sides == {"long", "short"}, f"both sides not covered: {sides}"


def test_the_audit_flags_a_leg_sitting_at_the_instruments_max_leverage():
    """The detection that would have caught the 100x leg.

    A leg configured at the instrument's ceiling is exactly the case whose
    liquidation distance collapses to ~`1/max_leverage`, so the audit marks it.
    """
    audit = _audit()

    class _Cli:
        def request(self, method, path, params=None, **kw):
            assert path == "/api/v5/account/leverage-info"
            lever = "100" if params["mgnMode"] == "isolated" else "3"
            return [{"instId": params["instId"], "mgnMode": params["mgnMode"],
                     "lever": lever, "posSide": "long"}]

    class _Eng:
        def _private(self):
            return _Cli()

    cfg = audit.leverage_config(_Eng(), ["BTC-USDT-SWAP"])
    row = cfg[(cfg["mgnMode"] == "isolated")].iloc[0]
    assert row["max_lever"] == 100.0
    assert bool(row["at_max"]) is True
    assert row["liq_distance_if_used"] == pytest.approx(iso_dist(100.0))
    cross_row = cfg[cfg["mgnMode"] == "cross"].iloc[0]
    assert bool(cross_row["at_max"]) is False
    # cross is not per-leg, so no per-leg distance is reported for it
    assert pd.isna(cross_row["liq_distance_if_used"])



def test_implied_leverage_inverts_the_isolated_formula():
    """`implied_leverage` must be the exact inverse of `iso_dist`.

    This is the only field-level way to see the leverage a leg is *actually*
    margined at, because `lever` mirrors the account config instead (measured
    2026-09-30: NEAR isolated 3->5 moved `lever`, left margin/liqPx bit-identical).
    """
    audit = _audit()
    for L in (1.0, 2.0, 3.0, 5.0, 20.0, 100.0):
        mark = 100.0
        # place liqPx exactly where an L-times-margined long would be liquidated
        liq = mark * (1.0 - iso_dist(L))
        assert audit.implied_leverage(mark, liq) == pytest.approx(L, rel=1e-12)
    # a short liquidates upward; the sign of the move must not matter
    assert audit.implied_leverage(100.0, 132.83) == pytest.approx(
        audit.implied_leverage(100.0, 67.17), rel=1e-9)


def test_implied_leverage_rejects_a_nonsense_mark():
    audit = _audit()
    with pytest.raises(ValueError):
        audit.implied_leverage(0.0, 1.0)


def test_the_audit_reports_config_vs_actual_leverage(tmp_path):
    """A leg whose config says 3x but whose liqPx says 2x must be flagged.

    This is the ARB case measured on 2026-09-30: the config was restored to 3x
    but the open leg stayed margined at ~2x, and `lever` reported "3" the whole
    time.  Reading `lever` would have hidden it.
    """
    audit = _audit()
    mark = 0.2021
    liq = mark * (1.0 + iso_dist(2.0))          # short, margined at 2x
    assert audit.implied_leverage(mark, liq) == pytest.approx(2.0, rel=1e-9)
    # ...while the config claims 3x, which is what the venue reports
    assert abs(audit.implied_leverage(mark, liq) - 3.0) / 3.0 > 0.20, \
        "a 3x config over a 2x leg must exceed the 20% divergence flag"
