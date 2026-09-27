"""Drive the paper trading desk end to end over HTTP, exactly as the UI does.

Plan -> execute -> reconcile -> read back orders/fills/runs/equity.  This walks
the same endpoints the browser walks, so it also exercises the job queue (a plan
returns a job id immediately and the page polls it).

  python scripts/live_e2e_paper.py [baseUrl] [nav]

Run it with the server up:  python webapp/server.py 8790
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

# The box sets HTTP_PROXY; a local request must not be sent to it (it answers 502).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
NAV = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0


def call(path: str, body: dict | None = None, method: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as r:
        return json.loads(r.read().decode())


def run_job(path: str, body: dict, label: str, timeout: float = 600.0) -> dict:
    job = call(path, body)
    if "id" not in job:
        raise SystemExit(f"{label}: 没有拿到 job id，返回 {job}")
    jid, t0, seen = job["id"], time.time(), 0
    print(f"  [{label}] job {jid} …", flush=True)
    while True:
        j = call(f"/api/live/job/{jid}")
        if len(j.get("log") or []) > seen:
            for line in j["log"][seen:]:
                print(f"      {line['msg']}", flush=True)
            seen = len(j["log"])
        if j["status"] in ("done", "failed"):
            print(f"  [{label}] {j['status']} in {j.get('elapsed')}s", flush=True)
            if j["status"] == "failed":
                print(j.get("trace", ""), flush=True)
                raise SystemExit(1)
            return j["result"] or {}
        if time.time() - t0 > timeout:
            raise SystemExit(f"{label}: 超时")
        time.sleep(1.0)


def main() -> None:
    print(f"base={BASE} nav={NAV}")
    print("\n=== 0. 重置纸面账户 ===")
    print(" ", call("/api/live/reset", {"mode": "paper", "nav": NAV}))
    st = call("/api/live/status?mode=paper")
    print(f"  nav={st['account']['nav']} 持仓={len(st['account']['positions'])} "
          f"stale={st['data_staleness']['stale']}")
    if st["data_staleness"]["stale"]:
        raise SystemExit("行情仍是陈旧的，先跑 scripts/refresh_data_once.py")

    print("\n=== 1. 生成下单计划（强制重算信号）===")
    prev = run_job("/api/live/plan", {"mode": "paper", "fresh": True}, "plan")
    pl = prev["plan"]
    print(f"  原始目标毛敞口 {pl['raw_target_gross']:.2f} -> 预算后 {pl['target_gross']:.2f}"
          f"（scale {pl['turnover_scale']:.4f}）")
    print(f"  订单 {pl['n_orders']} 笔 / 名义 {pl['order_notional']:.2f} / "
          f"换手 {pl['turnover_frac']:.4f} / 覆盖 {pl['coverage']:.4f} / "
          f"权重误差 {pl['weight_err']:.5f}")
    print(f"  跳过 {len(pl['skips'])} 条；警告 {len(pl['warnings'])} 条；"
          f"拦截 {sum(1 for v in prev['violations'] if v['sev']=='block')} 条")
    for v in prev["violations"]:
        print(f"    [{v['sev']}] {v['title']}")
    if prev.get("blocked"):
        raise SystemExit("计划被风控拦截，不应继续")

    print("\n=== 2. 执行（纸面成交）===")
    res = run_job("/api/live/execute",
                  {"mode": "paper", "dry_run": False, "force": True}, "execute")
    rec = res.get("record") or {}
    ok = sum(1 for r in (rec.get("results") or []) if r.get("ok"))
    print(f"  成交 {ok}/{len(rec.get('results') or [])}，stage={res.get('stage')}")
    for r in (rec.get("results") or []):
        flag = "ok " if r.get("ok") else "ERR"
        print(f"    {flag} {r['instId']:22s} {r.get('sz')} @ {r.get('px')} {r.get('error') or ''}")

    print("\n=== 3. 对账 ===")
    print(" ", run_job("/api/live/reconcile", {"mode": "paper"}, "reconcile"))

    print("\n=== 4. 读回账本 ===")
    st = call("/api/live/status?mode=paper")
    a = st["account"]
    print(f"  nav={a['nav']:.4f} 现金={a.get('cash', float('nan')):.4f} "
          f"未实现={a.get('unrealised', float('nan')):.4f} 持仓 {len(a['positions'])} 个 "
          f"换手已用 {st['turnover_used_today']:.4f}（预算 {st['turnover_budget']}）")
    tot, missing = 0.0, 0
    for p in a["positions"]:
        # `notional` from the server is |sz| * ctVal * markPx.  Recomputing it here
        # as |sz| * markPx treats contracts as coins (BTC off by 100x).
        n = p.get("notional")
        if not isinstance(n, (int, float)):
            missing += 1
            continue
        tot += float(n)
    for p in a["positions"][:6]:
        n = p.get("notional")
        print(f"    {p['instId']:22s} sz={p['pos']:>12.5f} avg={p['avgPx']:>12.6f} "
              f"mark={p['markPx']:>12.6f} 未实现={p['upl']:>9.3f} "
              f"名义={'—' if not isinstance(n, (int, float)) else f'{float(n):.2f}'}")
    if len(a["positions"]) > 6:
        print(f"    … 其余 {len(a['positions']) - 6} 个")
    print(f"  毛敞口合计 {tot:.2f} = {tot / a['nav']:.4f}× 净值"
          + (f"（{missing} 个缺规格，未计入）" if missing else ""))
    for kind in ("orders", "fills", "runs", "equity"):
        rows = call(f"/api/live/{kind}?mode=paper&limit=500").get("rows") or []
        print(f"  {kind:8s} {len(rows)} 条" + (f"  首条 keys={sorted(rows[0])}" if rows else ""))
    print("\nDONE")


if __name__ == "__main__":
    main()
