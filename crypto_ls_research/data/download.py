"""Download USDT-perp OHLCV + realised funding into a parquet cache.

Usage:
  python -m crypto_ls_research.data.download --dry-run
  python -m crypto_ls_research.data.download --bars 1h 15m 5m
  CRYPTO_CACHE_DIR=data_cache_binance python -m crypto_ls_research.data.download \
      --source binance --bars 1h --reuse-pool --only-candles

Cache layout (all times UTC, ms epoch -> pandas UTC DatetimeIndex):
  <cache>/candles/<bar>/<INST>.parquet
  <cache>/funding/<INST>.parquet
  <cache>/meta/instruments.parquet
  <cache>/meta/candidate_pool.json

Venue (`--source`)
------------------
`okx` (default) is the project's primary venue.  `binance` fetches the SAME OHLCV
series from Binance USDⓈ-M futures instead, keeping the OKX `instId` as the cache
filename so every downstream stage runs unmodified -- that is what makes a
venue-robustness comparison a one-flag change rather than a second pipeline.

The instrument *universe* still comes from OKX (the candidate pool), deliberately:
the question being asked is "does the strategy survive on another venue's bars?",
which requires holding the universe fixed and moving only the price source.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, List

import pandas as pd

from ..config.settings import CACHE_DIR
from ..data.binance_client import (KLINES_SAFE_RATE_PER_SEC, BinanceClient,
                                   okx_base)
from ..data.okx_client import OKXClient

# Single source of truth (`settings.CACHE_DIR`): honours `CRYPTO_CACHE_DIR`.
CACHE = CACHE_DIR

COLS = ["ts", "open", "high", "low", "close", "vol", "vol_ccy", "amount", "confirm"]

# ---- per-frequency download plan -------------------------------------------
PLAN: Dict[str, dict] = {
    "1h":  {"pool": "full",  "start": "2021-01-01"},
    "15m": {"pool": "top40", "start": "2024-01-01"},
    "5m":  {"pool": "top8",  "start": "2025-07-01"},
}
TOP_N_FOR_BAR = {"top40": 40, "top8": 8}

# Bar length in ms, used by the incremental (tail-append) path.
BAR_MS = {"1h": 3600_000, "15m": 900_000, "5m": 300_000}
# On a tail refresh, re-request this many bars of overlap so the seam is
# re-downloaded rather than trusted.  The last cached bar is always `confirm==1`
# (unconfirmed bars are dropped), so a small overlap is enough; a few bars of
# slack also repairs a partially-written tail.
INCREMENTAL_OVERLAP_BARS = 6


def ms(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)


def ensure_dirs() -> None:
    for p in ("candles/1h", "candles/15m", "candles/5m", "funding", "meta"):
        os.makedirs(os.path.join(CACHE, p), exist_ok=True)


# ---------------------------------------------------------------------------
def build_candidate_pool(cli: OKXClient, top_by_volume: int = 120,
                         listed_before: str = "2022-01-01") -> pd.DataFrame:
    """Candidate universe = (most liquid names today) UNION (everything listed before
    `listed_before`).

    This is deliberately a *superset* of any historical PIT top-N so that the PIT
    filter itself is not the binding constraint.  Note the honest caveat: it is
    still built from today's live instrument list, so delisted contracts are absent
    -> survivorship bias.  That bias is modelled separately in analysis/bias.py.
    """
    inst = pd.DataFrame(cli.instruments("SWAP"))
    inst = inst[inst["settleCcy"] == "USDT"].copy()
    inst = inst[inst["state"].isin(["live", "suspend"])].copy()
    inst["listTime"] = inst["listTime"].astype("int64")
    inst["list_dt"] = pd.to_datetime(inst["listTime"], unit="ms", utc=True)

    tk = pd.DataFrame(cli.tickers("SWAP"))
    tk = tk[["instId", "volCcy24h", "last", "bidPx", "askPx"]].copy()
    for c in ("volCcy24h", "last", "bidPx", "askPx"):
        tk[c] = pd.to_numeric(tk[c], errors="coerce")

    df = inst.merge(tk, on="instId", how="left")
    df["adv_now"] = df["volCcy24h"] * df["last"]        # USDT 24h turnover

    cut = pd.Timestamp(listed_before, tz="UTC")
    early = df[df["list_dt"] <= cut]
    top = df.sort_values("adv_now", ascending=False).head(top_by_volume)
    pool = pd.concat([early, top]).drop_duplicates("instId").copy()
    pool["reason"] = [
        ("early+top" if (i in set(early.instId) and i in set(top.instId))
         else "early" if i in set(early.instId) else "top")
        for i in pool["instId"]
    ]
    pool = pool.sort_values("adv_now", ascending=False).reset_index(drop=True)
    cols = ["instId", "listTime", "list_dt", "ctVal", "ctValCcy", "tickSz",
            "adv_now", "last", "reason"]
    return pool[cols]


# ---------------------------------------------------------------------------
def _dl_one(cli: OKXClient, inst: str, bar: str, start_ms: int, end_ms: int) -> list:
    return cli.candles_range(inst, bar, start_ms, end_ms, limit=300)


def _last_ts(path: str):
    """Newest timestamp already in the cache, or None."""
    try:
        old = pd.read_parquet(path)
    except Exception:                                      # noqa: BLE001
        return None
    if not len(old):
        return None
    return old.index[-1]


def _merge_append(path: str, fresh: pd.DataFrame) -> pd.DataFrame:
    """Append `fresh` onto the existing cache instead of replacing it.

    The downloaded tail overlaps the cached tail by a few bars, and OKX can
    revise the newest bars, so `keep="last"` (fresh wins on the seam) is the
    correct dedup order.
    """
    try:
        old = pd.read_parquet(path)
    except Exception:                                      # noqa: BLE001
        return fresh
    if not len(old):
        return fresh
    df = pd.concat([old, fresh])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _rows_to_df(rows: list) -> pd.DataFrame:
    """Normalise venue rows (OKX `history-candles` / Binance `klines`) to one frame.

    Shared by both sources on purpose: the two downloaders must not be able to drift
    apart in their dtype handling, confirm filtering or dedup order, or a venue
    comparison would silently measure the loader instead of the venue.
    """
    df = pd.DataFrame(rows, columns=COLS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "vol", "vol_ccy", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["confirm"] = pd.to_numeric(df["confirm"], errors="coerce").fillna(0).astype("int8")
    df = df[df["confirm"] == 1]                          # drop unconfirmed bars
    df = df.set_index("ts").drop(columns=["confirm"]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df.dropna(subset=["close", "amount"])


def download_candles(cli: OKXClient, insts: List[str], bar: str, start: str,
                     end: str, refresh: bool = False,
                     incremental: bool = False) -> None:
    """Download/sync OHLCV for `insts`.

    Three modes, and the difference matters a lot for wall-clock:

    * default  -- skip a symbol whose cache already reaches `end - 4h`, else
      **re-download its whole history** (OKX paginates back to `start`).
    * `incremental=True` -- keep the cache and only fetch the tail, from
      `last_cached_ts - 6 bars` to `end`, then merge.  One page per symbol when
      the cache is current.  This is what a live "refresh quotes" button needs:
      a full refresh of the 1h pool is ~27k requests (~40 min), while an
      incremental one is ~161 (~15 s).
    * `refresh=True` -- force a full re-download even if the cache is current.
    """
    outdir = os.path.join(CACHE, "candles", bar)
    start_ms, end_ms = ms(start), ms(end)
    bar_ms = BAR_MS[bar]

    def task(inst: str):
        path = os.path.join(outdir, f"{inst}.parquet")
        last = _last_ts(path)
        eff_start = start_ms
        append = False
        if last is not None:
            last_ms = int(last.timestamp() * 1000)
            if incremental and not refresh:
                if last_ms >= end_ms - bar_ms:
                    return "uptodate"
                eff_start = max(start_ms, last_ms - INCREMENTAL_OVERLAP_BARS * bar_ms)
                append = True
            elif not refresh and last_ms >= end_ms - 4 * 3600_000:
                return "skip"
        try:
            rows = _dl_one(cli, inst, bar, eff_start, end_ms)
        except KeyError:
            return "delisted"
        except Exception as e:                             # noqa: BLE001
            return f"ERR:{type(e).__name__}"
        if not rows:
            return "empty"
        df = _rows_to_df(rows)
        n_new = 0
        if append:
            before = last
            df = _merge_append(path, df)
            n_new = int((df.index > before).sum())
        df.to_parquet(path, compression="zstd")
        if append:
            return f"app:{n_new}"
        return f"ok:{len(df)}"

    t0 = time.time()
    res = cli.pmap(task, insts, desc=f"candles-{bar}")
    kinds: Dict[str, int] = {}
    added = 0
    for v in res.values():
        if isinstance(v, str) and v.startswith("app:"):
            added += int(v.split(":")[1])
        k = v if isinstance(v, str) else "?"
        kinds[k.split(":")[0]] = kinds.get(k.split(":")[0], 0) + 1
    extra = f"  added_bars={added}" if added else ""
    print(f"[{bar}] done in {time.time()-t0:.0f}s  {kinds}{extra}  req={cli.stats['req']}",
          flush=True)


def download_candles_binance(cli: BinanceClient, insts: List[str], bar: str,
                             start: str, end: str, refresh: bool = False) -> None:
    """Same OHLCV series, sourced from Binance USDⓈ-M futures.

    The cache filename stays the OKX `instId` (e.g. `PEPE-USDT-SWAP.parquet` even
    though the Binance contract is `1000PEPEUSDT`).  This is the whole trick: a
    symbol rename would break `store.load_panels`' join, the frozen asset-class
    snapshot, the funding series and every downstream table at once.  Only the
    *contents* change, and only the price columns do.

    Note the 1000x denomination (`1000PEPEUSDT`): returns and `range_pos` are ratios,
    so a constant scale factor cancels exactly.  `amount` is quoted in USDT on both
    venues, so it needs no adjustment either.
    """
    outdir = os.path.join(CACHE, "candles", bar)
    start_ms, end_ms = ms(start), ms(end)

    live = cli.perp_symbols()
    sym_of = {i: cli.match_symbol(okx_base(i), live) for i in insts}

    def task(inst: str):
        sym = sym_of.get(inst)
        if not sym:
            return "no_binance_listing"
        path = os.path.join(outdir, f"{inst}.parquet")
        last = _last_ts(path)
        if last is not None and not refresh:
            if int(last.timestamp() * 1000) >= end_ms - 4 * 3600_000:
                return "skip"
        try:
            rows = cli.klines_range(sym, bar, start_ms, end_ms)
        except Exception as e:                             # noqa: BLE001
            return f"ERR:{type(e).__name__}"
        if not rows:
            return "empty"
        df = _rows_to_df(rows)
        df.to_parquet(path, compression="zstd")
        return f"ok:{len(df)}"

    t0 = time.time()
    res = cli.pmap(task, insts, desc=f"binance-{bar}")
    kinds: Dict[str, int] = {}
    for v in res.values():
        k = v if isinstance(v, str) else "?"
        kinds[k.split(":")[0]] = kinds.get(k.split(":")[0], 0) + 1
    print(f"[{bar}] binance done in {time.time()-t0:.0f}s  {kinds}  req={cli.stats['req']} "
          f"err={cli.stats['err']}", flush=True)


def download_funding(cli: OKXClient, insts: List[str], start: str, end: str,
                     refresh: bool = False, incremental: bool = False) -> None:
    outdir = os.path.join(CACHE, "funding")
    start_ms, end_ms = ms(start), ms(end)

    def task(inst: str):
        path = os.path.join(outdir, f"{inst}.parquet")
        last = _last_ts(path)
        eff_start = start_ms
        append = False
        if last is not None:
            last_ms = int(last.timestamp() * 1000)
            if incremental and not refresh:
                if last_ms >= end_ms - 8 * 3600_000:
                    return "uptodate"
                # funding prints every 8h at most; 3 days of overlap is plenty
                eff_start = max(start_ms, last_ms - 3 * 86400_000)
                append = True
            elif not refresh and last_ms >= end_ms - 30 * 86400_000:
                return "skip"
        try:
            rows = cli.funding_history(inst, eff_start, end_ms)
        except KeyError:
            return "delisted"
        except Exception as e:                             # noqa: BLE001
            return f"ERR:{type(e).__name__}"
        if not rows:
            return "empty"
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df = df.set_index("ts")[["fundingRate"]].sort_index()
        df = df[~df.index.duplicated(keep="last")]
        if append:
            df = _merge_append(path, df)
        df.to_parquet(path, compression="zstd")
        return f"ok:{len(df)}"

    t0 = time.time()
    res = cli.pmap(task, insts, desc="funding")
    kinds: Dict[str, int] = {}
    for v in res.values():
        k = v if isinstance(v, str) else "?"
        kinds[k.split(":")[0]] = kinds.get(k.split(":")[0], 0) + 1
    print(f"[funding] done in {time.time()-t0:.0f}s  {kinds}  req={cli.stats['req']}", flush=True)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", nargs="*", default=["1h"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="force a full re-download even when the cache is current")
    ap.add_argument("--incremental", action="store_true",
                    help="keep the cache and only append the missing tail "
                         "(~1 request/symbol instead of a whole history re-download)")
    ap.add_argument("--only-candles", action="store_true",
                    help="skip funding (funding is a research input; a live quote "
                         "refresh does not need it)")
    ap.add_argument("--reuse-pool", action="store_true",
                    help="reuse the existing candidate_pool.json / instruments.parquet "
                         "instead of rebuilding the pool from today's live listings")
    ap.add_argument("--end", default="2026-09-26")
    ap.add_argument("--rate", type=float, default=11.0)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--source", choices=("okx", "binance"), default="okx",
                    help="price venue; 'binance' writes the same instId-keyed cache "
                         "from Binance klines (see module docstring)")
    a = ap.parse_args()

    ensure_dirs()
    if a.source == "binance":
        # Weight-limited endpoint: 1500-bar pages cost 10 weight against a 2400/min
        # budget, so ~4 req/s.  The default 11 would earn 418/429 and back off.
        rate = a.rate if a.rate != 11.0 else KLINES_SAFE_RATE_PER_SEC
        cli = BinanceClient(rate_per_sec=rate, max_workers=a.workers)
    else:
        cli = OKXClient(rate_per_sec=a.rate, max_workers=a.workers)

    pool_path = os.path.join(CACHE, "meta", "candidate_pool.json")
    if a.reuse_pool and os.path.exists(pool_path):
        with open(pool_path) as f:
            saved = json.load(f)
        full, by_vol, todo = saved["full"], saved["by_volume"], saved["todo"]
        for bar in a.bars:                       # a saved pool may predate this bar
            if bar not in todo:
                p = PLAN[bar]
                todo[bar] = full if p["pool"] == "full" else by_vol[: TOP_N_FOR_BAR[p["pool"]]]
        print(f"reusing candidate pool ({len(full)} instruments) from {pool_path}")
    else:
        print("building candidate pool ...", flush=True)
        pool = build_candidate_pool(cli)
        pool.to_parquet(os.path.join(CACHE, "meta", "instruments.parquet"))
        full = pool["instId"].tolist()
        by_vol = pool.sort_values("adv_now", ascending=False)["instId"].tolist()
        print(f"  candidate pool = {len(full)}  (early={int((pool.reason!='top').sum())}, "
              f"top-up={int((pool.reason!='early').sum())})")
        todo = {}
        for bar in a.bars:
            p = PLAN[bar]
            todo[bar] = full if p["pool"] == "full" else by_vol[: TOP_N_FOR_BAR[p["pool"]]]
        with open(pool_path, "w") as f:
            json.dump({"full": full, "by_volume": by_vol, "todo": todo}, f, indent=1)

    if not a.incremental:
        est = 0
        # OKX serves 300 bars/page, Binance 1500 -- the request estimate is 5x off if
        # the page size is assumed rather than read from the source.
        page = 1500 if a.source == "binance" else 300
        for bar in a.bars:
            p = PLAN[bar]
            insts = todo.get(bar) or []
            days = (pd.Timestamp(a.end, tz="UTC") - pd.Timestamp(p["start"], tz="UTC")).days
            est += len(insts) * days * 24 * 3600 / ({"1h": 3600, "15m": 900, "5m": 300}[bar]) / page
        funding_est = 0 if (a.only_candles or a.source == "binance") else (
            len(full) * (pd.Timestamp(a.end, tz="UTC") - pd.Timestamp("2021-01-01", tz="UTC")).days * 3 / 100)
        print(f"  estimated requests: candles={est:,.0f}  funding={funding_est:,.0f}  "
              f"total={est+funding_est:,.0f}")
        # Print the rate the client will ACTUALLY use, not the flag default: the
        # binance branch silently lowers it for weight reasons, and a wall-clock
        # estimate that disagrees with the run is worse than no estimate.
        eff_rate = cli.limiter.rate
        print(f"  estimated wall-clock @ {eff_rate:g} req/s = "
              f"{(est+funding_est)/eff_rate/60:,.0f} min")
    else:
        print(f"  incremental: only the missing tail will be fetched "
              f"(~{sum(len(todo.get(b) or []) for b in a.bars)} requests)")

    if a.dry_run:
        print("dry-run: nothing downloaded.")
        return

    for bar in a.bars:
        insts = todo.get(bar) or []
        print(f"\n=== downloading {bar} ({len(insts)} instruments) from {a.source} ===",
              flush=True)
        if a.source == "binance":
            download_candles_binance(cli, insts, bar, PLAN[bar]["start"], a.end,
                                     refresh=a.refresh)
        else:
            download_candles(cli, insts, bar, PLAN[bar]["start"], a.end,
                             refresh=a.refresh, incremental=a.incremental)

    if a.source == "binance":
        print("\nbinance source: funding is NOT downloaded (OKX retains ~3 months "
              "only; the spliced funding_hyb series is reused so the comparison "
              "moves one variable).", flush=True)
    elif a.only_candles:
        print("\nskipping funding (--only-candles)", flush=True)
    else:
        print("\n=== downloading funding (1h candidate pool) ===", flush=True)
        download_funding(cli, full, "2021-01-01", a.end,
                         refresh=a.refresh, incremental=a.incremental)

    print(f"\nALL DONE.  total requests={cli.stats['req']} errors={cli.stats['err']}", flush=True)


if __name__ == "__main__":
    main()
