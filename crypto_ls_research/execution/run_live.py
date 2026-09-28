"""Command line for the trading desk.

Same engine the web console drives, so anything the UI can do is reproducible
from a shell -- which matters when the UI is the thing that is broken.

    # what is the strategy asking for right now, and what would we send?
    python -m crypto_ls_research.execution.run_live --mode paper --action preview

    # simulate one full rebalance against live public prices, locally
    python -m crypto_ls_research.execution.run_live --mode paper --action execute

    # OKX demo: requires demo credentials, and does NOT place anything by default
    python -m crypto_ls_research.execution.run_live --mode demo --action preview
    python -m crypto_ls_research.execution.run_live --mode demo --action execute --send

    # emergency exit (allowed off-schedule, still fully guarded)
    python -m crypto_ls_research.execution.run_live --mode demo --action flatten --send
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .credentials import creds_status, set_kill_switch
from .engine import DEFAULT_SIGNAL, LiveEngine
from .limits import LiveLimits


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="crypto LS live trading desk")
    ap.add_argument("--mode", default="paper", choices=["paper", "demo", "live"])
    ap.add_argument("--action", default="status",
                    choices=["status", "preview", "execute", "flatten", "reconcile",
                             "creds", "kill"])
    ap.add_argument("--confirm", default=None,
                    help="live mode only: the exact phrase the server prints")
    ap.add_argument("--send", action="store_true",
                    help="actually send orders (preview/dry-run is the default)")
    ap.add_argument("--force", action="store_true",
                    help="allow executing off-schedule; never relaxes a risk cap")
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to these instIds")
    ap.add_argument("--fresh-signal", action="store_true",
                    help="bypass the signal cache and re-run the backtest")
    ap.add_argument("--limits", default=None, help="JSON dict of LiveLimits fields")
    ap.add_argument("--bar", default=DEFAULT_SIGNAL["bar"])
    ap.add_argument("--rebalance-days", type=float,
                    default=DEFAULT_SIGNAL["rebalance_days"])
    ap.add_argument("--asset-class", default=DEFAULT_SIGNAL["asset_class"])
    ap.add_argument("--ord-type", default="market", choices=["market", "ioc"])
    ap.add_argument("--kill", default=None, choices=["on", "off"])
    a = ap.parse_args(argv)

    if a.action == "creds":
        _print(creds_status())
        return 0
    if a.action == "kill":
        if a.kill is None:
            from .credentials import kill_switch_on, kill_switch_path
            _print({"kill_switch": kill_switch_on(), "path": kill_switch_path()})
        else:
            set_kill_switch(a.kill == "on")
            _print({"kill_switch": a.kill == "on"})
        return 0

    # `None` when `--limits` is absent, NOT `LiveLimits.from_dict(None)`: the
    # latter returns an all-default object, which silently overrode the caps the
    # user saved in the console with the factory values -- the same "the saved
    # setting does not apply" defect the console had.  `None` lets
    # `LiveEngine.__init__` read `limits.json` for the mode.
    limits = LiveLimits.from_dict(json.loads(a.limits)) if a.limits else None
    eng = LiveEngine(
        mode=a.mode, limits=limits, ord_type=a.ord_type,
        signal_kwargs={"bar": a.bar, "rebalance_days": a.rebalance_days,
                       "asset_class": a.asset_class,
                       "overrides": DEFAULT_SIGNAL["overrides"]})

    if a.action == "status":
        _print(eng.status())
    elif a.action == "reconcile":
        _print(eng.reconcile())
    elif a.action == "preview":
        _print(eng.preview(force_signal=a.fresh_signal, only=a.only))
    elif a.action == "execute":
        r = eng.execute(confirm=a.confirm, force=a.force, dry_run=not a.send,
                        only=a.only, force_signal=a.fresh_signal)
        _print(r)
        if r["stage"] in ("blocked", "confirm_failed"):
            return 2
    elif a.action == "flatten":
        _print(eng.flatten(confirm=a.confirm, dry_run=not a.send))
    return 0


if __name__ == "__main__":
    sys.exit(main())
