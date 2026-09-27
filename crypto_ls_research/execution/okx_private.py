"""Signed OKX v5 REST client for account / position / order endpoints.

Signing (OKX v5, private channels)
----------------------------------
    pre_sign = timestamp + METHOD + request_path_with_query + body
    sign     = base64( HMAC-SHA256( pre_sign, secret_key ) )

    timestamp : ISO-8601 UTC with milliseconds, e.g. ``2026-09-27T07:44:19.123Z``
                (OKX rejects anything more than 30 s off, code ``50114``)
    body      : the *exact* JSON string that is sent; ``""`` for GET

Because the signature covers the byte-for-byte request path and body, both are
built once and reused for signing and for sending.  Re-encoding the query
between those two steps is a classic source of ``50113 Invalid signature``.

Environment selection
---------------------
Demo trading is **the same host with an extra header**, not a different API:

    demo : https://www.okx.com  +  ``x-simulated-trading: 1``  + demo API key
    live : https://www.okx.com                                  + live API key

`OKX_BASE_URL` / `OKX_DEMO_BASE_URL` override the host if a region-specific
endpoint is needed.  A live API key presented in demo mode fails with
``50111 Invalid OK-ACCESS-KEY`` -- which is why `mode` is never guessed.

Retry policy
------------
GETs retry with exponential backoff.  **Order placement does not retry
blindly.**  A POST whose response never arrived is indistinguishable from one
the exchange never saw, and retrying it is how a rebalance places every order
twice.  Instead the order carries a deterministic ``clOrdId``; on an ambiguous
failure the client returns ``{"status": "unknown", "clOrdId": ...}`` and the
caller reconciles by querying that id.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence

from ..data.okx_client import RateLimiter

DEFAULT_BASE = os.environ.get("OKX_BASE_URL", "https://www.okx.com")
DEFAULT_DEMO_BASE = os.environ.get("OKX_DEMO_BASE_URL", "").strip()
DEFAULT_PROXY = os.environ.get("OKX_PROXY", "http://127.0.0.1:7897")

#: A round trip slower than this is reported.  Measured on this machine through
#: the local proxy: ~0.5-2.4s per call, so 2s separates "normal for this proxy"
#: from "the network is degrading" without crying wolf on every request.
SLOW_MS = 2000.0

#: OKX hard limits: 20 rows per batch endpoint.
MAX_ORDER_BATCH = 20
MAX_LEVERAGE_BATCH = 20

#: OKX codes worth translating.  Anything missing is reported verbatim -- an
#: unknown code is information, and paraphrasing it would destroy it.
ERROR_TEXT: Dict[str, str] = {
    "50011": "请求过于频繁（被限流），稍后重试",
    "50013": "系统繁忙",
    "50026": "系统维护中",
    "50100": "API 冻结",
    "50101": "API Key 与当前环境不匹配（实盘 key 用在模拟盘，或反之）",
    "50102": "时间戳已过期（>30 秒）",
    "50103": "请求头缺少 OK-ACCESS-KEY",
    "50104": "请求头缺少 OK-ACCESS-PASSPHRASE",
    "50105": "请求头缺少 OK-ACCESS-SIGN",
    "50106": "请求头缺少 OK-ACCESS-TIMESTAMP",
    "50107": "请求头时间戳格式错误",
    "50111": "API Key 无效——若这是模拟盘，请确认用的是"
             "「模拟盘专用」Key（实盘 Key 在模拟环境不被接受）",
    "50112": "API Key 已过期",
    "50113": "签名校验失败（secret 或签名串不对）",
    "50114": "请求时间与服务器时间相差过大（本机时钟可能不准）",
    "51000": "参数错误",
    "51001": "合约不存在或已下线",
    "51004": "委托数量小于最小下单量",
    "51005": "委托数量或金额超过上限",
    "51006": "委托价格不在允许范围内",
    "51008": "余额不足",
    "51009": "委托失败（可能是不允许同一方向重复开仓）",
    "51010": "当前账户模式不支持该操作",
    "51011": "重复的 clOrdId",
    "51020": "委托张数必须大于 0",
    "51121": "该合约无持仓可平",
    "51131": "可用保证金不足",
    "51169": "当前不存在该币种的持仓",
    "51603": "杠杆设置失败（可能超过该合约最大杠杆或存在持仓）",
    "59001": "保证金不足，下单被拒",
}


def describe_code(code: str, msg: str = "") -> str:
    t = ERROR_TEXT.get(str(code))
    if t:
        return f"{t}（{code}: {msg}）" if msg else f"{t}（{code}）"
    return f"OKX 错误 {code}: {msg}"


class OKXError(RuntimeError):
    """A definitive rejection from OKX (the exchange answered and said no)."""

    def __init__(self, code: str, msg: str = "", path: str = "", data: Any = None):
        self.code = str(code)
        self.msg = msg
        self.path = path
        self.data = data
        super().__init__(describe_code(self.code, msg))

    @property
    def is_auth(self) -> bool:
        return self.code in ("50101", "50102", "50103", "50104", "50105",
                             "50106", "50107", "50111", "50112", "50113", "50114")


class AmbiguousError(RuntimeError):
    """Transport-level failure: we do not know whether the exchange acted.

    Only raised for state-changing calls.  The caller must reconcile, not retry.
    """

    def __init__(self, path: str, cause: BaseException):
        self.path = path
        self.cause = cause
        super().__init__(f"{path}: 未收到确认响应（{type(cause).__name__}: {cause}）"
                         f"—— 结果未知，必须先对账，禁止直接重发")


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------
def iso_timestamp(ts: Optional[float] = None) -> str:
    """OKX wants `2020-03-28T12:21:41.274Z` -- ISO-8601 UTC, milliseconds."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None \
        else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sign(secret_key: str, timestamp: str, method: str, request_path: str,
         body: str = "") -> str:
    """Pure signing primitive -- unit-tested against the documented layout."""
    pre = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(secret_key.encode("utf-8"), pre.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def encode_query(params: Optional[dict]) -> str:
    """Deterministic query string, built once and reused for signing + sending."""
    if not params:
        return ""
    items = [(k, v) for k, v in sorted(params.items()) if v is not None]
    if not items:
        return ""
    return "?" + urllib.parse.urlencode(items, quote_via=urllib.parse.quote)


def encode_body(body: Optional[dict]) -> str:
    if body is None:
        return ""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
def make_clordid(prefix: str, inst_id: str, seq: int) -> str:
    """Deterministic, collision-resistant client order id (OKX: <=32 chars, [A-Za-z0-9]).

    Determinism is the whole point: if a POST times out we can ask the exchange
    "what happened to clOrdId X" instead of guessing.  An 8-hex-char digest of
    (prefix, instrument, seq) keeps two runs of the same rebalance from reusing
    each other's ids.
    """
    raw = f"{prefix}|{inst_id}|{seq}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    safe = "".join(c for c in f"{prefix}{inst_id}" if c.isalnum())[:20]
    return f"{safe}{h}{seq % 10}"[:32]


class OKXPrivate:
    """Signed client.  One instance per mode (`demo` / `live`)."""

    def __init__(self, creds, mode: str = "demo", base_url: Optional[str] = None,
                 proxy: Optional[str] = None, rate_per_sec: float = 8.0,
                 timeout: float = 20.0, retries: int = 3):
        if mode not in ("demo", "live"):
            raise ValueError(f"OKXPrivate mode must be demo|live, got {mode!r}")
        self.creds = creds
        self.mode = mode
        self.simulated = (mode == "demo")
        if base_url:
            self.base = base_url.rstrip("/")
        elif self.simulated and DEFAULT_DEMO_BASE:
            self.base = DEFAULT_DEMO_BASE.rstrip("/")
        else:
            self.base = DEFAULT_BASE.rstrip("/")
        self.proxy = DEFAULT_PROXY if proxy is None else proxy
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(rate_per_sec, burst=10)
        self._local = threading.local()
        self.stats = {"req": 0, "err": 0, "retry": 0, "slow": 0}
        self._rtt: Deque[float] = collections.deque(maxlen=64)
        self._lock = threading.Lock()
        #: Optional sink for human-readable notices (slow call, retry).  Wired to
        #: the job log by the engine, because a 40-second freeze with no output
        #: is indistinguishable from a hang -- which is exactly what "卡顿" is.
        self.on_notice: Optional[Any] = None

    # -- latency -----------------------------------------------------------
    def _record(self, ms: float, method: str, path: str, note: str = "") -> None:
        """Every attempt lands here, success or failure.

        The RTT distribution is what tells "the exchange is slow" apart from
        "we are doing too many round trips", and those have opposite fixes.
        """
        with self._lock:
            self._rtt.append(ms)
            slow = ms >= SLOW_MS
            if slow:
                self.stats["slow"] += 1
        if (slow or note) and self.on_notice:
            try:
                self.on_notice(f"{note}{method} {path.split('?')[0]} 耗时 {ms / 1000:.1f}s"
                               + ("（网络较慢）" if slow else ""))
            except Exception:                                     # noqa: BLE001
                pass                      # reporting must never break trading

    def latency(self) -> dict:
        """Round-trip statistics for the UI.  `None` means "no calls yet"."""
        with self._lock:
            s = sorted(self._rtt)
            out = {"requests": self.stats["req"], "errors": self.stats["err"],
                   "retries": self.stats["retry"], "slow": self.stats["slow"],
                   "timeout_s": self.timeout, "proxy": self.proxy, "base": self.base,
                   "samples": len(s),
                   "last_ms": round(s[-1]) if s else None,
                   "avg_ms": round(sum(s) / len(s)) if s else None,
                   "max_ms": round(s[-1]) if s else None}
        if s:
            out["p50_ms"] = round(s[len(s) // 2])
            out["p90_ms"] = round(s[min(len(s) - 1, int(0.9 * len(s)))])
            out["max_ms"] = round(s[-1])
        out["verdict"] = ("ok" if not s else
                          "slow" if (out["p50_ms"] or 0) >= SLOW_MS else "ok")
        return out

    # -- plumbing ----------------------------------------------------------
    def _opener(self):
        if not hasattr(self._local, "op"):
            handlers = []
            if self.proxy:
                handlers.append(urllib.request.ProxyHandler(
                    {"http": self.proxy, "https": self.proxy}))
            op = urllib.request.build_opener(*handlers)
            op.addheaders = [("User-Agent", "quant-research/1.0")]
            self._local.op = op
        return self._local.op

    def _headers(self, timestamp: str, signature: str) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.creds.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-PASSPHRASE": self.creds.passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
        }
        if self.simulated:
            h["x-simulated-trading"] = "1"
        return h

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None, *, idempotent: bool = True) -> Any:
        """One signed call.  Returns `data` from the envelope, or raises.

        `idempotent=False` marks a state-changing call: transport failures then
        raise `AmbiguousError` instead of being retried.
        """
        method = method.upper()
        query = encode_query(params)
        request_path = path + query
        raw_body = encode_body(body)
        payload = raw_body.encode("utf-8") if raw_body else None
        url = self.base + request_path

        attempts = self.retries if idempotent else 1
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            self.limiter.acquire()
            ts = iso_timestamp()
            sig = sign(self.creds.secret_key, ts, method, request_path, raw_body)
            req = urllib.request.Request(url, data=payload, method=method)
            for k, v in self._headers(ts, sig).items():
                req.add_header(k, v)
            t0 = time.time()
            failed = True
            try:
                try:
                    with self._opener().open(req, timeout=self.timeout) as r:
                        raw = r.read().decode("utf-8")
                    with self._lock:
                        self.stats["req"] += 1
                    failed = False
                    return self._unwrap(raw, request_path)
                except OKXError as e:
                    with self._lock:
                        self.stats["err"] += 1
                    # 50011/50013/50026 are transient; everything else is a verdict.
                    if e.code in ("50011", "50013", "50026", "50040") and attempt + 1 < attempts:
                        last = e
                    else:
                        raise
                except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                        TimeoutError) as e:
                    last = e
                    if not idempotent:
                        with self._lock:
                            self.stats["err"] += 1
                        raise AmbiguousError(request_path, e) from e
            finally:
                ms = (time.time() - t0) * 1000.0
                retried = failed and attempt + 1 < attempts
                if retried:
                    with self._lock:
                        self.stats["retry"] += 1
                # Every attempt is timed, including the ones that fail: a timeout
                # is the slowest response there is, and hiding it would hide the
                # very thing that made the desk feel frozen.
                self._record(ms, method, path,
                             note=f"第 {attempt + 1}/{attempts} 次失败，退避重试 · " if retried else "")
            if attempt + 1 < attempts:
                # Don't sleep after the last attempt: nothing follows it, so the
                # delay is pure latency on top of a call that already failed.
                time.sleep(min(6.0, 0.4 * (2 ** attempt)))
        with self._lock:
            self.stats["err"] += 1
        raise RuntimeError(f"{method} {request_path} failed: "
                           f"{type(last).__name__}: {last}")

    @staticmethod
    def _unwrap(raw: str, path: str) -> Any:
        try:
            js = json.loads(raw)
        except ValueError as e:
            raise RuntimeError(f"{path}: 响应不是 JSON: {raw[:200]!r}") from e
        code = str(js.get("code", ""))
        if code != "0":
            raise OKXError(code, js.get("msg", ""), path, js.get("data"))
        return js.get("data", [])

    # -- account -----------------------------------------------------------
    def account_config(self) -> dict:
        d = self.request("GET", "/api/v5/account/config")
        return d[0] if d else {}

    def balance(self, ccy: str = "USDT") -> dict:
        d = self.request("GET", "/api/v5/account/balance", {"ccy": ccy})
        return d[0] if d else {}

    def equity_usdt(self) -> float:
        """Total account equity, in USDT (`eq` of the USDT row, else `totalEq`)."""
        b = self.balance("USDT")
        for row in b.get("details") or []:
            if row.get("ccy") == "USDT":
                try:
                    return float(row.get("eq") or 0.0)
                except (TypeError, ValueError):
                    pass
        try:
            return float(b.get("totalEq") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def positions(self, inst_type: str = "SWAP", inst_id: Optional[str] = None) -> List[dict]:
        p: Dict[str, str] = {"instType": inst_type}
        if inst_id:
            p["instId"] = inst_id
        return self.request("GET", "/api/v5/account/positions", p)

    def set_leverage(self, inst_id: str, lever: float, mgn_mode: str = "cross") -> dict:
        d = self.request("POST", "/api/v5/account/set-leverage",
                         body={"instId": inst_id, "lever": str(lever),
                               "mgnMode": mgn_mode},
                         idempotent=True)
        return d[0] if d else {}

    def set_leverage_batch(self, items: Sequence[dict],
                           mgn_mode: str = "cross") -> Optional[List[dict]]:
        """One request for up to 20 instruments, instead of one per instrument.

        Returns `None` when the batch can't be trusted and the caller should fall
        back to `set_leverage` per instrument.  That happens when the response
        row count differs from the request row count -- geometry that misaligns
        is worse than round trips, because you'd end up recording leverage
        against the wrong instrument.
        """
        items = list(items)
        if not items:
            return []
        body = [{"instId": it["instId"], "lever": str(it["lever"]),
                 "mgnMode": it.get("mgnMode") or mgn_mode} for it in items]
        if len(body) > MAX_LEVERAGE_BATCH:
            raise ValueError(f"at most {MAX_LEVERAGE_BATCH} per batch, got {len(body)}")
        d = self.request("POST", "/api/v5/account/batch-set-leverage",
                         body=body, idempotent=True)
        if not isinstance(d, list) or len(d) != len(body):
            return None
        out = []
        for row, it in zip(d, body):
            r = dict(row)
            if r.get("instId") and r["instId"] != it["instId"]:
                return None                     # response was not aligned
            r.setdefault("instId", it["instId"])
            r["_ok"] = str(r.get("sCode", "0")) == "0"
            if not r["_ok"]:
                r["_error"] = describe_code(str(r.get("sCode")), r.get("sMsg", ""))
            r["_lever"] = it["lever"]
            out.append(r)
        return out

    def set_position_mode(self, pos_mode: str) -> dict:
        """`net_mode` | `long_short_mode`.  OKX refuses while positions exist."""
        d = self.request("POST", "/api/v5/account/set-position-mode",
                         body={"posMode": pos_mode}, idempotent=True)
        return d[0] if d else {}

    # -- orders ------------------------------------------------------------
    def place_order(self, inst_id: str, side: str, sz: str, *, pos_side: str = "net",
                    ord_type: str = "market", px: Optional[str] = None,
                    td_mode: str = "cross", cl_ord_id: Optional[str] = None,
                    reduce_only: bool = False, tgt_ccy: Optional[str] = None) -> dict:
        body: Dict[str, Any] = {
            "instId": inst_id, "tdMode": td_mode, "side": side,
            "posSide": pos_side, "ordType": ord_type, "sz": str(sz),
        }
        if px is not None:
            body["px"] = str(px)
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if reduce_only:
            body["reduceOnly"] = True
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        d = self.request("POST", "/api/v5/trade/order", body=body, idempotent=False)
        return self._row(d, inst_id=inst_id, cl_ord_id=cl_ord_id)

    def place_batch(self, orders: Sequence[dict]) -> List[dict]:
        """Up to 20 orders per request (OKX hard limit).  Per-order outcome.

        The envelope's `code` describes the *request*; each row carries
        `sCode`/`sMsg`.  A batch can therefore succeed at the HTTP level while
        every single order inside it is rejected -- so both must be checked, and
        the result rows are what the caller records.
        """
        orders = list(orders)
        if not orders:
            return []
        if len(orders) > MAX_ORDER_BATCH:
            raise ValueError(f"OKX accepts at most {MAX_ORDER_BATCH} orders per batch, "
                             f"got {len(orders)}")
        clean = []
        for o in orders:
            row = {k: (str(v) if not isinstance(v, bool) else v)
                   for k, v in o.items() if v is not None}
            clean.append(row)
        d = self.request("POST", "/api/v5/trade/orders", body=clean, idempotent=False)
        out = []
        for i, r in enumerate(d):
            rr = dict(r)
            rr["_instId"] = clean[i].get("instId") if i < len(clean) else None
            rr["_clOrdId"] = clean[i].get("clOrdId") if i < len(clean) else None
            sc = str(rr.get("sCode", "0"))
            rr["_ok"] = sc == "0"
            if sc != "0":
                rr["_error"] = describe_code(sc, rr.get("sMsg", ""))
            out.append(rr)
        return out

    def order_by_clordid(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        """Reconcile an ambiguous placement.  `51000`-ish misses return None."""
        try:
            d = self.request("GET", "/api/v5/trade/order",
                             {"instId": inst_id, "clOrdId": cl_ord_id})
        except OKXError as e:
            if e.code in ("51603", "51000", "51169"):     # "order does not exist"
                return None
            raise
        return d[0] if d else None

    def cancel_order(self, inst_id: str, ord_id: Optional[str] = None,
                     cl_ord_id: Optional[str] = None) -> dict:
        body = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        d = self.request("POST", "/api/v5/trade/cancel-order", body=body, idempotent=True)
        return self._row(d, inst_id=inst_id, cl_ord_id=cl_ord_id)

    def cancel_all(self, inst_type: str = "SWAP") -> List[dict]:
        """Cancel every pending order for the instrument type.  Used by the
        emergency stop, so it must not raise on a partial failure."""
        pending = self.orders_pending(inst_type)
        if not pending:
            return []
        out = []
        batch = [{"instId": p["instId"], "ordId": p["ordId"]} for p in pending if p.get("ordId")]
        for i in range(0, len(batch), 20):
            chunk = batch[i:i + 20]
            try:
                d = self.request("POST", "/api/v5/trade/cancel-batch-orders",
                                 body=chunk, idempotent=True)
                out.extend(d)
            except Exception as e:                                # noqa: BLE001
                out.append({"error": f"{type(e).__name__}: {e}"})
        return out

    def orders_pending(self, inst_type: str = "SWAP") -> List[dict]:
        return self.request("GET", "/api/v5/trade/orders-pending", {"instType": inst_type})

    def orders_history(self, inst_type: str = "SWAP", begin_ms: Optional[int] = None,
                       end_ms: Optional[int] = None, limit: int = 100) -> List[dict]:
        p: Dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if begin_ms:
            p["begin"] = str(int(begin_ms))
        if end_ms:
            p["end"] = str(int(end_ms))
        return self.request("GET", "/api/v5/trade/orders-history", p)

    def fills(self, inst_type: str = "SWAP", begin_ms: Optional[int] = None,
              end_ms: Optional[int] = None, limit: int = 100,
              history: bool = False) -> List[dict]:
        """Recent fills (3 days) or fill history (3 months)."""
        path = "/api/v5/trade/fills-history" if history else "/api/v5/trade/fills"
        p: Dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
        if begin_ms:
            p["begin"] = str(int(begin_ms))
        if end_ms:
            p["end"] = str(int(end_ms))
        return self.request("GET", path, p)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(d: Any, inst_id: str = "", cl_ord_id: Optional[str] = None) -> dict:
        """Single-order endpoints answer with a `{ordId, sCode, sMsg}` row, and the
        envelope `code` can be 0 while `sCode` is not."""
        row = dict(d[0]) if d else {}
        row.setdefault("instId", inst_id)
        if cl_ord_id:
            row.setdefault("clOrdId", cl_ord_id)
        sc = str(row.get("sCode", "0"))
        row["_ok"] = sc == "0"
        if sc != "0":
            row["_error"] = describe_code(sc, row.get("sMsg", ""))
        return row

    def probe(self) -> dict:
        """Cheap round-trip used by the UI.  Never raises -- reports instead."""
        out = {"mode": self.mode, "base": self.base, "simulated": self.simulated}
        try:
            cfg = self.account_config()
            out.update({"ok": True, "acctLv": cfg.get("acctLv"),
                        "posMode": cfg.get("posMode"), "uid": cfg.get("uid"),
                        "acctStpMode": cfg.get("acctStpMode")})
            try:
                out["equity_usdt"] = self.equity_usdt()
            except Exception as e:                                # noqa: BLE001
                out["equity_error"] = f"{type(e).__name__}: {e}"
        except OKXError as e:
            out.update({"ok": False, "code": e.code, "error": str(e),
                        "auth_problem": e.is_auth})
        except Exception as e:                                    # noqa: BLE001
            out.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return out
