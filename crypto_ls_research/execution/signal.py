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


def _cache_key(bar: str, rebalance_days: float, asset_class: str, start: str,
               end: str, overrides: dict, data_sig: str) -> str:
    payload = json.dumps({
        "bar": bar, "rebalance_days": float(rebalance_days), "asset_class": asset_class,
        "start": start, "end": end,
        "overrides": {k: overrides[k] for k in sorted(overrides)}, "data": data_sig,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


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
                     _data_signature(bar))
    if use_cache:
        row = _read_cache(key, ttl)
        if row:
            t = LiveTarget.from_dict(row)
            t.from_cache = True
            return t

    insts = resolve_insts(bar, asset_class)
    cfg = build_config(bar, rebalance_days, overrides, start=start, end=end,
                       capital=capital)
    panels = load_panels(bar, start, end, insts=insts)
    panels, dropped = _drop_forming_bar(panels, bar)
    if verbose:
        print(f"[signal] {len(panels.index):,} bars x {len(panels.insts)} insts, "
              f"last={panels.index[-1]}, forming-bar dropped={dropped}", flush=True)

    res = run_backtest(panels, cfg)
    if not res.rebalances:
        raise RuntimeError("回测没有产生任何调仓点，无法生成实盘目标")

    last = res.rebalances[-1]
    R = int(cfg.rebalance_bars)
    dec_idx = [i for i, ts in enumerate(panels.index) if ts == last["ts"]]
    d_bar = int(dec_idx[0]) if dec_idx else int(res.meta.get("warmup_bars", 0))
    panel_last = panels.index[-1]
    panel_last_pos = len(panels.index) - 1
    bars_since = panel_last_pos - d_bar
    due = bars_since >= R

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
