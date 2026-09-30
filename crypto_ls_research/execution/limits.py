"""Pre-trade guardrails.  Everything here runs *before* a single order is sent.

The research code is allowed to be optimistic: it can assume every rebalance is
executed in full, forever, at a modelled price.  A live account cannot.  These
checks exist to convert "the model wanted this" into "and here is why we are
still allowed to do it", and to fail closed.

Design choices worth calling out
--------------------------------
* **Absolute dollar caps, not just fractions.**  A fraction-of-NAV cap silently
  scales with the account.  If someone points this at an account 100x larger
  than intended, `max_gross_frac` happily approves a 100x larger book.  The
  absolute caps (`max_gross_notional`, `max_nav_usd`) are the ones that stop
  that.
* **The kill switch is a file.**  It has to survive a crashed web app, a stuck
  browser tab and a detached child process.  A boolean in memory does not.
* **Live mode needs a typed phrase.**  `LIVE-<today's date>` is checked
  server-side.  It is not a security control (the API key is), it is an
  "are you awake" control against a mis-clicked button.
* Violations are returned, never swallowed: the caller decides, and the UI
  shows every one of them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .credentials import kill_switch_on, kill_switch_path
from .planner import Plan
from ..risk.engine import DEFAULT_MMR, implied_leverage, isolated_liq_distance


@dataclass
class LiveLimits:
    """Hard caps.  Defaults are deliberately tight for a first live run."""

    max_gross_notional: float = 5_000.0        # USD, sum |position notional|
    max_gross_frac: float = 1.0                # gross / NAV
    max_order_notional: float = 1_000.0        # USD per single order
    max_orders: int = 60                       # per rebalance run
    max_turnover_frac: float = 1.0             # order notional / NAV
    max_nav_usd: Optional[float] = None        # refuse if account equity exceeds
    min_nav_usd: float = 50.0
    max_adv_participation: float = 0.05        # per order
    allowed_insts: Optional[Set[str]] = None
    require_rebalance_due: bool = True
    # Ceiling on the *account leverage we are willing to set*.  It used to be a
    # dead field: declared here, shipped to the UI, and never read by
    # `check_plan` -- so the console showed a leverage guard that guarded
    # nothing.  Anything configurable on screen must either bite or not be there.
    max_leverage: float = 5.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["allowed_insts"] = sorted(self.allowed_insts) if self.allowed_insts else None
        return d

    @staticmethod
    def from_dict(d: Optional[dict]) -> "LiveLimits":
        d = dict(d or {})
        allowed = d.pop("allowed_insts", None)
        known = {k: v for k, v in d.items() if k in LiveLimits.__dataclass_fields__}
        obj = LiveLimits(**known)
        if allowed:
            obj.allowed_insts = set(allowed)
        return obj


@dataclass
class Violation:
    sev: str          # "block" | "warn"
    key: str
    title: str
    body: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def live_confirm_phrase(now: Optional[float] = None) -> str:
    return "LIVE-" + time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def check_plan(plan: Plan, limits: LiveLimits, *, mode: str, nav: float,
               rebalance_due: bool = True, force: bool = False,
               actions: Sequence[str] = ("rebalance",),
               leverage: Optional[float] = None,
               venue_insts: Optional[Iterable[str]] = None,
               rebalance_days: Optional[float] = None) -> List[Violation]:
    """Return every problem with this plan.  An empty list means "cleared".

    `force=True` downgrades `require_rebalance_due` only -- it never relaxes a
    risk cap, so "force" can never mean "send an oversized order anyway".

    `leverage` is the account leverage we are about to *set*, not the exposure.
    Those two are routinely confused, and the confusion is expensive: raising
    the account leverage while the book stays the same size does not raise the
    return, it only removes margin -- i.e. it buys extra liquidation risk at
    zero expected gain.  So it is checked against a ceiling and narrated.

    `venue_insts` is the set of instruments the venue we are about to trade will
    actually accept, or `None` when that is unknown (no credentials, endpoint
    down).  Demo and live do not share a pool -- live lists ~492 USDT swaps,
    demo ~185 -- while the contract-spec cache is built from live data, so the
    planner can size legs the demo exchange has never heard of.  They are
    rejected one by one with `51001`, which is worth saying *before* the send.
    A warning, not a block: on demo these names are unplaceable by definition,
    and refusing to rebalance at all would be worse than trading the rest.

    `rebalance_days` is the grid the plan was actually built on, used only to
    narrate the `not_due` warning.  It is a parameter rather than a literal
    because the grid is a configuration value that has already changed once
    (3 days -> 1 day, 2026-09-29): the message used to assert 「每 3 天调仓一次」
    no matter what the desk was running, so the warning explained the deviation
    using a schedule nobody was on.  `None` means "unknown" and is narrated as
    such rather than guessed.
    """
    v: List[Violation] = []
    closing_only = all(a in ("flatten", "close") for a in actions)

    if leverage is not None:
        try:
            lev = float(leverage)
        except (TypeError, ValueError):
            v.append(Violation("block", "leverage", f"杠杆 {leverage!r} 不是数字",
                               "填一个数字，或留空表示不改杠杆。"))
            lev = None
        if lev is not None:
            if lev < 1:
                v.append(Violation(
                    "block", "leverage", f"杠杆 {lev:g}× 小于 1",
                    "杠杆不能小于 1×。留空表示不改杠杆。"))
            elif lev > limits.max_leverage + 1e-9:
                v.append(Violation(
                    "block", "max_leverage",
                    f"请求杠杆 {lev:g}× 超过上限 {limits.max_leverage:g}×",
                    "这是账户杠杆上限，不是敞口倍数。超过就拒绝下单；"
                    "确需更高请显式调高上限，并先看清下一行的说明。"))
            elif lev > 1:
                v.append(Violation(
                    "warn", "leverage_set", f"将设置账户杠杆 {lev:g}×",
                    f"<b>这不放大收益。</b>敞口由目标权重与换手预算决定，与杠杆无关；"
                    f"提高杠杆只是把维持保证金降到 1/{lev:g}，"
                    f"也就是<b>只增加爆仓风险、不增加预期收益</b>。"
                    f"想放大收益要调高毛敞口上限（当前 {limits.max_gross_frac:g}× 净值），"
                    f"代价是回撤按同一倍数放大（报告 §16：4× 时 MDD −42.9%）。"))

    if kill_switch_on():
        v.append(Violation(
            "block", "kill_switch", "交易熔断已开启",
            f"存在熔断文件 <span class='mono'>{kill_switch_path()}</span>，"
            f"所有下单被拒绝。确认无误后再解除。"))

    if mode not in ("paper", "demo", "live"):
        v.append(Violation("block", "mode", f"未知模式 {mode}", "模式必须是 paper/demo/live。"))

    if nav <= 0:
        v.append(Violation("block", "nav", "账户净值不可用",
                           "取不到净值就无法换算下单量，拒绝下单。"))
    else:
        if nav < limits.min_nav_usd:
            v.append(Violation(
                "block", "min_nav", f"净值 {nav:.2f} USDT 低于下限 {limits.min_nav_usd:.2f}",
                "本金过小时下单粒度噪声会大于信号（报告 §16）。"))
        if limits.max_nav_usd is not None and nav > limits.max_nav_usd:
            v.append(Violation(
                "block", "max_nav",
                f"账户净值 {nav:,.0f} USDT 超过预设上限 {limits.max_nav_usd:,.0f}",
                "这通常意味着连到了不打算交易的账户。请先确认，再调高上限。"))

    if plan.n_orders == 0 and not closing_only:
        v.append(Violation("warn", "no_orders", "当前无需调仓",
                           "目标书与现有持仓已经一致（或在最小下单额以下）。"))

    if plan.n_orders > limits.max_orders:
        v.append(Violation(
            "block", "max_orders",
            f"本次 {plan.n_orders} 笔订单，超过上限 {limits.max_orders}",
            "一次调仓拆成过多订单通常意味着 target 或持仓读取有误。"))

    if plan.order_notional > limits.max_order_notional * max(1, plan.n_orders):
        pass                                       # per-order check is done below

    for o in plan.orders:
        if o.delta_notional > limits.max_order_notional + 1e-9:
            v.append(Violation(
                "block", f"order_notional:{o.inst_id}",
                f"{o.inst_id} 单笔名义额 {o.delta_notional:,.0f} USDT 超过上限 "
                f"{limits.max_order_notional:,.0f}",
                "调高「单笔上限」或降低本金，否则拒绝整批下单。"))
        if limits.allowed_insts is not None and o.inst_id not in limits.allowed_insts:
            v.append(Violation(
                "block", f"not_allowed:{o.inst_id}",
                f"{o.inst_id} 不在允许交易的标的清单内", "检查池子范围设置。"))
        # ADV participation is only meaningful once we know ADV; skip when unknown
        if o.adv and o.adv > 0 and nav > 0:
            part = o.delta_notional / o.adv
            if part > limits.max_adv_participation:
                v.append(Violation(
                    "block", f"adv:{o.inst_id}",
                    f"{o.inst_id} 单笔占 30 日 ADV 的 {part:.2%}，超过上限 "
                    f"{limits.max_adv_participation:.2%}",
                    "冲击成本会显著偏离模型；把本金调小或跳过该名字。"))

    if venue_insts is not None:
        known = set(venue_insts)
        missing = sorted({o.inst_id for o in plan.orders if o.inst_id not in known})
        if missing:
            v.append(Violation(
                "warn", "venue_missing",
                f"{len(missing)} 个目标腿在当前场所不存在，会被逐笔拒绝",
                "模拟盘与实盘的合约池不是同一个：实盘约 492 个 USDT 永续，"
                "模拟盘只有约 185 个。合约规格缓存来自实盘公开数据，"
                "所以计划里会出现模拟盘根本没有的腿——它们会以 "
                "<span class='mono'>51001 合约不存在或已下线</span> 被拒，"
                "资金留在原有仓位上，本次调仓的其余腿不受影响。"
                "实盘不受此限。涉及："
                + "、".join(f"<span class='mono'>{m}</span>" for m in missing[:6])
                + ("…" if len(missing) > 6 else "") + "。"))

    if nav > 0:
        gross = plan.realised_gross
        if limits.max_gross_notional and gross > limits.max_gross_notional + 1e-6:
            v.append(Violation(
                "block", "max_gross_notional",
                f"目标总名义额 {gross:,.0f} USDT 超过绝对上限 "
                f"{limits.max_gross_notional:,.0f}",
                "绝对上限不随本金缩放——这是唯一能挡住「连错账户」的闸门。"))
        if limits.max_gross_frac and gross > limits.max_gross_frac * nav + 1e-6:
            v.append(Violation(
                "block", "max_gross_frac",
                f"总敞口 {gross / nav:.2f}× 净值，超过上限 {limits.max_gross_frac:.2f}×",
                "策略正常毛敞口约 0.47×，超过 1× 说明杠杆或持仓读取异常。"))
        if plan.turnover_frac > limits.max_turnover_frac + 1e-9:
            v.append(Violation(
                "block", "max_turnover",
                f"本次换手 {plan.turnover_frac:.2f}× 净值，超过上限 "
                f"{limits.max_turnover_frac:.2f}×",
                "策略的每日换手预算是 20% 毛敞口；一次调仓换掉整个账户一定是出错了。"))

    if limits.require_rebalance_due and not rebalance_due and not force and not closing_only:
        grid = ("每 %.3g 天调仓一次" % float(rebalance_days)
                if rebalance_days else "按固定网格调仓")
        v.append(Violation(
            "warn", "not_due", "当前不是调仓日",
            f"策略{grid}；现在执行属于计划外操作，需要显式勾选「强制」。"))

    if mode == "live":
        v.append(Violation(
            "warn", "live_mode", "实盘模式：订单将以真实资金成交",
            "请再次确认账户净值、单笔上限与总名义额上限。"))

    return v


def blocking(vs: Sequence[Violation]) -> List[Violation]:
    return [x for x in vs if x.sev == "block"]


# ---------------------------------------------------------------------------
# margin headroom: reconcile the gate's *assumption* with the venue's reality
# ---------------------------------------------------------------------------
def margin_headroom_violations(positions: Sequence[dict],
                               assumed_leverage: float, *,
                               mmr: float = DEFAULT_MMR,
                               flag_ratio: float = 1.2) -> List[Violation]:
    """Flag held legs margined far above the leverage the backtest gate assumes.

    This is the live counterpart of `risk.engine.liquidation_ok`.  The gate is a
    *backtest* filter: it screens instruments against `RiskConfig.leverage_in_force`
    and has no access to a venue.  So on its own it can only ever say "under my
    assumption this is safe" -- and on 2026-09-30 that assumption was 5x (19.50%)
    while a real leg sat at 100x, 0.57% from liquidation.  Nothing compared the
    two, so the divergence was invisible.

    `positions` are the parsed rows from `LiveEngine.account()["positions"]`
    (`instId`, `pos`, `markPx`, `liqPx`, `mgnMode`).  The per-leg truth is read
    from `liqPx`, never from `lever` -- `lever` mirrors the account *configuration*
    and does not move when a position is re-margined (see `implied_leverage`).

    Severity is **warn, never block**, and that is deliberate: blocking the
    rebalance would freeze a book whose best remedy *is* the rebalance that
    shrinks the offending leg.  The caller decides what to do; the point here is
    that the divergence stops being silent.
    """
    v: List[Violation] = []
    isolated = [p for p in positions if str(p.get("mgnMode")) == "isolated"]
    priced = 0
    for p in positions:
        inst = p.get("instId")
        mark, liq = p.get("markPx"), p.get("liqPx")
        if not inst or not mark or not liq or float(mark) <= 0 or float(liq) <= 0:
            continue
        priced += 1
        impl = implied_leverage(float(mark), float(liq), mmr)
        if impl <= float(assumed_leverage) * flag_ratio:
            continue
        dist = abs(float(liq) - float(mark)) / float(mark)
        v.append(Violation(
            "warn", f"margin_headroom:{inst}",
            f"{inst} 实际按 {impl:.0f}× 计价，距爆仓仅 {dist:.2%}",
            f"风控假设的杠杆是 <b>{assumed_leverage:g}×</b>"
            f"（距离 {isolated_liq_distance(float(assumed_leverage), mmr):.2%}），"
            f"这条腿比它差 <b>{impl / max(float(assumed_leverage), 1e-9):.0f} 倍</b>。"
            f"读的是 <span class='mono'>liqPx</span> 反推的真实杠杆，不是 "
            f"<span class='mono'>lever</span>（后者只是配置的镜像，改配置不会重算在场持仓）。"
            f"要真正改小只能<b>平掉重开</b>或<b>给该仓加保证金</b>。"))

    # "could not evaluate" must not look like "passed": if the account holds
    # isolated legs and *none* of them published a `liqPx`, the check did not run.
    if isolated and priced == 0:
        v.append(Violation(
            "warn", "margin_headroom_unavailable",
            f"{len(isolated)} 条逐仓腿都没有爆仓价，无法核对保证金余量",
            "账户里存在逐仓持仓，但一条都没读到 <span class='mono'>liqPx</span>，"
            "所以这次<b>没有评估</b>（不是通过）。等持仓端点能读到爆仓价后再看。"))
    return v
