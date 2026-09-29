"""The live target book -- produced by the *same* engine that produced the report.

Why this module exists at all
-----------------------------
A live trading system that re-derives its signal in a second code path will,
sooner or later, trade something that was never backtested.  The failure is
silent: the orders look plausible, the code looks correct, and the only symptom
is that live performance stops matching research.

So this module does the opposite of the usual thing.  It runs
`backtest.engine.run_backtest` -- the exact function behind `报告 §0` -- over the
panel that ends at the latest **closed** bar, and takes the final rebalance's
target vector as the order target.  Nothing about the signal is recomputed.

Two details that make it safe
-----------------------------
* **The forming bar is dropped.**  The cached panel's last row may be the hour
  currently in progress; using its "close" would be look-ahead in the most
  literal sense.  Any bar whose close time is still in the future is removed
  before the engine sees it.
* **The rebalance schedule is a fixed grid, not "the last row".**  `dec_idx =
  arange(warmup, T-1, R)` with a fixed `warmup` and `R` means the decision bars
  are `warmup + k*R` regardless of how far the panel extends.  So the strategy
  has a stable calendar and the module can answer "is a rebalance due?" instead
  of rebalancing every time the page is refreshed.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import MISSING, dataclass, field, fields
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..backtest.engine import run_backtest
from ..config.settings import BARS_PER_DAY, SECONDS_PER_BAR, BacktestConfig
from ..data.asset_class import filter_insts, load_categories
from ..data.store import CACHE, list_cached_insts, load_panels
from ..factors.engine import FACTOR_NAMES
from ..run.research import base_cfg

ART = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "artifacts"))
SIGNAL_DIR = os.path.join(ART, "live")
SIGNAL_CACHE = os.path.join(SIGNAL_DIR, "signal_cache.json")

DEFAULT_START = "2021-01-01"


@dataclass
class LiveTarget:
    """Everything the trading desk needs to describe "what the strategy wants"."""

    weights: Dict[str, float]              # signed NAV fraction, target book
    book: List[dict]                       # per-name detail for the UI
    pool: List[dict]                       # full eligible pool with scores
    decision_ts: str                       # signal bar (UTC ISO)
    next_decision_ts: str
    panel_last_ts: str
    bars_since_decision: int
    rebalance_bars: int
    rebalance_due: bool
    gross: float
    diagnostics: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    generated_at: float = field(default_factory=time.time)
    from_cache: bool = False

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["weights"] = {k: float(v) for k, v in self.weights.items()}
        return d

    @staticmethod
    def from_dict(d: dict) -> "LiveTarget":
        """Rebuild a target from a cached payload.

        The cache file carries JSON-side bookkeeping keys that are not dataclass
        fields -- most importantly ``_cached_at``, which `_write_cache` stamps
        on every entry so `_read_cache` can expire it.  Whichever side adds the
        key, the *consumer* must tolerate it.

        So filter by the dataclass' own field names instead of blacklisting the
        known extras by hand.  Blacklisting is exactly how this broke: `_cached_at`
        went unlisted, `LiveTarget(**d)` then raised `TypeError`, and the symptom
        was the worst possible one -- the **first** plan (cache miss) worked, and
        every plan after it (cache hit) crashed.  Whether the desk could re-derive
        its target depended on how recently it had already done so.
        """
        allowed = {f.name for f in fields(LiveTarget)}
        payload = {k: v for k, v in d.items() if k in allowed}
        payload.pop("from_cache", None)   # a runtime flag, not cached state
        required = {f.name for f in fields(LiveTarget)
                    if f.default is MISSING and f.default_factory is MISSING}
        absent = sorted(required - set(payload))
        if absent:
            raise ValueError(
                f"cached target is missing required field(s) {absent} "
                f"-- delete {SIGNAL_CACHE} and regenerate")
        return LiveTarget(**payload)


# ---------------------------------------------------------------------------
def resolve_insts(bar: str, asset_class: str = "crypto") -> Optional[List[str]]:
    """PIT pool scope.  Same code path as the research CLI, same fail-loud policy."""
    pool = list_cached_insts(bar)
    if asset_class in (None, "", "all"):
        return None
    cats = load_categories()
    scoped, unknown = filter_insts(pool, cats, asset_class)
    if unknown:
        raise SystemExit(
            f"asset-class scope '{asset_class}' cannot classify {len(unknown)} cached "
            f"instrument(s): {unknown[:8]} — refresh: "
            f"python -m crypto_ls_research.data.asset_class --build")
    return scoped


def build_config(bar: str, rebalance_days: float, overrides: Optional[dict] = None,
                 start: str = DEFAULT_START, end: Optional[str] = None,
                 capital: float = 100_000.0) -> BacktestConfig:
    bpd = BARS_PER_DAY[bar]
    rebal = max(1, int(round(float(rebalance_days) * bpd)))
    end = end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    return base_cfg(bar, rebal, start, end, capital=capital, **(overrides or {}))


def _drop_forming_bar(panels, bar: str, now: Optional[pd.Timestamp] = None):
    """Remove a bar whose close time has not happened yet.

    The panel index is the bar's OPEN time, so bar `t` closes at `t + step`.  If
    that is in the future the bar is still forming and its OHLC must not be
    treated as known.
    """
    if panels.close.empty:
        return panels, False
    step = pd.Timedelta(seconds=SECONDS_PER_BAR[bar])
    now = now or pd.Timestamp.utcnow()
    last = panels.index[-1]
    if last + step > now:
        idx = panels.index[:-1]
        if len(idx) == 0:
            return panels, False
        import dataclasses
        panels = dataclasses.replace(
            panels,
            open=panels.open.loc[idx], high=panels.high.loc[idx],
            low=panels.low.loc[idx], close=panels.close.loc[idx],
            vol=panels.vol.loc[idx], vol_ccy=panels.vol_ccy.loc[idx],
            amount=panels.amount.loc[idx], funding=panels.funding.loc[idx])
        return panels, True
    return panels, False


def _append_execution_bar(panels, bar: str):
    """Append the bar the desk is *trading at*, so the engine can book the decision.

    `run_backtest` books a rebalance at bar `t` from the decision taken at
    `d = t - 1`, and fills it at `open[t]`.  So the panel's LAST row is the
    **execution** bar, not merely the last bar whose OHLC is known -- and a panel
    that stops at the decision bar cannot book that decision at all, because
    `dec_idx = arange(warmup + off, T - 1, R)` structurally excludes the last row.

    `_drop_forming_bar` was written as if the last row were only *information*.
    Waiting for that row to close costs a whole hour at 1h bars, even though the
    only field the engine reads from it -- the fill price `open[T-1]` -- is known
    the instant the bar opens.  The candle cache stores closed bars only, so the
    live path supplies the row itself as a copy of the last closed bar.

    Not look-ahead: the copy carries nothing that was not already known when the
    previous bar closed, and the engine never takes a decision on it (`dec_idx`
    stops at `T - 2`, i.e. the copied bar).  It is read only as `open[T-1]`,
    `ret_exec[T-1]` and `alive[T-1]`; the last two are discarded.
    """
    import dataclasses
    new_ts = panels.index[-1] + pd.Timedelta(seconds=SECONDS_PER_BAR[bar])

    def grow(df: pd.DataFrame) -> pd.DataFrame:
        row = df.iloc[[-1]].copy()
        row.index = pd.DatetimeIndex([new_ts])
        return pd.concat([df, row])

    return dataclasses.replace(
        panels, open=grow(panels.open), high=grow(panels.high), low=grow(panels.low),
        close=grow(panels.close), vol=grow(panels.vol), vol_ccy=grow(panels.vol_ccy),
        amount=grow(panels.amount), funding=grow(panels.funding))


def _cache_key(bar: str, rebalance_days: float, asset_class: str, start: str,
               end: str, overrides: dict, data_sig: str, code_sig: str) -> str:
    payload = json.dumps({
        "bar": bar, "rebalance_days": float(rebalance_days), "asset_class": asset_class,
        "start": start, "end": end,
        "overrides": {k: overrides[k] for k in sorted(overrides)},
        "data": data_sig, "code": code_sig,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _code_signature() -> str:
    """Fingerprint of the code that produced the payload.

    The key has to include it.  Without it a change to the signal path stays
    invisible for up to `ttl` (30 min) and the daemon serves a target -- including a
    `rebalance_due=False` -- computed by the PREVIOUS version.  The symptom is the
    worst kind: the restart looks like it did not take effect, and the desk silently
    skips the window it was just fixed to catch.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for path in (os.path.abspath(__file__),
                 os.path.join(here, "..", "data", "store.py")):
        try:
            st = os.stat(path)
            out.append(f"{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            out.append("missing")
    return "|".join(out)


def _data_signature(bar: str) -> str:
    """Cheap fingerprint of the candle cache: file count + newest mtime."""
    cdir = os.path.join(CACHE, "candles", bar)
    try:
        names = [f for f in os.listdir(cdir) if f.endswith(".parquet")]
        newest = max((os.path.getmtime(os.path.join(cdir, f)) for f in names), default=0.0)
    except OSError:
        return "missing"
    return f"{len(names)}:{int(newest)}"


def _read_cache(key: str, ttl: float) -> Optional[dict]:
    try:
        with open(SIGNAL_CACHE, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    row = (blob or {}).get(key)
    if not row:
        return None
    if ttl >= 0 and time.time() - float(row.get("_cached_at", 0)) > ttl:
        return None
    return row


def _write_cache(key: str, payload: dict) -> None:
    os.makedirs(SIGNAL_DIR, exist_ok=True)
    try:
        with open(SIGNAL_CACHE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        if not isinstance(blob, dict):
            blob = {}
    except (OSError, ValueError):
        blob = {}
    payload = dict(payload)
    payload["_cached_at"] = time.time()
    blob[key] = payload
    # keep the file small: drop everything but the 6 most recent entries
    if len(blob) > 6:
        order = sorted(blob.items(), key=lambda kv: kv[1].get("_cached_at", 0))
        blob = dict(order[-6:])
    tmp = SIGNAL_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False)
    os.replace(tmp, SIGNAL_CACHE)


def rebalance_window_open(bars_since_decision: int) -> bool:
    """Is the (one-bar-wide) execution window open?

    The backtest books a rebalance at the **next** bar's open (`exec_price =
    next_open`), so a grid point can never be booked while it is still the
    panel's last bar.  That makes `due` an *edge*, not a level: it is true for
    exactly one value of `bars_since_decision` -- **1**, meaning the panel's last
    bar is one past a grid point, so that grid point's rebalance is now booked
    and is the book we should be holding.

    Named and tested rather than inlined because getting it wrong is silent.
    The rule used to be `bars_since_decision >= R`, which is true only when the
    panel's last bar **is** a grid point -- so the desk executed the book of the
    PREVIOUS grid point and the live book sat one whole rebalance period behind
    the backtest.  Nothing raised, nothing logged: the positions were simply a
    day (or three) stale.
    """
    return int(bars_since_decision) == 1


def needs_execution_bar(panel_last, last_booked_ts, R: int, bar: str) -> bool:
    """Is the panel's last bar itself a rebalance grid point?

    `last_booked_ts` is the newest rebalance the engine actually recorded.  If the
    panel's last bar sits exactly `R` bars after it, that last bar IS the next grid
    point -- and the engine cannot book it (`dec_idx` stops at `T - 2`), so the desk
    would go on holding the PREVIOUS period's book.

    Named and tested rather than inlined because both directions are silent: too
    eager and every tick looks due; too shy and the desk is a full period stale with
    nothing in the log.  `_append_execution_bar` fixes it.
    """
    step = pd.Timedelta(seconds=SECONDS_PER_BAR[bar])
    return pd.Timestamp(panel_last) == pd.Timestamp(last_booked_ts) + int(R) * step


def book_with_execution_bar(panels, cfg, bar: str):
    """Run the backtest, appending the execution bar when the panel ends on a grid point.

    Returns `(panels, result, appended)`.  Extracted from `compute_live_target` so
    the call site itself is testable: without the append the engine cannot book the
    panel's last bar (`dec_idx` stops at `T - 2`), so `rebalances[-1]` would still be
    the PREVIOUS grid point and the desk would hold a book a full period stale --
    silently, because the numbers all look plausible.
    """
    res = run_backtest(panels, cfg)
    if not res.rebalances:
        raise RuntimeError("回测没有产生任何调仓点，无法生成实盘目标")
    if not len(panels.index):
        return panels, res, False
    R = int(cfg.rebalance_bars)
    if not needs_execution_bar(panels.index[-1], res.reb_ts[-1], R, bar):
        return panels, res, False
    panels = _append_execution_bar(panels, bar)
    return panels, run_backtest(panels, cfg), True


# ---------------------------------------------------------------------------
def compute_live_target(bar: str = "1h", rebalance_days: float = 3.0,
                        asset_class: str = "crypto",
                        overrides: Optional[dict] = None,
                        start: str = DEFAULT_START, end: Optional[str] = None,
                        capital: float = 100_000.0,
                        use_cache: bool = True, ttl: float = 1800.0,
                        verbose: bool = False) -> LiveTarget:
    """Run the strategy up to the latest closed bar and return its target book."""
    overrides = dict(overrides or {})
    end = end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    key = _cache_key(bar, rebalance_days, asset_class, start, end, overrides,
                     _data_signature(bar), _code_signature())
    if use_cache:
        row = _read_cache(key, ttl)
        if row:
            t = LiveTarget.from_dict(row)
            t.from_cache = True
            return t

    insts = resolve_insts(bar, asset_class)
    cfg = build_config(bar, rebalance_days, overrides, start=start, end=end,
                       capital=capital)
    # `extend_to_last=True`: the panel must reach the newest *closed* bar, not stop
    # one short of it.  See the comment at the grid construction in `data.store`.
    panels = load_panels(bar, start, end, insts=insts, extend_to_last=True)
    panels, dropped = _drop_forming_bar(panels, bar)

    # Measured 2026-09-29 (R=24, G=09-29T02:00Z): the 1-day grid books 09-28T02:00Z
    # without the execution bar and 09-29T02:00Z with it, at `exec_ts=09-29T03:00Z`
    # = 北京 11:00 -- the instant bar G closed, which is the price the backtest
    # itself fills at.  See `book_with_execution_bar`.
    panels, res, appended = book_with_execution_bar(panels, cfg, bar)
    R = int(cfg.rebalance_bars)

    if verbose:
        print(f"[signal] {len(panels.index):,} bars x {len(panels.insts)} insts, "
              f"last={panels.index[-1]}, forming-bar dropped={dropped}, "
              f"execution-bar appended={appended}", flush=True)

    last = res.rebalances[-1]
    dec_idx = [i for i, ts in enumerate(panels.index) if ts == last["ts"]]
    d_bar = int(dec_idx[0]) if dec_idx else int(res.meta.get("warmup_bars", 0))
    panel_last = panels.index[-1]
    panel_last_pos = len(panels.index) - 1
    bars_since = panel_last_pos - d_bar
    # See `rebalance_window_open`.  The old rule was `bars_since >= R`, which
    # fired when the panel's last bar IS a grid point -- i.e. one bar too early
    # -- so `last` was still the PREVIOUS grid point's rebalance and the desk
    # traded a book one full period stale.  Measured 2026-09-29 (R=24,
    # G=09-28T02:00Z): the old trigger gave book(09-27T02:00Z) (8 long / 10
    # short); one bar later it is book(09-28T02:00Z) (7 long / 10 short) --
    # 5 of 18 legs differ, and the newer book's `exec_ts` is the bar that just
    # closed, i.e. the price the backtest actually filled at.
    #
    # `bars_since == 1` now spans TWO panel states, and that is deliberate: the
    # panel ending at G (with the execution bar appended above) and the panel
    # ending at G+1 carry the SAME decision and the same `exec_ts`, so the second
    # is an idempotent re-check.  It also removes the silent-skip failure mode:
    # one missed tick no longer costs the whole period.  `refresh_after_hours`
    # must still stay under one bar so the panel never advances two bars at once.
    due = rebalance_window_open(bars_since)

    def add_step(ts: pd.Timestamp, k: int) -> str:
        return (ts + k * pd.Timedelta(seconds=SECONDS_PER_BAR[bar])).isoformat()

    weights: Dict[str, float] = {}
    for inst, w in zip(last["long"], last["w_long"]):
        weights[inst] = weights.get(inst, 0.0) + float(w)
    for inst, w in zip(last["short"], last["w_short"]):
        weights[inst] = weights.get(inst, 0.0) + float(w)

    ix = {inst: j for j, inst in enumerate(res.insts)}
    last_ridx = len(res.reb_ts) - 1
    score_row = np.asarray(res.score_matrix[last_ridx], dtype="float64")
    adv_row = np.asarray(res.adv_matrix[last_ridx], dtype="float64")
    mask_row = np.asarray(res.mask_matrix[last_ridx], dtype=bool)
    beta_row = np.asarray(res.beta_matrix[last_ridx], dtype="float64")
    zrows = {k: np.asarray(res.factor_zs[k][last_ridx], dtype="float64")
             for k in FACTOR_NAMES}
    px_last = panels.close.iloc[-1]

    book = []
    for inst, w in sorted(weights.items(), key=lambda kv: -abs(kv[1])):
        j = ix.get(inst)
        if j is None:
            continue
        px = float(px_last.get(inst, np.nan))
        book.append({
            "instId": inst, "weight": float(w),
            "side": "long" if w > 0 else "short",
            "score": float(score_row[j]) if np.isfinite(score_row[j]) else None,
            "adv": float(adv_row[j]) if np.isfinite(adv_row[j]) else None,
            "beta": float(beta_row[j]) if np.isfinite(beta_row[j]) else None,
            "price": px if np.isfinite(px) else None,
            "factors": {k: (float(zrows[k][j]) if np.isfinite(zrows[k][j]) else None)
                        for k in FACTOR_NAMES},
        })

    pool = []
    for j, inst in enumerate(res.insts):
        if not mask_row[j]:
            continue
        pool.append({
            "instId": inst,
            "score": float(score_row[j]) if np.isfinite(score_row[j]) else None,
            "adv": float(adv_row[j]) if np.isfinite(adv_row[j]) else None,
            "price": float(px_last.get(inst, np.nan)) if np.isfinite(
                float(px_last.get(inst, np.nan))) else None,
            "selected": inst in weights,
        })
    pool.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1e9))

    diag = {
        "n_universe": int(mask_row.sum()),
        "n_long": len(last["long"]), "n_short": len(last["short"]),
        "exec_ts": str(last["exec_ts"]),
        "regime_scale": last.get("regime_scale"),
        "btc_vol_scale": last.get("btc_vol_scale"),
        "dd_scale": last.get("dd_scale"),
        "total_scale": last.get("scale"),
        "beta_u": last.get("beta_u"), "beta_v": last.get("beta_v"),
        "g_long": last.get("g_long"), "g_short": last.get("g_short"),
        "binding_adv": last.get("binding_adv"),
        "n_replaced": last.get("n_replaced"),
        "target_gross": float(np.abs(np.fromiter(weights.values(), dtype="float64")).sum())
        if weights else 0.0,
        "long_gross": float(sum(w for w in weights.values() if w > 0)),
        "short_gross": float(-sum(w for w in weights.values() if w < 0)),
        "forming_bar_dropped": bool(dropped),
        "execution_bar_appended": bool(appended),
        "warmup_bars": int(res.meta.get("warmup_bars", 0)),
        "decision_bar_index": d_bar,
        "panel_last_index": panel_last_pos,
    }

    t = LiveTarget(
        weights=weights, book=book, pool=pool,
        decision_ts=pd.Timestamp(last["ts"]).isoformat(),
        next_decision_ts=add_step(pd.Timestamp(last["ts"]), R),
        panel_last_ts=pd.Timestamp(panel_last).isoformat(),
        bars_since_decision=int(bars_since), rebalance_bars=R, rebalance_due=bool(due),
        gross=diag["target_gross"], diagnostics=diag,
        config={
            "bar": bar, "rebalance_days": float(rebalance_days),
            "asset_class": asset_class, "start": start, "end": end,
            "overrides": overrides, "capital": float(capital),
            "factor_subset": list(res.meta.get("factor_subset", [])),
            "exec_price": res.meta.get("exec_price"),
        },
    )
    if use_cache:
        _write_cache(key, t.as_dict())
    return t
