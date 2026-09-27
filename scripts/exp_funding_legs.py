"""
做空腿的资金费是不是更高？—— 多空腿资金费分解 + 流动性×费率横截面关系

用户问题：策略「多流动性最好、空流动性最差」，那做空腿的资金费会不会更高？

先厘清符号（engine.py:405 / 421）：
    name_fund = -mult * w * fr          （accounting：资金费是现金流）
  → fr = -name_fund / (mult * w)        （w != 0）
  fr > 0  ⇒  多头付钱、空头收钱。所以 fr>0 对空头腿是收益。
  单位：fr 是该调仓期内累计的资金费率（小数），与 bars 的 1h 频率无关，
       这里只看相对大小与横截面排序，不涉及年化。

要验的三件事：
  ① 前提是否成立：做空腿的流动性（ADV）是否真的比多头腿差？
  ② 核心问题：空头腿的 fr 是否系统性地高于多头腿 / 高于池均值？
  ③ 稳健性：该差异跨年是否稳定，还是由 2026 单独决定（本项目的老毛病）。
"""
import os
import pickle
import numpy as np
import pandas as pd

ROOT = r"C:\Users\50651\WorkBuddy\2026-09-26-14-25-09"
OUT = os.path.join(ROOT, "artifacts", "funding_legs")
os.makedirs(OUT, exist_ok=True)

PKL = os.path.join(ROOT, "artifacts", "fund_diag", "baseline.pkl")
b = pickle.load(open(PKL, "rb"))
r = b["result"]
mult = float(r.cfg.costs.funding_multiplier)
assert abs(mult - 1.0) < 1e-12, f"v3 的 funding_multiplier={mult}，反推公式需按此缩放"

# ---- 自校验：这份产物必须与已验收的 v3 逐位一致 ---------------------------
# v3 的 baseline.pkl 早于 `name_fund` 字段（那份是零矩阵），所以用 bars 复核。
_REF_FUND = 0.020574444790719387      # v3 报告：资金费 P&L 累计
_REF_SHARPE = 1.9373269448000032
_f = float(r.bars["funding"].sum())
print(f"[自校验] bars.funding 累计 {_f:.18f}  (期望 {_REF_FUND:.18f})  "
      f"Δ={_f - _REF_FUND:+.3e}")
assert abs(_f - _REF_FUND) < 1e-12, "产物与已验收 v3 不一致，分析不可信"
assert "name_fund" in r.__dict__, "name_fund 仍未落盘"

W = np.asarray(r.weight_matrix, dtype="float64")   # (D,N) 期内生效权重
F = np.asarray(r.fund_matrix, dtype="float64")     # (D,N) 资金费现金流（占净值）
A = np.asarray(r.adv_matrix, dtype="float64")      # (D,N) ADV（美元）
ts = pd.DatetimeIndex(r.reb_ts)
D, N = W.shape
print(f"调仓期 {D}  合约 {N}  funding_multiplier={mult}")

# ---- 反推每期每名的累计资金费率 -------------------------------------------
held_all = np.abs(W) > 1e-12
with np.errstate(divide="ignore", invalid="ignore"):
    FR = np.where(held_all, -F / (mult * np.where(held_all, W, np.nan)), np.nan)

rows = []
quint = []          # (期号, 五分位, 该组平均fr, 该组净权重方向)
for d in range(D):
    w, f, a, fr = W[d], F[d], A[d], FR[d]
    lng = held_all[d] & (W[d] > 0)
    sht = held_all[d] & (W[d] < 0)
    if lng.sum() == 0 or sht.sum() == 0:
        continue
    hold = lng | sht
    gl = float(np.abs(w[lng]).sum())
    gs = float(np.abs(w[sht]).sum())

    rows.append(dict(
        ts=ts[d],
        n_long=int(lng.sum()), n_short=int(sht.sum()),
        gross_long=gl, gross_short=gs,
        # 简单平均费率（回答「这些币的费率是多少」）
        fr_long=float(np.nanmean(fr[lng])),
        fr_short=float(np.nanmean(fr[sht])),
        fr_pool=float(np.nanmean(fr[hold])),
        # 名义加权平均费率（回答「对组合的贡献是多少」）
        fr_long_w=float(np.nansum(w[lng] * fr[lng]) / gl),
        fr_short_w=float(np.nansum(-w[sht] * fr[sht]) / gs),
        # 现金流方向（美元口径=占净值比例）
        fund_long=float(f[lng].sum()),      # 多头腿现金流（fr>0 时为负=支出）
        fund_short=float(f[sht].sum()),     # 空头腿现金流（fr>0 时为正=收入）
        # 流动性
        adv_long=float(np.nanmedian(a[lng])),
        adv_short=float(np.nanmedian(a[sht])),
        adv_pool=float(np.nanmedian(a[hold])),
    ))

    # 池内 ADV 五分位（每期内部排序，避免跨期 ADV 水平差异污染）
    m = hold & np.isfinite(a)
    if m.sum() >= 5:
        idx = np.where(m)[0]
        order = idx[np.argsort(a[idx])]
        k = len(order)
        for qi in range(5):
            sub = order[int(qi * k / 5):int((qi + 1) * k / 5)]
            if len(sub) == 0:
                continue
            sw = float(np.sum(w[sub]))
            samt = float(np.sum(np.abs(w[sub])))
            quint.append(dict(d=d, ts=ts[d], q=qi,
                              fr=float(np.nanmean(fr[sub])),
                              adv=float(np.nanmedian(a[sub])),
                              net_dir=sw / samt if samt > 0 else np.nan,
                              n=len(sub)))

df = pd.DataFrame(rows)
df["ts"] = pd.to_datetime(df["ts"])
df["year"] = df["ts"].dt.year
df["d_fr"] = df["fr_short"] - df["fr_long"]                 # 核心：空腿 − 多腿
df["d_fr_pool"] = df["fr_short"] - df["fr_pool"]            # 空腿相对池均值的超额
df["fund_total"] = df["fund_long"] + df["fund_short"]
df.to_csv(os.path.join(OUT, "funding_legs_per_reb.csv"), index=False, encoding="utf-8-sig")

q = pd.DataFrame(quint)
q.to_csv(os.path.join(OUT, "funding_legs_adv_quintile.csv"), index=False, encoding="utf-8-sig")

# ---- 汇总 -----------------------------------------------------------------
FMT = lambda x: f"{x:+.5f}"
print("\n" + "=" * 78)
print("① 前提检验：做空腿的流动性真的更差吗？")
print("=" * 78)
print(f"  ADV 中位数 —— 多头腿 {df.adv_long.median()/1e6:9.2f}M   空头腿 {df.adv_short.median()/1e6:9.2f}M   "
      f"池内 {df.adv_pool.median()/1e6:9.2f}M")
print(f"  空头腿 ADV 低于多头腿的调仓期占比：{(df.adv_short < df.adv_long).mean():.2%}"
      f"   （若≈50% 说明策略并没有按流动性选边）")

print("\n" + "=" * 78)
print("② 核心问题：空头腿的资金费率是否更高？")
print("=" * 78)
print(f"  每期平均费率（简单平均，跨 {len(df)} 个调仓期）")
print(f"    多头腿 {FMT(df.fr_long.mean())}    空头腿 {FMT(df.fr_short.mean())}    池内 {FMT(df.fr_pool.mean())}")
print(f"    差值 空−多 = {FMT(df.d_fr.mean())}    空−池 = {FMT(df.d_fr_pool.mean())}")
print(f"    空头腿费率高于多头腿的调仓期占比：{(df.d_fr > 0).mean():.2%}")
print(f"  名义加权平均费率（对组合的贡献口径）")
print(f"    多头腿 {FMT(df.fr_long_w.mean())}    空头腿 {FMT(df.fr_short_w.mean())}")
print(f"  资金费现金流（占净值，全样本累计）")
print(f"    多头腿累计 {df.fund_long.sum():+.6f}    空头腿累计 {df.fund_short.sum():+.6f}    "
      f"合计 {df.fund_total.sum():+.6f}")
print(f"    空头腿是净收取的比例（>0）：{(df.fund_short > 0).mean():.2%}；"
      f"多头腿是净支出的比例（<0）：{(df.fund_long < 0).mean():.2%}")

print("\n" + "=" * 78)
print("③ 池内 ADV 五分位 × 费率（q0=流动性最差 … q4=流动性最好）")
print("=" * 78)
agg = q.groupby("q").agg(fr=("fr", "mean"), adv=("adv", "median"),
                         net_dir=("net_dir", "mean"), n=("n", "mean"))
for qi, row in agg.iterrows():
    side = "净空" if row.net_dir < 0 else "净多"
    print(f"  q{qi}  ADV {row.adv/1e6:9.2f}M   平均费率 {FMT(row.fr)}   "
          f"净权重方向 {FMT(row.net_dir)} ({side})   平均 {row.n:4.1f} 名")

print("\n" + "=" * 78)
print("④ 逐年稳定性")
print("=" * 78)
yr = df.groupby("year").agg(
    n=("ts", "size"),
    fr_long=("fr_long", "mean"), fr_short=("fr_short", "mean"),
    fr_pool=("fr_pool", "mean"), d_fr=("d_fr", "mean"),
    fund_long=("fund_long", "sum"), fund_short=("fund_short", "sum"),
    fund_total=("fund_total", "sum"),
    adv_long=("adv_long", "median"), adv_short=("adv_short", "median"),
)
print(f"  {'年份':6s} {'期数':>4s} {'多腿fr':>10s} {'空腿fr':>10s} {'池fr':>10s} "
      f"{'空−多':>10s} {'空腿现金流':>12s} {'多腿现金流':>12s}")
for y, row in yr.iterrows():
    print(f"  {y:<6d} {int(row.n):4d} {FMT(row.fr_long):>10s} {FMT(row.fr_short):>10s} "
          f"{FMT(row.fr_pool):>10s} {FMT(row.d_fr):>10s} {row.fund_short:>+12.5f} {row.fund_long:>+12.5f}")
yr.to_csv(os.path.join(OUT, "funding_legs_by_year.csv"), encoding="utf-8-sig")

print(f"\n产物 → {OUT}")

# ===========================================================================
# ⑤ regime 分组：费率的符号决定一切
# ===========================================================================
print("\n" + "=" * 78)
print("⑤ regime 分组：池内平均费率 > 0 （多头付费） vs < 0 （空头付费）")
print("=" * 78)
# 年化系数：每期 = rebalance_days 个自然日
rd = float(r.cfg.rebalance_days) if hasattr(r.cfg, "rebalance_days") else 3.0
ann = 365.0 / rd
print(f"  （每期 {rd:.0f} 天，年化系数 ×{ann:.1f}）")
df["regime"] = np.where(df.fr_pool > 0, "费率正(多头拥挤)", "费率负(空头拥挤)")
for g, sub in df.groupby("regime"):
    print(f"\n  【{g}】{len(sub)} 期（占 {len(sub)/len(df):.1%}）")
    print(f"    平均费率  多腿 {FMT(sub.fr_long.mean())}  空腿 {FMT(sub.fr_short.mean())}  "
          f"池 {FMT(sub.fr_pool.mean())}   空−多 {FMT(sub.d_fr.mean())}")
    print(f"    空腿费率 > 多腿 的期数占比：{(sub.d_fr > 0).mean():.1%}")
    print(f"    现金流    空腿累计 {sub.fund_short.sum():+.5f}   多腿累计 {sub.fund_long.sum():+.5f}   "
          f"合计 {sub.fund_total.sum():+.5f}")
    print(f"    年化费率（简单平均）  空腿 {sub.fr_short.mean()*ann:+.2%}   多腿 {sub.fr_long.mean()*ann:+.2%}")

# ===========================================================================
# ⑥ 横截面秩相关：流动性 ↓ 是否真的伴随费率 ↑
# ===========================================================================
print("\n" + "=" * 78)
print("⑥ 池内横截面：rank(ADV) 与 fr 的秩相关（每期算一次，再汇总）")
print("=" * 78)
rhos = []
for d in range(D):
    hold = held_all[d]
    m = hold & np.isfinite(A[d])
    if m.sum() < 6:
        continue
    adv = A[d][m]
    fr = FR[d][m]
    if np.nanstd(adv) <= 0 or np.nanstd(fr) <= 0:
        continue
    rho = pd.Series(adv).corr(pd.Series(fr), method="spearman")
    if np.isfinite(rho):
        rhos.append((ts[d], rho, int(m.sum())))
rr = pd.DataFrame(rhos, columns=["ts", "rho", "n"])
rr["year"] = pd.to_datetime(rr.ts).dt.year
t_stat = rr.rho.mean() / (rr.rho.std(ddof=1) / np.sqrt(len(rr)))
print(f"  逐期 Spearman(ADV, fr) 均值 {rr.rho.mean():+.4f}   "
      f"t = {t_stat:+.2f}   （共 {len(rr)} 期，平均 {rr.n.mean():.1f} 名）")
print(f"  负相关（ADV 越低 fr 越高）的期数占比：{(rr.rho < 0).mean():.1%}")
print("  逐年：")
for y, sub in rr.groupby("year"):
    tt = sub.rho.mean() / (sub.rho.std(ddof=1) / np.sqrt(len(sub))) if sub.rho.std(ddof=1) > 0 else np.nan
    print(f"    {y}  均值 {sub.rho.mean():+.4f}   t={tt:+6.2f}   n={len(sub)}")
rr.to_csv(os.path.join(OUT, "funding_legs_rank_corr.csv"), index=False, encoding="utf-8-sig")
df.to_csv(os.path.join(OUT, "funding_legs_per_reb.csv"), index=False, encoding="utf-8-sig")

# ===========================================================================
# ⑦ 折算成年化：这块资金费到底值多少钱
# ===========================================================================
print("\n" + "=" * 78)
print("⑦ 年化折算（占净值）")
print("=" * 78)
span_years = (df.ts.iloc[-1] - df.ts.iloc[0]).days / 365.25
print(f"  样本跨度 {span_years:.2f} 年（{df.ts.iloc[0].date()} → {df.ts.iloc[-1].date()}）")
print(f"  资金费现金流累计：空腿 {df.fund_short.sum():+.5f}   多腿 {df.fund_long.sum():+.5f}   "
      f"合计 {df.fund_total.sum():+.5f}")
print(f"  年化（占净值）：空腿 {df.fund_short.sum()/span_years:+.2%}   "
      f"多腿 {df.fund_long.sum()/span_years:+.2%}   合计 {df.fund_total.sum()/span_years:+.2%}")
print(f"  同期净成本拖累（报告口径）：1.71%/yr  →  资金费约为成本的 "
      f"{abs(df.fund_total.sum()/span_years)/0.0171:.0%}")

# ===========================================================================
# ⑧ 同一流动性桶内部：多腿 vs 空腿的费率差还在不在？
#    若在 → 「空腿费率更高」是选币 alpha；若消失 → 只是流动性聚合效应。
# ===========================================================================
print("\n" + "=" * 78)
print("⑧ 控制流动性：同一个 ADV 五分位内，多腿 vs 空腿的费率")
print("=" * 78)
buckets = {qi: [] for qi in range(5)}
for d in range(D):
    w, a, fr = W[d], A[d], FR[d]
    lng = held_all[d] & (w > 0)
    sht = held_all[d] & (w < 0)
    hold = lng | sht
    m = hold & np.isfinite(a)
    if m.sum() < 5:
        continue
    idx = np.where(m)[0]
    order = idx[np.argsort(a[idx])]
    k = len(order)
    for qi in range(5):
        sub = set(order[int(qi * k / 5):int((qi + 1) * k / 5)].tolist())
        sl = [j for j in sub if lng[j]]
        ss = [j for j in sub if sht[j]]
        if len(sl) >= 1 and len(ss) >= 1:
            buckets[qi].append((np.mean(fr[sl]), np.mean(fr[ss]), len(sl), len(ss)))
print(f"  {'桶':4s} {'期数':>5s} {'多腿fr':>10s} {'空腿fr':>10s} {'空−多':>10s}  {'多/空名数':>10s}")
for qi in range(5):
    arr = np.array(buckets[qi], dtype=float)
    if len(arr) == 0:
        continue
    dl = arr[:, 0].mean(); ds = arr[:, 1].mean()
    print(f"  q{qi:<3d} {len(arr):5d} {FMT(dl):>10s} {FMT(ds):>10s} {FMT(ds - dl):>10s}  "
          f"{arr[:,2].mean():5.1f}/{arr[:,3].mean():5.1f}")
print("  （若每桶的空−多仍为正 → 差异不是流动性聚合造成的，而是选币本身挑到了高费率）")

# ===========================================================================
# ⑨ 简单平均 vs 名义加权：权重大的空头币费率是不是更低
# ===========================================================================
print("\n" + "=" * 78)
print("⑨ 口径差异：为什么「年化费率」远大于「实际收到」")
print("=" * 78)
print(f"  空腿费率  简单平均 {FMT(df.fr_short.mean())}   名义加权 {FMT(df.fr_short_w.mean())}   "
      f"→ 加权只有简单平均的 {df.fr_short_w.mean()/df.fr_short.mean():.0%}")
print(f"  多腿费率  简单平均 {FMT(df.fr_long.mean())}   名义加权 {FMT(df.fr_long_w.mean())}   "
      f"→ {df.fr_long_w.mean()/df.fr_long.mean():.0%}")
print(f"  空腿名义敞口 Σ|w_short| 平均 {df.gross_short.mean():.4f}   多腿 {df.gross_long.mean():.4f}   "
      f"合计 {df.gross_short.mean()+df.gross_long.mean():.4f}")
print(f"  → 加权费率只有简单平均的一半，说明权重大的空头币费率更低")
print(f"     （策略按 score 排权重，不是按费率；高费率的小币分到的权重并不更多）")
print(f"\n产物 → {OUT}")
