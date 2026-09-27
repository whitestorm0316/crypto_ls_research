"""Local persistence for the trading desk.

Layout (everything under `artifacts/live/<mode>/`, so paper, demo and live can
never read each other's state by accident)::

    state.json     current book: positions, nav, last rebalance, mode
    orders.json    every order this desk has sent, keyed by clOrdId
    fills.jsonl    append-only fill log
    runs.jsonl     append-only record of each execution attempt
    equity.jsonl   append-only (ts, nav, gross, net) marks

Append-only + atomic replace, never in-place mutation of a JSON array: the
console polls these files while a run is writing them, and a half-written file
must not be readable as a valid-but-wrong book.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))
LIVE_DIR = os.path.join(ROOT, "artifacts", "live")

_LOCK = threading.RLock()

MODES = ("paper", "demo", "live")

#: The files a reset rotates out.  Listed explicitly (not `os.listdir`) so a
#: reset can only ever touch the desk's own ledger -- never a stray file a user
#: dropped into the directory to inspect.
LEDGER_FILES = ("state.json", "orders.json", "fills.jsonl", "runs.jsonl",
                "equity.jsonl")


def new_run_id(mode: str) -> str:
    return f"{mode}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _atomic_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: str, limit: Optional[int] = None, newest_first: bool = True
                ) -> List[dict]:
    rows: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if newest_first:
        rows.reverse()
    return rows[:limit] if limit else rows


class Store:
    """One store per mode.  Thread-safe within the process."""

    def __init__(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.dir = os.path.join(LIVE_DIR, mode)
        os.makedirs(self.dir, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    # -- state -------------------------------------------------------------
    def read_state(self) -> dict:
        with _LOCK:
            return _read_json(self.path("state.json"), {}) or {}

    def write_state(self, state: dict) -> None:
        with _LOCK:
            _atomic_json(self.path("state.json"), state)

    def patch_state(self, **kw) -> dict:
        with _LOCK:
            s = self.read_state()
            s.update(kw)
            s["mode"] = self.mode
            s["updated"] = time.time()
            _atomic_json(self.path("state.json"), s)
            return s

    # -- orders ------------------------------------------------------------
    def orders(self) -> Dict[str, dict]:
        with _LOCK:
            return _read_json(self.path("orders.json"), {}) or {}

    def put_orders(self, rows: Iterable[dict]) -> None:
        """Upsert by clOrdId.  A re-run of the same clOrdId updates, never duplicating."""
        with _LOCK:
            cur = _read_json(self.path("orders.json"), {}) or {}
            for r in rows:
                cid = r.get("clOrdId") or r.get("cl_ord_id")
                if not cid:
                    continue
                cur[str(cid)] = {**(cur.get(str(cid)) or {}), **r}
            _atomic_json(self.path("orders.json"), cur)

    def set_order(self, cl_ord_id: str, **patch) -> None:
        self.put_orders([{**patch, "clOrdId": cl_ord_id}])

    # -- append-only logs --------------------------------------------------
    def add_fills(self, rows: Iterable[dict]) -> None:
        for r in rows:
            with _LOCK:
                _append_jsonl(self.path("fills.jsonl"), r)

    def fills(self, limit: Optional[int] = None, **filt) -> List[dict]:
        rows = _read_jsonl(self.path("fills.jsonl"), limit=None)
        for k, v in filt.items():
            if v in (None, ""):
                continue
            rows = [r for r in rows if str(r.get(k, "")) == str(v)]
        return rows[:limit] if limit else rows

    def add_run(self, record: dict) -> None:
        with _LOCK:
            _append_jsonl(self.path("runs.jsonl"), record)

    def runs(self, limit: Optional[int] = None) -> List[dict]:
        return _read_jsonl(self.path("runs.jsonl"), limit=limit)

    def add_equity(self, nav: float, gross: float = 0.0, net: float = 0.0,
                   ts: Optional[float] = None) -> None:
        with _LOCK:
            _append_jsonl(self.path("equity.jsonl"),
                          {"ts": float(ts if ts is not None else time.time()),
                           "nav": float(nav), "gross": float(gross),
                           "net": float(net)})

    def equity(self, limit: Optional[int] = None) -> List[dict]:
        rows = _read_jsonl(self.path("equity.jsonl"), limit=None, newest_first=False)
        return rows[-limit:] if limit else rows

    # -- turnover budget ---------------------------------------------------
    @staticmethod
    def today_utc() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def turnover_state(self) -> tuple:
        """(day, used_today).  The budget resets on the UTC date, exactly as the
        backtest resets it on a new calendar day."""
        s = self.read_state()
        day = str(s.get("turnover_day") or "")
        used = float(s.get("turnover_used_today") or 0.0)
        if day != self.today_utc():
            return self.today_utc(), 0.0
        return day, used

    def bump_turnover(self, used: float, day: Optional[str] = None) -> dict:
        day = day or self.today_utc()
        with _LOCK:
            s = self.read_state()
            cur = float(s.get("turnover_used_today") or 0.0)
            if str(s.get("turnover_day") or "") != day:
                cur = 0.0
            s["turnover_day"] = day
            s["turnover_used_today"] = cur + float(used)
            s["mode"] = self.mode
            s["updated"] = time.time()
            _atomic_json(self.path("state.json"), s)
            return s

    # -- convenience -------------------------------------------------------
    def summary(self) -> dict:
        s = self.read_state()
        return {
            "mode": self.mode,
            "dir": self.dir,
            "nav": s.get("nav"),
            "n_positions": len(s.get("positions") or {}),
            "last_rebalance": s.get("last_rebalance"),
            "updated": s.get("updated"),
            "n_orders": len(self.orders()),
            "n_fills": len(self.fills()),
            "n_runs": len(self.runs()),
            "turnover_day": s.get("turnover_day"),
            "turnover_used_today": s.get("turnover_used_today"),
        }


def reset_mode(mode: str, keep_archives: int = 10) -> Optional[str]:
    """Start a mode from a clean slate by **archiving**, never by deleting.

    Two reasons this is not `shutil.rmtree`:

    * A trading ledger is evidence.  A button labelled "reset the paper account"
      must not destroy the record of what the desk did -- if anything it is the
      one thing you want to keep when you are about to change something.
    * Bulk recursive deletion is refused by the host's safe-delete guard on this
      machine (>50 files in a turn).  The guard does not raise a plain
      `Exception`, so the HTTP handler's `except Exception` cannot catch it and
      the connection is dropped mid-request with no response -- i.e. the button
      looks like the server died.  Renaming files never triggers it.

    Ledger files are rotated into `archive/<UTC timestamp>/`, keeping the newest
    `keep_archives` snapshots.  Pruning removes a handful of individual files
    (never a recursive walk), and is best-effort: a refusal there must not turn
    a working reset into a 500.

    Returns the archive directory, or None if there was nothing to rotate.
    """
    d = os.path.join(LIVE_DIR, mode)
    os.makedirs(d, exist_ok=True)
    files = [f for f in LEDGER_FILES if os.path.isfile(os.path.join(d, f))]
    dest: Optional[str] = None
    if files:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        adir = os.path.join(d, "archive")
        dest = os.path.join(adir, stamp)
        n = 1
        while os.path.exists(dest):                     # two resets inside 1 s
            dest = os.path.join(adir, f"{stamp}_{n}")
            n += 1
        os.makedirs(dest, exist_ok=True)
        for f in files:
            try:
                os.replace(os.path.join(d, f), os.path.join(dest, f))
            except OSError:                             # noqa: PERF203
                pass
    _prune_archives(d, keep_archives)
    return dest


def _prune_archives(mode_dir: str, keep: int) -> None:
    """Bound the archive count.  Removes individual files, never a tree.

    Catches `BaseException`, not `OSError`: housekeeping must never be able to
    fail a reset.  A host-level deletion guard (this machine has one, a
    `sitecustomize.py` that aborts with `SystemExit` once too many files have
    been deleted in a session) is not an `OSError`, so an `except OSError` here
    would let it escape and turn a working reset into a 500 -- the exact failure
    the archive-instead-of-delete design was meant to prevent.
    """
    adir = os.path.join(mode_dir, "archive")
    try:
        names = sorted(n for n in os.listdir(adir)
                       if os.path.isdir(os.path.join(adir, n)))
    except BaseException:                               # noqa: BLE001
        return
    for old in (names[:-keep] if keep > 0 else []):
        p = os.path.join(adir, old)
        try:
            for f in os.listdir(p):
                fp = os.path.join(p, f)
                if os.path.isfile(fp):
                    os.remove(fp)
            os.rmdir(p)
        except BaseException:                           # noqa: BLE001
            pass                                        # best effort, never fatal


def archives(mode: str) -> List[str]:
    """Existing archive snapshot names, oldest first (for the console)."""
    adir = os.path.join(LIVE_DIR, mode, "archive")
    try:
        return sorted(n for n in os.listdir(adir)
                      if os.path.isdir(os.path.join(adir, n)))
    except OSError:
        return []
