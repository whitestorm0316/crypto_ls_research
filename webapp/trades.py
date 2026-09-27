"""交易记录：从已保存的 `baseline.pkl` 重建成交明细与逐名归因。

**不重跑回测**——所有数据都来自 `BacktestResult` 里已经存下来的矩阵：

    weight_matrix[d]  第 d 个调仓期开始时**在仓**的带符号权重（执行之后）
    name_turnover[d]  第 d 个调仓点的 |Δw|（只含主动调仓，不含被动平仓）
    name_gross[d]     第 d 期各合约的价格毛损益贡献（收益单位 = 占净值比例）
    name_cost[d]      第 d 个调仓点各合约的交易成本
    name_fund[d]      第 d 期各合约的资金费**现金流**（正 = 收取）
    rebalances[d]     选币名单 / 缩放 / ADV 绑定等决策元数据

## 两个必须说清的口径

**① 成交 = 相邻两期持仓的差**，`fills[d] = weight_matrix[d] - weight_matrix[d-1]`。
这正好等于引擎里执行的 `delta`，**但被动平仓（数据断档/退市，`bars["n_stale"]`）例外**：
那条路径直接把 `held` 清零、不动 `weight_matrix`，所以它**不会**出现在 `dW` 里。
`reconcile()` 把三套换手口径摊开对齐，差额就是被动平仓，不假装它是主动成交。

**② 权重是「占净值比例」，不是面值。** 名义金额要乘当时的净值：
`notional = |Δw| × initial_capital × equity[决策 bar 前一根]`。
引擎 sizing 用的正是决策 bar 的净值（`engine.py:240`），这里对齐它。
"""
from __future__ import annotations

import math
import os
import pickle
import threading

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ARTIFACTS = os.path.join(ROOT, "artifacts")

_CACHE: dict = {}
_LOCK = threading.Lock()

KIND_LONG = "多"
KIND_SHORT = "空"

#: Below this dollar amount a "fill" is numerical residue from cap renormalisation
#: and the turnover budget (weights around 1e-7), not a real order.  It is
#: CLASSIFIED, never dropped: the ledger reconciliation runs on the full 1e-9
#: threshold and hides nothing.
DUST_USD = 1.0


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def tag_dir(tag: str) -> str:
    return os.path.join(ARTIFACTS, tag)


def result_path(tag: str) -> str:
    return os.path.join(tag_dir(tag), "baseline.pkl")


def has_result(tag: str) -> bool:
    return os.path.exists(result_path(tag))


def load_result(tag: str):
    """Unpickle + precompute the derived matrices, cached by (path, mtime, size).

    A run that is still going has no pkl yet -> ``None``.  A re-run overwrites the
    pkl, so the mtime check is what keeps the cache honest across runs.
    """
    p = result_path(tag)
    if not os.path.exists(p):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (p, st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _CACHE.get(tag)
        if hit is not None and hit["key"] == key:
            return hit

    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    with open(p, "rb") as f:
        blob = pickle.load(f)
    r = blob["result"] if isinstance(blob, dict) else blob

    W = np.asarray(r.weight_matrix, dtype="float64")          # (D, N)
    D, N = W.shape
    # Δw per rebalance; row 0 opens the book from flat.
    dW = np.empty_like(W)
    dW[0] = W[0]
    if D > 1:
        dW[1:] = W[1:] - W[:-1]

    bars = r.bars
    # `bars.index` is tz-aware; go through DatetimeIndex so the tz survives the
    # lookup.  Passing raw datetime64 throws away the tz and numpy warns.
    import pandas as pd
    dec_ts = pd.DatetimeIndex([rb["ts"] for rb in r.rebalances])
    pos = bars.index.get_indexer(dec_ts)
    eq_before = np.ones(D, dtype="float64")
    eq = bars["equity"].to_numpy(dtype="float64")
    for i, p_ in enumerate(pos):
        if p_ > 0:
            eq_before[i] = eq[p_ - 1]
        elif p_ == 0:
            eq_before[i] = 1.0

    # execution bar index per rebalance (used to slice the period P&L)
    exec_pos = bars.index.get_indexer(pd.DatetimeIndex([rb["exec_ts"] for rb in r.rebalances]))

    obj = {
        "key": key,
        "tag": tag,
        "r": r,
        "insts": list(r.insts),
        "bars": bars,
        "rebalances": r.rebalances,
        "W": W,
        "dW": dW,
        "G": np.asarray(r.name_gross, dtype="float64"),
        "C": np.asarray(r.name_cost, dtype="float64"),
        "T": np.asarray(r.name_turnover, dtype="float64"),
        "F": r.fund_matrix,
        "has_fund": getattr(r, "name_fund", None) is not None,
        "D": D,
        "N": N,
        "eq_before": eq_before,
        "exec_pos": exec_pos,
        "capital": float(getattr(r.cfg, "initial_capital", 100000.0)),
        "years": np.array([ts.year for ts in bars.index], dtype="int16"),
    }
    with _LOCK:
        _CACHE[tag] = obj
    return obj


# ---------------------------------------------------------------------------
# fills
# ---------------------------------------------------------------------------
def _action(wb: float, wa: float) -> str:
    if abs(wb) <= 1e-12 and abs(wa) <= 1e-12:
        return "无变化"
    if abs(wb) <= 1e-12:
        return "开仓"
    if abs(wa) <= 1e-12:
        return "平仓"
    if (wb > 0) != (wa > 0):
        return "反手"
    return "加仓" if abs(wa) > abs(wb) else "减仓"


def build_fills(o) -> list:
    """One record per (rebalance, instrument) with a non-zero change in weight.

    Returned as plain dicts so callers can filter/page without touching numpy.
    """
    W, dW, C = o["W"], o["dW"], o["C"]
    rb = o["rebalances"]
    insts, cap, eqb = o["insts"], o["capital"], o["eq_before"]
    out = []
    for d in range(o["D"]):
        row = dW[d]
        idx = np.flatnonzero(np.abs(row) > 1e-9)
        if idx.size == 0:
            continue
        meta = rb[d]
        sel_long = set(meta.get("long", ()))
        sel_short = set(meta.get("short", ()))
        prev_long = set(rb[d - 1].get("long", ())) if d else set()
        prev_short = set(rb[d - 1].get("short", ())) if d else set()
        ts, ex = meta["ts"], meta["exec_ts"]
        ts_s, ex_s = _iso(ts), _iso(ex)
        base = cap * float(eqb[d])
        for i in idx:
            j = int(i)
            wb = float(W[d - 1, j]) if d else 0.0
            wa = float(row[j]) + wb
            dw = float(row[j])
            notional = abs(dw) * base
            cost_w = float(C[d, j])                 # cost in RETURN units
            cost = cost_w * base                    # -> dollars, same base as notional
            name = insts[j]
            was = KIND_LONG if wb > 0 else (KIND_SHORT if wb < 0 else None)
            now = KIND_LONG if wa > 0 else (KIND_SHORT if wa < 0 else None)
            sel_now = (name in sel_long and KIND_LONG) or (name in sel_short and KIND_SHORT)
            sel_prev = (name in prev_long and KIND_LONG) or (name in prev_short and KIND_SHORT)
            out.append({
                "d": d,
                "ts": ts_s,
                "exec_ts": ex_s,
                "year": int(ts.year),
                "inst": name,
                "side": now or was or "",
                "action": _action(wb, wa),
                "w_before": wb,
                "w_after": wa,
                "dw": dw,
                "notional": notional,
                "cost": cost,
                "dust": bool(notional < DUST_USD),
                # cost per unit of traded notional -> bps; independent of the base
                "cost_bps": (cost_w / abs(dw) * 1e4) if abs(dw) > 0 else None,
                "selected_now": bool(sel_now),
                "selected_before": bool(sel_prev),
                "universe": int(meta.get("n_universe", 0)),
                "scale": float(meta.get("scale", float("nan"))),
                "binding_adv": float(meta.get("binding_adv", 0.0)),
            })
    return out


def _iso(ts) -> str:
    try:
        return ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# ---------------------------------------------------------------------------
# summary / attribution / blotter
# ---------------------------------------------------------------------------
def reconcile(o) -> dict:
    """Put the three turnover measures side by side.  The gap is passive closing."""
    bars = o["bars"]
    a = float(np.abs(o["dW"]).sum())          # 账本变化（含被动平仓的镜像）
    b = float(o["T"].sum())                   # 主动调仓，只算 delta
    c = float(bars["turnover"].sum())         # bars 口径，含被动平仓
    n_stale = float(bars["n_stale"].sum()) if "n_stale" in bars else 0.0
    return {
        "turnover_from_book_delta": a,
        "turnover_scheduled": b,
        "turnover_bars_total": c,
        "passive_close_notional": max(0.0, c - b),
        "n_stale_marks": n_stale,
        "book_vs_scheduled_gap": abs(a - b),
    }


def trade_summary(tag: str) -> dict:
    o = load_result(tag)
    if o is None:
        return {}
    fills = build_fills(o)
    cap = o["capital"]
    tot_notional = sum(f["notional"] for f in fills)
    tot_cost = sum(f["cost"] for f in fills)
    by_year: dict = {}
    for f in fills:
        y = by_year.setdefault(f["year"], {"year": f["year"], "n": 0,
                                           "notional": 0.0, "cost": 0.0})
        y["n"] += 1
        y["notional"] += f["notional"]
        y["cost"] += f["cost"]
    kinds: dict = {}
    for f in fills:
        k = kinds.setdefault(f["action"], 0)
        kinds[f["action"]] = k + 1
    insts = sorted({f["inst"] for f in fills})
    dust = [f for f in fills if f["dust"]]
    dust_n = sum(f["notional"] for f in dust)
    return {
        "tag": tag,
        "n_rebalances": o["D"],
        "n_fills": len(fills),
        "n_instruments": len(insts),
        "fills_per_rebalance": (len(fills) / o["D"]) if o["D"] else None,
        "capital": cap,
        "total_notional": tot_notional,
        "total_cost": tot_cost,
        "cost_bps_of_notional": (tot_cost / tot_notional * 1e4) if tot_notional else None,
        "avg_fill_notional": (tot_notional / len(fills)) if fills else None,
        "dust": {
            "threshold_usd": DUST_USD,
            "n": len(dust),
            "share_of_fills": (len(dust) / len(fills)) if fills else None,
            "notional": dust_n,
            "share_of_notional": (dust_n / tot_notional) if tot_notional else None,
        },
        "by_year": [by_year[y] for y in sorted(by_year)],
        "by_action": [{"action": k, "n": v} for k, v in
                      sorted(kinds.items(), key=lambda kv: -kv[1])],
        "days": (o["bars"].index[-1] - o["bars"].index[0]).days,
        "has_funding_attribution": bool(o["has_fund"]),
        "reconcile": reconcile(o),
    }


def trade_attribution(tag: str, sort: str = "net", limit: int = 0) -> list:
    """Per-instrument contribution: gross, cost, funding, net, and how it was held."""
    o = load_result(tag)
    if o is None:
        return []
    G, C, F, W = o["G"], o["C"], o["F"], o["W"]
    held = np.abs(W) > 1e-9
    n_held = held.sum(0)
    gross = G.sum(0)
    cost = C.sum(0)
    fund = F.sum(0)
    net = gross - cost + fund
    long_w = np.where(W > 0, W, 0.0).sum(0)
    short_w = np.where(W < 0, W, 0.0).sum(0)
    trades = (np.abs(o["dW"]) > 1e-9).sum(0)
    # period-level win rate for the instrument, counting only periods it was held
    with np.errstate(invalid="ignore"):
        gp = np.where(held, G, np.nan)
    wins = np.nansum(np.where(gp > 0, 1, 0), axis=0)
    lng = np.nansum(np.where(held & (G > 0), 1, 0), axis=0)
    rows = []
    for j in range(o["N"]):
        if n_held[j] == 0:
            continue
        rows.append({
            "inst": o["insts"][j],
            "gross": float(gross[j]),
            "cost": float(cost[j]),
            "funding": float(fund[j]) if o["has_fund"] else None,
            "net": float(net[j]),
            "n_periods_held": int(n_held[j]),
            "n_trades": int(trades[j]),
            "avg_abs_w": float(np.abs(W[:, j][held[:, j]]).mean()),
            "max_abs_w": float(np.abs(W[:, j]).max()),
            "long_share": float(long_w[j] / (abs(long_w[j]) + abs(short_w[j])))
            if (abs(long_w[j]) + abs(short_w[j])) > 0 else None,
            "win_rate": float(wins[j] / n_held[j]),
            "win_share_long": float(lng[j] / wins[j]) if wins[j] else None,
        })
    keys = {"net": lambda r: -r["net"], "net_worst": lambda r: r["net"],
            "gross": lambda r: -r["gross"], "cost": lambda r: -r["cost"],
            "trades": lambda r: -r["n_trades"], "name": lambda r: r["inst"]}
    rows.sort(key=keys.get(sort, keys["net"]))
    return rows[:limit] if limit else rows


def trade_blotter(tag: str, page: int = 0, per_page: int = 60, filt: dict | None = None) -> dict:
    """One row per rebalance: what was selected, how much it traded, and what it earned."""
    o = load_result(tag)
    if o is None:
        return {"rows": [], "total": 0, "page": 0, "per_page": per_page}
    filt = filt or {}
    bars = o["bars"]
    rb = o["rebalances"]
    reb_ts = o["r"].reb_ts
    exec_pos = o["exec_pos"]
    dW, C, T = o["dW"], o["C"], o["T"]
    n = o["D"]
    gross = bars["gross_ret"].to_numpy(dtype="float64")
    net = bars["net_ret"].to_numpy(dtype="float64")
    fund = bars["funding"].to_numpy(dtype="float64")
    cost = (bars["fee"] + bars["spread"] + bars["impact"]).to_numpy(dtype="float64")

    rows = []
    for d in range(n):
        if d < n - 1:
            lo, hi = int(exec_pos[d]), int(exec_pos[d + 1])
        else:
            lo, hi = int(exec_pos[d]), len(bars)
        lo = max(0, lo)
        if hi <= lo:
            hi = min(len(bars), lo + 1)
        m = rb[d]
        rows.append({
            "d": d,
            "ts": _iso(m["ts"]),
            "exec_ts": _iso(m["exec_ts"]),
            "year": int(reb_ts[d].year),
            "n_universe": int(m["n_universe"]),
            "n_long": len(m.get("long", ())),
            "n_short": len(m.get("short", ())),
            "long": list(m.get("long", ())),
            "short": list(m.get("short", ())),
            "exposure": float(m["exposure"]),
            "long_gross": float(m.get("long_gross", float("nan"))),
            "short_gross": float(m.get("short_gross", float("nan"))),
            "scale": float(m["scale"]),
            "regime_scale": float(m.get("regime_scale", float("nan"))),
            "dd_scale": float(m.get("dd_scale", float("nan"))),
            "beta_u": float(m.get("beta_u", float("nan"))),
            "beta_v": float(m.get("beta_v", float("nan"))),
            "n_replaced": int(m.get("n_replaced", 0)),
            "binding_adv": float(m.get("binding_adv", 0.0)),
            "turnover_w": float(np.abs(dW[d]).sum()),
            "scheduled_w": float(T[d].sum()),
            "n_fills": int((np.abs(dW[d]) > 1e-9).sum()),
            "cost_w": float(C[d].sum()),
            "period_gross": float(gross[lo:hi].sum()),
            "period_net": float(net[lo:hi].sum()),
            "period_funding": float(fund[lo:hi].sum()),
            "period_cost": float(cost[lo:hi].sum()),
            "n_bars": int(hi - lo),
        })

    year = filt.get("year")
    inst = filt.get("inst")
    if year:
        rows = [r for r in rows if r["year"] == int(year)]
    if inst:
        rows = [r for r in rows if inst in r["long"] or inst in r["short"]]
    total = len(rows)
    rows = rows[::-1]  # newest first
    page = max(0, int(page))
    pp = max(1, min(int(per_page), 400))
    sl = rows[page * pp:(page + 1) * pp]
    return {"rows": sl, "total": total, "page": page, "per_page": pp,
            "n_pages": max(1, math.ceil(total / pp)),
            "unit_note": ("turnover_w / scheduled_w 是净值倍数；cost_w / period_* 是"
                          "占净值比例（0.001 = 0.1%）。成交明细表里的 notional / cost "
                          "已折算成美元。")}


def trade_fills(tag: str, page: int = 0, per_page: int = 80, filt: dict | None = None) -> dict:
    o = load_result(tag)
    if o is None:
        return {"rows": [], "total": 0, "page": 0, "per_page": per_page}
    rows = o.get("_fills")
    if rows is None:
        rows = build_fills(o)
        o["_fills"] = rows
    filt = filt or {}
    year, inst = filt.get("year"), filt.get("inst")
    action, side = filt.get("action"), filt.get("side")
    q = (filt.get("q") or "").strip().upper()
    # Dust is excluded by default so the table shows real orders, but the caller is
    # always told how much was set aside -- and `include_dust` lists it all.
    include_dust = bool(filt.get("include_dust"))
    if filt.get("min_notional") not in (None, ""):
        min_notional = float(filt["min_notional"])
    else:
        min_notional = 0.0 if include_dust else DUST_USD
    out = rows
    if year:
        out = [f for f in out if f["year"] == int(year)]
    if inst:
        out = [f for f in out if f["inst"] == inst]
    elif q:
        out = [f for f in out if q in f["inst"].upper()]
    if action:
        out = [f for f in out if f["action"] == action]
    if side:
        out = [f for f in out if f["side"] == side]
    if min_notional:
        out = [f for f in out if f["notional"] >= min_notional]
    total = len(out)
    out = out[::-1]
    page = max(0, int(page))
    pp = max(1, min(int(per_page), 500))
    sl = out[page * pp:(page + 1) * pp]
    agg = {"notional": sum(f["notional"] for f in out),
           "cost": sum(f["cost"] for f in out)}
    dust = [f for f in rows if f["dust"]]
    dust_n = sum(f["notional"] for f in dust)
    allwhy = sum(f["notional"] for f in rows)
    return {"rows": sl, "total": total, "page": page, "per_page": pp,
            "n_pages": max(1, math.ceil(total / pp)),
            "n_fills_all": len(rows), "agg": agg,
            "include_dust": include_dust, "min_notional": min_notional or None,
            "dust": {"threshold_usd": DUST_USD, "n": len(dust),
                     "share_of_fills": (len(dust) / len(rows)) if rows else None,
                     "notional": dust_n,
                     "share_of_notional": (dust_n / allwhy) if allwhy else None}}


def fills_csv(tag: str, filt: dict | None = None) -> str:
    """Full (unpaged) fill list as CSV -- what an auditor actually wants."""
    import csv
    import io
    o = load_result(tag)
    if o is None:
        return ""
    rows = build_fills(o)
    filt = filt or {}
    if filt.get("year"):
        rows = [f for f in rows if f["year"] == int(filt["year"])]
    if filt.get("inst"):
        rows = [f for f in rows if f["inst"] == filt["inst"]]
    if filt.get("action"):
        rows = [f for f in rows if f["action"] == filt["action"]]
    if filt.get("side"):
        rows = [f for f in rows if f["side"] == filt["side"]]
    # The export is the audit artefact: it keeps EVERY fill (dust included) unless
    # the caller explicitly asks for a dollar floor, and marks dust in its own column.
    if filt.get("min_notional") not in (None, ""):
        rows = [f for f in rows if f["notional"] >= float(filt["min_notional"])]
    cols = ["d", "ts", "exec_ts", "year", "inst", "side", "action", "w_before",
            "w_after", "dw", "notional", "cost", "cost_bps", "dust", "selected_now",
            "selected_before", "universe", "scale", "binding_adv"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for f in rows:
        w.writerow({c: (f"{f[c]:.10g}" if isinstance(f[c], float) else f[c]) for c in cols})
    return buf.getvalue()


def attribution_csv(tag: str) -> str:
    import csv
    import io
    rows = trade_attribution(tag)
    cols = ["inst", "gross", "cost", "funding", "net", "n_periods_held", "n_trades",
            "avg_abs_w", "max_abs_w", "long_share", "win_rate"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else
                        (f"{r[c]:.10g}" if isinstance(r[c], float) else r[c])) for c in cols})
    return buf.getvalue()


def blotter_csv(tag: str) -> str:
    import csv
    import io
    o = load_result(tag)
    if o is None:
        return ""
    d = trade_blotter(tag, page=0, per_page=400)
    rows = list(d["rows"])
    while len(rows) < d["total"]:
        nxt = trade_blotter(tag, page=len(rows) // 400, per_page=400)["rows"]
        if not nxt:
            break
        rows.extend(nxt)
    flat = []
    for r in rows:
        g = dict(r)
        g["long"] = "|".join(r["long"])
        g["short"] = "|".join(r["short"])
        flat.append(g)
    cols = ["d", "ts", "exec_ts", "year", "n_universe", "n_long", "n_short",
            "long", "short", "exposure", "scale", "n_replaced", "binding_adv",
            "turnover_w", "n_fills", "cost_w", "period_gross", "period_net",
            "period_funding", "period_cost", "n_bars"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in flat:
        w.writerow({c: ("" if r.get(c) is None else
                        (f"{r[c]:.10g}" if isinstance(r[c], float) else r[c])) for c in cols})
    return buf.getvalue()


def trade_options(tag: str) -> dict:
    """Filter dropdowns: which years / instruments / actions exist."""
    o = load_result(tag)
    if o is None:
        return {"years": [], "instruments": [], "actions": [], "sides": []}
    rows = build_fills(o)
    return {
        "years": sorted({f["year"] for f in rows}, reverse=True),
        "instruments": sorted({f["inst"] for f in rows}),
        "actions": sorted({f["action"] for f in rows}),
        "sides": sorted({f["side"] for f in rows if f["side"]}),
    }
