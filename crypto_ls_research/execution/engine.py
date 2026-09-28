"""The trading desk: signal -> plan -> guardrails -> orders -> reconciliation.

Modes
-----
`paper` simulates fills locally against live public prices, charging the *same*
itemised cost model the backtest uses (`backtest.costs.trade_cost`).  It needs
no API key, which is the point: the whole loop is testable today, and a paper
fill is not a made-up number but the backtest's own cost function applied to a
real observed price.

`demo` and `live` send real orders -- to OKX's simulated environment and to the
real one respectively.  The only difference between them is the
`x-simulated-trading` header and which credential slot is used, so there is no
separate "demo code path" that can rot.

Order of operations on every execution (the order matters)
----------------------------------------------------------
1. build the target from the strategy,
2. read the account (net worth, current signed contract positions),
3. price everything, quantise to the lot grid, diff against current,
4. run every guardrail; **any** `block` violation aborts the whole batch,
5. only then send, in batches of 20 (OKX's limit), with deterministic
   `clOrdId`s so a lost response is reconciled rather than retried.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..config.settings import BARS_PER_DAY, CostConfig, SECONDS_PER_BAR
from ..data.okx_client import OKXClient
from ..data.store import CACHE
from .credentials import kill_switch_on, load_creds
from .limits import (LiveLimits, Violation, blocking, check_plan,  # noqa: F401
                     live_confirm_phrase)
from .okx_private import (MAX_LEVERAGE_BATCH, MAX_ORDER_BATCH, AmbiguousError,
                          OKXError, OKXPrivate, make_clordid)
from .planner import Order, Plan, build_plan, prices_from_cache
from .signal import LiveTarget, compute_live_target
from .specs import InstSpec, load_specs
from .store import (RebalanceBusy, Store, display_ts, new_run_id,  # noqa: F401
                    rebalance_lock)

#: The v3 verified configuration.  Kept here (not in webapp/spec.py) so the
#: execution layer does not depend on the UI.
DEFAULT_SIGNAL: dict = {
    "bar": "1h",
    "rebalance_days": 3.0,
    "asset_class": "crypto",
    "overrides": {
        "factors.subset": ["range_pos", "hitrate"],
        "portfolio.max_weight_per_instrument": 0.20,
        "execution.max_daily_turnover": 0.20,
    },
}

PRICE_TTL = 15.0          # seconds; public tickers are re-fetched at most this often
_PUBLIC = {"cli": None, "lock": None}

#: A cache older than this is reported as stale.  12h at 1h bars = half a
#: trading day; the signal steps every 3 days, so a half-day lag changes the
#: bars the decision is taken on without looking any different in the UI.
STALE_AFTER_HOURS = 12.0
_NEWEST_CACHE: Dict[str, tuple] = {}


def _age_str(h) -> str:
    if h is None:
        return "未知"
    if h < 1:
        return f"{h * 60:.0f} 分钟"
    if h < 48:
        return f"{h:.1f} 小时"
    return f"{h / 24:.1f} 天"


def candle_staleness(bar: str = "1h") -> dict:
    """How old is the newest candle file in the cache?

    Uses the file mtime rather than the newest bar timestamp on purpose: the
    mtime answers "did the download actually run", which is the question a
    refresh button needs to answer.
    """
    cdir = os.path.join(CACHE, "candles", bar)
    newest = None
    try:
        for f in os.listdir(cdir):
            if not f.endswith(".parquet"):
                continue
            mt = os.path.getmtime(os.path.join(cdir, f))
            newest = mt if newest is None or mt > newest else newest
    except OSError:
        newest = None
    ages_h = None if newest is None else (time.time() - newest) / 3600.0
    return {"bar": bar, "cache_mtime": newest, "age_hours": ages_h,
            "stale": bool(ages_h is not None and ages_h > STALE_AFTER_HOURS)}


def newest_cached_bar(bar: str = "1h",
                      inst: str = "BTC-USDT-SWAP") -> Optional[str]:
    """Newest *bar timestamp* the signal will actually see, as ISO, or None.

    Distinct from `candle_staleness`, which reports file mtime.  This one answers
    "how recent is the data", which is the question that decides signal quality.

    Probed on one reference instrument, not as `max()` over the pool: the max is
    a lie whenever even a single symbol got updated, and it costs a scan of 161
    parquet footers.  If the reference is missing we fall back to a bounded scan
    (so a pool without BTC still reports something honest).
    """
    now = time.time()
    key = f"{bar}|{inst}"
    hit = _NEWEST_CACHE.get(key)
    if hit and now - hit[0] < 60.0:
        return hit[1]

    cdir = os.path.join(CACHE, "candles", bar)
    cands = [os.path.join(cdir, f"{inst}.parquet")]
    if not os.path.exists(cands[0]):
        try:
            cands = [os.path.join(cdir, f) for f in sorted(os.listdir(cdir))
                     if f.endswith(".parquet")][:400]
        except OSError:
            return None
    best = None
    for path in cands:
        try:
            idx = pd.read_parquet(path, columns=[]).index
        except Exception:                                      # noqa: BLE001
            continue
        if not len(idx):
            continue
        t = idx[-1]
        best = t if best is None or t > best else best
    out = None if best is None else best.isoformat()
    _NEWEST_CACHE[key] = (now, out)
    return out


def staleness(bar: str = "1h") -> dict:
    """How recent is the data the signal will see?

    Two independent numbers, because they fail differently:

    * `age_hours` -- file mtime age.  Answers "did the refresh actually run",
      which is what the refresh button must verify.
    * `bar_age_hours` -- age of the newest *closed bar* on the reference
      instrument.  Answers "is the decision being taken on current prices".
      A cache can have a fresh mtime and a stale bar if a download wrote a
      partial tail, so this is the one that gates trading.

    `stale` is true if **either** is over the threshold: a live signal on stale
    candles is worse than no signal, because it looks fresh.
    """
    out = candle_staleness(bar)
    nb = newest_cached_bar(bar)
    out["newest_bar"] = nb
    out["bar_age_hours"] = None
    if nb:
        try:
            lag = pd.Timestamp.now(tz="UTC") - pd.Timestamp(nb)
            out["bar_age_hours"] = lag.total_seconds() / 3600.0
        except Exception:                                      # noqa: BLE001
            out["bar_age_hours"] = None
    ba = out["bar_age_hours"]
    out["stale"] = bool(out["stale"] or (ba is not None and ba > STALE_AFTER_HOURS))
    return out


def _public_client() -> OKXClient:
    import threading
    if _PUBLIC["lock"] is None:
        _PUBLIC["lock"] = threading.Lock()
    with _PUBLIC["lock"]:
        if _PUBLIC["cli"] is None:
            _PUBLIC["cli"] = OKXClient()
    return _PUBLIC["cli"]


# ---------------------------------------------------------------------------
def daily_vol_map(insts: Sequence[str], bar: str = "1h", window: int = 60) -> Dict[str, float]:
    """Annualised vol from the cached candles -- the same primitive the backtest uses."""
    bpd = BARS_PER_DAY.get(bar, 24)
    out: Dict[str, float] = {}
    cdir = os.path.join(CACHE, "candles", bar)
    for inst in insts:
        p = os.path.join(cdir, f"{inst}.parquet")
        if not os.path.exists(p):
            continue
        try:
            c = pd.read_parquet(p, columns=["close"])["close"].astype("float64")
        except Exception:                                        # noqa: BLE001
            continue
        c = c.replace(0.0, np.nan).dropna()
        if len(c) < 10:
            continue
        r = np.log(c).diff().dropna().to_numpy()[-window:]
        sd = float(np.std(r, ddof=0))
        if math.isfinite(sd):
            out[inst] = sd * math.sqrt(bpd * 365.0)
    return out


# ---------------------------------------------------------------------------
def _clordid_prefix(run_id: str) -> str:
    """A per-**run** clOrdId prefix, derived from the whole run id.

    This used to be `run_id.replace("_", "")[:10]`.  For
    `demo_20260928_150854_862ed8` that is `demo202609` -- the mode plus the
    *year and month*.  The date, the time and the random suffix, i.e.
    everything that makes one run different from the next, were truncated away,
    so every rebalance in the same month minted byte-identical client order ids
    for the same (instrument, index).  The ledger then cannot say which run an
    order belongs to, and `order_by_clordid` reconciles against the wrong one.

    A digest of the full run id keeps the readable mode prefix, stays inside the
    id's 20-char readable budget, and is unique per run by construction rather
    than by hoping the surviving fragment was the distinctive one.
    """
    mode = (run_id.split("_") or [""])[0][:4] or "run"
    return mode + hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
class LiveEngine:
    """One engine per mode.  Holds no long-lived connection; REST only."""

    def __init__(self, mode: str = "paper", limits: Optional[LiveLimits] = None,
                 signal_kwargs: Optional[dict] = None,
                 costs: Optional[CostConfig] = None,
                 ord_type: str = "market", limit_buffer_bps: float = 15.0,
                 td_mode: str = "cross", set_leverage: Optional[float] = None):
        if mode not in ("paper", "demo", "live"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.store = Store(mode)
        # The saved caps beat the factory defaults.  They used to live only in
        # the caller's memory (`live_api._ENGINES`), so restarting the console
        # silently reverted every risk limit to `LiveLimits()` -- indistinguishable
        # from "the save button does nothing", which is exactly how it was
        # reported.  `limits=` still wins when a caller passes them explicitly.
        self.limits = limits if limits is not None else LiveLimits.from_dict(
            self.store.load_limits())
        self.signal_kwargs = {**DEFAULT_SIGNAL, **(signal_kwargs or {})}
        self.costs = costs or CostConfig()
        self.ord_type = ord_type
        self.limit_buffer_bps = limit_buffer_bps
        self.td_mode = td_mode
        self.set_leverage = set_leverage
        self._last_leverage_notes: List[dict] = []
        self._prices: Dict[str, float] = {}
        self._prices_ts = 0.0
        self._specs: Dict[str, InstSpec] = {}
        self._signals = {"msg": "未开始", "ts": None, "ok": None}
        self._cli: Optional[tuple] = None      # (credential key, OKXPrivate)
        #: `(ts, set_or_None)` for the venue's tradable instrument list.
        self._venue: tuple = (0.0, None)
        #: Set by whoever runs the job; slow/retry notices are streamed here.
        self._net_notice: Optional[Any] = None

    #: The tradable-instrument list is stable; the endpoint's failure mode is
    #: not, so a failure is cached far more briefly than a success.
    _VENUE_TTL = 600.0
    _VENUE_ERR_TTL = 60.0

    def venue_instruments(self) -> Optional[set]:
        """Instrument ids the *current venue* will accept, or `None` if unknown.

        The demo and live universes are not the same pool: live carries ~492
        USDT swaps, demo about 185.  The contract-spec cache is built from
        public *live* data, so the planner will happily size legs the demo
        exchange has never heard of.  They come back as `51001 合约不存在或已下线`
        only *after* the orders are sent, inside a batch whose envelope code
        says nothing about which rows failed -- so the failure arrives without
        a usable reason.  One request up front moves it into the plan.

        Best effort by design: this is a public endpoint, and a network hiccup
        must never block a rebalance.  On failure this returns `None` and the
        gate is skipped rather than guessed.
        """
        now = time.time()
        ts, val = self._venue
        if (now - ts) < (self._VENUE_TTL if val is not None else self._VENUE_ERR_TTL):
            return val
        try:
            rows = self._private().request("GET", "/api/v5/public/instruments",
                                           {"instType": "SWAP"})
            val = {str(r.get("instId")) for r in rows
                   if str(r.get("state") or "live") == "live"} or None
        except Exception:                                          # noqa: BLE001
            val = None
        self._venue = (now, val)
        return val

    # ================= connectivity =================
    def _private(self) -> OKXPrivate:
        """The signed client, **cached per credentials**.

        Rebuilding it per call looked harmless but cost real time: every
        `OKXPrivate` carries its own urllib opener, so a fresh instance means a
        fresh TCP+TLS handshake per request.  It also reset the latency stats,
        which is precisely the evidence you need when the desk feels slow.
        """
        c = load_creds(self.mode)
        if c is None:
            raise RuntimeError(
                f"{self.mode} 模式尚未配置 API Key。"
                + ("OKX 模拟盘需要用「模拟盘专用」Key。" if self.mode == "demo" else ""))
        key = (c.api_key, getattr(c, "secret_key", ""), getattr(c, "passphrase", ""))
        cached = self._cli
        if cached is None or cached[0] != key:
            client = OKXPrivate(c, mode=self.mode)
            self._cli = (key, client)
        client = self._cli[1]
        client.on_notice = getattr(self, "_net_notice", None)
        return client

    def net_latency(self) -> dict:
        """Round-trip stats for the UI; empty dict until the first exchange call."""
        cli = self._cli[1] if self._cli else None
        return cli.latency() if cli else {}

    def specs(self) -> Dict[str, InstSpec]:
        if not self._specs:
            self._specs = load_specs()
        return self._specs

    def target(self, force_signal: bool = False, ttl: float = 1800.0) -> LiveTarget:
        self._signals = {"msg": "正在计算信号（复用回测引擎，约 40 秒）",
                         "ts": time.time(), "ok": None}
        try:
            t = compute_live_target(use_cache=not force_signal, ttl=ttl,
                                    **self.signal_kwargs)
        except Exception as e:                                    # noqa: BLE001
            self._signals = {"msg": f"信号计算失败：{type(e).__name__}: {e}",
                             "ts": time.time(), "ok": False}
            raise
        self._signals = {"msg": "信号就绪", "ts": time.time(), "ok": True,
                         "decision_ts": t.decision_ts, "from_cache": t.from_cache}
        return t

    # ================= market data =================
    def prices(self, insts: Optional[Sequence[str]] = None,
               force: bool = False) -> Dict[str, float]:
        """Live public last prices.  Falls back to the candle cache when offline."""
        now = time.time()
        if force or not self._prices or (now - self._prices_ts) > PRICE_TTL:
            live: Dict[str, float] = {}
            err = None
            try:
                for r in _public_client().tickers("SWAP"):
                    try:
                        px = float(r.get("last") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if px > 0:
                        live[r["instId"]] = px
            except Exception as e:                                # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            if live:
                self._prices = live
                self._prices_ts = now
                self._prices_err = None
            else:
                self._prices_err = err or "行情接口无返回"
        if insts is None:
            return dict(self._prices)
        missing = [i for i in insts if i not in self._prices]
        out = {i: self._prices[i] for i in insts if i in self._prices}
        if missing:
            out.update(prices_from_cache(missing, self.signal_kwargs.get("bar", "1h")))
        return out

    @property
    def prices_source(self) -> str:
        return "okx_tickers" if self._prices_ts and (time.time() - self._prices_ts) < 3600 \
            else "candle_cache"

    # ================= account =================
    def account(self) -> dict:
        if self.mode == "paper":
            return self._paper_account()
        cli = self._private()
        cfg = cli.account_config()
        pos_mode = cfg.get("posMode") or "net_mode"
        # `positions()` and `equity_usdt()` are two **independent** signed round
        # trips -- measured ~0.31s and ~0.30s on this link.  Run in series they
        # cost the sum, and the desk reloads this after *every* action (save
        # limits, kill switch, reconcile, reset), so the sum is exactly the lag
        # the user feels.  Neither needs the other's answer.
        #
        # Concurrency here is not a hack: `OKXPrivate` already keeps a
        # per-thread urllib opener (`self._local`), a locked RTT deque and a
        # locked rate limiter -- it was built to be called from more than one
        # thread.  Measured 616ms -> ~320ms.
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_pos = pool.submit(cli.positions, "SWAP")
            fut_nav = pool.submit(cli.equity_usdt)
            rows = fut_pos.result()
            nav = fut_nav.result()
        sp = self.specs()                    # cached in-process; needed for notional
        cur_sz: Dict[str, float] = {}
        marks: Dict[str, float] = {}
        detail: List[dict] = []
        for r in rows:
            inst = r.get("instId")
            if not inst:
                continue
            try:
                sz = float(r.get("pos") or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(sz) <= 1e-12:
                continue
            if str(r.get("posSide")) == "short":
                sz = -abs(sz)
            elif str(r.get("posSide")) == "long":
                sz = abs(sz)
            cur_sz[inst] = cur_sz.get(inst, 0.0) + sz
            mp = 0.0
            try:
                mp = float(r.get("markPx") or 0.0)
                if mp > 0:
                    marks[inst] = mp
            except (TypeError, ValueError):
                pass
            # The console renders position notional as |sz| * ctVal * markPx and
            # deliberately refuses to guess it client-side (dropping ctVal is a
            # 100x error on BTC).  It used to receive no `notional` at all here,
            # so *every* position was reported as "contract spec missing,
            # probably delisted" -- wrong for live instruments.  Send it, and
            # send None only when the spec genuinely is unknown.
            spec = sp.get(inst)
            notional = (abs(sz) * spec.ct_val * mp
                        if (spec is not None and mp > 0) else None)
            detail.append({"instId": inst, "pos": sz, "posSide": r.get("posSide"),
                           "avgPx": _f(r.get("avgPx")), "markPx": _f(r.get("markPx")),
                           "upl": _f(r.get("upl")), "lever": r.get("lever"),
                           "mgnMode": r.get("mgnMode"), "liqPx": _f(r.get("liqPx")),
                           "notional": notional})
        nav = fut_nav.result()
        return {"mode": self.mode, "nav": nav, "cur_sz": cur_sz, "marks": marks,
                "positions": detail, "pos_mode": pos_mode,
                "acctLv": cfg.get("acctLv"), "uid": cfg.get("uid"),
                "leverage": cfg.get("lever"), "source": "okx",
                "equity_error": None if nav > 0 else "净值读取为 0"}

    # ---- paper account ---------------------------------------------------
    def _paper_state(self) -> dict:
        s = self.store.read_state()
        if "cash" not in s:
            s = {"mode": "paper", "cash": float(os.environ.get("PAPER_NAV", 1000.0)),
                 "nav0": float(os.environ.get("PAPER_NAV", 1000.0)),
                 "positions": {}, "pos_mode": "net_mode", "created": time.time()}
            self.store.write_state(s)
        return s

    def _paper_account(self) -> dict:
        s = self._paper_state()
        insts = list((s.get("positions") or {}).keys())
        px = self.prices(insts) if insts else {}
        specs = self.specs()
        unreal = 0.0
        detail = []
        cur_sz: Dict[str, float] = {}
        marks: Dict[str, float] = {}
        for inst, p in (s.get("positions") or {}).items():
            sz = float(p.get("sz") or 0.0)
            avg = float(p.get("avg_px") or 0.0)
            cur_sz[inst] = sz
            m = px.get(inst) or avg
            marks[inst] = m
            spec = specs.get(inst)
            ct = spec.ct_val if spec else 0.0
            upl = (m - avg) * sz * ct if ct else 0.0
            unreal += upl
            detail.append({"instId": inst, "pos": sz, "posSide": "net",
                           "avgPx": avg, "markPx": m, "upl": upl,
                           "notional": abs(sz) * ct * m if ct else None,
                           "lever": None, "liqPx": None, "mgnMode": "paper"})
        nav = float(s.get("cash", 0.0)) + unreal
        return {"mode": "paper", "nav": nav, "cur_sz": cur_sz, "marks": marks,
                "positions": detail, "pos_mode": "net_mode", "acctLv": "paper",
                "uid": "paper", "unrealised": unreal, "cash": float(s.get("cash", 0.0)),
                "source": "local", "nav0": s.get("nav0")}

    # ================= planning =================
    @property
    def turnover_budget(self) -> Optional[float]:
        """`execution.max_daily_turnover`, read from the signal overrides.

        This is not a cosmetic parameter: with v3 the *backtested* book averages
        0.472 gross against a raw target of 1.032, because the 0.20/period budget
        never lets it catch up.  The reported Sharpe belongs to the throttled
        book, so live must throttle identically.
        """
        v = (self.signal_kwargs.get("overrides") or {}).get(
            "execution.max_daily_turnover")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def build(self, target: LiveTarget, acct: Optional[dict] = None,
              only: Optional[Sequence[str]] = None,
              turnover_budget: Optional[float] = None,
              use_budget: bool = True) -> Tuple[Plan, dict, Dict[str, float]]:
        acct = acct or self.account()
        insts = sorted(set(target.weights) | set(acct["cur_sz"]))
        px = self.prices(insts)
        px.update({k: v for k, v in (acct.get("marks") or {}).items() if k not in px})
        adv = {r["instId"]: r["adv"] for r in target.pool
               if r.get("adv")}
        adv.update({r["instId"]: r["adv"] for r in target.book if r.get("adv")})
        _, used = self.store.turnover_state()
        budget = self.turnover_budget if turnover_budget is None else turnover_budget
        if not use_budget:
            budget = None
        plan = build_plan(
            target.weights, acct["nav"], current_sz=acct["cur_sz"], prices=px,
            adv=adv, specs=self.specs(), pos_mode=acct.get("pos_mode", "net_mode"),
            td_mode=self.td_mode, ord_type=self.ord_type,
            limit_buffer_bps=self.limit_buffer_bps,
            turnover_budget=budget, turnover_used=used,
            max_adv_participation=self.limits.max_adv_participation,
            min_order_notional=float(os.environ.get("LIVE_MIN_ORDER_USD", 0) or 0))
        if only:
            keep = set(only)
            plan.orders = [o for o in plan.orders if o.inst_id in keep]
            plan.order_notional = sum(o.delta_notional for o in plan.orders)
            plan.turnover_frac = plan.order_notional / acct["nav"] if acct["nav"] > 0 else 0.0
        return plan, acct, px

    def preview(self, force_signal: bool = False,
                only: Optional[Sequence[str]] = None,
                progress=None) -> dict:
        p = progress or (lambda m: None)
        self._net_notice = p
        t0 = time.time()
        p("① 计算策略目标权重（复用回测引擎，首次约 40–60 秒）")
        target = self.target(force_signal=force_signal)
        p(f"② 信号日 {display_ts(target.decision_ts)} · 池宽 "
          f"{target.diagnostics.get('n_universe')} · 目标毛敞口 {target.gross:.4f}")
        p("③ 读取账户与持仓")
        acct = self.account()
        p(f"④ 净值 {acct['nav']:.2f} USDT · 现有持仓 {len(acct['cur_sz'])} 个")
        p("⑤ 拉取实时行情并量化到合约张数")
        plan, acct, px = self.build(target, acct, only=only)
        p(f"⑥ 生成 {plan.n_orders} 笔订单 · 名义额 {plan.order_notional:,.0f} USDT · "
          f"覆盖率 {plan.coverage:.1%}")
        violations = check_plan(plan, self.limits, mode=self.mode, nav=acct["nav"],
                                rebalance_due=target.rebalance_due,
                                leverage=self.set_leverage,
                                venue_insts=self.venue_instruments())
        stale = self.data_staleness()
        if stale.get("stale"):
            violations.append(Violation(
                "warn", "stale_data",
                f"行情缓存已过期（{stale['age_hours']:.1f} 小时未更新）",
                "信号用的是缓存里最后一根已收盘 bar。先刷新行情再下单，"
                "否则会按几小时前（甚至几天前）的价格决策。"))
        blocked = blocking(violations)
        p("⑦ 风控闸门：需要拦截" if blocked else "⑦ 风控闸门：全部通过 ✓")
        return {
            "mode": self.mode, "elapsed": round(time.time() - t0, 2),
            "target": target.as_dict(), "plan": plan.to_dict(),
            "account": {k: v for k, v in acct.items() if k != "marks"},
            "violations": [v.to_dict() for v in violations],
            "blocked": bool(blocked),
            "limits": self.limits.to_dict(),
            "data_staleness": stale,
            "confirm_phrase": live_confirm_phrase() if self.mode == "live" else None,
            "signal_state": dict(self._signals),
        }

    # ================= execution =================
    def execute(self, *, confirm: Optional[str] = None, force: bool = False,
                dry_run: bool = True, only: Optional[Sequence[str]] = None,
                force_signal: bool = False, progress=None) -> dict:
        """`_execute_inner`, wrapped in the cross-process rebalance lock.

        The console and the unattended auto-trader are **separate processes**
        over the same `artifacts/live/<mode>/`.  Two concurrent executes each
        read the same starting book and each send the full order list, so every
        position is placed twice -- and `AmbiguousError` is no help, because
        nothing was lost, it was duplicated.  Fails fast, before the signal is
        even computed.
        """
        with rebalance_lock(self.mode):
            return self._execute_inner(confirm=confirm, force=force,
                                       dry_run=dry_run, only=only,
                                       force_signal=force_signal,
                                       progress=progress)

    def _execute_inner(self, *, confirm: Optional[str] = None, force: bool = False,
                       dry_run: bool = True, only: Optional[Sequence[str]] = None,
                       force_signal: bool = False, progress=None) -> dict:
        p = progress or (lambda m: None)
        # Slow or retried exchange calls are streamed to whoever is watching the
        # run, because a silent wait and a hang look identical from the UI.
        self._net_notice = p
        run_id = new_run_id(self.mode)
        t0 = time.time()
        p("① 计算策略目标权重")
        target = self.target(force_signal=force_signal)
        p(f"② 信号日 {display_ts(target.decision_ts)} · 目标毛敞口 {target.gross:.4f}")
        acct = self.account()
        p(f"③ 净值 {acct['nav']:.2f} USDT · 现有持仓 {len(acct['cur_sz'])} 个")
        plan, acct, px = self.build(target, acct, only=only)
        nav = acct["nav"]
        p(f"④ 计划 {plan.n_orders} 笔订单 · 名义额 {plan.order_notional:,.0f} USDT")
        violations = check_plan(plan, self.limits, mode=self.mode, nav=nav,
                                rebalance_due=target.rebalance_due, force=force,
                                leverage=self.set_leverage,
                                venue_insts=self.venue_instruments())
        blocked = blocking(violations)
        p("⑤ 风控闸门拦截，未发送任何订单" if blocked else "⑤ 风控闸门通过 ✓")
        record: dict = {
            "run_id": run_id, "mode": self.mode, "ts": time.time(),
            "action": "rebalance", "dry_run": bool(dry_run) or bool(blocked),
            "decision_ts": target.decision_ts, "nav": nav,
            "n_orders": plan.n_orders, "n_ok": 0, "n_fail": 0,
            "coverage": plan.coverage, "weight_err": plan.weight_err,
            "order_notional": plan.order_notional,
            "turnover_frac": plan.turnover_frac,
            "gross_target": plan.target_gross, "gross_realised": plan.realised_gross,
            "violations": [v.to_dict() for v in violations],
            "results": [], "errors": [], "elapsed": None,
            "forced": bool(force),
        }

        if blocked:
            record["errors"] = [v.title for v in blocked]
            record["elapsed"] = round(time.time() - t0, 2)
            self.store.add_run(record)
            return self._result(record, plan, target, acct, violations, px,
                                stage="blocked")

        if dry_run:
            record["elapsed"] = round(time.time() - t0, 2)
            self.store.add_run(record)
            return self._result(record, plan, target, acct, violations, px,
                                stage="dry_run")

        if self.mode == "live":
            want = live_confirm_phrase()
            if (confirm or "").strip() != want:
                v = {"sev": "block", "key": "confirm",
                     "title": "实盘确认短语不正确",
                     "body": f"需要输入 <span class='mono'>{want}</span> 才会真正下单。"}
                record["violations"] = record["violations"] + [v]
                record["errors"] = [v["title"]]
                record["elapsed"] = round(time.time() - t0, 2)
                self.store.add_run(record)
                return self._result(record, plan, target, acct, violations, px,
                                    stage="confirm_failed")

        if plan.n_orders == 0:
            record["elapsed"] = round(time.time() - t0, 2)
            self.store.add_run(record)
            return self._result(record, plan, target, acct, violations, px,
                                stage="no_orders")

        results = self._send(plan, run_id, target, log=p)
        p(f"⑥ 已提交 {len(results)} 笔订单"
          + ("（本地模拟成交）" if self.mode == "paper" else "，等待交易所回报"))
        # Leverage is set before the orders go out, and it is per-instrument.
        # If it failed anywhere the book is not backed by the margin we sized
        # against -- so it goes into the record (visible) instead of being
        # dropped on the floor.
        lev_notes = getattr(self, "_last_leverage_notes", [])
        self._last_leverage_notes = []
        if lev_notes:
            record["leverage"] = lev_notes
            failed = [n for n in lev_notes if not n["ok"]]
            capped = [n for n in lev_notes if n["capped"]]
            if failed:
                record["violations"] = record["violations"] + [{
                    "sev": "warn", "key": "leverage_failed",
                    "title": f"{len(failed)} 个合约未能设置杠杆",
                    "body": "实际杠杆可能不是你以为的值，保证金占用与预期不符："
                            + "、".join(f"{n['instId']}（{n['error']}）" for n in failed[:5])
                            + ("…" if len(failed) > 5 else "") + "。请到交易所确认后再加仓。"}]
            if capped:
                record["violations"] = record["violations"] + [{
                    "sev": "warn", "key": "leverage_capped",
                    "title": f"{len(capped)} 个合约被交易所上限压低",
                    "body": "交易所对该合约的杠杆上限低于你的设置，已按较低值设置："
                            + "、".join(f"{n['instId']} → {n['leverage']}×（上限 {n['max_lever']}×）"
                                        for n in capped[:5])
                            + ("…" if len(capped) > 5 else "") + "。"}]
        record["results"] = results
        record["n_ok"] = sum(1 for r in results if r.get("ok"))
        record["n_fail"] = len(results) - record["n_ok"]
        record["errors"] = [f"{r.get('instId')}: {r.get('error')}"
                            for r in results if not r.get("ok")]
        record["elapsed"] = round(time.time() - t0, 2)
        # `plan.realised_gross` is the book the plan *wanted*.  Once the results
        # are in, the honest number is the one the account will actually hold --
        # a rejected leg leaves its current position standing.  Recording the
        # intended figure here claimed 10,842 USDT of exposure from 19 orders of
        # which 11 filled, and that claim then flowed into `add_equity(...)`.
        gross, net = self._realised_book(plan, results, acct)
        record["gross_realised"] = gross
        record["realised_net"] = net
        # Charged here rather than in `_after_execute` because `add_run` serialises
        # the record immediately -- a field added afterwards would never reach the
        # ledger, and "how much budget did this run eat" is exactly the thing that
        # must be auditable.
        record["turnover_charged"] = self._charged_turnover(plan, results, nav)
        self.store.add_run(record)
        p(f"⑦ 成交/受理 {record['n_ok']}/{len(results)} 笔"
          + (f" · 换手预算已用 {record['turnover_charged']:.2%}"
             if plan.turnover_budget is not None else ""))
        self._after_execute(target, acct, plan, record)
        return self._result(record, plan, target, acct, violations, px, stage="sent")

    def _send(self, plan: Plan, run_id: str, target: LiveTarget,
              log: Any = None) -> List[dict]:
        if self.mode == "paper":
            return self._send_paper(plan, target, run_id)
        return self._send_okx(plan, run_id, log=log)

    # ---- paper fills -----------------------------------------------------
    def _send_paper(self, plan: Plan, target: LiveTarget, run_id: str) -> List[dict]:
        s = self._paper_state()
        nav = self.account()["nav"]
        adv = {r["instId"]: r["adv"] for r in target.pool if r.get("adv")}
        volv = daily_vol_map([o.inst_id for o in plan.orders],
                             self.signal_kwargs.get("bar", "1h"))
        fills, results = [], []
        for o in plan.orders:
            px = self.prices([o.inst_id]).get(o.inst_id, o.price)
            spec = self.specs().get(o.inst_id)
            ct = spec.ct_val if spec else 0.0
            dq = o.sz if o.side == "buy" else -o.sz
            notional = abs(dq) * ct * px
            dw = np.array([notional / nav]) if nav > 0 else np.array([0.0])
            cost = _trade_cost_scalar(dw, adv.get(o.inst_id), nav,
                                      volv.get(o.inst_id), self.costs)
            pos = (s.get("positions") or {}).get(o.inst_id, {"sz": 0.0, "avg_px": 0.0})
            new_sz, new_avg, pnl = _apply_fill(float(pos.get("sz") or 0.0),
                                               float(pos.get("avg_px") or 0.0),
                                               dq, px, ct)
            s.setdefault("positions", {})[o.inst_id] = {"sz": new_sz, "avg_px": new_avg}
            if abs(new_sz) <= 1e-12:
                s["positions"].pop(o.inst_id, None)
            s["cash"] = float(s.get("cash", 0.0)) + pnl - cost
            cid = make_clordid(_clordid_prefix(run_id), o.inst_id, len(results))
            fill = {"tradeId": cid + "P", "clOrdId": cid, "instId": o.inst_id,
                    "side": o.side, "px": px, "sz": o.sz, "fee": -cost,
                    "notional": notional, "ct_val": ct,
                    "ts": time.time(), "mode": "paper", "action": o.action,
                    "simulated": True}
            fills.append(fill)
            results.append({"instId": o.inst_id, "clOrdId": cid, "ordId": None,
                            "ok": True, "sz": o.sz, "side": o.side,
                            "action": o.action, "px": px, "notional": notional,
                            "cost": cost, "error": None, "simulated": True})
        self.store.write_state(s)
        self.store.add_fills(fills)
        self.store.put_orders([{
            "clOrdId": r["clOrdId"], "instId": r["instId"], "side": r["side"],
            "sz": r["sz"], "action": r["action"], "state": "filled",
            "px": r["px"], "notional": r["notional"], "cost": r["cost"],
            "mode": "paper", "ts": time.time(), "run_id": run_id, "simulated": True,
        } for r in results])
        return results

    # ---- real orders -----------------------------------------------------
    def _apply_leverage(self, plan: Plan) -> List[dict]:
        """Set the account leverage on every name about to be traded.

        Why every name rather than once per account: OKX keeps leverage *per
        instrument per margin mode*, so "the account is 5x" is not a thing you
        can set.  A name we never set stays at whatever it was -- which is
        exactly the failure that must not be silent: you think you are 5x,
        you are actually 20x (or 1x), and the margin that backs the book is not
        the margin you sized against.

        So every failure is returned as a note and surfaced, never swallowed.

        It is however per instrument *within one request*: OKX has
        `/account/batch-set-leverage` (≤20 rows), and going one-request-per-name
        made a 19-name rebalance pay ~19 round trips before a single order went
        out.  The batch response is positionally aligned; if anything about it
        doesn't line up we fall back to per-name calls rather than guess.
        """
        if not self.set_leverage:
            return []
        cli = self._private()
        notes: List[dict] = []
        want = float(self.set_leverage)
        items = []
        for o in plan.orders:
            spec = self.specs().get(o.inst_id)
            cap = spec.max_lever if (spec and spec.max_lever) else None
            lev = min(want, cap) if cap else want
            items.append({"instId": o.inst_id, "lever": lev,
                          "capped": bool(cap and cap < want - 1e-9),
                          "max_lever": cap})
        truth = {it["instId"]: it for it in items}

        def one_note(it: dict, ok: bool, err: Optional[str]) -> dict:
            return {"instId": it["instId"], "ok": ok,
                    "leverage": it["lever"] if ok else None,
                    "capped": it["capped"], "max_lever": it["max_lever"],
                    "error": err}

        remaining: List[dict] = []
        for i in range(0, len(items), MAX_LEVERAGE_BATCH):
            chunk = items[i:i + MAX_LEVERAGE_BATCH]
            rows = None
            try:
                rows = cli.set_leverage_batch(
                    [{"instId": it["instId"], "lever": it["lever"]} for it in chunk],
                    self.td_mode)
            except Exception as e:                                # noqa: BLE001
                rows = None
                if self._net_notice:
                    self._net_notice(f"批量设置杠杆请求失败，逐个重试：{type(e).__name__}")
            if rows is None:
                remaining.extend(chunk)
                continue
            done = set()
            for r in rows:
                inst = r.get("instId")
                if inst in truth:
                    done.add(inst)
                    notes.append(one_note(truth[inst], bool(r.get("_ok")),
                                          r.get("_error")))
            for it in chunk:                        # rows the batch never reported
                if it["instId"] not in done:
                    remaining.append(it)

        for it in remaining:                        # fallback: one call per name
            try:
                cli.set_leverage(it["instId"], it["lever"], self.td_mode)
                notes.append(one_note(it, True, None))
            except OKXError as e:
                # already set, or an open position blocks the change.  Not fatal
                # to the order, but the *actual* leverage is then unknown -- so
                # it is reported, not passed over.
                notes.append(one_note(it, False, str(e)))
        pos = {it["instId"]: k for k, it in enumerate(items)}
        notes.sort(key=lambda n: pos.get(n["instId"], 1_000_000))
        return notes

    def _send_okx(self, plan: Plan, run_id: str, log: Any = None) -> List[dict]:
        """Submit every order in ⌈N/20⌉ requests, not N.

        `place_batch` always existed, but it was called with a one-element list
        per order -- so a 19-name rebalance paid 19 round trips (~15s of silence
        through this machine's proxy) for something the exchange accepts in one.
        The per-order outcome still arrives per order (`sCode` inside the batch),
        so nothing about the result reporting changes.

        The one thing that does change: if a whole batch times out, every order
        in it is ambiguous, not just one.  They are all marked unknown so
        reconciliation looks them all up.
        """
        cli = self._private()
        lev_notes = self._apply_leverage(plan)
        self._last_leverage_notes = lev_notes
        results: List[dict] = []
        orders = list(plan.orders)
        bodies: Dict[str, dict] = {}
        for i, o in enumerate(orders):
            cid = make_clordid(_clordid_prefix(run_id), o.inst_id, i)
            bodies[cid] = o.to_body(self.td_mode, cid)

        cids = list(bodies)
        total_batches = max(1, math.ceil(len(cids) / MAX_ORDER_BATCH))
        for b, start in enumerate(range(0, len(cids), MAX_ORDER_BATCH)):
            sub = cids[start:start + MAX_ORDER_BATCH]
            if log:
                log(f"⑥ 提交订单 {start + 1}-{start + len(sub)}/{len(cids)}"
                    f"（第 {b + 1}/{total_batches} 批，1 次请求）")
            chunk = [bodies[c] for c in sub]
            try:
                rows = cli.place_batch(chunk)
                by_cid = {r.get("_clOrdId"): r for r in rows}
                for cid in sub:
                    o = orders[cids.index(cid)]
                    r = by_cid.get(cid) or {}
                    results.append({
                        "instId": o.inst_id, "clOrdId": cid, "ordId": r.get("ordId"),
                        "ok": bool(r.get("_ok")), "sz": o.sz, "side": o.side,
                        "action": o.action, "px": o.px or o.price,
                        "notional": o.delta_notional,
                        "error": r.get("_error") or (None if r else "交易所未返回该笔结果"),
                        "sCode": r.get("sCode")})
            except AmbiguousError as e:
                # The whole batch is one transport event: we cannot know which of
                # these got through, so none of them may be called a failure.
                for cid in sub:
                    o = orders[cids.index(cid)]
                    results.append({
                        "instId": o.inst_id, "clOrdId": cid, "ordId": None, "ok": False,
                        "sz": o.sz, "side": o.side, "action": o.action,
                        "notional": o.delta_notional, "ambiguous": True,
                        "error": f"响应丢失，需对账：{e}"})
            except OKXError as e:
                for cid in sub:
                    o = orders[cids.index(cid)]
                    results.append({
                        "instId": o.inst_id, "clOrdId": cid, "ordId": None, "ok": False,
                        "sz": o.sz, "side": o.side, "action": o.action,
                        "notional": o.delta_notional, "code": e.code, "error": str(e)})
        self.store.put_orders([{
            "clOrdId": r["clOrdId"], "instId": r["instId"], "side": r["side"],
            "sz": r["sz"], "action": r["action"], "ordId": r.get("ordId"),
            "state": "pending" if r["ok"] else ("unknown" if r.get("ambiguous") else "rejected"),
            "error": r.get("error"), "notional": r.get("notional"),
            "px": r.get("px"), "mode": self.mode, "ts": time.time(),
            "run_id": run_id,
        } for r in results])
        return results

    # ================= post-trade =================
    @staticmethod
    def _realised_book(plan: Plan, results: List[dict],
                       acct: dict) -> Tuple[float, float]:
        """`(gross, net)` exposure the account will actually hold after this send.

        `plan.realised_gross` / `plan.realised_net` describe the book the plan
        *intends*: every leg at its target.  They are the right thing for the
        pre-trade risk gates (which ask "is this plan acceptable?") and the wrong
        thing for the record (which asks "what happened?").

        A rejected leg does not move, so its **current** position is what
        remains.  Orders that were never sent at all are treated the same way.
        Per-contract notional comes from the order's own delta where there is an
        order, and from the account's reported notional otherwise; anything
        still unpriceable is skipped rather than silently counted as zero.
        """
        tgt = plan_target_positions(plan)
        accepted = {r.get("instId") for r in (results or []) if r.get("ok")}
        cur = acct.get("cur_sz") or {}

        per_ct: Dict[str, float] = {}
        for o in plan.orders:
            if o.sz and o.delta_notional:
                per_ct[o.inst_id] = abs(float(o.delta_notional)) / abs(float(o.sz))
            elif o.tgt_sz and o.target_notional:
                per_ct[o.inst_id] = abs(float(o.target_notional)) / abs(float(o.tgt_sz))
        for p in (acct.get("positions") or []):
            n, sz = p.get("notional"), p.get("pos")
            if n and sz:
                per_ct.setdefault(str(p.get("instId")),
                                  abs(float(n)) / abs(float(sz)))

        gross = net = 0.0
        for inst in set(tgt) | set(cur):
            if inst in accepted:
                sz = float((tgt.get(inst) or {}).get("sz") or 0.0)
            else:
                sz = float(cur.get(inst) or 0.0)
            pc = per_ct.get(inst)
            if not sz or not pc:
                continue
            gross += abs(sz) * pc
            net += sz * pc
        return gross, net

    @staticmethod
    def _charged_turnover(plan: Plan, results: List[dict], nav: float) -> float:
        """Turnover actually consumed by a send, as a fraction of NAV.

        Only orders the exchange accepted count.  This used to be
        `plan.turnover_frac`, bumped unconditionally -- so a rebalance whose every
        order was *rejected* still burned the whole day's budget.  One failed demo
        run left `turnover_used_today = 0.19994` of 0.20, and the next plan came
        out scaled by ~5e-5: orders of a few cents, with nothing in the UI to
        explain why.  A rejected order moved nothing and must not be charged.

        An *ambiguous* batch is the opposite case: the response was lost, so the
        orders may well have gone through.  Charging nothing there would let the
        same budget be spent twice before reconciliation, so it is charged in
        full -- for a risk limit, "assume it happened" is the safe side.

        `paper` always fills, so its charge equals the planned fraction; that
        keeps live and paper comparable.
        """
        if plan.turnover_budget is None:
            return 0.0
        done = sum(float(r.get("notional") or 0.0)
                   for r in (results or []) if r.get("ok"))
        used = (done / nav) if nav and nav > 0 else 0.0
        if any(r.get("ambiguous") for r in (results or [])):
            used = max(used, float(plan.turnover_frac or 0.0))
        return used

    def _after_execute(self, target: LiveTarget, acct: dict, plan: Plan,
                       record: dict) -> None:
        """Record what happened.  Note what is *not* written here: `positions`.

        The book has exactly one writer per mode -- `_send_paper` for paper
        (actual fills, carrying the fill price) and the exchange via
        `reconcile()` for demo/live.  Writing the *intended* book here used to
        replace paper's `{"sz", "avg_px"}` with `{"sz", "px"}` from the plan, so
        the next `_paper_account` computed `upl = (mark - 0) * sz * ct`: the
        unrealised P&L silently became the book's **net notional**.  On a
        dollar-neutral book that lands near zero by luck, which is why it went
        unnoticed -- and it would be badly wrong on any book with net exposure.

        The intention is still worth keeping, so it goes in its own field.
        """
        state = {
            "mode": self.mode, "nav": acct["nav"],
            "last_rebalance": target.decision_ts,
            "last_run": record["run_id"], "last_run_ts": record["ts"],
            "target_positions": plan_target_positions(plan),
        }
        self.store.patch_state(**state)
        # Consume the day's turnover budget, exactly like the backtest does --
        # but only for what actually went out (see `_charged_turnover`).
        if plan.turnover_budget is not None:
            charged = record.get("turnover_charged")
            self.store.bump_turnover(
                float(plan.turnover_frac) if charged is None else float(charged))
        self.store.add_equity(acct["nav"],
                              plan.realised_gross if record.get("gross_realised") is None
                              else float(record["gross_realised"]),
                              plan.realised_net if record.get("realised_net") is None
                              else float(record["realised_net"]))
        if self.mode != "paper":
            try:
                self.reconcile(quiet=True)
            except Exception as e:                                # noqa: BLE001
                record.setdefault("errors", []).append(f"对账失败：{e}")

    def reconcile(self, quiet: bool = False) -> dict:
        """Pull order states + fills from OKX and refresh the local book."""
        if self.mode == "paper":
            acct = self._paper_account()
            self.store.add_equity(acct["nav"])
            return {"mode": "paper", "orders": 0, "fills": 0, "nav": acct["nav"]}
        cli = self._private()
        ords = self.store.orders()
        pending = [c for c, r in ords.items()
                   if r.get("state") in ("pending", "unknown")]
        updated = 0
        for cid in pending[:200]:
            inst = ords[cid].get("instId")
            if not inst:
                continue
            try:
                row = cli.order_by_clordid(inst, cid)
            except Exception as e:                                # noqa: BLE001
                if not quiet:
                    raise
                continue
            if row is None:
                continue
            st = row.get("state")
            self.store.set_order(cid, state=st, ordId=row.get("ordId"),
                                 avgPx=_f(row.get("avgPx")),
                                 accFillSz=_f(row.get("accFillSz")),
                                 fee=_f(row.get("fee")), updated=time.time())
            updated += 1
        known = {f.get("tradeId") for f in self.store.fills()}
        new_fills: List[dict] = []
        try:
            recent = cli.fills("SWAP", begin_ms=int((time.time() - 3600 * 24) * 1000))
        except Exception:                                         # noqa: BLE001
            recent = []
        for f in recent:
            tid = f.get("tradeId")
            if not tid or tid in known:
                continue
            spec = self.specs().get(f.get("instId"))
            ct = spec.ct_val if spec else None
            px, sz = _f(f.get("fillPx")), _f(f.get("fillSz"))
            new_fills.append({
                "tradeId": tid, "clOrdId": f.get("clOrdId"), "instId": f.get("instId"),
                "side": f.get("side"), "posSide": f.get("posSide"),
                "px": px, "sz": sz,
                # Carry the notional so nothing downstream has to remember
                # `|sz| * ctVal * px` -- forgetting `ctVal` inflates BTC 100x.
                "notional": (abs(sz) * ct * px) if (ct and px and sz) else None,
                "ct_val": ct,
                "fee": _f(f.get("fee")), "ts": _f(f.get("ts")),
                "mode": self.mode, "execType": f.get("execType"),
                "simulated": False,
            })
        if new_fills:
            self.store.add_fills(new_fills)
        acct = self.account()
        self.store.add_equity(acct["nav"])
        # Store the book in the *local* schema.  `account()` returns a display
        # list; assigning it here would replace `{instId: {sz, avg_px}}` with a
        # list of differently-named fields, and anything reading the book later
        # (paper accounting, `Store.summary`) would be reading the wrong shape.
        self.store.patch_state(nav=acct["nav"], positions=_book_from_account(acct))
        return {"mode": self.mode, "orders": updated, "fills": len(new_fills),
                "nav": acct["nav"]}

    def flatten(self, *, confirm: Optional[str] = None, dry_run: bool = True,
                progress=None) -> dict:
        """Close everything.  Allowed even off-schedule; still fully guarded.

        Takes the same cross-process lock as `execute`: "flatten" and "rebalance"
        racing each other is the worst version of the double-order problem, since
        the flatten closes a book the rebalance is simultaneously rebuilding.
        """
        with rebalance_lock(self.mode):
            return self._flatten_inner(confirm=confirm, dry_run=dry_run,
                                       progress=progress)

    def _flatten_inner(self, *, confirm: Optional[str] = None,
                       dry_run: bool = True, progress=None) -> dict:
        p = progress or (lambda m: None)
        self._net_notice = p
        run_id = new_run_id(self.mode)
        p("① 读取账户与持仓")
        acct = self.account()
        nav = acct["nav"]
        p(f"② 净值 {nav:,.2f} USDT · 待平持仓 {len(acct['cur_sz'])} 个")
        px = self.prices(list(acct["cur_sz"]))
        plan = build_plan({}, nav, current_sz=acct["cur_sz"], prices=px,
                          specs=self.specs(), pos_mode=acct.get("pos_mode", "net_mode"),
                          td_mode=self.td_mode, ord_type=self.ord_type,
                          limit_buffer_bps=self.limit_buffer_bps)
        violations = check_plan(plan, self.limits, mode=self.mode, nav=nav,
                                rebalance_due=True, actions=("flatten",),
                                leverage=self.set_leverage)
        blocked = blocking(violations)
        record = {"run_id": run_id, "mode": self.mode, "ts": time.time(),
                  "action": "flatten", "dry_run": bool(dry_run) or bool(blocked),
                  "nav": nav, "n_orders": plan.n_orders, "n_ok": 0, "n_fail": 0,
                  "coverage": 0.0, "weight_err": 0.0,
                  "order_notional": plan.order_notional,
                  "turnover_frac": plan.turnover_frac,
                  "violations": [v.to_dict() for v in violations],
                  "results": [], "errors": [v.title for v in blocked]}
        if blocked or dry_run:
            self.store.add_run(record)
            return {"record": record, "plan": plan.to_dict(),
                    "violations": record["violations"], "stage":
                        "blocked" if blocked else "dry_run"}
        if self.mode == "live" and (confirm or "").strip() != live_confirm_phrase():
            record["errors"] = ["实盘确认短语不正确"]
            self.store.add_run(record)
            return {"record": record, "plan": plan.to_dict(),
                    "violations": record["violations"], "stage": "confirm_failed"}
        results = self._send(plan, run_id, LiveTarget(
            weights={}, book=[], pool=[], decision_ts="", next_decision_ts="",
            panel_last_ts="", bars_since_decision=0, rebalance_bars=0,
            rebalance_due=True, gross=0.0), log=p)
        p(f"③ 已提交 {plan.n_orders} 笔平仓单"
          + ("（本地模拟成交）" if self.mode == "paper" else "，等待交易所回报"))
        record["results"] = results
        record["n_ok"] = sum(1 for r in results if r.get("ok"))
        record["n_fail"] = len(results) - record["n_ok"]
        record["errors"] = [f"{r.get('instId')}: {r.get('error')}"
                            for r in results if not r.get("ok")]
        self.store.add_run(record)
        if self.mode != "paper":
            try:
                self.reconcile(quiet=True)
            except Exception:                                     # noqa: BLE001
                pass
        return {"record": record, "plan": plan.to_dict(),
                "violations": record["violations"], "stage": "sent"}

    # ================= status =================
    def status(self) -> dict:
        # `summary()` already carries the day-checked turnover figure.  Deriving
        # the top-level `turnover_used_today` from that same dict (rather than
        # re-reading the state file) is not just one file read saved: the two
        # fields are now the same value by construction, and they *were* able to
        # disagree -- the account panel read `store.turnover_used_today` (raw,
        # never reset) while the planner sized against the reset one.
        store_sum = self.store.summary()
        out: dict = {"mode": self.mode, "limits": self.limits.to_dict(),
                     "limits_saved": self.store.load_limits(),
                     "limits_path": self.store.limits_path(),
                     "ord_type": self.ord_type, "td_mode": self.td_mode,
                     "signal": dict(self._signals),
                     "store": store_sum,
                     "prices_source": self.prices_source,
                     "turnover_budget": self.turnover_budget,
                     "turnover_used_today": store_sum["turnover_used_today"],
                     "kill_switch": kill_switch_on(),
                     "confirm_phrase": live_confirm_phrase() if self.mode == "live" else None}
        st = self.store.read_state()
        out["last_rebalance"] = st.get("last_rebalance")
        out["state"] = {k: v for k, v in st.items() if k != "positions"}
        out["data_staleness"] = self.data_staleness()
        if self.mode == "paper":
            acct = self._paper_account()
            out["connected"] = True
            out["account"] = acct
            return out
        c = load_creds(self.mode)
        out["connected"] = False
        if c is None:
            out["account_error"] = ("未配置 API Key。OKX 模拟盘需要在「模拟盘 → 个人中心 → "
                                    "模拟盘 API」单独创建 Key。")
            return out
        try:
            # `probe()` used to run here *and* `account()` below, paying for the
            # same two endpoints twice on every status load.  `account()` already
            # carries everything status shows, so one round of it is enough --
            # and it fails loudly if the key is wrong.
            acct = self.account()
            out["connected"] = True
            out["ok"] = True
            out["uid"] = acct.get("uid")
            out["acctLv"] = acct.get("acctLv")
            out["equity_usdt"] = acct.get("nav")
            out["account"] = acct
            out["pos_mode"] = acct.get("pos_mode")
        except Exception as e:                                    # noqa: BLE001
            out["ok"] = False
            out["account_error"] = f"{type(e).__name__}: {e}"
            # Translate the failure while we still have the failing connection in
            # hand: "50111 Invalid OK-ACCESS-KEY" in demo mode almost always
            # means a *live* key was pasted in, and saying so beats re-checking
            # the key string twenty times.
            try:
                out["diagnosis"] = self._private().probe()
            except Exception:                                     # noqa: BLE001
                pass
        out["net"] = self.net_latency()
        return out

    def data_staleness(self) -> dict:
        """See `staleness()` for what the two ages mean and why both are reported."""
        return staleness(self.signal_kwargs.get("bar", "1h"))

    @staticmethod
    def refresh_market_data(bar: str = "1h", end: Optional[str] = None,
                            progress=None) -> dict:
        """Refresh the candle cache so the next signal sees current bars.

        Deliberately **incremental**: the naive `--refresh` path re-downloads the
        whole history for every symbol, because `OKXClient.candles_range` always
        paginates back to `start`.  For the 1h pool that is ~27k requests
        (~40 min) per click -- unusable as a button and it rewrites every
        parquet file, invalidating cached signals.  The tail-append path is
        ~1 request per symbol (~15 s) and touches only the files that moved.

        The candidate pool is reused rather than rebuilt (`--reuse-pool`) so a
        quote refresh cannot silently change the tradable universe behind the
        signal.

        `end` defaults to **tomorrow's** UTC date, not today's.  `--end` is an
        exclusive midnight bound (`ts <= ms(end)`), so `--end <today>` stops at
        today's 00:00 bar -- which is exactly the bar the cache already holds --
        and the refresh then reports `uptodate: 161` while still missing every
        bar since midnight.  (Observed for real: 32 h stale, `req=0`.)  Passing
        tomorrow's midnight takes every *confirmed* bar up to now; OKX's
        in-progress bar is dropped by the `confirm == 1` filter, so this cannot
        pull a half-formed candle in.
        """
        import subprocess
        import sys
        p = progress or (lambda m: None)
        end = end or time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
        before = staleness(bar)
        p(f"增量刷新 {bar} K 线缓存（截止 {end}，刷新前最新 bar 落后 "
          f"{_age_str(before.get('bar_age_hours'))}）…")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8:replace"}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-m", "crypto_ls_research.data.download",
                            "--bars", bar, "--incremental", "--only-candles",
                            "--reuse-pool", "--end", end],
                           cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                               os.path.abspath(__file__)))),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=3600)
        tail = "\n".join((r.stdout or "").strip().splitlines()[-6:])
        _NEWEST_CACHE.clear()          # the probe must not report the pre-refresh bar
        after = staleness(bar)
        p(f"数据刷新完成（exit={r.returncode}）：最新 bar "
          f"{after.get('newest_bar') or '未知'}，落后 {_age_str(after.get('bar_age_hours'))}"
          f"（刷新前 {_age_str(before.get('bar_age_hours'))}）")
        for line in tail.splitlines():
            p(line)
        if r.returncode != 0:
            p((r.stderr or "")[-400:])
        if after["stale"]:
            p("注意：刷新后数据仍然是陈旧的——检查网络/代理，或本地缓存可能无法访问 OKX。")
        return {"exit_code": r.returncode, "tail": tail,
                "staleness_before": before, "staleness_after": after}

    # ================= rendering helpers =================
    def _result(self, record: dict, plan: Plan, target: LiveTarget, acct: dict,
                violations, px: Dict[str, float], stage: str) -> dict:
        return {
            "stage": stage, "mode": self.mode, "record": record,
            "plan": plan.to_dict(),
            "violations": [v.to_dict() if hasattr(v, "to_dict") else v
                           for v in violations],
            "summary": {
                "nav": acct["nav"], "n_orders": plan.n_orders,
                "order_notional": plan.order_notional,
                "coverage": plan.coverage, "weight_err": plan.weight_err,
                "turnover_frac": plan.turnover_frac,
                "min_viable_capital": plan.min_viable_capital,
            },
            "target": {"decision_ts": target.decision_ts,
                       "next_decision_ts": target.next_decision_ts,
                       "rebalance_due": target.rebalance_due,
                       "bars_since_decision": target.bars_since_decision,
                       "gross": target.gross},
            "prices": px,
        }


# ---------------------------------------------------------------------------
def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _trade_cost_scalar(delta_w: np.ndarray, adv, equity: float,
                       daily_vol, ccfg: CostConfig) -> float:
    """Same cost function as the backtest, for one instrument at a time."""
    from ..backtest.costs import trade_cost
    adv_arr = np.array([adv if adv else np.nan], dtype="float64")
    dv = np.array([daily_vol if daily_vol else 0.0], dtype="float64")
    out = trade_cost(np.asarray(delta_w, dtype="float64"), adv_arr, equity, dv, ccfg)
    return float(out["total"].sum() * equity)


def _apply_fill(pos_sz: float, avg_px: float, dq: float, price: float,
                ct_val: float) -> Tuple[float, float, float]:
    """Signed-position update + realised P&L.  See tests for the sign table."""
    new_sz = pos_sz + dq
    if abs(new_sz) < 1e-12:
        new_sz = 0.0
    if abs(pos_sz) < 1e-12 or pos_sz * dq > 0:
        denom = abs(pos_sz) + abs(dq)
        new_avg = ((avg_px * abs(pos_sz) + price * abs(dq)) / denom) if denom else price
        return new_sz, new_avg, 0.0
    closed = min(abs(dq), abs(pos_sz))
    direction = 1.0 if pos_sz > 0 else -1.0
    pnl = direction * (price - avg_px) * closed * ct_val
    if abs(dq) > abs(pos_sz):
        return new_sz, price, pnl            # flipped: remainder opens at `price`
    return new_sz, (avg_px if new_sz != 0 else 0.0), pnl


def plan_target_positions(plan: Plan) -> Dict[str, dict]:
    """The book the plan *intends* to hold.  Display only -- never the book."""
    out: Dict[str, dict] = {}
    for o in plan.orders:
        out[o.inst_id] = {"sz": o.tgt_sz, "px": o.price}
    return out


def _book_from_account(acct: dict) -> Dict[str, dict]:
    """Normalise an `account()` payload into the local book schema.

    `account()` returns a *display* list (`instId`/`pos`/`avgPx`/`markPx`/`upl`/
    `notional`) while the local book is `{instId: {sz, avg_px}}`.  Those two are
    easy to mix up -- doing exactly that is how `avg_px` got destroyed and the
    unrealised P&L silently turned into net notional -- so the conversion is
    explicit and lives in one place.
    """
    out: Dict[str, dict] = {}
    for p in (acct.get("positions") or []):
        i = p.get("instId")
        if not i:
            continue
        out[i] = {"sz": float(p.get("pos") or 0.0),
                  "avg_px": float(p.get("avgPx") or 0.0)}
    return out
