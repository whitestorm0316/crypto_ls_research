"""Unattended rebalancing: the console's own engine, on a timer.

Why this is a module with a scheduler inside it, rather than a cron entry that
calls `run_live --action execute`
--------------------------------------------------------------------------
`check_plan()` reports an off-schedule rebalance as a **`warn`**, not a `block`
-- `test_off_schedule_is_only_a_warning_and_force_clears_it` pins that contract
deliberately.  So `execute(force=False)` will happily send a rebalance on a
Tuesday.  The rule "off-schedule needs the 强制 box" lives only in the console's
checkbox, not in the engine.

A scheduler that trusted the server to hold that line would rebalance **every
hour** instead of every `rebalance_days`, and the only thing capping the damage
is the 20%/day turnover budget -- which would then be spent on noise.  So the
due-ness gate is implemented here, in code, and asserted by tests.

Guards, in the order they are applied
------------------------------------
1. **kill switch on**      -> skip.  Never trade past the emergency brake.
2. **market data stale**   -> refresh incrementally, re-check; still stale ->
                              skip.  A signal computed off a stale cache is a
                              *wrong* signal, not a late one.
3. **not a rebalance day** -> skip.  (see above)
4. **any `block`**         -> skip.  `force` is never passed, so no cap is ever
                              relaxed to make a trade happen.
5. otherwise               -> execute.

Every decision, **including every skip**, is appended to `auto.jsonl` and
mirrored into `auto.json`, because "it has been running for a week" and "it died
on the first night" look identical from the outside.

The strategy is `engine.DEFAULT_SIGNAL` -- the v3 verified configuration (1h
bars, 3-day rebalance, `range_pos`+`hitrate`, 20% daily turnover throttle).  It
is the same object the console shows as 「当前最优」, so there is no second
definition of "optimal" to drift out of sync.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Optional

from .credentials import kill_switch_on
from .engine import DEFAULT_SIGNAL, LiveEngine
from .limits import LiveLimits, live_confirm_phrase
from .notify import Notifier, notify_for
from .store import RebalanceBusy, Store

#: Below this the loop would hammer the exchange on a typo (`--interval 0.01`).
MIN_INTERVAL_MIN = 1.0


def bar_hours(bar: str) -> float:
    """`"1h"` -> 1.0, `"15m"` -> 0.25.  Unknown shapes fall back to 1h."""
    b = str(bar or "1h").strip().lower()
    try:
        if b.endswith("m"):
            return float(b[:-1]) / 60.0
        if b.endswith("h"):
            return float(b[:-1])
        if b.endswith("d"):
            return float(b[:-1]) * 24.0
    except ValueError:
        pass
    return 1.0


class AutoTrader:
    """One rebalance loop for one mode.  No thread pool, no queue, no retries.

    Deliberately single-threaded and non-retrying: an order path that retries
    on its own is an order path that can double a position.  `AmbiguousError`
    propagates to the caller, is recorded, and the loop simply waits for the
    next tick -- reconciliation is a separate, explicit action.
    """

    STATE_FILE = "auto.json"
    LOG_FILE = "auto.jsonl"

    #: The loop sleeps in slices so SIGTERM is honoured promptly.  A class
    #: attribute rather than a local constant so a test can drive the loop
    #: without waiting a real minute per iteration.
    _SLEEP_SLICE = 5.0

    def __init__(self, mode: str = "demo", interval_min: float = 60.0, *,
                 bar: Optional[str] = None,
                 refresh_after_hours: float = 2.0,
                 dry_run: bool = False,
                 allow_live: bool = False,
                 limits: Optional[LiveLimits] = None,
                 signal_kwargs: Optional[dict] = None,
                 engine: Optional[LiveEngine] = None,
                 notifier: Optional[Notifier] = None,
                 log=None) -> None:
        if mode not in ("paper", "demo", "live"):
            raise ValueError(f"unknown mode {mode!r}")
        if mode == "live" and not allow_live:
            raise PermissionError(
                "live 模式需要显式 --allow-live。自动交易会在无人值守时下真钱单，"
                "这个开关必须由人明确打开一次。")
        self.mode = mode
        self.interval_min = max(MIN_INTERVAL_MIN, float(interval_min))
        self.interval_sec = self.interval_min * 60.0
        self.bar = bar or DEFAULT_SIGNAL["bar"]
        self.refresh_after_hours = float(refresh_after_hours)
        self.dry_run = bool(dry_run)
        self.allow_live = bool(allow_live)
        self.store = Store(mode)
        self._log_fn = log
        # `limits=None` (not `LiveLimits.from_dict(None)`) so the engine reads the
        # caps the user actually saved.  `from_dict(None)` returns an all-default
        # object, which would silently override their saved caps with the factory
        # values -- `run_live.py` still has exactly that bug.
        self.engine = engine or LiveEngine(
            mode=mode, limits=limits,
            signal_kwargs=signal_kwargs or {
                "bar": self.bar,
                "rebalance_days": DEFAULT_SIGNAL["rebalance_days"],
                "asset_class": DEFAULT_SIGNAL["asset_class"],
                "overrides": DEFAULT_SIGNAL["overrides"],
            })
        self.stats = {"checks": 0, "trades": 0, "skips": {}, "errors": 0}
        # Best-effort push (微信/企业微信/webhook).  A missing or broken
        # `config/notify.json` means "off", never a failed start -- the daemon
        # must run whether or not anyone configured notifications.
        self._injected_notifier = notifier is not None
        self.notifier = (notifier if notifier is not None
                         else Notifier.from_config(mode=mode, log=self._log))
        self._notify_mtime = self._config_mtime()
        self.started = time.time()
        self.last: dict = {}

    # -- plumbing ----------------------------------------------------------
    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
        if self._log_fn is not None:
            self._log_fn(line)
        print(line, flush=True)

    def state_path(self) -> str:
        return self.store.path(self.STATE_FILE)

    def log_path(self) -> str:
        return self.store.path(self.LOG_FILE)

    def _config_mtime(self):
        try:
            return os.path.getmtime(self.notifier.config_path())
        except OSError:
            return None

    def _notifier_now(self) -> Notifier:
        """The notifier, re-read from disk whenever `config/notify.json` changes.

        This daemon runs for weeks.  Somebody who configures the webhook *after*
        starting it must not have to know to restart it -- and if they did,
        `--test` (a fresh process) would pass while the daemon stayed silent,
        which reads as "the strategy did nothing".  An injected notifier (tests)
        is used as-is.
        """
        if self._injected_notifier:
            return self.notifier
        mtime = self._config_mtime()
        if mtime != self._notify_mtime:
            self.notifier = Notifier.from_config(mode=self.mode, log=self._log)
            self._notify_mtime = mtime
            nd = self.notifier.describe()
            self._log("通知配置已重新读取："
                      + (f"{nd['provider']} → {nd['url']}"
                         if nd["ready"] else "仍未启用"))
        return self.notifier

    def _record(self, out: dict) -> None:
        """Append the decision, then rewrite the heartbeat.  Both, every time.

        The jsonl is the audit trail (append-only, survives a crash mid-write
        because a partial last line is dropped by the reader).  The json is the
        heartbeat: a single small file a human or the console can read to answer
        "is it alive, and what did it last do".
        """
        self.last = out
        try:
            with open(self.log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
        except OSError as e:                                    # noqa: BLE001
            self._log(f"写审计日志失败（不影响交易）：{e}")
        self._write_state()

    def _write_state(self, enabled: Optional[bool] = None) -> None:
        st = {
            "mode": self.mode,
            "enabled": bool(enabled) if enabled is not None else self._enabled,
            "pid": os.getpid(),
            "interval_min": self.interval_min,
            "bar": self.bar,
            "dry_run": self.dry_run,
            "allow_live": self.allow_live,
            "started": self.started,
            "started_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "now": time.time(),
            "checks": self.stats["checks"],
            "trades": self.stats["trades"],
            "skips": dict(self.stats["skips"]),
            "errors": self.stats["errors"],
            "last": self.last,
            "state_path": self.state_path(),
            "log_path": self.log_path(),
            "kill_switch": kill_switch_on(),
            # So "did I configure notifications, and are they actually wired?" is
            # answerable from the heartbeat instead of from memory.
            "notify": self._notifier_now().describe(),
        }
        try:
            tmp = self.state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.state_path())
        except OSError as e:                                    # noqa: BLE001
            self._log(f"写心跳文件失败：{e}")

    _enabled = False

    # -- guards ------------------------------------------------------------
    def _needs_refresh(self, st: dict) -> bool:
        if self.refresh_after_hours <= 0:
            return False
        ba = st.get("bar_age_hours")
        # `None` = the reference bar could not be read at all.  Refresh rather
        # than assume freshness: "unknown" is not "fine".
        if ba is None:
            return True
        return float(ba) > self.refresh_after_hours

    def _refresh(self) -> dict:
        self._log("行情偏旧，先做增量刷新…")
        try:
            r = LiveEngine.refresh_market_data(bar=self.bar, progress=self._log)
            return {"exit_code": r.get("exit_code"),
                    "bar_age_before": (r.get("staleness_before") or {}).get("bar_age_hours"),
                    "bar_age_after": (r.get("staleness_after") or {}).get("bar_age_hours")}
        except Exception as e:                                  # noqa: BLE001
            # A failed refresh is not fatal on its own -- the staleness check
            # right after it decides whether we may still trade.
            self._log(f"增量刷新失败：{type(e).__name__}: {e}")
            return {"exit_code": None, "error": f"{type(e).__name__}: {e}"}

    def _confirm(self) -> Optional[str]:
        # Live needs the phrase the console prints.  Computed from the same
        # function, so it cannot drift; `--allow-live` is what actually gates it.
        return live_confirm_phrase() if self.mode == "live" else None

    # -- one decision ------------------------------------------------------
    def once(self) -> dict:
        t0 = time.time()
        out: dict = {"ts": t0, "mode": self.mode}
        self._log("—— 检查开始")

        if kill_switch_on():
            return self._finish(out, "skip", "kill_switch", t0)

        st = self.engine.data_staleness()
        if self._needs_refresh(st):
            out["refresh"] = self._refresh()
            st = self.engine.data_staleness()
        out["bar_age_hours"] = st.get("bar_age_hours")
        out["newest_bar"] = st.get("newest_bar")
        if st.get("stale"):
            return self._finish(out, "skip", "data_stale", t0)

        target = self.engine.target(force_signal=False)
        out["decision_ts"] = target.decision_ts
        out["next_decision_ts"] = target.next_decision_ts
        out["rebalance_due"] = bool(target.rebalance_due)
        out["bars_since_decision"] = int(target.bars_since_decision)
        out["signal_from_cache"] = bool(target.from_cache)
        out["target_gross"] = float(target.gross)

        # THE gate the server does not hold.  Without it this loop rebalances
        # every tick; `check_plan` only warns.
        if self.engine.limits.require_rebalance_due and not target.rebalance_due:
            self._log(f"未到调仓日（信号日 {target.decision_ts[:16]}，"
                      f"下次 {target.next_decision_ts[:16]}），不下单")
            return self._finish(out, "skip", "not_due", t0)

        self._log(f"到调仓日（信号日 {target.decision_ts[:16]}），"
                  f"目标毛敞口 {target.gross:.3f}，提交执行…")
        # `force=False` always: the only thing `force` does is silence the
        # off-schedule warning, and we have already decided we are on-schedule.
        try:
            r = self.engine.execute(confirm=self._confirm(), force=False,
                                    dry_run=self.dry_run, only=None,
                                    force_signal=False, progress=self._log)
        except RebalanceBusy as e:
            # The console is mid-rebalance.  Not an error -- the other actor is
            # doing exactly this job -- but it must be visible, because "skipped
            # for two days because a human left a tab executing" is a real
            # failure mode.
            self._log(f"另一进程正在调仓，本轮让路：{e}")
            out["error"] = str(e)
            return self._finish(out, "skip", "busy", t0)
        rec = r.get("record") or {}
        out["stage"] = r.get("stage")
        out["run_id"] = rec.get("run_id")
        out["nav"] = rec.get("nav")
        out["n_orders"] = rec.get("n_orders")
        out["n_ok"] = rec.get("n_ok")
        out["n_fail"] = rec.get("n_fail")
        out["turnover_frac"] = rec.get("turnover_frac")
        out["gross_target"] = rec.get("gross_target")
        out["gross_realised"] = rec.get("gross_realised")
        out["blocked_by"] = [v.get("key") for v in (r.get("violations") or [])
                             if v.get("sev") == "block"]
        # **Why** an order was rejected exists only in this response: a rejected
        # order never enters the venue's order book, so it cannot be looked up
        # afterwards.  Carry the reasons into the notification instead of making
        # the user open the console to find out (they asked exactly that once:
        # 「为什么下了19单 只成交11单」).
        out["failures"] = [
            {"instId": x.get("instId"),
             "code": x.get("sCode") or x.get("code"),
             "error": x.get("error")}
            for x in (rec.get("results") or []) if not x.get("ok")][:6]
        out["warnings"] = ([v.get("title") for v in (r.get("violations") or [])
                            if v.get("sev") == "warn"]
                           + [str(w) for w in ((r.get("plan") or {}).get("warnings") or [])])[:4]
        stage = r.get("stage")
        if stage == "blocked":
            self._log(f"风控闸门拦截，未发送任何订单：{out['blocked_by']}")
            return self._finish(out, "blocked", "risk_gate", t0)
        if stage == "confirm_failed":
            return self._finish(out, "blocked", "confirm_failed", t0)
        if stage == "no_orders":
            return self._finish(out, "noop", "no_orders", t0)
        if stage == "dry_run":
            return self._finish(out, "dry_run", "dry_run", t0)
        self._log(f"执行完成：{out.get('n_ok')}/{out.get('n_orders')} 成交"
                  f"（run {out.get('run_id')}）")
        return self._finish(out, "traded", "executed", t0)

    def _finish(self, out: dict, action: str, reason: str, t0: float) -> dict:
        out["action"] = action
        out["reason"] = reason
        out["elapsed"] = round(time.time() - t0, 2)
        self.stats["checks"] += 1
        if action == "traded":
            self.stats["trades"] += 1
        elif action == "skip":
            self.stats["skips"][reason] = self.stats["skips"].get(reason, 0) + 1
        self._log(f"结论：{action} / {reason}（{out['elapsed']}s）")
        self._notify(out)      # sets out["notify"]; never raises
        self._record(out)      # audit + heartbeat, now including the push result
        return out

    def _notify(self, out: dict) -> None:
        """Push the outcome.  A notification must never affect trading.

        Every failure mode is recorded on `out["notify"]` and logged, so "did it
        tell me?" is answerable from the heartbeat rather than by guessing.
        """
        try:
            r = notify_for(out, self._notifier_now())
        except Exception as e:                                  # noqa: BLE001
            # Belt and braces: `send()` already swallows everything, but a broken
            # formatter must not be able to kill a rebalance either.
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out["notify"] = r
        if r.get("ok"):
            self._log(f"已推送通知（{r.get('provider')}）")
        elif r.get("skipped"):
            return                  # routine: not configured, or not this action
        else:
            self._log(f"通知发送失败（不影响交易）：{r.get('error')}")

    # -- the loop ----------------------------------------------------------
    def loop(self, stop: Optional[threading.Event] = None) -> None:
        self._enabled = True
        self._write_state(True)
        self._log(f"自动交易启动：mode={self.mode} 每 {self.interval_min:g} 分钟检查一次"
                  f"{'（dry-run，只算不下单）' if self.dry_run else ''}")
        if self.mode == "live":
            self._log("⚠ 实盘模式：订单将以真实资金成交。熔断开关可随时在控制台拉下。")
        self._log(f"心跳 {self.state_path()} · 审计 {self.log_path()}")
        nd = self._notifier_now().describe()
        if nd["ready"]:
            self._log(f"调仓通知已启用：{nd['provider']} → {nd['url']}"
                      f"，推送时机 {nd['notify_on']}（模式 {nd['modes']}）")
        else:
            self._log("调仓通知未启用。一行配好并自检：python -m "
                      "crypto_ls_research.execution.notify --set-url <你的机器人 webhook>")
        try:
            while not (stop is not None and stop.is_set()):
                try:
                    self.once()
                except Exception as e:                          # noqa: BLE001
                    self.stats["errors"] += 1
                    self._log(f"本轮出错（已记录，等下一个周期）：{type(e).__name__}: {e}")
                    rec = {"ts": time.time(), "mode": self.mode,
                           "action": "error", "reason": type(e).__name__,
                           "error": str(e)[:400]}
                    self._notify(rec)
                    self._record(rec)
                deadline = time.time() + self.interval_sec
                # Sleep in slices so SIGTERM is honoured promptly instead of
                # after a full interval.
                while time.time() < deadline and not (stop is not None and stop.is_set()):
                    time.sleep(min(self._SLEEP_SLICE, max(0.0, deadline - time.time())))
        finally:
            self._enabled = False
            self._write_state(False)
            self._log("自动交易已停止。")


# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="无人值守自动调仓（按 engine.DEFAULT_SIGNAL = v3 最优配置）")
    ap.add_argument("--mode", default="demo", choices=["paper", "demo", "live"])
    ap.add_argument("--interval", type=float, default=60.0,
                    help="检查间隔（分钟），下限 %.0f" % MIN_INTERVAL_MIN)
    ap.add_argument("--bar", default=DEFAULT_SIGNAL["bar"])
    ap.add_argument("--refresh-after-hours", type=float, default=2.0,
                    help="最新已收盘 bar 落后超过这么多小时就先做增量刷新；0 = 从不刷新")
    ap.add_argument("--once", action="store_true",
                    help="只跑一次决策就退出（用于验证/外部定时器）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只算不下单（写的是 dry_run 运行记录）")
    ap.add_argument("--allow-live", action="store_true",
                    help="live 模式必须显式带上：自动交易会在无人值守时下真钱单")
    a = ap.parse_args(argv)

    try:
        bot = AutoTrader(mode=a.mode, interval_min=a.interval,
                         bar=a.bar, refresh_after_hours=a.refresh_after_hours,
                         dry_run=a.dry_run, allow_live=a.allow_live)
    except PermissionError as e:
        print(f"拒绝启动：{e}", file=sys.stderr)
        return 3

    if a.once:
        out = bot.once()
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        # 2 = a human should look (a cap bit, or the confirm phrase failed).
        return 2 if out["action"] == "blocked" else 0

    stop = threading.Event()

    def _bye(signum, _frame):                                   # noqa: ANN001
        bot._log(f"收到信号 {signum}，正在退出…")
        stop.set()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    bot.loop(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
