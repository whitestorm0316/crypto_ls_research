"""Load the parquet cache into aligned wide panels (index=UTC timestamp, columns=instrument).

Panel invariants
----------------
* Index is a **complete, regular** bar grid from `start` to `end`.  A missing bar for
  an instrument is NaN -- never forward-filled, never zero-filled.  Downstream code
  treats NaN as "no data / not investable", which is the conservative choice.
* `list_dt[i]` = first bar where instrument i has a valid close.  This is the ONLY
  listing date used anywhere in the pipeline, so the universe gate is listing-bias safe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config.settings import SECONDS_PER_BAR, BacktestConfig

CACHE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data_cache")
)

FIELDS = ["open", "high", "low", "close", "vol", "vol_ccy", "amount"]


@dataclass
class Panels:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    vol: pd.DataFrame
    vol_ccy: pd.DataFrame
    amount: pd.DataFrame            # USDT turnover
    funding: pd.DataFrame           # sum of funding rates whose settlement falls in the bar
    list_dt: pd.Series              # per-instrument first valid bar

    @property
    def insts(self) -> List[str]:
        return list(self.close.columns)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def vwap(self) -> pd.DataFrame:
        """Bar VWAP approximated by turnover / base volume (both from the exchange)."""
        v = self.amount / self.vol_ccy.replace(0.0, np.nan)
        return v.where(np.isfinite(v))


def _read_one(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    return df if len(df) else None


def list_cached_insts(bar: str, cache: str = CACHE) -> List[str]:
    """Every instrument that has a candle file for `bar`, sorted.

    Used to scope the universe (e.g. crypto-only) *before* panels are loaded --
    scoping at the panel level is the one choke point that no downstream stage
    can bypass.
    """
    cdir = os.path.join(cache, "candles", bar)
    if not os.path.isdir(cdir):
        raise FileNotFoundError(f"no candle cache at {cdir}; run data.download first")
    return sorted(f[:-len(".parquet")] for f in os.listdir(cdir) if f.endswith(".parquet"))


def load_panels(bar: str, start: str, end: str, insts: Optional[List[str]] = None,
                cache: str = CACHE, drop_unconfirmed: bool = True,
                extend_to_last: bool = False) -> Panels:
    """Load the cache into aligned wide panels.

    `extend_to_last=True` makes the grid reach the newest cached candle itself.
    The default does not, because every archived v3 number was produced with the
    off-by-one below in place -- see the comment at the grid construction.
    """
    cdir = os.path.join(cache, "candles", bar)
    # OKX retains only ~3 months of funding settlements, so long backtests read the
    # spliced OKX+Binance series ("funding_hyb").  Set FUNDING_DIR=funding to force
    # the pure-OKX series.  Never fall back silently: a missing funding directory
    # would otherwise look like "funding = 0", which flatters every result.
    pref = os.environ.get("FUNDING_DIR", "funding_hyb")
    fdir = os.path.join(cache, pref)
    if not os.path.isdir(fdir):
        alt = os.path.join(cache, "funding")
        print(f"[store] WARNING: funding dir '{pref}' missing; using '{os.path.basename(alt)}' "
              f"(pure OKX, ~3 months of settlements only).", flush=True)
        fdir = alt
    if not os.path.isdir(fdir):
        print("[store] WARNING: no funding directory found -- funding P&L will be zero.",
              flush=True)
    if not os.path.isdir(cdir):
        raise FileNotFoundError(f"no candle cache at {cdir}; run data.download first")

    files = sorted(f[:-8] for f in os.listdir(cdir) if f.endswith(".parquet"))
    if insts is not None:
        keep = set(insts)
        files = [f for f in files if f in keep]
    if not files:
        raise FileNotFoundError(f"no instruments found in {cdir}")

    raw: Dict[str, pd.DataFrame] = {}
    fund: Dict[str, pd.Series] = {}
    for inst in files:
        df = _read_one(os.path.join(cdir, f"{inst}.parquet"))
        if df is None:
            continue
        raw[inst] = df
        fdf = _read_one(os.path.join(fdir, f"{inst}.parquet"))
        if fdf is not None:
            fund[inst] = fdf["fundingRate"]

    # ---- regular grid ------------------------------------------------------
    step = pd.Timedelta(seconds=SECONDS_PER_BAR[bar])
    grid = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                         freq=step, inclusive="left")
    # extend to the last available bar so the tail of the sample is not truncated
    last = max(df.index[-1] for df in raw.values() if len(df))
    # `inclusive="left"` treats `end` as exclusive, so whenever `last` sat past
    # `end` the rebuild below stopped one bar SHORT of the newest cached candle,
    # and `grid <= last` cannot recover a timestamp that was never generated.
    # Every archived v3 number was produced with that off-by-one in place, so the
    # default keeps it; `extend_to_last=True` reaches `last` itself, which the live
    # signal path needs (see `execution.signal.compute_live_target`).
    stop = last + step if extend_to_last else last
    if stop > grid[-1]:
        grid = pd.date_range(grid[0], stop, freq=step, inclusive="left")
    grid = grid[grid <= last]

    def wide(field: str) -> pd.DataFrame:
        cols = {i: df[field] for i, df in raw.items() if field in df.columns}
        w = pd.DataFrame(cols)
        w = w.reindex(grid)
        # float32 keeps a 50k x 161 panel at ~32 MB per field.  Relative precision is
        # ~1e-7, five orders of magnitude finer than the return granularity we trade,
        # so it cannot move a backtest result -- but it does halve peak memory, which
        # is what lets the parameter sweeps run 4-wide without swapping.
        return w.astype("float32")

    panels = {f: wide(f) for f in FIELDS}
    close = panels["close"]

    # ---- funding, aggregated into the bar it settles in ---------------------
    fcols = {}
    for inst, s in fund.items():
        s = s.copy()
        s.index = pd.to_datetime(s.index, utc=True)
        fcols[inst] = s.groupby(s.index.floor(step)).sum()
    funding = pd.DataFrame(fcols).reindex(grid)
    funding = funding.reindex(columns=close.columns)
    funding = funding.fillna(0.0).astype("float32")
    # a bar with no data at all must not silently pay/receive funding
    funding = funding.where(close.notna(), np.nan).fillna(0.0)

    # ---- listing dates (first bar with a real close) ------------------------
    list_dt = pd.Series(
        {c: (close[c].first_valid_index() if close[c].notna().any() else pd.NaT)
         for c in close.columns},
        name="list_dt",
    )
    list_dt = list_dt.dropna()

    keep = [c for c in close.columns if c in list_dt.index]
    close = close[keep]
    panels = {k: v[keep] for k, v in panels.items()}
    funding = funding[keep]

    return Panels(
        open=panels["open"], high=panels["high"], low=panels["low"], close=close,
        vol=panels["vol"], vol_ccy=panels["vol_ccy"], amount=panels["amount"],
        funding=funding, list_dt=list_dt[keep],
    )


def panels_to_arrays(p: Panels) -> Dict[str, np.ndarray]:
    """Float32 numpy views for the hot loop.  Columns follow `p.insts` order."""
    return {
        "open": p.open.to_numpy(dtype="float32", na_value=np.nan),
        "high": p.high.to_numpy(dtype="float32", na_value=np.nan),
        "low": p.low.to_numpy(dtype="float32", na_value=np.nan),
        "close": p.close.to_numpy(dtype="float32", na_value=np.nan),
        "amount": p.amount.to_numpy(dtype="float32", na_value=np.nan),
        "vol_ccy": p.vol_ccy.to_numpy(dtype="float32", na_value=np.nan),
        "funding": p.funding.to_numpy(dtype="float32", na_value=0.0),
    }
