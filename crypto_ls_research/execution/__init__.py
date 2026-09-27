"""Order execution layer: turn the research book into real orders on OKX.

Three modes, and the mode is carried explicitly all the way down -- never
inferred:

    paper  本地纸面   no API key at all.  Fills are simulated against the live
                      public last price using the *same* CostConfig the
                      backtest uses, so the whole loop (signal -> plan ->
                      execute -> reconcile -> P&L) is exercisable today, on
                      this machine, with zero exchange access.
    demo   OKX 模拟盘  the real OKX matching engine, but a simulated balance.
                      Requires **demo-specific** API keys and the
                      `x-simulated-trading: 1` header.  Live keys do NOT
                      authenticate here, and the failure (`50111 Invalid
                      OK-ACCESS-KEY`) reads like a typo rather than a
                      mis-selected environment -- hence the explicit mode.
    live   实盘        real money.  Same client, header omitted.  Gated behind
                      extra confirmations and tighter defaults.

The single most important design rule in this package: **the target book is
produced by running the very same `backtest.engine.run_backtest` that produced
the reported numbers**, on a panel that ends at the latest closed bar.  The
live target is the last rebalance's target vector.  Re-deriving the signal in
a second code path is how a live system silently stops trading the strategy it
was validated on.
"""
from __future__ import annotations

from .credentials import (  # noqa: F401
    Creds, creds_status, kill_switch_on, kill_switch_path, load_creds,
    save_creds, set_kill_switch,
)

__all__ = [
    "Creds", "creds_status", "load_creds", "save_creds",
    "kill_switch_on", "kill_switch_path", "set_kill_switch",
]
