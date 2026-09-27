"""Minimal Binance USDⓈ-M futures public client — used ONLY as a funding-history
fallback.

Why this exists
---------------
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
