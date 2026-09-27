"""一次性实验：探三个「当前生效、但从未被扫过」的参数角落。

为什么要做这一轮（侦察结论）：

  * `15_sensitivity_ofat` 里 momentum / flow 系列的三组 lookback 网格
    跑出来的 Sharpe **逐位相同**，全是 no-op —— v3 已经把那两个因子剪掉了，
    扫描它们等于扫空气，三组网格的算力白费。
  * 真正生效的因子参数只剩两个窗口：`range_days` 与 `hitrate_days`。
    - `hitrate_days` 原网格 [1, 2, 5.25] 日 → Sharpe 1.650 / 1.787 / **1.937**，
      **单调上升，5.25 日正是网格上界 = argmax 落在端点**，方向没探完。
    - `range_days` 原网格 [3.75, 7.5, 15] 日 → 1.506 / **1.937** / 1.617，
      内点最优但只有 3 个点、跨度 4 倍，邻域太粗。
  * **因子权重比从未被当作参数扫过**：v3 子集 (range_pos, hitrate) 下，
    4 个 profile 实际只给出两个不同的点 —— `A_MOM_TILT` → (0.6, 0.4)，
    B / C / D_EQUAL 全部 → (0.5, 0.5)。复合打分只剩 **1 个自由度**，
    而因子剪枝是本项目收益最大的一项 —— 说明"怎么组合因子"是高杠杆区。

每个网格都保留一个「当前默认点」做自校验：必须**逐位复现** v3 的
Sharpe 1.9373269448000032，否则说明 override 路径没接上，整张表作废。

判据用报告 §11b（**不看全样本 Δ**）：
    全年正向占比 >= 2/3  ·  单年贡献 <= Σ|Δ| 的 35%  ·  锁定样本(2026) 同号

本轮新增的第二个口径（**这是重点**）：全样本 argmax 与 2021–2025 argmax
并排。本项目已三次发现"全样本小增益由 2026 单独决定"，所以每个参数都要问
一次：**它的最优值是不是 2026 的产物**。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from crypto_ls_research.analysis.sweep import run_sweep            # noqa: E402
from crypto_ls_research.config.settings import BARS_PER_DAY        # noqa: E402
from crypto_ls_research.data.asset_class import filter_insts, load_categories  # noqa: E402
from crypto_ls_research.data.store import list_cached_insts        # noqa: E402

BASE_SHARPE = 1.9373269448000032          # v3 已验收值，用作自校验
BAR, RD, START, END = "1h", 3.0, "2021-01-01", "2026-09-26"
REBAL = max(1, int(round(RD * BARS_PER_DAY[BAR])))
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "artifacts", "window_sweep"))

BASE_OV = {
    "factors.subset": ["range_pos", "hitrate"],
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
}

DIMS = (("hitrate_days=", "hitrate_days"), ("range_days=", "range_days"),
        ("w_range=", "w_range"))


def scoped_insts():
    pool = list_cached_insts(BAR)
    cats = load_categories()
    scoped, unknown = filter_insts(pool, cats, "crypto")
    if unknown:
        raise SystemExit(f"cannot classify {len(unknown)} cached instrument(s): {unknown[:6]}")
    return scoped


def spec(label, **extra_ov):
    return {"label": label, "overrides": {**BASE_OV, **extra_ov}, "rebalance_bars": REBAL}


def build_specs():
    specs = []

    # --- 1. hitrate_days 向上延伸（原网格上界 5.25 日是 argmax） ---
    for d in (3.75, 5.25, 7.5, 10.0, 15.0, 21.0):
        specs.append(spec(f"hitrate_days={d}", **{"factors.hitrate_days": d}))

    # --- 2. range_days 邻域细化（原网格只有 3 点、跨 4 倍） ---
    for d in (4.0, 5.0, 6.0, 7.5, 9.0, 11.0, 15.0):
        specs.append(spec(f"range_days={d}", **{"factors.range_days": d}))

    # --- 3. 因子权重比 range_pos : hitrate（v3 下只提供 0.6:0.4 与 0.5:0.5 两点） ---
    # profiles 元组按 (momentum, flow, range_pos, hitrate, rev_short) 索引，
    # 子集 (range_pos, hitrate) 取索引 2、3 后内部归一化 -> 就是 w 与 1-w。
    for w in (0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00):
        specs.append(spec(
            f"w_range={w:.2f}",
            **{"factors.profiles": {"A_MOM_TILT": (0.0, 0.0, w, 1.0 - w, 0.0)}},
        ))

    return specs


def year_sharpe(bars: pd.DataFrame) -> dict:
    """逐年 Sharpe，口径 = 先日频求和再 ×√365（与 35_year_structure.csv 一致）。"""
    nr = bars["net_ret"].astype("float64")
    daily = nr.resample("1D").sum()
    out = {}
    for y, x in daily.groupby(daily.index.year):
        x = x.dropna()
        out[int(y)] = float(x.mean() / x.std() * np.sqrt(365)) if len(x) > 1 and float(x.std()) > 0 else np.nan
    return out


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    insts = scoped_insts()
    specs = build_specs()
    print(f"### window_sweep: {len(specs)} 配置, pool={len(insts)} 合约, "
          f"rebalance={REBAL} bars ({RD}d), window={START}..{END}", flush=True)

    res = run_sweep(specs, BAR, START, END, insts=insts, n_jobs=4, desc="window")

    rows = []
    for r in res:
        if "error" in r:
            print(f"  !! {r['label']} failed: {r['error']}")
            continue
        ys = year_sharpe(r["bars"])
        rows.append({
            "label": r["label"],
            "sharpe": float(r["metrics"]["Sharpe"]),
            "cagr": float(r["metrics"]["CAGR"]),
            "mdd": float(r["metrics"]["Max Drawdown"]),
            **{f"sh_{y}": v for y, v in ys.items()},
        })

    df = pd.DataFrame(rows)

    # ---- 两个口径并排：全样本 vs 2021–2025 ----
    YC = [c for c in df.columns if c.startswith("sh_") and c != "sh_2026"]
    df["sh_2125_mean"] = df[YC].mean(axis=1)
    df["sh_2125_med"] = df[YC].median(axis=1)

    # ---- 自校验 ----
    print("\n=== 自校验（默认点必须逐位复现 v3）===")
    ok = True
    for p in ("hitrate_days=5.25", "range_days=7.5", "w_range=0.60"):
        hit = df.loc[df["label"] == p, "sharpe"]
        if hit.empty:
            print(f"  {p}: 缺失！"); ok = False; continue
        v = float(hit.iloc[0])
        same = abs(v - BASE_SHARPE) < 1e-12
        ok &= same
        print(f"  {p:22s} {v:.16f}  {'OK 逐位一致' if same else 'FAIL Δ=%+.3e' % (v - BASE_SHARPE)}")
    if not ok:
        print("\n  !! 自校验失败 —— override 路径可能没接上，整张表不可用。")

    # ---- 判据（§11b，基准 = 默认点） ----
    base_row = df.loc[df["label"] == "hitrate_days=5.25"]
    if not base_row.empty:
        b = base_row.iloc[0]
        d_full = df["sharpe"] - float(b["sharpe"])
        jud = []
        for _, r in df.iterrows():
            dy = {c: float(r[c]) - float(b[c]) for c in YC}
            is_base = abs(float(d_full[r.name])) < 1e-12
            pos = [v for v in dy.values() if np.isfinite(v)]
            share_pos = float(np.mean([v > 0 for v in pos])) if pos else np.nan
            tot = sum(abs(v) for v in pos)
            max_share = float(max((abs(v) for v in pos), default=0) / tot) if tot else np.nan
            d2026 = float(r["sh_2026"]) - float(b["sh_2026"])
            same_sign = np.isfinite(d2026) and np.sign(d2026) == np.sign(d_full[r.name])
            jud.append({
                "label": r["label"],
                "d_full": d_full[r.name],
                "d_2021_2025_正数占比": share_pos,
                "单年最大占比": max_share,
                "d_2026": d2026,
                "锁定样本同号": bool(same_sign),
                "判据": ("BASE" if is_base else
                         ("PASS" if (share_pos >= 2 / 3 and max_share <= 0.35 and same_sign)
                          else "FAIL")),
            })
        df = df.merge(pd.DataFrame(jud), on="label", how="left")

    df.to_csv(os.path.join(OUT, "window_sweep.csv"), index=False)

    # ---- 打印：全样本 + 逐年 ----
    year_cols = sorted([c for c in df.columns
                        if c.startswith("sh_") and not c.startswith("sh_2125")])
    print("\n=== 全样本 + 逐年 Sharpe（逐年口径 = 日频求和 ×√365）===")
    show = df[["label", "sharpe", "cagr", "mdd", "d_full"] + year_cols
              + ["sh_2125_mean", "sh_2125_med",
                 "d_2021_2025_正数占比", "单年最大占比", "d_2026", "锁定样本同号", "判据"]]
    with pd.option_context("display.width", 260, "display.max_columns", 40,
                           "display.float_format", lambda v: f"{v: .4f}"):
        print(show.to_string(index=False))

    # ---- 打印：两个口径的 argmax 对比（重点） ----
    print("\n=== 两个口径的 argmax：全样本 vs 2021–2025 ===")
    print(f"{'维度':14s} {'argmax(全样本)':>16s} {'argmax(21-25均值)':>18s} "
          f"{'argmax(21-25中位)':>18s}   结论")
    for pref, name in DIMS:
        sub = df[df["label"].str.startswith(pref)].copy()
        sub["v"] = sub["label"].str.replace(pref, "", regex=False).astype(float)
        a_f = sub.loc[sub["sharpe"].idxmax()]
        a_m = sub.loc[sub["sh_2125_mean"].idxmax()]
        a_d = sub.loc[sub["sh_2125_med"].idxmax()]
        agree = abs(a_f["v"] - a_m["v"]) < 1e-9
        print(f"{name:14s} {a_f['v']:10.2f} ({a_f['sharpe']:.4f}) "
              f"{a_m['v']:12.2f} ({a_m['sh_2125_mean']:.4f}) "
              f"{a_d['v']:12.2f} ({a_d['sh_2125_med']:.4f})   "
              f"{'两个口径一致' if agree else '**全样本 argmax 由 2026 决定**'}")

    best_sub = df[df["d_full"] < -1e-12]
    print(f"\n替代配置数 {len(best_sub)}，其中 d_full > 0 的: "
          f"{int((df['d_full'] > 1e-12).sum())}（0 表示基准未被任何替代配置超越）")
    print(f"产物: {os.path.join(OUT, 'window_sweep.csv')}")


if __name__ == "__main__":
    main()
