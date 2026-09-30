"""Minimal Binance USDⓈ-M futures public client.

Two jobs
--------
1. **Funding back-fill (the original reason this file exists).**
   OKX's public `/api/v5/public/funding-rate-history` endpoint retains roughly the
   last three months of settlements (verified empirically: for BTC-USDT-SWAP the
   oldest reachable settlement was 2026-06-22, and the next `after` page came back
   empty).  The backtest window starts 2021-01-01, so OKX alone cannot supply the
   realised funding series the specification requires.

   Per the project's data-sourcing rule (OKX first, other venues only for what OKX
   cannot provide), Binance is used to back-fill funding for the period OKX does
   not retain.  Binance `fapi/v1/fundingRate` returns the complete history since
   listing.

   The result is a SPLICED series, not a pure OKX series.  `funding_build.py`
   quantifies the proxy error on the OKX/Binance overlap and records it in
   `artifacts/<tag>/tables/00b_funding_source.json`.

2. **A second price venue (`klines_range`).**
   The strategy's edge could in principle be an artefact of one venue's bar
   construction (OKX `history-candles` vs Binance `klines` differ in tick rounding,
   volume accounting and the exact bar boundary).  `klines_range` returns Binance
   OHLCV **in OKX row shape** so the identical pipeline can be run on a second
   venue and the two headline metrics compared.  See `scripts/binance_backtest.py`.

Interval handling
-----------------
Binance has migrated many symbols from 8h to 4h (and some to 1h) funding
intervals, while OKX settles every 8h.  A settlement rate is a charge per
interval, so summing the Binance rates inside each 8h window produces the
economically equivalent 8h charge and keeps the spliced series on one scale.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Set, Tuple

BASE = "https://fapi.binance.com"
DEFAULT_PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7897")

FUNDING_MAX_LIMIT = 1000

# Binance interval codes.  Unlike OKX these are already lowercase, so this table is
# only a whitelist -- it exists so an unsupported bar fails loudly here instead of
# being sent to the API and coming back as a bare 400.
_BAR_API: Dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w",
}

# `/fapi/v1/klines` is weight-limited by `limit`: [1,100)=1, [100,500)=2,
# [500,1000)=5, [1000,1500]=10.  The venue allows 2400 weight/min, so a 1500-bar page
# costs 10 and the safe request rate is 2400/10/60 = 4 req/s -- NOT the 8 the funding
# path uses (that endpoint is weight 1).  Exceeding it earns 418/429 and the client's
# backoff, which turns a 19-minute download into an hour.
KLINES_MAX_LIMIT = 1500
KLINES_SAFE_RATE_PER_SEC = 4.0

# Binance kline row layout, as returned by `/fapi/v1/klines`.
_K_OPEN_TIME, _K_OPEN, _K_HIGH, _K_LOW, _K_CLOSE = 0, 1, 2, 3, 4
_K_VOLUME, _K_CLOSE_TIME, _K_QUOTE_VOLUME = 5, 6, 7

# Milliseconds per bar, used to advance the pagination cursor.
_BAR_MS: Dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}


class RateLimiter:
    """Token bucket shared by all worker threads."""

    def __init__(self, rate_per_sec: float, burst: int = 10):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                need = (1.0 - self.tokens) / self.rate
            time.sleep(need)


class BinanceClient:
    def __init__(self, proxy: str = DEFAULT_PROXY, rate_per_sec: float = 8.0,
                 max_workers: int = 8):
        self.proxy = proxy
        self.limiter = RateLimiter(rate_per_sec)
        self.max_workers = max_workers
        self._local = threading.local()
        self.stats = {"req": 0, "err": 0}
        self._lock = threading.Lock()
        self._perp_cache: Optional[Set[str]] = None
        self._perp_lock = threading.Lock()

    # -- low level -----------------------------------------------------------
    def _opener(self):
        if not hasattr(self._local, "op"):
            handlers = []
            if self.proxy:
                handlers.append(
                    urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
                )
            op = urllib.request.build_opener(*handlers)
            op.addheaders = [("User-Agent", "quant-research/1.0")]
            self._local.op = op
        return self._local.op

    def get(self, path: str, params: Optional[dict] = None, timeout: int = 30,
            retries: int = 5):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        last_err = None
        for attempt in range(retries):
            self.limiter.acquire()
            try:
                with self._opener().open(url, timeout=timeout) as r:
                    js = json.loads(r.read().decode("utf-8"))
                with self._lock:
                    self.stats["req"] += 1
                return js
            except urllib.error.HTTPError as e:
                last_err = e
                # 418/429 = rate limited; back off hard.
                if e.code in (418, 429):
                    with self._lock:
                        self.stats["err"] += 1
                    time.sleep(min(30.0, 2.0 ** attempt))
                    continue
                if e.code == 400:
                    # e.g. invalid symbol -> not retryable
                    return {"code": -1121, "msg": "invalid symbol"}
                time.sleep(min(10.0, 0.5 * 2 ** attempt))
            except Exception as e:                                  # noqa: BLE001
                last_err = e
                time.sleep(min(10.0, 0.5 * 2 ** attempt))
        with self._lock:
            self.stats["err"] += 1
        raise RuntimeError(f"binance request failed: {last_err}")

    # -- universe ------------------------------------------------------------
    def perp_symbols(self, quote: str = "USDT") -> Set[str]:
        """All live USDⓈ-M PERPETUAL symbols for the given quote asset."""
        with self._perp_lock:
            if self._perp_cache is not None:
                return self._perp_cache
        js = self.get("/fapi/v1/exchangeInfo")
        out: Set[str] = set()
        for s in js.get("symbols", []) or []:
            if (s.get("contractType") == "PERPETUAL"
                    and s.get("quoteAsset") == quote
                    and s.get("status") == "TRADING"):
                out.add(s["symbol"])
        with self._perp_lock:
            self._perp_cache = out
        return out

    def match_symbol(self, base: str, live: Optional[Set[str]] = None) -> Optional[str]:
        """Map an OKX base asset to the Binance perp symbol, if one exists.

        Handles Binance's 1000x/1000000x denomination convention, e.g. OKX
        `PEPE-USDT-SWAP` <-> Binance `1000PEPEUSDT`.
        """
        live = live if live is not None else self.perp_symbols()
        for cand in (f"{base}USDT", f"1000{base}USDT", f"1000000{base}USDT"):
            if cand in live:
                return cand
        return None

    # -- klines --------------------------------------------------------------
    def klines_page(self, symbol: str, bar: str, start_ms: int, end_ms: int,
                    limit: int = KLINES_MAX_LIMIT, retries: int = 5) -> list:
        """One page of `/fapi/v1/klines`, ascending by open time."""
        if bar not in _BAR_API:
            raise ValueError(f"unsupported bar '{bar}'; known: {sorted(_BAR_API)}")
        return self.get("/fapi/v1/klines",
                        {"symbol": symbol, "interval": _BAR_API[bar],
                         "startTime": int(start_ms), "endTime": int(end_ms),
                         "limit": int(limit)}, retries=retries)

    def klines_range(self, symbol: str, bar: str, start_ms: int, end_ms: int,
                     limit: int = KLINES_MAX_LIMIT, now_ms: Optional[int] = None) -> List[list]:
        """Full OHLCV history for [start_ms, end_ms], oldest-first, **in OKX row shape**.

        Why reshape instead of returning Binance's own layout
        ----------------------------------------------------
        Binance returns `[openTime, o, h, l, c, volume, closeTime, quoteVolume, ...]`
        while OKX `history-candles` returns
        `[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]`.  Emitting the OKX shape
        here means `data.download`, `store.load_panels` and every downstream stage work
        completely unmodified, so swapping venue stays a data-sourcing decision rather
        than a second code path that can drift out of sync with the first.

        Field mapping (this is the part that must not be got wrong):
          * `vol_ccy` <- base-asset volume (idx 5)  -- `store.vwap = amount/vol_ccy`
            and `backtest.engine` price `next_vwap` both depend on this being BASE.
          * `amount`  <- quote-asset volume (idx 7) -- ADV gate, the `flow` factor and
            the liquidity universe all read `amount` as USDT turnover.
          * `vol`     <- base volume as well.  OKX's `vol` is a *contract* count with no
            Binance analogue; the field is only consumed by the Monte-Carlo bootstrap
            as a shuffled companion series, so base volume is the honest substitute.
          * `confirm` -- Binance has no confirm flag, so a bar is final iff its
            `closeTime` is already in the past.  The in-progress bar is therefore
            dropped, matching the OKX path's `confirm == 1` filter.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if bar not in _BAR_MS:
            raise ValueError(f"unsupported bar '{bar}'; known: {sorted(_BAR_MS)}")
        bar_ms = _BAR_MS[bar]
        out: List[list] = []
        seen: Set[int] = set()
        cursor = int(start_ms)
        for _ in range(20000):
            page = self.klines_page(symbol, bar, cursor, end_ms, limit=limit)
            if isinstance(page, dict):
                if page.get("code") == -1121:
                    return out
                page = page.get("data") or []
            if not page:
                break
            newest = cursor
            for r in page:
                ts = int(r[_K_OPEN_TIME])
                if ts in seen:
                    continue
                seen.add(ts)
                newest = max(newest, ts)
                if ts < start_ms or ts > end_ms:
                    continue
                out.append([
                    ts,
                    r[_K_OPEN], r[_K_HIGH], r[_K_LOW], r[_K_CLOSE],
                    r[_K_VOLUME],          # vol      (contracts -> base volume)
                    r[_K_VOLUME],          # vol_ccy  (base)     -- vwap depends on this
                    r[_K_QUOTE_VOLUME],    # amount   (quote)    -- ADV / flow depend on this
                    1 if int(r[_K_CLOSE_TIME]) < now_ms else 0,
                ])
            if len(page) < limit:
                break
            if newest <= cursor:
                break
            cursor = newest + bar_ms
            if cursor > end_ms:
                break
        out.sort(key=lambda r: r[0])
        return out

    # -- funding -------------------------------------------------------------
    def funding_history(self, symbol: str, start_ms: int, end_ms: int,
                        retries: int = 5) -> List[Tuple[int, float]]:
        """All settlements in [start_ms, end_ms], ascending.  Paginates forward.

        `retries` is per page.  A full history is ~6 pages for a 2021-start symbol,
        so a rate-limited run needs a generous budget: a page that fails is
        indistinguishable downstream from a symbol that has no history at all.
        """
        out: List[Tuple[int, float]] = []
        cursor = start_ms
        for _ in range(500):
            js = self.get("/fapi/v1/fundingRate",
                          {"symbol": symbol, "startTime": cursor, "endTime": end_ms,
                           "limit": FUNDING_MAX_LIMIT}, retries=retries)
            if isinstance(js, dict):
                if js.get("code") == -1121:
                    return out
                data = js.get("data") or []
            else:
                data = js
            if not data:
                break
            for r in data:
                out.append((int(r["fundingTime"]), float(r["fundingRate"])))
            newest = max(int(r["fundingTime"]) for r in data)
            if len(data) < FUNDING_MAX_LIMIT:
                break
            if newest <= cursor:
                break
            cursor = newest + 1
        out.sort(key=lambda x: x[0])
        return out

    # -- parallel ------------------------------------------------------------
    def pmap(self, fn, items: Iterable, desc: str = ""):
        items = list(items)
        out: Dict = {}
        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(fn, it): it for it in items}
            for f in as_completed(futs):
                it = futs[f]
                try:
                    out[it] = f.result()
                except Exception as e:                              # noqa: BLE001
                    out[it] = f"ERR:{type(e).__name__}"
                done += 1
                if done % 20 == 0 or done == len(items):
                    el = time.time() - t0
                    print(f"  [{desc}] {done}/{len(items)}  {el:.0f}s "
                          f"(eta {el / done * (len(items) - done):.0f}s)", flush=True)
        return out


def okx_base(inst_id: str) -> str:
    """`BTC-USDT-SWAP` -> `BTC`."""
    return inst_id.split("-")[0]
