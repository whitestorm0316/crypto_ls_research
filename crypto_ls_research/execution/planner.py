"""Turn a target book into an explicit, auditable order list.

The planner is deliberately *dumb and explicit*: it does no risk judgement, it
just answers "given this target, this account and these contract specs, what
orders would have to be sent, and what would the resulting book actually look
like".  Every rejection carries a machine-readable reason, because the
interesting question for a small account is never "did it place orders" but
"which names did it silently fail to place, and how far off target did that
leave the book".

Three things this module refuses to hide
----------------------------------------
1. **Dust.**  A delta below half the minimum order size is dropped, not rounded
   up.  Rounding it up invents a position out of rounding noise.
2. **Unreachable targets.**  When a target leg is smaller than the minimum
   order, the name is skipped and counted.  The plan then reports
   `coverage`, `weight_err` and the *minimum viable capital* implied by the
   book, instead of pretending the target was reached.
3. **Turnover.**  Orders are reported with the notional they move, so the
   caller can compare against the strategy's 20%-of-gross daily budget before
   anything is sent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..config.settings import MARGIN_MODE
from .specs import InstSpec, load_specs

EPS = 1e-12


@dataclass
class Order:
    inst_id: str
    side: str                     # "buy" | "sell"
    pos_side: str                 # "net" | "long" | "short"
    sz: float                     # contracts, always > 0
    ord_type: str                 # "market" | "ioc" | "limit"
    px: Optional[float]
    price: float                  # reference mark price
    cur_sz: float                 # signed contracts currently held
    tgt_sz: float                 # signed contracts wanted
    action: str                   # open | close | increase | reduce | flip_close | flip_open
    reduce_only: bool = False
    target_notional: float = 0.0
    delta_notional: float = 0.0
    min_notional: float = 0.0
    adv: Optional[float] = None

    @property
    def instId(self) -> str:
        return self.inst_id

    def to_body(self, td_mode: str, cl_ord_id: str) -> dict:
        body = {"instId": self.inst_id, "tdMode": td_mode, "side": self.side,
                "posSide": self.pos_side, "ordType": self.ord_type,
                "sz": _fmt_sz(self.sz), "clOrdId": cl_ord_id}
        if self.px is not None:
            body["px"] = _fmt_num(self.px)
        if self.reduce_only:
            body["reduceOnly"] = True
        return body

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["instId"] = d.pop("inst_id")
        return d


@dataclass
class Skip:
    inst_id: str
    reason: str
    weight: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {"instId": self.inst_id, "reason": self.reason,
                "weight": self.weight, "detail": self.detail}


@dataclass
class Plan:
    orders: List[Order] = field(default_factory=list)
    skips: List[Skip] = field(default_factory=list)
    nav: float = 0.0
    target_gross: float = 0.0
    target_net: float = 0.0
    realised_gross: float = 0.0
    realised_net: float = 0.0
    coverage: float = 1.0            # achievable share of target gross notional
    weight_err: float = 0.0          # 0.5 * L1 distance between realised and target
    order_notional: float = 0.0      # total notional moved by the orders
    turnover_frac: float = 0.0       # order_notional / nav
    min_viable_capital: float = 0.0
    pos_mode: str = "net_mode"
    td_mode: str = MARGIN_MODE
    # --- turnover budget bookkeeping -------------------------------------
    # The strategy's realised book is NOT its target book: with the 20%-per-day
    # turnover budget the backtested position averages 0.47 gross while the raw
    # target averages 1.03.  Trading the raw target live would run 2.2x the
    # exposure that produced the reported Sharpe, so the budget is applied here
    # exactly as `backtest/engine.py` applies it.
    turnover_budget: Optional[float] = None
    turnover_used: float = 0.0
    turnover_scale: float = 1.0
    turnover_wanted: float = 0.0
    raw_target_gross: float = 0.0    # before the budget
    adv_binding: float = 0.0         # share of deltas cut by the ADV cap
    warnings: List[str] = field(default_factory=list)

    @property
    def n_orders(self) -> int:
        return len(self.orders)

    def to_dict(self) -> dict:
        return {
            "nav": self.nav, "target_gross": self.target_gross,
            "target_net": self.target_net, "realised_gross": self.realised_gross,
            "realised_net": self.realised_net, "coverage": self.coverage,
            "weight_err": self.weight_err, "order_notional": self.order_notional,
            "turnover_frac": self.turnover_frac,
            "min_viable_capital": self.min_viable_capital,
            "pos_mode": self.pos_mode, "td_mode": self.td_mode,
            "turnover_budget": self.turnover_budget,
            "turnover_used": self.turnover_used,
            "turnover_scale": self.turnover_scale,
            "turnover_wanted": self.turnover_wanted,
            "raw_target_gross": self.raw_target_gross,
            "adv_binding": self.adv_binding,
            "n_orders": self.n_orders, "warnings": list(self.warnings),
            "orders": [o.to_dict() for o in self.orders],
            "skips": [s.to_dict() for s in self.skips],
        }


def _fmt_sz(v: float) -> str:
    """Contract counts must not arrive as '3.0000000000000004'."""
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    return s or "0"


def _fmt_num(v: float) -> str:
    s = f"{v:.12f}".rstrip("0").rstrip(".")
    return s or "0"


# ---------------------------------------------------------------------------
def _orders_for_instrument(inst: str, cur_sz: float, tgt_sz: float, pos_mode: str,
                           price: Optional[float], spec: Optional[InstSpec],
                           ord_type: str,
                           limit_buffer_bps: float) -> List[dict]:
    """The 1-or-2 leg(s) that move `cur_sz` -> `tgt_sz` in this account's mode.

    In `net_mode` OKX has a single signed position per instrument, so a flip is
    one order.  In `long_short_mode` a flip is two: the old side must be closed
    before the new one can be opened, and sending it as one order would be
    rejected (or worse, misinterpreted).
    """
    legs: List[dict] = []

    def px_for(side: str) -> Optional[float]:
        # A market order carries no price at all, which is exactly why a close
        # must stay expressible when we have no mark for the instrument.
        if ord_type == "market" or price is None or not (price > 0):
            return None
        buf = limit_buffer_bps / 1e4
        return price * (1.0 + buf) if side == "buy" else price * (1.0 - buf)

    if pos_mode == "net_mode":
        d = tgt_sz - cur_sz
        if abs(d) <= EPS:
            return []
        side = "buy" if d > 0 else "sell"
        legs.append({"side": side, "pos_side": "net", "sz": abs(d),
                     "px": px_for(side),
                     "action": ("close" if abs(tgt_sz) <= EPS
                                else ("flip" if cur_sz * tgt_sz < 0 else
                                      ("increase" if abs(tgt_sz) > abs(cur_sz) else "reduce"))),
                     # Only a full close is reduce-only: a flip must be allowed
                     # to cross through zero.
                     "reduce_only": abs(tgt_sz) <= EPS})
        return legs

    # --- long/short mode ---------------------------------------------------
    def leg(side: str, ps: str, sz: float, action: str) -> dict:
        return {"side": side, "pos_side": ps, "sz": sz, "px": px_for(side),
                "action": action, "reduce_only": False}

    if abs(cur_sz) > EPS and abs(tgt_sz) > EPS and cur_sz * tgt_sz < 0:
        legs.append(leg("sell" if cur_sz > 0 else "buy",
                        "long" if cur_sz > 0 else "short", abs(cur_sz), "flip_close"))
        legs.append(leg("buy" if tgt_sz > 0 else "sell",
                        "long" if tgt_sz > 0 else "short", abs(tgt_sz), "flip_open"))
        return legs
    if abs(tgt_sz) <= EPS:
        if abs(cur_sz) > EPS:
            legs.append(leg("sell" if cur_sz > 0 else "buy",
                            "long" if cur_sz > 0 else "short", abs(cur_sz), "close"))
        return legs
    ps = "long" if tgt_sz > 0 else "short"
    if abs(cur_sz) <= EPS:
        legs.append(leg("buy" if tgt_sz > 0 else "sell", ps, abs(tgt_sz), "open"))
        return legs
    # The side must come from the *signed* delta.  In `long_short_mode` `posSide`
    # names which book the order acts on and `side` is the direction, so
    # **increasing a short is a SELL** (and reducing it is a BUY).  Deriving the
    # side from `abs(tgt) - abs(cur)` throws that sign away and sends every
    # short leg backwards -- which is silent: the order is accepted, the position
    # just moves the wrong way.  Measured consequence on the demo account before
    # this fix: 7 longs at $9,431 against 5 shorts at $817, i.e. a book that is
    # **92% long** while the strategy targets 47.6% long, so the market-neutral
    # claim was not being executed at all.
    d = tgt_sz - cur_sz
    if abs(d) <= EPS:
        return []
    legs.append(leg("buy" if d > 0 else "sell", ps, abs(d),
                    "increase" if abs(tgt_sz) > abs(cur_sz) else "reduce"))
    return legs


def build_plan(target_weights: Dict[str, float], nav: float,
               current_sz: Optional[Dict[str, float]] = None,
               prices: Optional[Dict[str, float]] = None,
               adv: Optional[Dict[str, float]] = None,
               specs: Optional[Dict[str, InstSpec]] = None,
               pos_mode: str = "net_mode", td_mode: str = MARGIN_MODE,
               ord_type: str = "market", limit_buffer_bps: float = 15.0,
               dust_ratio: float = 0.5,
               min_order_notional: float = 0.0,
               turnover_budget: Optional[float] = None,
               turnover_used: float = 0.0,
               max_adv_participation: Optional[float] = None) -> Plan:
    """Compute the orders that move the account from `current_sz` to the target.

    All notionals are USDT.  `current_sz` values are *signed contracts*
    (positive = long), which is what OKX returns in net mode.

    `turnover_budget` reproduces `execution.max_daily_turnover`.  This is not an
    optional extra: with the v3 configuration the *backtested* position averages
    0.472 gross while the raw target averages 1.032, i.e. the reported Sharpe was
    earned by a book throttled to ~46% of the target's exposure.  Sending the
    raw target live would run 2.2x the validated gross.  Set
    `turnover_budget=None` only for a deliberate full exit.
    """
    specs = specs if specs is not None else load_specs()
    current_sz = dict(current_sz or {})
    prices = dict(prices or {})
    adv = dict(adv or {})
    plan = Plan(nav=float(nav), pos_mode=pos_mode, td_mode=td_mode,
                turnover_budget=turnover_budget, turnover_used=float(turnover_used))
    if nav <= 0:
        plan.warnings.append("账户净值未知或为 0，无法换算下单量")
        plan.coverage = 0.0
        return plan

    raw_weights = {k: float(v) for k, v in target_weights.items() if abs(float(v)) > EPS}
    plan.raw_target_gross = sum(abs(w) for w in raw_weights.values()) * nav

    # --- current weights ---------------------------------------------------
    cur_w: Dict[str, float] = {}
    for inst, sz in current_sz.items():
        spec = specs.get(inst)
        px = prices.get(inst)
        if spec is None or px is None or not (px > 0) or not math.isfinite(px):
            continue
        cur_w[inst] = float(sz) * spec.notional_per_contract(px) / nav

    # --- turnover budget: the strategy's own throttle ----------------------
    universe = set(raw_weights) | set(cur_w)
    raw_delta = {i: raw_weights.get(i, 0.0) - cur_w.get(i, 0.0) for i in universe}
    plan.turnover_wanted = sum(abs(v) for v in raw_delta.values())
    if turnover_budget is not None and turnover_budget > 0:
        remaining = max(0.0, float(turnover_budget) - float(turnover_used))
        if plan.turnover_wanted > 0:
            plan.turnover_scale = float(min(1.0, remaining / plan.turnover_wanted))
        else:
            plan.turnover_scale = 1.0
    else:
        plan.turnover_scale = 1.0

    # --- per-name ADV cap (same rule as `risk.adv_cap_delta`) --------------
    capped = 0
    for inst in universe:
        d = raw_delta[inst] * plan.turnover_scale
        if max_adv_participation is None or not d:
            continue
        a = adv.get(inst)
        if not a or not math.isfinite(a) or a <= 0:
            continue
        room = a * float(max_adv_participation) / nav     # in weight units
        if abs(d) > room:
            if room <= 0:
                raw_delta[inst] = -cur_w.get(inst, 0.0) / max(plan.turnover_scale, EPS)
            else:
                raw_delta[inst] = math.copysign(room, d) / max(plan.turnover_scale, EPS)
            capped += 1
    if universe:
        plan.adv_binding = capped / len(universe)

    # Effective target = where we can actually get to this cycle.
    target_weights = {i: cur_w.get(i, 0.0) + raw_delta[i] * plan.turnover_scale
                      for i in universe}
    target_weights = {k: v for k, v in target_weights.items() if abs(v) > EPS}

    # --- what the (throttled) target *asks* for ---------------------------
    plan.target_gross = sum(abs(w) for w in target_weights.values()) * nav
    plan.target_net = sum(target_weights.values()) * nav

    realised: Dict[str, float] = {}      # inst -> realised signed notional
    min_viable = 0.0
    unplaceable_gross = 0.0

    for inst in sorted(set(target_weights) | set(current_sz)):
        w = target_weights.get(inst, 0.0)
        tgt_notional = w * nav
        px = prices.get(inst)
        spec = specs.get(inst)
        cur_sz = float(current_sz.get(inst, 0.0) or 0.0)
        have_px = px is not None and math.isfinite(px) and px > 0
        spec_live = spec is not None and (not spec.state or spec.state == "live")

        if not (spec_live and have_px):
            reason = ("no_spec" if spec is None
                      else ("no_price" if not have_px else f"state_{spec.state}"))
            detail = {"no_spec": "没有该合约的规格（可能已下线）",
                      "no_price": "缺少可用价格"}.get(reason, "合约非 live 状态")
            if abs(w) > EPS:
                plan.skips.append(Skip(inst, reason, w, detail))
                unplaceable_gross += abs(tgt_notional)
            # **A missing spec or price must never trap us in a position.**
            # OKX reports the signed contract count directly, and a market order
            # carries no price, so a full exit is always expressible -- even for
            # a delisted contract whose spec we can no longer look up.
            if abs(cur_sz) > EPS:
                side = "sell" if cur_sz > 0 else "buy"
                plan.orders.append(Order(
                    inst_id=inst, side=side,
                    pos_side=("net" if pos_mode == "net_mode"
                              else ("long" if cur_sz > 0 else "short")),
                    sz=abs(cur_sz), ord_type="market", px=None,
                    price=float(px) if have_px else float("nan"),
                    cur_sz=cur_sz, tgt_sz=0.0, action="close", reduce_only=True,
                    delta_notional=(abs(cur_sz) * spec.notional_per_contract(px)
                                    if (spec_live and have_px) else 0.0),
                    adv=adv.get(inst)))
                final_sz = 0.0
            else:
                final_sz = 0.0
            if spec_live and have_px:
                realised[inst] = final_sz * spec.notional_per_contract(px)
            continue

        tgt_sz = spec.contracts_for(tgt_notional, px, dust_ratio=dust_ratio)
        no_per_ct = spec.notional_per_contract(px)
        min_not = spec.min_notional(px)
        final_sz = tgt_sz
        if abs(w) > EPS:
            # capital at which *this* leg alone becomes placeable
            min_viable = max(min_viable, min_not / abs(w))
            if abs(tgt_sz) <= EPS:
                plan.skips.append(Skip(
                    inst, "below_min_order", w,
                    f"目标 {abs(tgt_notional):.2f} USDT < 最小下单额 {min_not:.2f} USDT"))
                unplaceable_gross += abs(tgt_notional)
                final_sz = cur_sz      # we keep whatever we already hold
        delta_sz = tgt_sz - cur_sz

        # delta smaller than one legal order -> leave it alone rather than churn
        if abs(delta_sz) > EPS and abs(delta_sz) < spec.min_sz - 1e-12:
            if abs(w) <= EPS:
                plan.skips.append(Skip(inst, "residual_below_min", 0.0,
                                       f"残留 {abs(delta_sz):g} 张 < minSz {spec.min_sz:g}"))
            else:
                plan.skips.append(Skip(inst, "delta_below_min", w,
                                       f"差额 {abs(delta_sz):g} 张 < minSz {spec.min_sz:g}"))
            delta_sz = 0.0
            final_sz = cur_sz

        if abs(delta_sz) > EPS:
            legs = _orders_for_instrument(inst, cur_sz, tgt_sz, pos_mode, px, spec,
                                          ord_type, limit_buffer_bps)
            for lg in legs:
                o = Order(
                    inst_id=inst, side=lg["side"], pos_side=lg["pos_side"],
                    sz=float(lg["sz"]), ord_type=ord_type, px=lg["px"], price=px,
                    cur_sz=cur_sz, tgt_sz=tgt_sz, action=lg["action"],
                    reduce_only=bool(lg["reduce_only"]),
                    target_notional=tgt_notional,
                    delta_notional=float(lg["sz"]) * no_per_ct,
                    min_notional=min_not, adv=adv.get(inst))
                if min_order_notional > 0 and o.delta_notional < min_order_notional - 1e-9:
                    plan.skips.append(Skip(inst, "below_order_floor", w,
                                           f"单笔 {o.delta_notional:.2f} < 下限 "
                                           f"{min_order_notional:.2f} USDT"))
                    final_sz = cur_sz
                    continue
                plan.orders.append(o)

        realised[inst] = final_sz * no_per_ct

    # --- quality metrics ---------------------------------------------------
    plan.realised_gross = sum(abs(v) for v in realised.values())
    plan.realised_net = sum(realised.values())
    plan.order_notional = sum(o.delta_notional for o in plan.orders)
    plan.turnover_frac = plan.order_notional / nav if nav > 0 else 0.0
    plan.min_viable_capital = min_viable

    if plan.turnover_budget and plan.turnover_scale < 0.999:
        plan.warnings.append(
            f"换手预算 {plan.turnover_budget:.0%}/期：目标变动需要换手 "
            f"{plan.turnover_wanted:.2f}× 净值，本次只执行其中 "
            f"{plan.turnover_scale:.1%}。这是策略本身的设计——回测的实际持仓毛敞口"
            f"均值 0.472，而目标毛敞口均值 1.032，报告的 Sharpe 来自被预算拖住的账。")
    if plan.adv_binding > 0:
        plan.warnings.append(
            f"{plan.adv_binding:.0%} 的名字被单笔 ADV 参与率上限削过。")

    if plan.target_gross > 0:
        placed = sum(abs(realised.get(i, 0.0))
                     for i in target_weights if abs(target_weights[i]) > EPS)
        plan.coverage = max(0.0, min(1.0, placed / plan.target_gross))
    else:
        plan.coverage = 1.0

    # 0.5 * L1 distance between realised and target *weights* (a proper metric:
    # it equals the fraction of gross that would have to be traded to fix it).
    l1 = sum(abs(realised.get(i, 0.0) - w * nav)
             for i, w in target_weights.items())
    l1 += sum(abs(realised.get(i, 0.0)) for i in realised if i not in target_weights)
    plan.weight_err = 0.5 * l1 / nav if nav > 0 else 0.0

    if unplaceable_gross > 0 and plan.target_gross > 0:
        n_below = sum(1 for s in plan.skips if s.reason == "below_min_order")
        plan.warnings.append(
            f"{n_below} 个目标腿（占毛敞口 {unplaceable_gross / plan.target_gross:.1%}）"
            f"小于最小下单额，已被跳过")
    if plan.min_viable_capital > 0 and nav < plan.min_viable_capital:
        plan.warnings.append(
            f"要让**每一个**目标腿都能下单，本金需 ≥ {plan.min_viable_capital:,.0f} USDT"
            f"（当前 {nav:,.0f}）。最小下单额是固定美元门槛，与本金无关，"
            f"因此小资金必然砍掉最细的几条腿。")
    if plan.coverage < 0.95 and plan.target_gross > 0:
        plan.warnings.append(
            f"信号覆盖率仅 {plan.coverage:.1%}——实盘书比回测书更窄，不宜直接套用回测指标")
    return plan


def prices_from_cache(insts: Iterable[str], bar: str = "1h") -> Dict[str, float]:
    """Last cached close per instrument, as a fallback when no live ticker is used."""
    import os
    import pandas as pd
    from ..data.store import CACHE
    out: Dict[str, float] = {}
    cdir = os.path.join(CACHE, "candles", bar)
    for inst in insts:
        p = os.path.join(cdir, f"{inst}.parquet")
        if not os.path.exists(p):
            continue
        try:
            v = float(pd.read_parquet(p, columns=["close"])["close"].iloc[-1])
        except Exception:                                         # noqa: BLE001
            continue
        if math.isfinite(v) and v > 0:
            out[inst] = v
    return out
