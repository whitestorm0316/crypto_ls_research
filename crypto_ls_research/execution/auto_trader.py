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
3. **not a rebalance day** -> skip, UNLESS an exec window is configured and we
                              are inside it (see `--exec-window`).
4. **any `block`**         -> skip.  `force` is never passed, so no cap is ever
                              relaxed to make a trade happen.
5. otherwise               -> execute.

Exec windows (`--exec-window HH:MM`)
------------------------------------
The backtest grid is anchored at **UTC 02:00 = Beijing 10:00** and that anchor
is not a parameter: `dec_idx = np.arange(warmup, T - 1, R)` with `warmup = 722`
on a panel that starts 2021-01-01 00:00 UTC.  Every one of the 2,064 rebalance
points in the delivered v3 sits on hour 02 minute 00.

An operator who wants orders placed at, say, 23:30 Beijing cannot move that
anchor, so the choice is:

  * **pretend to move it** -- place the order 13.5h late and keep quoting the
    backtest Sharpe, which is now a number for a strategy nobody is running; or
  * **say what is happening** -- keep the signal exactly as verified, let the
    due-ness gate be overridden deliberately inside a declared window, and
    record the override so the deviation is visible in the audit log.

This implements the second.  A window is a local-time `HH:MM` plus a tolerance
(default 5 min); the first tick inside it forces one rebalance.  Forcing is
rate-limited to **once per `rebalance_days`** so a 30-minute tick cannot spend
the whole 20%/day turnover budget on noise -- the same failure the due-ness gate
exists to prevent.

A forced run is **not** silently equivalent: it trades on a signal that is
`(window - anchor)` hours old.  Every forced decision writes
`"forced_by": "exec_window"` and the measured signal age into `auto.jsonl`, so
the deviation can be audited later instead of being discovered in the P&L.

Every decision, **including every skip**, is appended to `auto.jsonl` and
mirrored into `auto.json`, because "it has been running for a week" and "it died
on the first night" look identical from the outside.

The strategy is `engine.DEFAULT_SIGNAL` -- the accepted configuration (1h bars,
the grid in `config.settings.ACCEPTED_REBALANCE_DAYS`, `range_pos`+`hitrate`,
20% daily turnover throttle).  It is the same object the console shows as
「已验收最优」, so there is no second definition of "optimal" to drift out of
sync.  That is not a slogan: the grid used to be a literal in `DEFAULT_SIGNAL`
that disagreed with the daemon's `--rebalance-days`, and the desk went on
generating plans for a strategy nobody was running.
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
from .store import RebalanceBusy, Store, display_ts

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


def explain_not_due(target) -> str:
    """Why this tick did not rebalance — in words that cannot contradict themselves.

    `target.next_decision_ts` is **not** "the next decision, still ahead".  It is
    `last_booked_grid_point + R`, and the backtest books a rebalance at the
    *next* bar's open — so the newest grid point stays unbooked until the panel
    holds a bar after it.  On a 1-day grid that makes `next_decision_ts` normally
    a moment that has **already passed**.

    The old wording was
    `未到调仓日（信号日 2026-09-28 10:00，下次 2026-09-29 10:00）`, printed at
    10:16 on 2026-09-29: it called a grid point sixteen minutes in the past
    「下次」, and told the user the day was not a rebalance day when it was.  On a
    daily grid that sentence contradicts itself **every single day**, and it reads
    as "the bot is not running" — the exact impression this panel exists to
    prevent.  It is not a cosmetic problem: the only way to tell "it ran and
    correctly declined" from "it never ran" is this line.

    So say it in **bars**, which is what the gate actually measures.  The window
    opens when the panel's last bar is **one past** a grid point -- the grid point
    itself cannot be booked (exec price is `next_open`), so
    `bars_since_decision == 1` is the trigger.

    That "one bar short" state is no longer reachable: `signal.compute_live_target`
    appends the execution bar whenever the panel's last bar IS a grid point, so
    `bars_since_decision` becomes 1 there too.  Hence the wait is `R - since`, with
    no `+ 1` -- the panel no longer has to wait for a bar to *close* before the
    decision on the bar before it can be booked.
    """
    R = int(getattr(target, "rebalance_bars", 0) or 0)
    since = int(getattr(target, "bars_since_decision", 0) or 0)
    dec = display_ts(getattr(target, "decision_ts", "")) or "?"
    if not R:
        return f"未到可执行窗口（信号日 {dec}）"
    # `since` in [2, R-1] is the normal state *after* the window: the target book
    # is still the one from `decision_ts` and it will not change until the panel
    # reaches the next grid point.  Do not claim "已执行" -- without per-period
    # state this function cannot know whether the window was caught.
    #
    # `R - since`, and the formula lives in TWO render points (`here` and
    # `app.js`'s `dueHint`): fixing one and forgetting the other is how the same
    # wait came to be printed as two different numbers.
    remain = max(0, R - since)
    return (f"本周期不在窗口内：目标持仓取自 {dec} 的调仓，"
            f"已过 {since}/{R} 根 bar；窗口在调仓点收盘后打开，"
            f"还差 {remain} 根到下个窗口")


def parse_hhmm(s: str) -> int:
    """`"23:30"` -> 1410 (minutes since local midnight).

    Raises rather than guessing.  A typo'd window that silently became
    "00:00" would move every future order by nine hours and look like a
    strategy change, which is exactly the class of bug this file exists to
    make impossible.
    """
    t = str(s or "").strip()
    parts = t.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"时刻必须形如 'HH:MM'（24 小时制），收到 {s!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"时刻超出范围，收到 {s!r}")
    return hh * 60 + mm


def in_exec_window(window_min: Optional[int], now: Optional[float] = None,
                   tol_min: float = 5.0) -> bool:
    """Is local wall-clock time inside `[window, window + tol)`?

    Local time, deliberately: the operator says "11:30 at night" and means the
    clock on the wall next to them.  Converting that to UTC is the caller's
    job via `--exec-window-utc`, because a daemon that silently reinterpreted
    the number would place orders 8 hours off for an Asia/Shanghai operator
    and nobody would notice until the fills looked wrong.
    """
    if window_min is None:
        return False
    t = time.localtime(time.time() if now is None else now)
    cur = t.tm_hour * 60 + t.tm_min
    lo = int(window_min)
    hi = lo + max(0.0, float(tol_min))
    # Windows are short (<24h) by construction, so no midnight wrap handling:
    # a window of "23:58 + 5min" legitimately spans two days and is supported
    # by the modulo; anything longer is a misconfiguration, not a schedule.
    if hi <= 1440:
        return lo <= cur < hi
    return cur >= lo or cur < (hi - 1440)


def utc_offset_label(now: Optional[float] = None) -> str:
    """A stable ASCII UTC-offset label for the *current* local zone.

    NOT `time.strftime("%Z")`: on a Chinese Windows that returns `中国标准时间`,
    which then travels through argv, JSON state files and log lines and can
    crash a GBK console (`UnicodeEncodeError`) or mojibake the audit log.  The
    offset is what actually matters for reading a schedule anyway -- "UTC+08:00"
    is unambiguous and ASCII on every platform.

    `%z` also gives the offset but is locale-independent only on POSIX; the
    manual computation below is the same code path everywhere.
    """
    t = time.localtime(time.time() if now is None else now)
    off = -time.timezone if not t.tm_isdst else -time.altzone
    sign = "+" if off >= 0 else "-"
    off = abs(int(off))
    return f"UTC{sign}{off // 3600:02d}:{(off % 3600) // 60:02d}"


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
                 rebalance_days: Optional[float] = None,
                 refresh_after_hours: float = 1.0,
                 dry_run: bool = False,
                 allow_live: bool = False,
                 exec_window: Optional[str] = None,
                 exec_window_tol_min: float = 5.0,
                 exec_window_utc: bool = False,
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
        # `None` means "whatever `DEFAULT_SIGNAL` says" -- i.e. the accepted
        # grid, the same number the console calls 已验收最优.  Passing a number
        # overrides only this one field, so a caller can move the rebalance grid
        # without having to restate the whole accepted configuration and risk
        # dropping a factor or a cap by omission.  Note that a caller *can* move
        # it off the accepted grid; when they do, the acceptance headline no
        # longer describes what is running.
        self.rebalance_days = (float(rebalance_days)
                               if rebalance_days is not None
                               else float(DEFAULT_SIGNAL["rebalance_days"]))
        if not (self.rebalance_days > 0):
            raise ValueError(f"rebalance_days 必须为正，收到 {rebalance_days!r}")
        self.refresh_after_hours = float(refresh_after_hours)
        self.dry_run = bool(dry_run)
        self.allow_live = bool(allow_live)
        # Exec window: `None` = never override the due-ness gate (the archived
        # behaviour, and what every test before this feature assumed).
        self.exec_window_min = parse_hhmm(exec_window) if exec_window else None
        self.exec_window_tol_min = float(exec_window_tol_min)
        self.exec_window_utc = bool(exec_window_utc)
        # Rate limit for forced runs.  Without it a 30-minute tick inside a
        # 5-minute window would fire once, but a mis-set tolerance (say 60 min)
        # would fire twice and spend 40% of gross in one night.
        self._last_forced_ts: float = 0.0
        self._forced_count: int = 0
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
                "rebalance_days": self.rebalance_days,
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

    # -- exec window -------------------------------------------------------
    def _window_label(self) -> Optional[str]:
        """Human-readable window, always stating the timezone it is in.

        A bare "23:30" in a log is ambiguous the moment the machine's TZ
        differs from the operator's, and this project has already been bitten
        once by a UTC/local mix-up in the funding-fee join.
        """
        if self.exec_window_min is None:
            return None
        hh, mm = divmod(int(self.exec_window_min), 60)
        tz = "UTC" if self.exec_window_utc else utc_offset_label()
        return f"{hh:02d}:{mm:02d} {tz} (+{self.exec_window_tol_min:g}min)"

    def _exec_window_open(self) -> bool:
        """True if we may override the due-ness gate right now.

        Three conditions, all required:
          1. a window is configured;
          2. local (or UTC, if `--exec-window-utc`) time is inside it;
          3. the rate limit has elapsed -- at most one forced run per
             `rebalance_days`, which is the same period the grid uses.  Without
             (3) a tolerance typo turns the 20%/day turnover budget into a
             per-tick budget and the book churns to death on fees.
        """
        if self.exec_window_min is None:
            return False
        now = time.time()
        if self.exec_window_utc:
            t = time.gmtime(now)
            cur = t.tm_hour * 60 + t.tm_min
            lo = int(self.exec_window_min)
            hi = lo + max(0.0, self.exec_window_tol_min)
            inside = (lo <= cur < hi) if hi <= 1440 else (cur >= lo or cur < hi - 1440)
        else:
            inside = in_exec_window(self.exec_window_min, now,
                                    self.exec_window_tol_min)
        if not inside:
            return False
        min_gap_sec = self.rebalance_days * 24.0 * 3600.0
        if self._last_forced_ts and (now - self._last_forced_ts) < min_gap_sec:
            self._log(f"     执行窗口已触发过（{self._forced_count} 次，"
                      f"{min_gap_sec/3600:.0f}h 内不再强制），本次跳过。")
            return False
        return True

    def _write_state(self, enabled: Optional[bool] = None) -> None:
        st = {
            "mode": self.mode,
            "enabled": bool(enabled) if enabled is not None else self._enabled,
            "pid": os.getpid(),
            "interval_min": self.interval_min,
            "bar": self.bar,
            "rebalance_days": self.rebalance_days,
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
            # The exec window must be in the heartbeat, not only in the argv:
            # "why did it trade on a Tuesday" has to be answerable from
            # `auto.json` alone, and a restart without the flag must be
            # distinguishable from one with it.
            "exec_window": self._window_label(),
            "exec_window_min": self.exec_window_min,
            "exec_window_utc": self.exec_window_utc,
            "forced_runs": self._forced_count,
            "last_forced_ts": self._last_forced_ts or None,
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
        # Carried into the heartbeat so the console can say *how far* the panel
        # still has to advance before the window opens, instead of labelling a
        # past timestamp 「下一次决策 · 未到」.  See `explain_not_due`.
        out["rebalance_bars"] = int(getattr(target, "rebalance_bars", 0) or 0)
        out["panel_last_ts"] = getattr(target, "panel_last_ts", None)
        out["signal_from_cache"] = bool(target.from_cache)
        out["target_gross"] = float(target.gross)

        # THE gate the server does not hold.  Without it this loop rebalances
        # every tick; `check_plan` only warns.
        forced = False
        if self.engine.limits.require_rebalance_due and not target.rebalance_due:
            if self._exec_window_open():
                forced = True
                age_h = float(target.bars_since_decision) * bar_hours(self.bar)
                out["forced_by"] = "exec_window"
                out["exec_window"] = self._window_label()
                out["signal_age_hours"] = age_h
                self._forced_count += 1
                self._last_forced_ts = time.time()
                # Loud on purpose.  A forced run is a real deviation from the
                # verified grid and must be visible in the notification, the
                # audit log and the console -- not buried in a debug field.
                self._log(
                    f"⚠ 执行窗口 {self._window_label()} 触发强制调仓："
                    f"信号日 {display_ts(target.decision_ts)}，信号已旧 {age_h:.1f} 小时。"
                    f"本次下单偏离回测网格（锚点 UTC02:00/北京10:00），"
                    f"策略绩效不再是 v3 口径。")
            else:
                out["not_due_explain"] = explain_not_due(target)
                self._log(out["not_due_explain"])
                if self.exec_window_min is not None:
                    self._log(f"     执行窗口：{self._window_label()}（当前不在窗口内）")
                return self._finish(out, "skip", "not_due", t0)

        if forced:
            self._write_state(True)
        else:
            self._log(f"到调仓日（信号日 {display_ts(target.decision_ts)}），"
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
        # Print the grid that actually gates trading.  It is the one number that
        # decides whether tonight's tick is a rebalance or a no-op, so a
        # heartbeat that does not state it cannot answer "why did nothing
        # happen" without reading the source.
        self._log(f"调仓网格：{self.bar} bar、每 {self.rebalance_days:g} 天一次"
                  f"（资产分类 {DEFAULT_SIGNAL['asset_class']}）")
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
        description="无人值守自动调仓（按 engine.DEFAULT_SIGNAL = 已验收口径）")
    ap.add_argument("--mode", default="demo", choices=["paper", "demo", "live"])
    ap.add_argument("--interval", type=float, default=60.0,
                    help="检查间隔（分钟），下限 %.0f" % MIN_INTERVAL_MIN)
    ap.add_argument("--bar", default=DEFAULT_SIGNAL["bar"])
    ap.add_argument("--rebalance-days", type=float,
                    default=DEFAULT_SIGNAL["rebalance_days"],
                    help="调仓网格（天）。默认 %.2g = 已验收口径，与 /api/spec 的 "
                         "optimal_cli 同源（`config.settings.ACCEPTED_REBALANCE_DAYS`）—— "
                         "守护进程、交易台的计划、控制台的「已验收最优」读的是同一个数。"
                         "**换网格就是换口径**：网格越密换手与回撤都越重，越疏越轻，"
                         "两边的数字必须一起报，不能只说赢的那一半。这里刻意不写具体"
                         "指标 —— 以前写死的「Sharpe −0.08、MDD 深 4.1pp」按当前数据"
                         "不复现（实测是 Sharpe 反而略高、回撤深约 2.2 倍），写死的"
                         "数字会跟着数据过期却继续断言。实测见 artifacts/FINDINGS.md "
                         "Q22 与 /api/spec。"
                         % DEFAULT_SIGNAL["rebalance_days"])
    ap.add_argument("--refresh-after-hours", type=float, default=1.0,
                    help="最新已收盘 bar 落后超过这么多小时就先做增量刷新；0 = 从不刷新。"
                         "默认 1.0 = **每个 tick 都刷新**，因为刷新刚做完时 bar_age 就已经"
                         "≈1.17h（`bar_age` 量的是 bar 的**开盘**时间），阈值设 2.0 会让"
                         "刷新间隔变成 1h。而 `due` 窗口只有 **2 根 bar 宽**（面板末尾"
                         "落在网格点、以及落在它后一根 —— 补执行 bar 让这两个状态同解，"
                         "互为幂等复查），间隔必须**小于一根 bar**：漏掉一个 tick 就会让"
                         "面板一次推进 2 根、把整个窗口跨过去、窗口被静默跳过。")
    ap.add_argument("--once", action="store_true",
                    help="只跑一次决策就退出（用于验证/外部定时器）")
    ap.add_argument("--exec-window", default=None, metavar="HH:MM",
                    help="执行窗口（本地时间）。到点后即使不在调仓网格上也会"
                         "强制调仓一次。**这会偏离回测网格**：信号日仍是 "
                         "UTC02:00/北京10:00 那个 bar，下单却晚了几小时。"
                         "不传 = 不强制（默认，与回测口径一致）。")
    ap.add_argument("--exec-window-tol", type=float, default=5.0, metavar="MIN",
                    help="执行窗口宽度（分钟，默认 5）。窗口内第一个 tick 触发。")
    ap.add_argument("--exec-window-utc", action="store_true",
                    help="把 --exec-window 解释成 UTC 而不是本地时间。"
                         "默认本地时间，因为『晚上11点半』指的是墙上的钟。")
    ap.add_argument("--dry-run", action="store_true",
                    help="只算不下单（写的是 dry_run 运行记录）")
    ap.add_argument("--allow-live", action="store_true",
                    help="live 模式必须显式带上：自动交易会在无人值守时下真钱单")
    a = ap.parse_args(argv)

    try:
        bot = AutoTrader(mode=a.mode, interval_min=a.interval,
                         bar=a.bar, rebalance_days=a.rebalance_days,
                         refresh_after_hours=a.refresh_after_hours,
                         dry_run=a.dry_run, allow_live=a.allow_live,
                         exec_window=a.exec_window,
                         exec_window_tol_min=a.exec_window_tol,
                         exec_window_utc=a.exec_window_utc)
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
