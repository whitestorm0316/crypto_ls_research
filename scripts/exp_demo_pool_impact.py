"""只保留模拟盘可开的合约，对策略结论的影响有多大？

对照两组：
  A) 全池（134 个 crypto）        <- 当前回测口径
  B) 模拟盘子集（68 个）           <- 如果只准开模拟盘有的
用完全相同的配置、相同的回测引擎，看 Sharpe / CAGR / MDD 差多少。
"""
import os, sys, json, time
import numpy as np
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from crypto_ls_research.config.settings import default_config
from crypto_ls_research.data.store import load_panels, list_cached_insts
from crypto_ls_research.backtest.engine import run_backtest
from crypto_ls_research.analysis.metrics import compute_metrics
from crypto_ls_research.execution.engine import LiveEngine
from crypto_ls_research.execution.signal import resolve_insts

BAR, START, END = "1h", "2021-01-01", "2026-09-26"
OV = {"portfolio.max_weight_per_instrument": 0.20,
      "execution.max_daily_turnover": 0.20,
      "factors.subset": ["range_pos", "hitrate"],
      "execution.exec_price": "next_open"}

def cfg_for(rd):
    c = default_config(bar=BAR, rebalance_bars=int(rd * 24))
    c.start, c.end = START, END
    for k, v in OV.items():
        parts = k.split("."); t = c
        for p in parts[:-1]: t = getattr(t, p)
        setattr(t, parts[-1], v)
    return c

eng = LiveEngine(mode="demo")
venue = eng.venue_instruments() or set()
pool = resolve_insts(BAR, "crypto")
sub = [i for i in pool if i in venue]
print(f"全池 {len(pool)}  模拟盘子集 {len(sub)}", flush=True)

def run(insts, label):
    p = load_panels(BAR, START, END, insts=insts)
    t0 = time.time()
    r = run_backtest(p, cfg_for(3.0))
    m = compute_metrics(r.bars, label)
    print(f"  {label:<16} bars={len(p.index):,} insts={len(p.insts):3d} "
          f"rebs={len(r.rebalances):3d}  Sharpe={m['Sharpe']:.4f}  "
          f"CAGR={m['CAGR']*100:6.2f}%  MDD={m['Max Drawdown']*100:7.2f}%  "
          f"[{time.time()-t0:.0f}s]", flush=True)
    return m

print("\n=== 全样本 2021-2026 ===", flush=True)
a = run(pool, "全池(134)")
b = run(sub, "模拟盘子集(68)")

# 分窗对照（去掉 2026）
print("\n=== 2021-2025 分窗 ===", flush=True)
def run_win(insts, label, s, e):
    p = load_panels(BAR, s, e, insts=insts)
    c = cfg_for(3.0)
    r = run_backtest(p, c)
    m = compute_metrics(r.bars, label)
    print(f"  {label:<16} Sharpe={m['Sharpe']:.4f}  CAGR={m['CAGR']*100:6.2f}%  "
          f"MDD={m['Max Drawdown']*100:7.2f}%", flush=True)
    return m
a2 = run_win(pool, "全池(134)", "2021-01-01", "2025-12-31")
b2 = run_win(sub, "模拟盘子集(68)", "2021-01-01", "2025-12-31")

print(f"\nΔ Sharpe 全样本 = {b['Sharpe']-a['Sharpe']:+.4f}")
print(f"Δ Sharpe 2021-25 = {b2['Sharpe']-a2['Sharpe']:+.4f}")
json.dump({"full": {"pool": a["Sharpe"], "demo": b["Sharpe"]},
           "win": {"pool": a2["Sharpe"], "demo": b2["Sharpe"]}},
          open("artifacts/min_capital/demo_pool_impact.json", "w"), indent=1)
