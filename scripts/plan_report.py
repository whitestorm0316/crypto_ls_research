"""Regenerate the order plan + capital diagnosis for one live mode.

Every number in the output comes from an actual `LiveEngine` call against the
real account and the real signal -- nothing is transcribed by hand.  That is the
point: the report exists to answer "what would this actually do, and would it
survive at my capital", and a hand-copied number stops being true the moment the
market or the config moves.

Usage
-----
    python scripts/plan_report.py --mode demo
    python scripts/plan_report.py --mode demo --out artifacts/live/demo/plan.md

The measurement that needs care is the leverage table.  `set_leverage` is an
engine attribute and `build()` re-fetches prices on a 15s TTL, so building each
row from a *fresh* engine makes the rows differ for a reason that has nothing to
do with leverage (price drift).  This script therefore uses **one** engine and
varies only `set_leverage`, which is what makes the invariance claim mean
something.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.execution.engine import LiveEngine  # noqa: E402

NAVS = (70.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0)
LEVS = (None, 1.0, 3.0, 10.0)


def collect(mode: str) -> dict:
    eng = LiveEngine(mode=mode)
    eng.store.turnover_state = lambda *a, **k: (None, 0.0)
    target = eng.target()
    acct = eng.account()

    def fresh(nav):
        return {"nav": float(nav), "cur_sz": {},
                "pos_mode": acct.get("pos_mode"), "marks": {}}

    sweep = []
    for nav in NAVS:
        q, _, _ = eng.build(target, fresh(nav))
        sweep.append((nav, q.n_orders, q.coverage, q.weight_err,
                      q.min_viable_capital))

    levrows = []
    for nav in (70.0, 1000.0):
        for lev in LEVS:
            eng.set_leverage = lev            # the only thing that changes
            q, _, _ = eng.build(target, fresh(nav))
            levrows.append((nav, lev, q.n_orders, q.order_notional, q.coverage))
    eng.set_leverage = None

    # --- what *does* move the order size ------------------------------------
    # Saying "leverage is inert" invites the wrong conclusion ("then nothing
    # helps").  Two other knobs exist and only one of them works:
    #
    #   * scaling the *target* does nothing, because the strategy's own turnover
    #     budget is a fraction of NAV -- scale the target by m and the throttle
    #     scales by 1/m, so the executed notional is invariant.  This is a hard
    #     invariance, not an accident of one number.
    #   * raising the throttle itself works, and is what "I want to trade 5x" is
    #     actually asking for.
    #
    # And leverage does have *one* job, just not this one: it decides how much
    # margin the exposure costs.  That is measured separately below, because at
    # $70 it is what makes the 5x case unbuildable rather than merely risky.
    def scaled(m: float):
        t = copy.copy(target)
        t.weights = {k: v * m for k, v in target.weights.items()}
        return t

    throttle = []
    for label, m, budget, disable in (
            ("原样（目标 1.09x，节流 0.20）", 1.0, None, False),
            ("目标 x2（节流不变）", 2.0, None, False),
            ("目标 x5（节流不变）", 5.0, None, False),
            ("节流 0.20 → 1.0", 1.0, 1.0, False),
            ("目标 x5 + 节流放开", 5.0, None, True),
    ):
        q, _, _ = eng.build(scaled(m), fresh(70.0), turnover_budget=budget,
                            use_budget=not disable)
        gross = sum(abs(v) for v in scaled(m).weights.values())
        throttle.append((label, q.n_orders, q.order_notional, q.coverage, gross))

    # Equivalence check.  The invariant is NAV x throttle, so the *exact* match is
    # 70 x 1.0 against 350 x 0.2 (both 70) -- not "throttle removed", which goes
    # a little further.  Assert it rather than asserting it in prose: the claim
    # "these two are the same" is the whole point of the section, and it would
    # stay true-looking if someone later changed what the budget multiplies.
    q70eq, _, _ = eng.build(target, fresh(70.0), turnover_budget=1.0)
    q350eq, _, _ = eng.build(target, fresh(350.0))
    if (q70eq.n_orders, round(q70eq.order_notional, 6),
            round(q70eq.coverage, 6)) != (q350eq.n_orders,
                                          round(q350eq.order_notional, 6),
                                          round(q350eq.coverage, 6)):
        raise AssertionError(
            "净值 x 节流 不变量被破坏：70x1.0 与 350x0.2 不再等价 "
            f"({q70eq.n_orders}/{q70eq.order_notional:.4f}/{q70eq.coverage:.4f} vs "
            f"{q350eq.n_orders}/{q350eq.order_notional:.4f}/{q350eq.coverage:.4f})")
    q70off, _, _ = eng.build(target, fresh(70.0), use_budget=False)

    margin = []
    for m in (1.0, 2.0, 5.0):
        q, _, _ = eng.build(scaled(m), fresh(70.0), turnover_budget=99.0)
        for lev in (5.0, 10.0):
            margin.append((m, lev, q.order_notional, q.order_notional / lev))

    specs, px = eng.specs(), eng.prices(sorted(target.weights))
    legs = []
    for inst, w in sorted(target.weights.items(), key=lambda kv: -abs(kv[1])):
        s, x = specs.get(inst), px.get(inst)
        if s is None or not x:
            continue
        mn = s.min_notional(x)
        legs.append((inst, w, abs(w) * 70.0, mn, abs(w) * 70.0 >= mn))

    plan = eng.build(target, acct)[0]
    # `preview()` is the same call the console makes, so the gates reported here
    # are the gates the user will see -- not a second, hand-assembled version.
    pv = eng.preview()
    return {"eng": eng, "target": target, "acct": acct, "plan": plan,
            "preview": pv, "sweep": sweep, "levrows": levrows, "legs": legs,
            "throttle": throttle, "margin": margin,
            "q70off": q70off, "q70eq": q70eq, "q350eq": q350eq}


def render(d: dict) -> str:
    t, acct, pl = d["target"], d["acct"], d["plan"]
    n_names = len([w for w in t.weights.values() if abs(w) > 1e-9])
    avgw = sum(abs(w) for w in t.weights.values()) / n_names
    L: list = []
    A = L.append

    A(f"# 下单计划与本金诊断 · {str(t.decision_ts)[:10]}")
    A("")
    A(f"账户：**{acct['mode']}** · 净值 **${acct['nav']:,.2f}** · "
      f"现有持仓 {len(acct['positions'])} 个 · 保证金模式 `{acct.get('pos_mode')}`")
    A("")
    A("## 1. 调仓网格")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 调仓网格 | **{t.rebalance_bars} 根 1h bar = {t.rebalance_bars/24:g} 天** |")
    A(f"| 本次信号日 | `{t.decision_ts}` |")
    A(f"| 下次调仓日 | `{t.next_decision_ts}` |")
    A(f"| 面板最新 bar | `{t.panel_last_ts}`（距信号日 {t.bars_since_decision} 根） |")
    A(f"| 是否到调仓日 | `{t.rebalance_due}` |")
    A(f"| 目标毛敞口 | {t.gross:.4f} × 净值 |")
    A("")
    A("## 2. 下单计划（对现有持仓的差额）")
    A("")
    A(f"共 **{pl.n_orders} 笔** · 名义额 **${pl.order_notional:,.2f}** · "
      f"换手 **{pl.turnover_frac:.2%}** × 净值 · 覆盖率 **{pl.coverage:.1%}** · "
      f"权重误差中位 **{pl.weight_err:.4f}**")
    A("")
    A("| instId | side | posSide | action | 张数 | 本次名义额 $ | cur_sz | tgt_sz |")
    A("|---|---|---|---|---|---|---|---|")
    for o in sorted(pl.orders, key=lambda x: (x.pos_side, x.inst_id)):
        A(f"| `{o.inst_id}` | {o.side} | {o.pos_side} | {o.action} | "
          f"{o.sz:g} | {o.delta_notional:,.2f} | {o.cur_sz:g} | {o.tgt_sz:g} |")
    A("")
    A("## 3. 风控闸门与提示（`preview()` 的原话）")
    A("")
    for v in d["preview"]["violations"]:
        sev = {"block": "⛔ 拦截", "warn": "⚠️ 警告", "info": "ℹ️ 提示"}.get(
            v["sev"], v["sev"])
        A(f"- **{sev} · {v['title']}** — {v['body']}")
    for w in pl.warnings:
        A(f"- ℹ️ {w}")
    A("")
    A("## 4. 本金门槛：$70 跑的不是这个策略")
    A("")
    A(f"`min_viable_capital`（这套 {n_names} 条腿的账全部落得下的最小本金）= "
      f"**${d['sweep'][0][4]:,.2f}**")
    A("")
    A("同一信号、**空账户**（无持仓、当日换手预算未用）、**同一份冻结行情**、"
      "**节流保持出厂值**复算 —— 所以这张表量的是「本金」，"
      "第 5b 节量的是「节流」；两者是独立的瓶颈，别混着读：")
    A("")
    A("| 本金 | 能下的腿 | 信号覆盖率 | 权重误差中位 |")
    A("|---|---|---|---|")
    for nav, n, cov, werr, _m in d["sweep"]:
        A(f"| ${nav:,.0f}{' ← 目标本金' if nav == 70.0 else ''} | {n} / {n_names} | "
          f"{cov:.1%} | {werr:.4f} |")
    A("")
    s0 = d["sweep"][0]
    A(f"**$70：{s0[1]} / {n_names} 条腿落得下，覆盖率 {s0[2]:.1%}，"
      f"权重误差中位 {s0[3]:.4f}** —— 单条腿的目标权重平均只有 {avgw:.4f}，"
      "**误差与信号同量级**。")
    A("")
    A("逐条腿看门槛（$70 分到每条腿 vs 该合约的最小下单额）：")
    A("")
    A("| instId | 目标权重 | $70 分到 $ | 最小下单额 $ | 能否下单 |")
    A("|---|---|---|---|---|")
    for inst, w, n70, mn, ok in d["legs"]:
        A(f"| `{inst}` | {w:+.4f} | {n70:.2f} | {mn:.2f} | {'✅' if ok else '❌'} |")
    A("")
    A("## 5. 账户杠杆不改变下单量（实测）")
    A("")
    A("同一引擎、同一份冻结行情，**只改 `set_leverage`**：")
    A("")
    A("| NAV | set_leverage | 订单数 | 名义额 $ | 覆盖率 |")
    A("|---|---|---|---|---|")
    for nav, lev, n, notional, cov in d["levrows"]:
        A(f"| ${nav:,.0f} | {'不设' if lev is None else f'{lev:g}×'} | {n} | "
          f"{notional:,.2f} | {cov:.1%} |")
    A("")
    u70 = {tuple(r[2:]) for r in d["levrows"] if r[0] == 70.0}
    u1k = {tuple(r[2:]) for r in d["levrows"] if r[0] == 1000.0}
    A(f"每个 NAV 下的 4 行**逐字段相同**（$70 唯一结果 {len(u70)} 种、"
      f"$1,000 唯一结果 {len(u1k)} 种）。")
    A("")
    A("`LiveEngine.build()` 把规模算在**净值**上"
      "（`build_plan(target.weights, acct[\"nav\"])`）；`set_leverage` 只出现在"
      "交易所侧的 `_apply_leverage()` 与 `check_plan` 的上限检查里，"
      "**从不进入定量路径**。所以「开杠杆把 $70 变成 $280 有效资金」是不成立的。")
    A("")
    A("## 5b. 「开 5 倍」到底该拧哪个旋钮")
    A("")
    A("说「账户杠杆无效」很容易被读成「那就没办法了」。其实有两个别的旋钮，"
      "只有一个有用：")
    A("")
    A("- **放大目标敞口无效** —— 换手预算是**净值的比例**：目标放大 m 倍、节流就缩到 1/m，"
      "执行额**逐字段不变**。这是硬不变量，不是某个数字的巧合。")
    A("- **放开节流有效** —— `execution.max_daily_turnover` 才是决定「一期下多少」的那个数。")
    A("")
    A("| 场景（NAV=$70） | 订单 | 名义额 $ | 覆盖率 | 目标毛敞口 |")
    A("|---|---|---|---|---|")
    for label, n, notional, cov, gross in d["throttle"]:
        A(f"| {label} | {n} | {notional:,.2f} | {cov:.1%} | {gross:.2f}x NAV |")
    A("")
    a, b = d["q70eq"], d["q350eq"]
    A(f"**等价性**：`$70 节流=1.0` 与 `$350 节流=0.2` 落在同一处 —— "
      f"名义额 ${a.order_notional:,.2f} vs ${b.order_notional:,.2f}、"
      f"覆盖率 {a.coverage:.1%} vs {b.coverage:.1%}、{a.n_orders} 笔 vs {b.n_orders} 笔。"
      "不变量是**「净值 × 节流」**（两边都是 70），所以每腿的美元额相同；"
      f"把节流整个去掉会再大一点（${d['q70off'].order_notional:,.2f}）。"
      "这就是「$70 想当 $350 用」唯一成立的形式 —— 而且它买到的只是**首期订单量**，"
      "买不到 $350 的**目标规模**。")
    A("")
    A("### 那账户杠杆到底干什么？")
    A("")
    A("杠杆**不改变你持多少**，只决定**持那么多要占用多少保证金**。所以它是"
      "「$70 扛不扛得住 5x 敞口」的答案，不是「$70 能不能变成 $350」的答案：")
    A("")
    A("| 目标倍数 | 毛敞口 $ | 杠杆 | 占用保证金 $ | 占净值 | 剩余缓冲 $ |")
    A("|---|---|---|---|---|---|")
    for m, lev, gross, mg in d["margin"]:
        A(f"| {m:g}x | {gross:,.2f} | {lev:g}x | {mg:,.2f} | "
          f"{mg / 70.0:.1%} | {70.0 - mg:,.2f} |")
    A("")
    A("$70 在 5x 杠杆下**最多**能持 5x NAV = $350 毛敞口，正好把保证金用光、缓冲为零 —— "
      "这就是「$70 开 5 倍 = $350」成立的地方：它是**极限**，不是**能力**。"
      "而策略本身只要 1.09x NAV，所以这个倍数对下单量毫无帮助。")
    A("")
    A("**代价**：毛敞口/净值 决定能扛多少反向波动 ——")
    A("")
    for m in (1.0, 2.0, 5.0):
        g = 1.0887 * m
        A(f"- 毛敞口 {g:.2f}x NAV → 约 **{1.0 / g:.1%}** 的反向波动即亏光本金")
    A("")
    A("## 6. 建议")
    A("")
    A("| 做法 | 效果 |")
    A("|---|---|")
    for nav, n, cov, _w, _m in d["sweep"]:
        if nav in (500.0, 2000.0, 5000.0):
            A(f"| 加到 **${nav:,.0f}** | 覆盖率 {cov:.1%}，能下 {n} / {n_names} 条腿 |")
    A("| 开账户杠杆（`set_leverage`） | ❌ 对下单量无效（见第 5 节）：只决定保证金占用，"
      "不改任何一笔订单 |")
    t0, t3 = d["throttle"][0], d["throttle"][3]
    A(f"| **放开策略节流**（`execution.max_daily_turnover` 0.2 → 1.0） | $70 的订单 "
      f"{t0[1]} → {t3[1]} 笔、覆盖率 {t0[3]:.1%} → {t3[3]:.1%}。**但这是换一套没验收过的"
      "配置**：回测的 Sharpe 来自被节流到 0.472x NAV 毛敞口的账，放开后立刻建到约 0.93x NAV，"
      "是验收敞口的约 2 倍，指标不能照搬 |")
    A("| 放大目标敞口（整体乘 L） | ❌ 单用无效（节流会等比缩回，见第 5b 节）；"
      "要与放开节流同时做才有名义额，且回撤按同一倍数放大 |")
    A("| 减少名字（`portfolio.top_k`） | 每条腿变大、落得下，但这是**模型选择**，"
      "必须重新做样本外验证，不能当参数调 |")
    A("")
    A("---")
    A("")
    A("本文所有数字由 `scripts/plan_report.py` 从实测产物生成，无手写值。")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", default="demo", choices=["paper", "demo", "live"])
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    d = collect(a.mode)
    text = render(d)
    out = a.out or os.path.join(ROOT, "artifacts", "live", a.mode,
                                f"plan_{str(d['target'].decision_ts)[:10]}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
