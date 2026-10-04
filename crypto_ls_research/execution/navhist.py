"""Account NAV history, rebuilt from the local ledger.

Why this module exists
----------------------
The desk records a NAV mark on every rebalance and every reconcile
(`Store.add_equity`), and each run record carries the NAV it saw.  That is a
*real* record -- but it is one or two points per day, so on its own the 交易台
can only ever draw a couple of dots.  Nobody can see whether the book has been
bleeding for three days or just flattened out.

There is no way to *look up* the account's past NAV: OKX exposes the current
balance only, and the local ledger stores the current book, not its history.
So the history has to be **rebuilt**, and this module does it by walking the
book backwards from the newest NAV measurement:

    nav(t-1) = nav(t) - Σ_i q_i · ctVal_i · (mark_i(t) - mark_i(t-1)) - fees(t)

`q_i` is the signed position held during that interval, which comes from
undoing the fill log (`fills.jsonl`) backwards from the current book; the marks
come from candles.  Because a perpetual's cash only moves on fees, realised
PnL and funding, and the rest of the NAV is the mark-to-market of an
instrument whose contract size is `ctVal`, this walk is exact **except** for:

* funding payments -- not in the local ledger, so they are attributed to the
  bar in which they were paid but the *stored* NAV already includes them, i.e.
  the whole curve carries a small level shift near each funding stamp;
* deposits/withdrawals -- a transfer shows up as an unexplained step;
* marks at fill timestamps use the fill price (correct to a few bps of slippage).

Everything above is a *level* effect, and the module does not ask anyone to
take that on faith: `checks` re-walks to each recorded NAV point and reports
the residual.  A curve whose residuals are large is reported as such rather
than drawn as if it were measured.

Traps this file guards against
-----------------------------
* `positions` are in **contracts** and the contract multiplier is `ctVal`
  (BTC = 0.01, DOGE = 1000, PEPE = 1e7).  Dropping it misprices every leg.
* Fill `ts` is **milliseconds** in the OKX payload and seconds elsewhere;
  a factor of 1000 puts every fill 55 years in the past and makes the walk a
  no-op that still looks plausible.
* The candle cache index is `datetime64[ms]` -- `astype("int64")` yields
  *milliseconds*, not nanoseconds.
* A held instrument with no candles cannot be marked.  Skipping it silently
  would make the curve disagree with the very NAV points it is anchored to,
  so it is reported in `missing_bars`.
* Positions held at the start of the window with no fills at all are assumed
  to have been held throughout.  That assumption is named in `assumed_held`
  instead of being baked in silently.
"""
from __future__ import annotations

import bisect
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..config.settings import CACHE_DIR, SECONDS_PER_BAR
from .specs import load_specs

#: The reconstruction granularity.  1h because it is the decision grid *and*
#: the only frequency whose cache covers the whole tradable pool.
DEFAULT_BAR = "1h"
SUPPORTED_BARS = ("15m", "1h", "4h", "1d")
#: Windows longer than this are refused rather than served as a 40k-point page.
MAX_DAYS = 400
#: OKX history-candles pages to pull per instrument before giving up.  8 pages
#: of 300 covers ~100 days at 1h; beyond that the caller gets the cache.
OKX_MAX_PAGES = 8
_OKX_PAGE = 300
#: Relative tolerance for the cache-vs-OKX overlap check before splicing.  Both
#: sides come from the same endpoint, so a genuine match is exact; anything
#: above this means the cache is a different series and must not be joined.
SPLICE_REL_TOL = 1e-6
KEY_ERROR = KeyError("instrument not found")
#: Wall-clock budget for the whole OKX fetch.  A stalled proxy must not hold the
#: panel open for a minute: whatever was not fetched in time comes from the cache
#: (which is then reported as `mixed`, per instrument).
OKX_BUDGET_SECS = 20.0
#: How long a reachability probe result is reused.  Without this, every switch of
#: mode/range would pay the probe's timeout again just to learn the same thing.
PROBE_TTL = 90.0
_PROBE: Dict[str, object] = {"at": 0.0, "ok": None, "note": ""}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def normalize_ts(v) -> Optional[float]:
    """Epoch seconds from a seconds-or-milliseconds stamp.  None if unusable.

    The two spellings coexist in this ledger (`equity.jsonl` is written by us
    in seconds, `fills.jsonl` carries OKX's milliseconds) and mixing them is
    silent: a millisecond stamp read as seconds lands in 1970, so ordering
    still "works" and the curve just comes out flat.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    while f > 1e11:          # ms -> s, us -> ms (one loop is enough for both)
        f /= 1000.0
    return f


def signed_size(fill: dict) -> float:
    """Signed size delta of a fill, in contracts.

    `side` is the only trustworthy direction: `posSide` is `net`/`long`/`short`
    depending on the account's position mode, and reading it as the direction
    flips every leg on a one-way account.
    """
    try:
        sz = abs(float(fill.get("sz") or 0.0))
    except (TypeError, ValueError):
        return 0.0
    if sz <= 0:
        return 0.0
    return sz if str(fill.get("side", "")).lower() == "buy" else -sz


def _fee(fill: dict) -> float:
    try:
        f = float(fill.get("fee") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _pos_sz(row) -> Optional[float]:
    """Signed size out of one `state.positions` entry.

    The ledger stores `{instId: {sz, avg_px}}`, but `account()`'s display list
    and a hand-edited state file have both been seen with a bare number.  Both
    are read rather than crashed on -- a malformed book must degrade the curve,
    not take down the desk's history panel.
    """
    if isinstance(row, dict):
        v = row.get("sz")
    elif isinstance(row, (int, float)) and not isinstance(row, bool):
        v = row
    else:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _mark_lookup(mark: dict) -> dict:
    """`{inst: (ts_list, close_list)}` from whatever the loaders produced."""
    out = {}
    for inst, v in (mark or {}).items():
        if not v:
            continue
        ts, px = v
        if len(ts) < 2 or len(ts) != len(px):
            continue
        out[inst] = (ts, px)
    return out


# ---------------------------------------------------------------------------
# marks: local candle cache
# ---------------------------------------------------------------------------
def bars_from_cache(insts: Sequence[str], bar: str = DEFAULT_BAR,
                    cache_dir: Optional[str] = None
                    ) -> Tuple[Dict[str, Tuple[list, list]], List[str]]:
    """Read `data_cache/candles/<bar>/<inst>.parquet`.

    Returns `({inst: (ts, close)}, missing)`; a file that exists but holds no
    usable row counts as missing rather than as an empty series.
    """
    import pandas as pd

    root = os.path.join(cache_dir or CACHE_DIR, "candles", bar)
    out: Dict[str, Tuple[list, list]] = {}
    missing: List[str] = []
    for inst in insts:
        path = os.path.join(root, f"{inst}.parquet")
        if not os.path.isfile(path):
            missing.append(inst)
            continue
        try:
            df = pd.read_parquet(path, columns=["close"])
        except Exception:                                          # noqa: BLE001
            missing.append(inst)
            continue
        if df is None or len(df) == 0 or "close" not in df.columns:
            missing.append(inst)
            continue
        idx = df.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("UTC")
        # `as_unit("s")` before `astype(int64)`: the cache is datetime64[ms], so
        # a bare astype gives milliseconds and every bar lands in 1970.
        secs = idx.as_unit("s").astype("int64").tolist()
        px = df["close"].astype("float64").tolist()
        keep = [(s, p) for s, p in zip(secs, px)
                if p is not None and math.isfinite(p) and p > 0]
        if not keep:
            missing.append(inst)
            continue
        out[inst] = ([k[0] for k in keep], [k[1] for k in keep])
    return out, missing


# ---------------------------------------------------------------------------
# marks: OKX public candles (live tail, includes the bar still forming)
# ---------------------------------------------------------------------------
def _parse_rows(rows: Sequence[Sequence[str]]) -> Dict[float, float]:
    """`ts -> close` from OKX candle rows, dropping anything unusable."""
    out: Dict[float, float] = {}
    for r in rows or []:
        if len(r) < 5:
            continue
        t = normalize_ts(r[0])
        try:
            c = float(r[4])
        except (TypeError, ValueError):
            continue
        if t is None or not math.isfinite(c) or c <= 0:
            continue
        out[t] = c
    return out


def _okx_series(inst: str, api_bar: str, client, floor_s: Optional[float],
                max_pages: int, deadline: Optional[float]
                ) -> Tuple[Dict[float, float], Optional[str]]:
    """One instrument's candles, paged back until `floor_s` is covered.

    `/market/candles` first, because it is the only endpoint that returns the
    bar **currently forming** (`confirm == "0"`); `history-candles` starts one
    bar behind it and is paged with `after`.

    `floor_s=None` means "the newest page is all I need" -- the common case,
    since the local cache already holds the history (see `splice_okx_tail`).
    """
    rows = list(client.get("/api/v5/market/candles",
                           {"instId": inst, "bar": api_bar,
                            "limit": str(_OKX_PAGE)}))
    pages = 0
    while rows and pages < max_pages and floor_s is not None:
        if deadline is not None and time.time() > deadline:
            return _parse_rows(rows), "OKX 取数超过时间预算"
        oldest = min((normalize_ts(r[0]) or 0) for r in rows if r)
        if oldest <= floor_s:
            break
        older = list(client.get("/api/v5/market/history-candles",
                                {"instId": inst, "bar": api_bar,
                                 "limit": str(_OKX_PAGE),
                                 "after": str(int(oldest * 1000))}))
        new = [r for r in older if normalize_ts(r[0]) is not None]
        if not new:
            break
        rows.extend(new)
        pages += 1
    return _parse_rows(rows), None


def _parallel(insts: Sequence[str], fn, workers: int) -> Dict[str, object]:
    """Run `fn(inst)` across `insts`.  Order is irrelevant, wall clock is not:
    behind a proxy each request costs ~1s, so a 20-name book serialised is 20s
    of staring at nothing."""
    from concurrent.futures import ThreadPoolExecutor
    out: Dict[str, object] = {}
    if not insts:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(insts)))) as ex:
        for inst, res in zip(insts, ex.map(fn, insts)):
            out[inst] = res
    return out


def bars_from_okx(insts: Sequence[str], bar: str, start_s: float, end_s: float,
                  client, budget: Optional[float] = OKX_BUDGET_SECS
                  ) -> Tuple[Dict[str, Tuple[list, list]], List[str], List[str]]:
    """Pull the whole `start_s..end_s` window per instrument from OKX.

    This is the **fallback for names with no local cache at all**; the ordinary
    path is `splice_okx_tail`, which reuses the cache for history instead of
    re-downloading years of bars that are already on disk.

    `budget` caps the wall clock for the batch; whatever misses it is absent and
    the caller tops it up from the cache.  A desk that draws a stale curve
    silently is worse than one that says so.
    """
    from ..data.okx_client import to_api_bar

    api_bar = to_api_bar(bar)                 # OKX wants `1H`, not `1h`
    step = SECONDS_PER_BAR.get(bar, 3600)
    deadline = (time.time() + budget) if budget else None
    missing: List[str] = []
    notes: List[str] = []

    def one(inst):
        if deadline is not None and time.time() > deadline:
            return KEY_ERROR, None
        try:
            return _okx_series(inst, api_bar, client, start_s, OKX_MAX_PAGES, deadline)
        except KeyError as e:                                      # delisted
            return e, None
        except Exception as e:                                     # noqa: BLE001
            return e, None

    raw = _parallel(insts, one, getattr(client, "max_workers", 8))
    out: Dict[str, Tuple[list, list]] = {}
    first_err: Optional[str] = None
    timed_out = False
    for inst in insts:
        res = raw.get(inst)
        if not isinstance(res, tuple):
            continue
        val, note = res
        if isinstance(val, KeyError):
            missing.append(inst)
            continue
        if isinstance(val, Exception):
            if first_err is None:
                first_err = f"{type(val).__name__}: {val}"
            missing.append(inst)
            continue
        if note:
            timed_out = True
        seq = sorted((t, c) for t, c in val.items()
                     if start_s - step <= t <= end_s + step)
        if len(seq) < 2:
            missing.append(inst)
            continue
        out[inst] = ([t for t, _ in seq], [c for _, c in seq])

    if timed_out:
        notes.append("OKX 取数超过时间预算，其余合约改用本地缓存")
    if first_err is not None:
        # One note, not one per name: a dead proxy fails identically for all 20
        # names and 20 identical lines hide everything else.
        notes.insert(0, f"OKX 实时K线不可用（{first_err}）")
    return out, missing, notes


def splice_okx_tail(insts: Sequence[str], bar: str, cache_marks: Dict[str, tuple],
                    start_s: float, end_s: float, client,
                    budget: Optional[float] = OKX_BUDGET_SECS
                    ) -> Tuple[Dict[str, Tuple[list, list]], List[str], List[str], List[str]]:
    """Reuse the cache for history and pull **only the tail** from OKX.

    The local cache is not a second opinion: it is
    `data_cache/candles/<bar>/<inst>.parquet`, downloaded from this very
    endpoint, several years deep and typically one or two bars behind.  Pulling
    a 30-day window again costs three paged requests per name to re-learn bars
    that are already on disk.  Only the newest bars are genuinely new.

    So: fetch the newest page, cut at its oldest bar, and verify the junction
    **bar by bar** against the cache before splicing.  A disagreement means the
    cache came from somewhere else, and a splice there would turn into a
    phantom PnL step in the walk -- so that name stays pure-cache and says why.

    Returns `(marks, okx_names, cache_names, notes)`.
    """
    from ..data.okx_client import to_api_bar

    api_bar = to_api_bar(bar)
    step = SECONDS_PER_BAR.get(bar, 3600)
    deadline = (time.time() + budget) if budget else None
    notes: List[str] = []

    def one(inst):
        cache = cache_marks.get(inst)
        if not cache or not cache[0]:
            return None, None                       # no cache -> caller fetches all
        c_ts, c_px = cache
        floor = c_ts[-1] - 2 * step                 # want >=2 overlapping bars
        try:
            tail, note = _okx_series(inst, api_bar, client, floor,
                                     OKX_MAX_PAGES, deadline)
        except KeyError:
            return "delisted", None
        except Exception as e:                                     # noqa: BLE001
            return e, None
        if not tail:
            return "empty", note
        return (c_ts, c_px, tail), note

    raw = _parallel(insts, one, getattr(client, "max_workers", 8))
    out: Dict[str, Tuple[list, list]] = {}
    okx_names: List[str] = []
    cache_names: List[str] = []
    first_err: Optional[str] = None
    timed_out = False

    for inst in insts:
        res = raw.get(inst)
        if not isinstance(res, tuple):
            continue
        val, note = res
        if note:
            timed_out = True
        if val is None or isinstance(val, str):
            continue                                    # caller handles these
        if isinstance(val, Exception):
            if first_err is None:
                first_err = f"{type(val).__name__}: {val}"
            continue
        c_ts, c_px, tail = val
        cut = min(tail)
        # The junction must be verified, not assumed.
        common = [(t, px) for t, px in zip(c_ts, c_px) if t in tail]
        if not common:
            notes.append(f"{inst} 缓存与 OKX 无重叠 bar，未拼接（用本地缓存）")
            cache_names.append(inst)
            continue
        worst = 0.0
        for t, px in common:
            worst = max(worst, abs(tail[t] - px) / max(abs(px), 1e-12))
        if worst > SPLICE_REL_TOL:
            notes.append(f"{inst} 缓存与 OKX 在重叠 bar 上不一致（最大 "
                         f"{worst:.2%}），未拼接（用本地缓存）")
            cache_names.append(inst)
            continue
        ts = [t for t in c_ts if t < cut and t >= start_s - step]
        px = [p for t, p in zip(c_ts, c_px) if t < cut and t >= start_s - step]
        ts += [t for t in sorted(tail) if t >= cut and t <= end_s + step]
        px += [tail[t] for t in sorted(tail) if t >= cut and t <= end_s + step]
        if len(ts) < 2:
            cache_names.append(inst)
            continue
        out[inst] = (ts, px)
        okx_names.append(inst)

    if timed_out:
        notes.append("OKX 取数超过时间预算，其余合约使用本地历史")
    if first_err is not None:
        notes.append(f"OKX 实时K线部分不可用（{first_err}）")
    return out, okx_names, cache_names, notes


def okx_reachable(client, inst: Optional[str], bar: str = DEFAULT_BAR,
                  ttl: float = PROBE_TTL, timeout: float = 6.0
                  ) -> Tuple[bool, str]:
    """One cheap request, to decide whether to bother with the live path at all.

    `OKXClient.get` retries four times with exponential backoff.  Per name that
    is fine; across a 20-name book behind a dead proxy it is minutes of nothing,
    spent on every panel load.  So: probe once, cache the verdict briefly.
    """
    if not inst or client is None:
        return False, "没有可用于探测的合约"
    now = time.time()
    if ttl > 0 and _PROBE["ok"] is not None and now - float(_PROBE["at"]) < ttl:
        return bool(_PROBE["ok"]), str(_PROBE["note"])
    ok, note = False, ""
    try:
        from ..data.okx_client import to_api_bar
        rows = client.get("/api/v5/market/candles",
                          {"instId": inst, "bar": to_api_bar(bar), "limit": "2"},
                          timeout=timeout, retries=1)
        ok = bool(rows)
        if not ok:
            note = "OKX 实时K线返回空数据"
    except Exception as e:                                         # noqa: BLE001
        note = f"OKX 实时K线不可用（{type(e).__name__}: {e}）"
    _PROBE.update({"at": now, "ok": ok, "note": note})
    return ok, note


def load_marks(insts: Sequence[str], bar: str, start_s: float, end_s: float,
               use_okx: bool = True, client=None, cache_dir: Optional[str] = None,
               budget: Optional[float] = OKX_BUDGET_SECS,
               probe_ttl: float = PROBE_TTL
               ) -> Tuple[Dict[str, Tuple[list, list]], str, List[str], List[str]]:
    """Marks from OKX when reachable, from the local cache otherwise.

    Returns `(marks, source, missing, notes)`.  `source` is one of:

    * `okx+cache` -- history from the cache, the newest bars live from OKX
      (the ordinary case; the two are verified to agree at the junction);
    * `okx`       -- every bar pulled from OKX (names with no cache at all);
    * `cache`     -- OKX unreachable, everything from disk;
    * `mixed`     -- some names live, some not.

    Never a guess: the panel prints it, because a curve whose newest bar is
    hours old is *not* the same claim as one carrying the live price.
    """
    cache_marks, cache_missing = bars_from_cache(insts, bar, cache_dir=cache_dir)
    if not use_okx or client is None:
        notes = [] if use_okx else ["未接入 OKX 实时K线，使用本地缓存"]
        return cache_marks, "cache", cache_missing, notes

    ok, note = okx_reachable(client, insts[0] if insts else None, bar,
                             ttl=probe_ttl)
    if not ok:
        return cache_marks, "cache", cache_missing, [note]

    started = time.time()
    # ① 有缓存的：只补尾部，并在重叠 bar 上核对后才拼接。
    out, okx_names, _cached_ok, notes = splice_okx_tail(
        insts, bar, cache_marks, start_s, end_s, client, budget=budget)

    # ② 本地根本没有缓存的：整段向 OKX 要（这些通常只上市不久）。
    need = [i for i in insts if i not in out and i not in cache_marks]
    if need:
        left = None if budget is None else max(2.0, budget - (time.time() - started))
        full, _miss, notes2 = bars_from_okx(need, bar, start_s, end_s, client,
                                            budget=left)
        out.update(full)
        notes.extend(notes2)
        okx_names = okx_names + list(full)

    missing = [i for i in insts if i not in out]
    for inst in list(missing):
        if inst in cache_marks:
            out[inst] = cache_marks[inst]
            missing.remove(inst)
            notes.append(f"{inst} 无 OKX K线，改用本地缓存")

    n_live = len([i for i in insts if i in okx_names])
    if n_live == 0:
        notes.append("已回退本地K线缓存")
        return out, "cache", missing, notes
    if n_live < len(insts):
        return out, "mixed", missing, notes
    # 每个合约都带上了实时尾部。历史段是不是来自磁盘，必须说出来 ——
    # 「仪表盘上一行字说这是 OKX 数据、实际上 99% 的 bar 在读盘」是误导。
    if any(i in cache_marks for i in insts):
        notes.append("K线：历史段取自本地缓存（与 OKX 同源），最近若干根为 OKX 实时")
        return out, "okx+cache", missing, notes
    return out, "okx", missing, notes


# ---------------------------------------------------------------------------
# recorded NAV points
# ---------------------------------------------------------------------------
def recorded_points(equity: Sequence[dict], runs: Sequence[dict]) -> List[dict]:
    """Every NAV measurement the desk has written: `equity.jsonl` + `runs.jsonl`.

    The run log matters as much as the equity log: a **dry run** measures the
    account (mark-to-market) without appending an equity row, so a session of
    planning-only leaves the equity log empty while the runs are full of real
    NAV observations.
    """
    rows: List[dict] = []
    for r in equity or []:
        t = normalize_ts(r.get("ts"))
        nav = r.get("nav")
        if t is None or nav is None:
            continue
        try:
            rows.append({"ts": t, "nav": float(nav), "kind": "equity"})
        except (TypeError, ValueError):
            continue
    for r in runs or []:
        t = normalize_ts(r.get("ts"))
        nav = r.get("nav")
        if t is None or nav is None:
            continue
        try:
            rows.append({"ts": t, "nav": float(nav),
                         "kind": "dry" if r.get("dry_run") else "run"})
        except (TypeError, ValueError):
            continue
    rows = [r for r in rows if math.isfinite(r["nav"]) and r["nav"] > 0]
    rows.sort(key=lambda r: r["ts"])
    return rows


def _last_per_ts(rows: Sequence[dict]) -> List[dict]:
    """One row per timestamp -- the last one, i.e. **after** that run's trades.

    `execute()` writes three marks within a second (pre-trade, post-trade,
    settle).  Comparing the rebuilt curve against the pre-trade mark measures
    the trade, not the rebuild, and would report a bogus residual.
    """
    out: List[dict] = []
    for r in rows:
        if out and abs(out[-1]["ts"] - r["ts"]) < 1.0:
            out[-1] = r
        else:
            out.append(dict(r))
    return out


# ---------------------------------------------------------------------------
# the rebuild
# ---------------------------------------------------------------------------
@dataclass
class NavHistory:
    mode: str
    bar: str
    source: str
    ts: List[float] = field(default_factory=list)
    nav: List[float] = field(default_factory=list)
    recorded: List[dict] = field(default_factory=list)
    checks: List[dict] = field(default_factory=list)
    anchor: Optional[dict] = None
    warnings: List[str] = field(default_factory=list)
    missing_bars: List[str] = field(default_factory=list)
    assumed_held: List[str] = field(default_factory=list)
    max_pct: Optional[float] = None
    partial: bool = False
    n_fills: int = 0

    def to_dict(self) -> dict:
        n = len(self.ts)
        span = (self.ts[-1] - self.ts[0]) if n >= 2 else 0.0
        return {
            "mode": self.mode, "bar": self.bar, "source": self.source,
            "ts": self.ts, "nav": [round(v, 8) for v in self.nav],
            "recorded": self.recorded,
            "checks": self.checks,
            "anchor": self.anchor,
            "warnings": self.warnings,
            "missing_bars": self.missing_bars,
            "assumed_held": self.assumed_held,
            "max_pct": self.max_pct,
            "partial": self.partial,
            "n": n,
            "n_fills": self.n_fills,
            "coverage": {"from": self.ts[0] if n else None,
                         "to": self.ts[-1] if n else None,
                         "span_secs": span},
        }


def reconstruct(*, mode: str, positions_now: Dict[str, float], fills: Sequence[dict],
                ct: Dict[str, Optional[float]],
                marks: Dict[str, Tuple[list, list]],
                anchor_ts: float, anchor_nav: float,
                recorded: Sequence[dict] = (), bar: str = DEFAULT_BAR,
                start_s: float = 0.0, source: str = "cache",
                warnings: Optional[List[str]] = None,
                missing_bars: Optional[List[str]] = None) -> NavHistory:
    """Walk the book backwards from (`anchor_ts`, `anchor_nav`).

    Pure function: no filesystem, no network.  `marks` is
    `{inst: (sorted ts list, close list)}` and may be short an instrument --
    those are reported, never silently treated as a zero mark.
    """
    hist = NavHistory(mode=mode, bar=bar, source=source)
    hist.warnings = list(warnings or [])
    hist.missing_bars = sorted(set(missing_bars or []))
    step = SECONDS_PER_BAR.get(bar, 3600)
    if not start_s:
        start_s = anchor_ts - 30 * 86400

    fills_by_t: Dict[float, List[dict]] = {}
    for f in fills or []:
        t = normalize_ts(f.get("ts"))
        inst = f.get("instId")
        if t is None or not inst or signed_size(f) == 0:
            continue
        fills_by_t.setdefault(t, []).append(f)

    # -- book at the anchor: undo anything that happened after it -------------
    pos: Dict[str, float] = {i: float(v or 0.0) for i, v in (positions_now or {}).items()}
    for t, rows in fills_by_t.items():
        if t > anchor_ts:
            for f in rows:
                pos[f["instId"]] = pos.get(f["instId"], 0.0) - signed_size(f)
    traded = {f["instId"] for f in (fills or []) if f.get("instId")}
    hist.assumed_held = sorted(
        i for i, q in pos.items() if q and i not in traded)

    # -- mark lookup ---------------------------------------------------------
    look = _mark_lookup(marks)
    unmarked = [i for i in pos if i not in look]
    hist.missing_bars = sorted(set(hist.missing_bars) | set(unmarked))
    if hist.missing_bars:
        hist.warnings.append(
            "以下合约没有K线，其盈亏无法计入，曲线可能偏离：" + "、".join(hist.missing_bars))
    no_ct = [i for i in pos if not ct.get(i)]
    if no_ct:
        hist.warnings.append("以下合约缺少合约面值 ctVal，已跳过：" + "、".join(no_ct))

    def mark_at(inst: str, t: float, fill_px: Optional[float] = None) -> Optional[float]:
        if fill_px is not None and math.isfinite(fill_px) and fill_px > 0:
            return fill_px
        v = look.get(inst)
        if not v:
            return None
        ts, px = v
        j = bisect.bisect_right(ts, t) - 1
        if j < 0:
            return None
        c = px[j]
        return c if (c is not None and math.isfinite(c) and c > 0) else None

    # -- event grid: bar closes of every held instrument + fill stamps --------
    events = {float(anchor_ts)}
    for inst in pos:
        v = look.get(inst)
        if not v:
            continue
        ts = v[0]
        lo = bisect.bisect_left(ts, int(start_s))
        hi = bisect.bisect_right(ts, int(anchor_ts))
        events.update(float(x) for x in ts[lo:hi])
    events.update(float(t) for t in fills_by_t if start_s <= t <= anchor_ts)
    grid = sorted(events)

    # A window with a single event means we have bars but no fills and no
    # recorded points other than the anchor: report it instead of drawing a
    # one-dot "history".
    if len(grid) < 2:
        hist.anchor = {"ts": anchor_ts, "nav": anchor_nav}
        hist.ts, hist.nav = [anchor_ts], [anchor_nav]
        hist.warnings.append("本地流水与K线都不足以重建历史（不足 2 个采样点）")
        hist.partial = True
        return hist

    nav = float(anchor_nav)
    out: List[Tuple[float, float]] = [(float(anchor_ts), nav)]
    # invariant: at the top of iteration k, `pos` == book after every fill at
    # event[k]; the interval (event[k-1], event[k]] is therefore exposed at
    # `pos` *after* dropping event[k]'s own fills.
    for k in range(len(grid) - 1, 0, -1):
        tc, tp = grid[k], grid[k - 1]
        for f in fills_by_t.get(tc, ()):
            pos[f["instId"]] = pos.get(f["instId"], 0.0) - signed_size(f)
        gains = 0.0
        for inst, q in pos.items():
            if not q:
                continue
            cv = ct.get(inst)
            if not cv:
                continue
            fpx = None
            for f in fills_by_t.get(tc, ()):
                if f["instId"] == inst:
                    try:
                        fpx = float(f.get("px"))
                    except (TypeError, ValueError):
                        fpx = None
                    break
            m1 = mark_at(inst, tc, fpx)
            m0 = mark_at(inst, tp)
            if m1 is None or m0 is None:
                continue
            gains += q * cv * (m1 - m0)
        fees = sum(_fee(f) for f in fills_by_t.get(tc, ()))
        nav = nav - gains - fees
        if not math.isfinite(nav):
            hist.warnings.append(f"净值在某一步变为非有限值（{tc}），已截断")
            break
        out.append((tp, nav))
    out.reverse()

    # Anchor the curve's start at the requested window; the walk already stops
    # at the first event, so only the tail needs trimming.
    out = [(t, v) for t, v in out if t >= start_s - step]
    hist.ts = [t for t, _ in out]
    hist.nav = [v for _, v in out]
    hist.anchor = {"ts": float(anchor_ts), "nav": float(anchor_nav)}
    if hist.ts and hist.ts[-1] < anchor_ts - step:
        hist.warnings.append(
            "K线比账户快照早 "
            f"{round((anchor_ts - hist.ts[-1]) / 3600.0, 1)} 小时，这段未覆盖")

    # -- validation: rebuild up to each recorded point ------------------------
    rep = _last_per_ts([r for r in recorded if r["ts"] <= anchor_ts])
    worst = 0.0
    for r in rep:
        j = bisect.bisect_left(hist.ts, r["ts"])
        if j >= len(hist.ts):
            continue
        # only compare when the curve actually reaches that timestamp; a point
        # outside the walked window proves nothing either way
        if abs(hist.ts[j] - r["ts"]) > max(step, 120):
            continue
        recon = hist.nav[j]
        diff = recon - r["nav"]
        pct = diff / r["nav"] if r["nav"] else 0.0
        hist.checks.append({"ts": r["ts"], "kind": r["kind"],
                            "recorded": r["nav"], "recon": recon,
                            "diff": diff, "pct": pct})
        worst = max(worst, abs(pct))
    if hist.checks:
        hist.max_pct = worst
    hist.recorded = [{"ts": r["ts"], "nav": r["nav"], "kind": r["kind"]} for r in rep]
    if not hist.checks:
        hist.warnings.append("没有可与重建曲线比对的记录点，偏差未知")
    return hist


# ---------------------------------------------------------------------------
# convenience wrapper over the live store
# ---------------------------------------------------------------------------
def build(mode: str, *, bar: str = DEFAULT_BAR, days: Optional[int] = None,
          use_okx: bool = True, client=None, store=None,
          cache_dir: Optional[str] = None, now: Optional[float] = None,
          specs: Optional[dict] = None) -> NavHistory:
    """Rebuild `mode`'s NAV history.  Never raises for a missing ledger."""
    from .store import Store

    if bar not in SUPPORTED_BARS:
        raise ValueError(f"unsupported bar {bar!r}; known: {list(SUPPORTED_BARS)}")
    if days is not None and (days <= 0 or days > MAX_DAYS):
        raise ValueError(f"days must be 1..{MAX_DAYS}")
    st = store or Store(mode)

    warnings: List[str] = []
    state = st.read_state() or {}
    equity = st.equity()
    runs = st.runs()
    fills = st.fills()

    rec = recorded_points(equity, runs)
    if not rec:
        h = NavHistory(mode=mode, bar=bar, source="none")
        h.warnings.append("这个账户还没有任何净值记录（对账/调仓/dry-run 之后才会有）")
        h.partial = True
        return h

    anchor = rec[-1]
    anchor_ts, anchor_nav = anchor["ts"], anchor["nav"]

    first_fill = min((normalize_ts(f.get("ts")) for f in fills
                      if normalize_ts(f.get("ts")) is not None),
                     default=anchor_ts)
    floor = min(first_fill, rec[0]["ts"])
    start_s = floor if not days else max(floor, anchor_ts - days * 86400)

    positions_now = {i: _pos_sz(r) for i, r in (state.get("positions") or {}).items()}
    positions_now = {i: q for i, q in positions_now.items() if q is not None}
    if not positions_now:
        warnings.append("本地账本没有持仓快照；曲线只反映流水，起点可能不准")
    insts = sorted(set(positions_now) | {f.get("instId") for f in fills
                                         if f.get("instId")})
    if not insts:
        warnings.append("本地账本没有成交流水，无法重建仓位历史")

    sp = load_specs() if specs is None else specs
    ct: Dict[str, Optional[float]] = {}
    for i in insts:
        s = sp.get(i)
        ct[i] = float(s.ct_val) if s is not None and s.ct_val else None
    for f in fills:                      # the fill's own ctVal is authoritative
        cv = f.get("ct_val")
        try:
            cv = float(cv)
        except (TypeError, ValueError):
            cv = None
        if cv and cv > 0 and not ct.get(f.get("instId")):
            ct[f.get("instId")] = cv

    step = SECONDS_PER_BAR.get(bar, 3600)
    if fills and start_s < first_fill - step:
        warnings.append(
            "窗口起点早于本地第一笔成交，这段的持仓是按流水反推的（无流水可验证）")

    marks, source, missing, notes = load_marks(
        insts, bar, start_s, anchor_ts + 3600, use_okx=use_okx, client=client,
        cache_dir=cache_dir)
    warnings.extend(notes)

    hist = reconstruct(
        mode=mode, positions_now=positions_now, fills=fills, ct=ct, marks=marks,
        anchor_ts=anchor_ts, anchor_nav=anchor_nav, recorded=rec, bar=bar,
        start_s=start_s, source=source, warnings=warnings, missing_bars=missing)
    hist.n_fills = len(fills or [])
    return hist


def okx_client():
    """Public market-data client (no API key).  Isolated so tests can stub it."""
    from ..data.okx_client import OKXClient
    return OKXClient()


def now_ts() -> float:
    return time.time()
