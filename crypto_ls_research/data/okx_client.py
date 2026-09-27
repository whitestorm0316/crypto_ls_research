"""Rate-limited, concurrency-safe OKX v5 public-market-data client.

Proxy is read from env `OKX_PROXY` (default http://127.0.0.1:7897).

All public market-data endpoints used here require no API key:
  /api/v5/public/instruments            -> contract specs + listTime  (listing-bias control)
  /api/v5/market/tickers                -> current 24h turnover (candidate pool only)
  /api/v5/market/history-candles        -> OHLCV, 300 bars/page, paginate with `after`
  /api/v5/public/funding-rate-history   -> realised funding, 100 records/page
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
from typing import Callable, Dict, Iterable, List, Optional

BASE = "https://www.okx.com"
DEFAULT_PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7897")

# OKX bar codes: minute/hour/day suffixes are case-sensitive.  `1h` returns code
# 51000 "Parameter bar error"; `1H` is the valid form.  Internally we use lowercase
# everywhere and translate at the API boundary.
_BAR_API: Dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "2d": "2D", "3d": "3D", "1w": "1W", "1M": "1M",
}


def to_api_bar(bar: str) -> str:
    if bar not in _BAR_API:
        raise ValueError(f"unsupported bar '{bar}'; known: {sorted(_BAR_API)}")
    return _BAR_API[bar]


class RateLimiter:
    """Token bucket shared by all worker threads."""

    def __init__(self, rate_per_sec: float, burst: int = 20):
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


class OKXClient:
    def __init__(self, proxy: str = DEFAULT_PROXY, rate_per_sec: float = 11.0, max_workers: int = 12):
        self.proxy = proxy
        self.limiter = RateLimiter(rate_per_sec)
        self.max_workers = max_workers
        self._local = threading.local()
        self.stats = {"req": 0, "err": 0, "retry": 0}
        self._lock = threading.Lock()

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
            retries: int = 4) -> list:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        last_err = None
        for attempt in range(retries):
            self.limiter.acquire()
            try:
                with self._opener().open(url, timeout=timeout) as r:
                    js = json.loads(r.read().decode("utf-8"))
                with self._lock:
                    self.stats["req"] += 1
                code = js.get("code")
                if code == "0":
                    return js.get("data", [])
                if code in ("51001", "51000"):        # instrument does not exist / delisted
                    raise KeyError(js.get("msg", "instrument not found"))
                if code == "50011":                   # rate limited
                    raise urllib.error.HTTPError(url, 429, "okx rate limit", {}, None)
                last_err = RuntimeError(f"okx code={code} msg={js.get('msg')}")
            except KeyError:
                raise
            except Exception as e:                    # noqa: BLE001
                last_err = e
            with self._lock:
                self.stats["retry"] += 1
            time.sleep(min(8.0, 0.5 * (2 ** attempt)) * (1 + 0.2 * threading.get_ident() % 1))
        with self._lock:
            self.stats["err"] += 1
        raise RuntimeError(f"GET {path} failed after {retries}: {type(last_err).__name__}: {last_err}")

    # -- instrument metadata -------------------------------------------------
    def instruments(self, inst_type: str = "SWAP") -> list:
        return self.get("/api/v5/public/instruments", {"instType": inst_type})

    def tickers(self, inst_type: str = "SWAP") -> list:
        return self.get("/api/v5/market/tickers", {"instType": inst_type})

    # -- candles -------------------------------------------------------------
    def candles_page(self, inst_id: str, bar: str, before_ts: Optional[int] = None,
                     limit: int = 300) -> list:
        """One page of history-candles.  OKX returns newest-first.

        `before_ts` = exclusive upper bound on the returned (older) candles, i.e.
        page *backwards* in time.  Implemented via the `after` parameter, which
        OKX documents as "pagination of data to return records earlier than the
        requested ts".
        """
        p = {"instId": inst_id, "bar": to_api_bar(bar), "limit": str(limit)}
        if before_ts is not None:
            p["after"] = str(int(before_ts))
        return self.get("/api/v5/market/history-candles", p)

    def candles_range(self, inst_id: str, bar: str, start_ms: int, end_ms: int,
                      limit: int = 300, on_page: Optional[Callable[[int], None]] = None) -> list:
        """Full OHLCV history for [start_ms, end_ms], oldest-first, deduplicated."""
        rows: List[list] = []
        cursor = None
        seen = set()
        for _ in range(20000):
            page = self.candles_page(inst_id, bar, before_ts=cursor, limit=limit)
            if not page:
                break
            stop = False
            for r in page:
                ts = int(r[0])
                if ts in seen:
                    continue
                seen.add(ts)
                if ts < start_ms:
                    stop = True
                    continue
                if ts <= end_ms:
                    rows.append(r)
            oldest = min(int(r[0]) for r in page)
            if stop or oldest <= start_ms:
                break
            if cursor is not None and oldest >= cursor:
                break                      # no progress -> bail out
            cursor = oldest
            if on_page:
                on_page(len(rows))
        rows.sort(key=lambda r: int(r[0]))
        return rows

    def candles_range_until(self, inst_id: str, bar: str, start_ms: int, end_ms: int,
                            limit: int = 300) -> list:
        """Convenience wrapper used when the *newest* bar may predate end_ms (delisted)."""
        return self.candles_range(inst_id, bar, start_ms, end_ms, limit=limit)

    # -- funding -------------------------------------------------------------
    def funding_history(self, inst_id: str, start_ms: int, end_ms: int) -> list:
        rows: List[dict] = []
        cursor = None
        seen = set()
        for _ in range(2000):
            p = {"instId": inst_id, "limit": "100"}
            if cursor is not None:
                p["after"] = str(int(cursor))
            page = self.get("/api/v5/public/funding-rate-history", p)
            if not page:
                break
            stop = False
            for r in page:
                t = int(r["fundingTime"])
                if t in seen:
                    continue
                seen.add(t)
                if t < start_ms:
                    stop = True
                    continue
                if t <= end_ms:
                    rows.append({"fundingTime": t, "fundingRate": float(r["fundingRate"])})
            oldest = min(int(r["fundingTime"]) for r in page)
            if stop or oldest <= start_ms:
                break
            if cursor is not None and oldest >= cursor:
                break
            cursor = oldest
        rows.sort(key=lambda r: r["fundingTime"])
        return rows

    # -- parallel map --------------------------------------------------------
    def pmap(self, fn: Callable, items: Iterable, desc: str = "", verbose: bool = True):
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
                except Exception as e:                      # noqa: BLE001
                    out[it] = e
                done += 1
                if verbose and (done % 10 == 0 or done == len(items)):
                    el = time.time() - t0
                    print(f"  [{desc}] {done}/{len(items)}  {done/max(el,1e-9):.1f}/s  "
                          f"req={self.stats['req']} err={self.stats['err']}", flush=True)
        return out
