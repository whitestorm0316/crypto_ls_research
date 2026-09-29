"""The accepted rebalance grid has exactly one source.

Why this file exists
--------------------
`config.settings.ACCEPTED_REBALANCE_DAYS` is read by four surfaces that each
used to carry their own literal:

* `execution.engine.DEFAULT_SIGNAL` -- the strategy the trade desk plans on,
* the CLI defaults of `auto_trader` / `run_live` / `auto_ctl` -- the grid the
  daemon runs on when nobody overrides it,
* `webapp.spec.OPTIMAL_CLI` -- what the console calls 「已验收最优」,
* the acceptance run in `artifacts/<OPTIMAL_TAG>/`.

When they disagreed, nothing raised.  `DEFAULT_SIGNAL` said 3 days while the
daemon had been started with `--rebalance-days 1`, so `/api/live/plan` produced
a **3-day** plan and the console printed 「下次调仓」 three days out.  The page
was healthy, no traceback anywhere -- every number on it simply described a book
nobody was trading.

So these assertions are about **agreement**, not about the value: they must stay
green when the basis is deliberately re-designated (that happened 2026-09-29,
3 days -> 1 day, and the point of the change was that the acceptance should
describe the *running* configuration), and go red when one surface drifts.

Note what is deliberately *not* done here: reading `DEFAULT_SIGNAL` and
comparing it to itself.  The CLI defaults are exercised through `main()`,
because the defect was a default that disagreed with the value the daemon was
launched with -- an inspection of module attributes cannot see that.
"""
from __future__ import annotations

import os

import pytest

from crypto_ls_research.config.settings import ACCEPTED_REBALANCE_DAYS
from crypto_ls_research.execution import auto_ctl
from crypto_ls_research.execution import auto_trader as at_mod
from crypto_ls_research.execution import run_live as rl_mod
from crypto_ls_research.execution import store as store_mod
from crypto_ls_research.execution.engine import DEFAULT_SIGNAL, LiveEngine

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))


@pytest.fixture
def tmp_live(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


class _Recorder:
    """Stands in for an engine constructor and remembers what the CLI asked for.

    `auto_trader.main` and `run_live.main` both call their engine with keyword
    arguments only, so one recorder serves both.  It also implements the two
    methods `main` touches afterwards, so the CLI runs to completion without
    touching the network.
    """

    def __init__(self, seen: dict, payload: dict):
        self._seen, self._payload = seen, payload

    def __call__(self, **kw):
        self._seen.update(kw)
        return self

    def once(self):                 # `auto_trader --once`
        return self._payload

    def status(self):               # `run_live --action status`
        return {}


# ---------------------------------------------------------------------------
# the desk and the daemon must agree -- this is the pair that actually broke
# ---------------------------------------------------------------------------
def test_the_desk_plans_on_the_accepted_grid(tmp_live):
    """`LiveEngine.signal_kwargs` is what `/api/live/plan` and `preview()` use.

    If this drifts, the console shows a plan for a strategy the daemon is not
    running -- and the only symptom is that 「下次调仓」 is the wrong number of
    days away, which looks like a working system.
    """
    eng = LiveEngine(mode="paper")
    assert eng.signal_kwargs["rebalance_days"] == ACCEPTED_REBALANCE_DAYS


def test_the_daemon_defaults_to_the_accepted_grid(tmp_live):
    """`AutoTrader` with no explicit grid == whatever `DEFAULT_SIGNAL` says.

    No engine is injected: the point is the grid the bot picks *and* the grid
    the engine it builds for itself will plan on.  Injecting a fake would let
    the two disagree without this test noticing.
    """
    bot = at_mod.AutoTrader(mode="paper")
    assert bot.rebalance_days == float(ACCEPTED_REBALANCE_DAYS)
    assert bot.engine.signal_kwargs["rebalance_days"] == ACCEPTED_REBALANCE_DAYS


# ---------------------------------------------------------------------------
# the CLI defaults, exercised through `main()` rather than inspected
# ---------------------------------------------------------------------------
def test_the_auto_trader_cli_default_is_the_accepted_grid(tmp_live, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(at_mod, "AutoTrader",
                        _Recorder(seen, {"action": "skip"}))
    rc = at_mod.main(["--mode", "paper", "--once"])
    assert rc == 0
    assert seen.get("rebalance_days") == ACCEPTED_REBALANCE_DAYS, (
        "`--rebalance-days` 的默认值不是已验收口径；"
        f"拿到的是 {seen.get('rebalance_days')!r}")


def test_the_live_cli_default_is_the_accepted_grid(tmp_live, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rl_mod, "LiveEngine", _Recorder(seen, {}))
    rc = rl_mod.main(["--action", "status"])
    assert rc == 0
    kw = seen.get("signal_kwargs") or {}
    assert kw.get("rebalance_days") == ACCEPTED_REBALANCE_DAYS, (
        "`run_live --rebalance-days` 的默认值不是已验收口径；"
        f"拿到的是 {kw.get('rebalance_days')!r}")


def test_auto_ctl_start_defaults_to_the_accepted_grid(tmp_live, monkeypatch):
    """Two genuinely different paths, so both are asserted.

    * the **signature default** of `start()` -- what a programmatic caller gets;
    * the **CLI fallback** used by `auto_ctl --mode demo` with no
      `--rebalance-days` and no grid on disk (the command `AUTO_TRADER_README.md`
      §七 documents).  That path resolves through `DEFAULT_SIGNAL`, *not* through
      the signature default, so mutating one leaves the other green -- which is
      exactly what the mutation batch showed when only the CLI half was checked.
    """
    import inspect

    default = inspect.signature(auto_ctl.start).parameters["rebalance_days"].default
    assert default == ACCEPTED_REBALANCE_DAYS, (
        f"`auto_ctl.start` 的签名默认值不是已验收口径：{default!r}")

    seen: dict = {}

    def fake_start(mode, **kw):
        seen.update(kw)
        return {"ok": True, "mode": mode, "ctl": {}}

    monkeypatch.setattr(auto_ctl, "start", fake_start)
    monkeypatch.setattr(auto_ctl, "wait_for_heartbeat",
                        lambda mode, timeout=0.0: {"ok": True, "state": {}})
    rc = auto_ctl.main(["--mode", "demo"])
    assert rc == 0
    assert seen.get("rebalance_days") == ACCEPTED_REBALANCE_DAYS, (
        "`auto_ctl` 的兜底网格不是已验收口径；"
        f"拿到的是 {seen.get('rebalance_days')!r}")


def test_the_console_default_is_the_accepted_grid():
    """The console's own default, plus the argv it builds for a run.

    `build_run_args` is the path that turns 「跑一次」 into an actual backtest,
    so a stale fallback there re-measures a grid the project no longer accepts
    while wearing the accepted tag.
    """
    from webapp import spec as spec_mod

    assert spec_mod.OPTIMAL_CLI["rebalance_days"] == ACCEPTED_REBALANCE_DAYS

    argv = spec_mod.build_run_args({}, dict(spec_mod.OPTIMAL_CLI), ["base"], "t")
    i = argv.index("--rebalance-days")
    assert float(argv[i + 1]) == ACCEPTED_REBALANCE_DAYS

    # The same class of drift applies to the overrides: the desk's plan and the
    # console's "accepted" tag must describe the same portfolio, not just the
    # same grid.
    assert spec_mod.OPTIMAL_OVERRIDES == DEFAULT_SIGNAL["overrides"]


# ---------------------------------------------------------------------------
# nothing else may re-type the number
# ---------------------------------------------------------------------------
def test_the_not_due_message_states_the_actual_grid():
    """The warning that explains a deviation must not use a stale schedule.

    `check_plan` narrated `not_due` as 「策略每 3 天调仓一次」 -- a literal, while
    the desk had moved to a 1-day grid.  So the one message whose job is to say
    *why* this is off-schedule was itself describing a schedule nobody was on,
    and it appeared in the console's plan view.  The grid is now passed in.
    """
    from crypto_ls_research.execution.limits import LiveLimits, check_plan
    from crypto_ls_research.execution.planner import Plan

    plan = Plan(nav=1000.0)
    vs = check_plan(plan, LiveLimits(), mode="paper", nav=1000.0,
                    rebalance_due=False,
                    rebalance_days=ACCEPTED_REBALANCE_DAYS)
    body = next(x for x in vs if x.key == "not_due").body
    assert f"每 {ACCEPTED_REBALANCE_DAYS:g} 天调仓一次" in body, body
    assert "3 天调仓" not in body, body

    # `None` = unknown: say so rather than guess a number.
    body2 = next(x for x in check_plan(plan, LiveLimits(), mode="paper",
                                       nav=1000.0, rebalance_due=False)
                 if x.key == "not_due").body
    assert "按固定网格调仓" in body2 and "天调仓" not in body2, body2


def test_the_grid_is_derived_from_the_target_not_assumed():
    """`grid_days` reads the target's own `rebalance_bars`."""
    from crypto_ls_research.execution.engine import grid_days

    class _T:
        rebalance_bars = 24

    assert grid_days(_T(), "1h") == 1.0
    assert grid_days(_T(), "15m") == 0.25
    assert grid_days(_T(), "nope") is None        # unknown bar -> unknown grid


def _call_args(src: str, name: str) -> list:
    """The argument text of every `name(...)` call in `src` (paren-balanced)."""
    out, i = [], 0
    while True:
        i = src.find(name + "(", i)
        if i < 0:
            return out
        j = src.index("(", i)
        depth, k = 0, j
        while k < len(src):
            if src[k] == "(":
                depth += 1
            elif src[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(src[j + 1:k])
        i = k


def test_every_check_plan_call_site_that_can_warn_is_told_the_grid():
    """Deriving the grid is not enough -- the engine has to *hand it over*.

    `check_plan` falls back to 「按固定网格调仓」 when it is not told, which is
    honest but useless; a call site that forgets the argument looks exactly like
    one that passed the right value.  So this asserts on the *wiring*, for the
    call sites that can actually reach the `not_due` branch -- i.e. the ones
    passing a live `rebalance_due`.  `_flatten_inner` hard-codes
    `rebalance_due=True`, so it can never warn and correctly does not pass a
    grid.

    A structural check, deliberately: the alternative is a live signal plus a
    network round trip to read one sentence.
    """
    with open(os.path.join(ROOT, "crypto_ls_research/execution/engine.py"),
              encoding="utf-8") as f:
        src = f.read()

    calls = _call_args(src, "check_plan")
    dynamic = [c for c in calls if "rebalance_due=target.rebalance_due" in c]
    assert len(dynamic) == 2, (
        f"预期 2 处 check_plan 会传入活的 rebalance_due（preview / execute），"
        f"实际 {len(dynamic)} 处 —— 新增调用点请一并更新这条断言")
    for c in dynamic:
        assert "rebalance_days=grid_days(" in c, (
            "有一处 check_plan 没被告知网格：它会说「按固定网格调仓」，"
            "而不是告诉用户真实的网格。调用参数：\n" + c)


@pytest.mark.parametrize("rel", [
    "webapp/spec.py",
    "scripts/auto_demo.py",
    "crypto_ls_research/execution/engine.py",
    "crypto_ls_research/execution/auto_ctl.py",
])
def test_these_files_derive_the_grid_instead_of_re_typing_it(rel):
    """A literal is how the drift starts, so the derived sites must name the
    constant.  Cheap, and it fails loudly the moment someone "helpfully" pastes
    the number back in.

    `scripts/auto_demo.py` is checked as text rather than imported on purpose:
    importing it executes a daemon-launching script.
    """
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    assert "ACCEPTED_REBALANCE_DAYS" in src, (
        f"{rel} 没有引用 ACCEPTED_REBALANCE_DAYS —— 网格又变成了一个字面量")
