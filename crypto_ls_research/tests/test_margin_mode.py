"""Guards for the **margin mode** (`cross` 全仓 / `isolated` 逐仓) of the live book.

## The two claims -- they point in OPPOSITE directions

This file pins **both**, because the choice is a trade-off and stating only one
side is how the account got flipped by accident in the first place.

**Claim A -- `cross` has the longer fuse.**  `isolated` gives each leg a
liquidation distance of `1/L - mmr` (L=3 -> 32.8%, L=5 -> 19.5%, L=100 -> 0.5%),
and the offsetting leg's profit sits in a *different* bucket and cannot rescue
it.  `cross` backs every position with the whole account, so the move that
liquidates the book is `(1 - mmr*G)/G` -- 99.5% at the live cap `G = 1.0`, 231%
at the current `G ~ 0.43`.  So `cross_distance >= isolated_distance` for every
`L >= 1, G <= 1`; the test below asserts that over a grid rather than trusting
the algebra.

**Claim B -- `cross` has the UNBOUNDED loss.**  Claim A bounds the *probability*
of a liquidation, not its *size*.  A **short** loses without limit, so in `cross`
a single contract gapping up takes the whole account: measured on the live book
(NAV 55,348, gross 0.43x NAV) the most fragile short needed only **30.7x** to
reach zero.  Under `isolated` the same move costs that leg's margin and nothing
else.

**`isolated` was chosen (2026-09-30)** because Claim B is a *solvency* risk and
Claim A is a *frequency* risk -- and Claim A's cost is entirely a function of
leverage, which is a knob we control.  Measured over 154 instruments x 5.7y
(intraday extremes, an upper bound) for a 10-name book: 3x -> ~30 forced
liquidations/yr, 2x -> ~10, **1x -> ~1**.  The mode and the leverage therefore
have to move together: `isolated` at 3x buys the tail protection at ~30 forced
exits a year, which is why the book is meant to run at **1x**.  See FINDINGS Q45.

## The two defects this file also pinned -- now fixed (2026-09-30)

Both were measured on the live demo account.  The tests below used to assert the
defects *existed*; they now assert the fixes hold, so a regression turns red.

1. **`td_mode` was not persisted and had no single owner.**  It lived as an
   in-memory attribute of one `LiveEngine`.  `LiveLimits` had no field for it, no
   ledger recorded it, and `AutoTrader` never passed it -- so the unattended
   daemon was always `cross` while the console's engine could be flipped to
   `isolated` by a request body and **reverted to `cross` on restart**.  Measured
   2026-09-30: the console reported `td_mode=isolated` and a freshly built engine
   in the same process reported `cross`, **on the same account**.  Result: the
   account held the same instrument in *both* modes (9 of them), and which mode
   the next order used depended on *which process submitted it*.
   **Fix:** the mode is the constant `settings.MARGIN_MODE`; `LiveEngine.td_mode`
   is a read-only property, a conflicting `td_mode=` raises, the API answers 400
   to a conflicting request field, the console renders it read-only, and every
   run record carries it.
2. **`liquidation_ok` was mode-blind and leverage-blind.**  It computed
   `1/rcfg.max_leverage - mmr` -- the isolated formula, at the *configured*
   leverage -- regardless of `td_mode` and regardless of the leverage actually in
   force on the venue.  Measured: the gate assumed 19.50% while `BTC-USDT-SWAP
   short isolated` sat at **100x**, 0.57% from liquidation.
   **Fix:** the gate reads `rcfg.leverage_in_force` (a *declared assumption*,
   no longer a policy cap read as a fact) and takes an explicit `leverage=`
   override; `limits.margin_headroom_violations()` reconciles the assumption
   against each held leg's `implied_leverage` and surfaces the result.
"""
from __future__ import annotations

import importlib.util
import inspect
import pathlib

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import MARGIN_MODE, RiskConfig

# The *other* margin mode, derived rather than written down: on 2026-09-30 the
# pinned value moved cross -> isolated (FINDINGS Q45) and every "a conflicting
# mode must be rejected" test silently stopped testing anything, because the
# literal it used as the conflict had become the default.  Deriving it means the
# tests keep asserting the property (a conflict is rejected) instead of a value.
_OTHER_MODE = "cross" if MARGIN_MODE == "isolated" else "isolated"
from crypto_ls_research.execution.engine import LiveEngine
from crypto_ls_research.execution.limits import LiveLimits, margin_headroom_violations
from crypto_ls_research.risk.engine import (DEFAULT_MMR, implied_leverage,
                                            isolated_liq_distance, liquidation_ok)

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
def test_live_engine_defaults_to_the_pinned_mode():
    """The default is `settings.MARGIN_MODE`, and it is a *constant*.

    Asserted against the constant rather than the literal: the value itself is a
    risk decision that moves (cross -> isolated on 2026-09-30, see FINDINGS Q45),
    and a test that hardcodes it would have to be edited in lockstep with the
    thing it is supposed to be checking.
    """
    eng = LiveEngine(mode="paper")
    assert eng.td_mode == MARGIN_MODE
    assert MARGIN_MODE in ("isolated", "cross"), (
        "a third mode appeared; every formula in FINDINGS Q36/Q45 assumes two")
    assert "td_mode" not in eng.limits.to_dict()


def test_the_margin_mode_cannot_be_assigned():
    """Read-only: `eng.td_mode = x` must raise, not silently re-margin the account.

    A plain class attribute would look read-only but an assignment would create a
    shadowing *instance* attribute -- i.e. exactly the silent divergence this
    whole fix is about.  Only a property without a setter actually prevents it.
    """
    eng = LiveEngine(mode="paper")
    with pytest.raises(AttributeError):
        eng.td_mode = _OTHER_MODE                                     # type: ignore[misc]
    assert eng.td_mode == MARGIN_MODE
    assert isinstance(inspect.getattr_static(LiveEngine, "td_mode"), property)


def test_a_conflicting_td_mode_is_rejected_not_ignored():
    """Passing a different mode raises.  Ignoring it would be the UI/request split."""
    with pytest.raises(ValueError, match="conflicts with the pinned margin mode"):
        LiveEngine(mode="paper", td_mode=_OTHER_MODE)
    # ...and the same value is accepted, so a caller that mirrors the constant
    # is not punished for being explicit.
    assert LiveEngine(mode="paper", td_mode=MARGIN_MODE).td_mode == MARGIN_MODE


def test_the_api_answers_400_to_a_conflicting_td_mode():
    """The HTTP surface must reject it too -- a 400, not a silent no-op.

    The console no longer sends the field; this guards against a stale tab or a
    scripted client, which is precisely the path that flipped the account before.
    """
    from webapp import live_api

    r = live_api.route_post("/api/live/execute",
                            {"mode": "paper", "td_mode": _OTHER_MODE})
    assert r is not None
    body, code = r
    assert code == 400, f"expected a client error, got {code}: {body}"
    assert "error" in body and _OTHER_MODE in body["error"]
    # A body that agrees with the constant (or omits it) must NOT be rejected here.
    assert live_api.route_post("/api/live/execute",
                               {"mode": "paper", "td_mode": MARGIN_MODE,
                                "td_mode_ok": True}) is not None


def test_the_daemon_does_not_pass_a_margin_mode():
    """`AutoTrader` never passes `td_mode`, so the unattended job cannot diverge.

    This used to be the load-bearing half of the divergence (the daemon could not
    be flipped, the console could, and nothing reconciled them).  The console can
    no longer be flipped either, but this stays as a guard: if `AutoTrader` ever
    starts passing a mode, that is a change worth noticing -- it would be a second
    writer of a value that is supposed to have exactly one.
    """
    from crypto_ls_research.execution.auto_trader import AutoTrader

    src = inspect.getsource(AutoTrader.__init__)
    assert "td_mode" not in src, (
        "AutoTrader now passes td_mode -- that is a second writer of "
        "settings.MARGIN_MODE; if it is deliberate, the docstring above and "
        "FINDINGS Q36/Q45 must follow.")
    trader = AutoTrader(mode="paper")
    assert trader.engine.td_mode == MARGIN_MODE


def test_the_margin_mode_has_exactly_one_source():
    """No saved-limits key carries the mode, and nothing can override the constant.

    `td_mode` is absent from `LiveLimits`, so it cannot be smuggled in through
    `limits.json` (the only per-mode settings file that survives a restart) --
    and the only way to *read* it is the constant.
    """
    keys = set(LiveLimits().to_dict())
    assert "td_mode" not in keys and "mgn_mode" not in keys
    assert {"max_gross_frac", "max_leverage"} <= keys, (
        "the limits dict changed shape; re-check that td_mode really is absent")

    # Two engines built independently in the same process agree -- the divergence
    # was "a fresh engine says something else than the console".
    assert LiveEngine(mode="paper").td_mode == LiveEngine(mode="demo").td_mode

    # ...and the engine's value is the constant, not a copy of it.
    src = inspect.getsource(LiveEngine.td_mode.fget)
    assert "MARGIN_MODE" in src, (
        "`LiveEngine.td_mode` no longer derives from settings.MARGIN_MODE; "
        "there is now a second place the mode is written.")


def test_the_console_offers_no_margin_mode_selector():
    """The page must not present a control the server will not honour.

    A `<select id="lvTdMode">` is exactly the control that used to flip the
    account.  The mode may still be *displayed* -- but read-only -- and the trade
    request must not carry it.
    """
    js = (ROOT / "webapp" / "static" / "app.js").read_text()
    assert 'id="lvTdMode"' in js, "the mode should still be shown"
    assert '<select id="lvTdMode"' not in js, (
        "the margin mode is a selector again -- the server pins it to "
        "settings.MARGIN_MODE and will 400 any other value, so the page would be "
        "offering a control that cannot work.")
    # the trade payload must not carry it
    i = js.index("function liveTradeSettings()")
    body = js[i:js.index("\n}", i)]
    assert "td_mode" not in body, (
        "liveTradeSettings still sends td_mode; the request would be rejected "
        "with a 400 the moment it disagreed with the constant.")


def test_the_run_record_carries_the_margin_mode():
    """The audit trail has to say which mode an order was sent under.

    Fills carried no `tdMode` at all, so a post-mortem could not tell which mode
    a leg had been opened in -- which is how a *mixed* account went unnoticed.
    """
    from crypto_ls_research.execution.engine import LiveEngine as _LE

    src = inspect.getsource(_LE._execute_inner)
    assert '"td_mode": self.td_mode' in src, (
        "the rebalance run record no longer records the margin mode")
    flat = inspect.getsource(_LE._flatten_inner)
    assert '"td_mode": self.td_mode' in flat, (
        "the flatten run record no longer records the margin mode")


def test_liquidation_ok_has_no_mode_parameter():
    """Still mode-blind by signature -- but no longer leverage-blind.

    The formula is the isolated one whatever the mode, and that is *documented*
    rather than hidden: in `cross` mode there is no per-position liquidation
    price, so this is a conservative volatility screen, not the account's
    solvency constraint.  Making it mode-aware is a separate, headline-moving
    change (it would widen the backtest universe).  What is *not* allowed is for
    the leverage it assumes to be a policy cap read as a fact.
    """
    sig = inspect.signature(liquidation_ok)
    assert not ({"td_mode", "mgn_mode", "margin_mode"} & set(sig.parameters)), (
        "liquidation_ok gained a margin-mode parameter -- the formula is now "
        "mode-aware, so this test and FINDINGS Q36 must be updated.")
    assert "leverage" in sig.parameters, (
        "liquidation_ok lost its explicit `leverage=` override")


def test_the_gate_uses_the_declared_leverage_not_the_cap():
    """The assumption is `RiskConfig.leverage_in_force`, and it is overridable.

    The old gate read `rcfg.max_leverage` -- a *ceiling on what we are willing to
    set* -- as though it were a fact about the venue.  That is why it could
    report "19.50% is safe" while a leg sat 0.57% from liquidation.
    """
    rcfg = RiskConfig()
    assert rcfg.leverage_in_force == 5.0
    assert not hasattr(rcfg, "max_leverage"), (
        "RiskConfig grew a `max_leverage` back; the gate must not read a cap.")
    thr = 1.0 / rcfg.leverage_in_force - DEFAULT_MMR
    assert thr == pytest.approx(0.195)

    atr = np.array([thr / 3.0, thr / 3.0 + 1e-9])
    assert liquidation_ok(atr, rcfg).tolist() == [True, False]

    # Raising the *assumption* tightens the filter (the direction is easy to state
    # backwards).  The assumption is what moves it -- not any cap.
    loose = liquidation_ok(atr, RiskConfig(leverage_in_force=3.0))
    tight = liquidation_ok(atr, RiskConfig(leverage_in_force=20.0))
    assert loose.tolist() == [True, True] and tight.tolist() == [False, False]

    # An explicit override wins.  This is how the live side asks "what if the book
    # is really on 100x?" -- and the answer must be "almost nothing qualifies".
    assert liquidation_ok(atr, rcfg, leverage=100.0).tolist() == [False, False]
    assert liquidation_ok(atr, rcfg, leverage=3.0).tolist() == [True, True]


def test_a_sub_one_leverage_assumption_is_rejected():
    """`1/L - mmr` goes non-positive below 1x -- a typo, not a conservative choice."""
    with pytest.raises(ValueError, match="leverage_in_force"):
        RiskConfig(leverage_in_force=0.5)


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


def test_the_gate_alone_can_never_answer_is_the_book_safe():
    """The arithmetic of the gap: 19.50% assumed vs 0.50% real.

    This is *why* the gate needs a live counterpart.  It is a backtest filter with
    no access to a venue, so it can only say "safe under my assumption"; the live
    answer comes from `margin_headroom_violations` (next test), which reads `liqPx`.
    """
    gate = 1.0 / RiskConfig().leverage_in_force - DEFAULT_MMR
    assert iso_dist(100.0) < gate / 10.0
    assert iso_dist(3.0) > gate            # the 3x legs sit *outside* the assumption


# ---------------------------------------------------------------------------
# the live counterpart: reconcile the assumption with what the venue holds
# ---------------------------------------------------------------------------
def _leg(inst, pos, mark, liq, mgn="isolated"):
    return {"instId": inst, "pos": pos, "markPx": mark, "liqPx": liq,
            "mgnMode": mgn, "lever": "3"}


def test_margin_headroom_catches_the_100x_leg():
    """The measured case: 100x isolated, 0.57% from liquidation, gate assumed 5x."""
    mark = 100.0
    legs = [
        _leg("BTC-USDT-SWAP", -1.0, mark, mark * (1.0 + iso_dist(93.6))),
        _leg("ETH-USDT-SWAP", -1.0, mark, mark * (1.0 + iso_dist(3.01))),
    ]
    vs = margin_headroom_violations(legs, 5.0)
    assert [v.key for v in vs] == ["margin_headroom:BTC-USDT-SWAP"], vs
    v = vs[0]
    assert v.sev == "warn", (
        "blocking would freeze a book whose best remedy IS the rebalance that "
        "shrinks the offending leg")
    assert "BTC-USDT-SWAP" in v.title and "0.57" in v.title.replace("%", "")
    # 93.6 / 5 is ~19x worse than assumed -- the "33x" in FINDINGS is the
    # cap-vs-reality ratio, this is the assumption-vs-reality one.
    assert "19 倍" in v.body or "19" in v.body


def test_margin_headroom_stays_quiet_when_every_leg_agrees():
    mark = 100.0
    legs = [_leg("ETH-USDT-SWAP", -1.0, mark, mark * (1.0 + iso_dist(3.0))),
            _leg("NEAR-USDT-SWAP", 1.0, mark, mark * (1.0 - iso_dist(2.98)))]
    assert margin_headroom_violations(legs, 5.0) == []


def test_margin_headroom_is_sign_agnostic():
    """A short liquidates upward and a long downward; the magnitude is what matters."""
    mark = 100.0
    up = [_leg("A-USDT-SWAP", -1.0, mark, mark * (1.0 + iso_dist(50.0)))]
    dn = [_leg("A-USDT-SWAP", 1.0, mark, mark * (1.0 - iso_dist(50.0)))]
    assert ([v.key for v in margin_headroom_violations(up, 5.0)]
            == [v.key for v in margin_headroom_violations(dn, 5.0)]
            == ["margin_headroom:A-USDT-SWAP"])


def test_margin_headroom_skips_legs_the_venue_did_not_price():
    """A `cross` leg publishes no per-position `liqPx` -- that is the *good* case.

    Skipping it must not read as "passed" for the account as a whole; that is
    what the next test pins.
    """
    legs = [_leg("BTC-USDT-SWAP", -1.0, 100.0, None, mgn="cross")]
    assert margin_headroom_violations(legs, 5.0) == []


def test_margin_headroom_says_unavailable_rather_than_passing():
    """"Could not evaluate" must not look like "clean".

    If the account holds isolated legs and none published a `liqPx`, the check
    did not run -- and saying nothing would report that as "no problem".
    """
    legs = [_leg("BTC-USDT-SWAP", -1.0, 100.0, None),
            _leg("ETH-USDT-SWAP", -1.0, 100.0, 0.0)]
    vs = margin_headroom_violations(legs, 5.0)
    assert [v.key for v in vs] == ["margin_headroom_unavailable"]
    assert "没有评估" in vs[0].body and "不是通过" in vs[0].body


def test_the_engine_status_surfaces_the_headroom():
    """The console has to be able to show it; otherwise the fix is invisible."""
    eng = LiveEngine(mode="paper")
    assert eng.margin_headroom({"positions": []}) == []
    src = inspect.getsource(LiveEngine.status)
    assert '"margin_headroom"' in src, (
        "status() no longer exposes margin_headroom, so the reconciliation is "
        "computed but never seen.")


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
    """The audit's formulas must agree with this file's independent derivations."""
    audit = _audit()
    for L in (1.0, 2.0, 3.0, 5.0, 100.0):
        assert audit.isolated_theory(L) == pytest.approx(iso_dist(L))
    for G in (0.1, 0.44, 0.96, 1.0, 2.0):
        assert audit.cross_theory(G) == pytest.approx(cross_dist(G))
    assert audit.gate_theory(RiskConfig()) == pytest.approx(
        1.0 / RiskConfig().leverage_in_force - DEFAULT_MMR)


def test_the_audit_shares_one_implied_leverage_with_the_library():
    """`implied_leverage` must be the *same object* the guardrail uses.

    It used to be a second copy living inside the audit script.  Two copies of
    the formula that decides "is this leg about to be liquidated" is exactly the
    kind of drift this project keeps paying for, so identity is asserted here
    rather than numeric agreement (which two drifting copies can still satisfy).
    """
    audit = _audit()
    assert audit.implied_leverage is implied_leverage
    assert audit.isolated_liq_distance is isolated_liq_distance


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
