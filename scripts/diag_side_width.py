"""诊断：crypto 池下每个调仓点「每侧被选中的名字数」分布（直接读 baseline.pkl）。

背景：portfolio/construct.build_units 的 cap 判据是 cap*n <= 1 -> 等权。
v3 用 cap=0.20, top_k=10 -> 每侧理想 n=10，cap*n=2.0 可行。
但窄池早期每侧可能只有 1~5 个名字，cap*n <= 1 再次成立 -> sizing 又静默退化。

只读，不写数据目录。
"""
import os
import pickle
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)          # unpickle needs the package importable
PICK = os.path.join(ROOT, "artifacts", "v3", "baseline.pkl")


def main(path=PICK):
    with open(path, "rb") as f:
        blob = pickle.load(f)
    res = blob["result"]
    cfg = blob["cfg"]
    cap = cfg.portfolio.max_weight_per_instrument
    top_k = cfg.portfolio.top_k

    print(f"bar={blob['bar']}  {blob['start']} -> {blob['end']}")
    print(f"top_k={top_k}  cap={cap}  exec={cfg.execution.exec_price}")
    print(f"factor_subset={res.meta.get('factor_subset')}")
    print(f"rebalances={len(res.reb_ts)}  names={res.weight_matrix.shape[1]}")

    wm = res.weight_matrix           # (D, N)
    am = res.adv_matrix              # (D, N)  eligible (finite & >0) where in pool
    mm = res.mask_matrix

    # 每侧重名数 = 权重矩阵中非零个数按符号分开
    n_long = (wm > 0).sum(axis=1)
    n_short = (wm < 0).sum(axis=1)
    n_side = np.maximum(n_long, n_short)
    n_elig = (np.isfinite(am) & (am > 0) & mm).sum(axis=1)

    print("\n每侧名字数分布：")
    vals, cnts = np.unique(n_side, return_counts=True)
    for v, c in zip(vals, cnts):
        deg = "  <-- cap*n<=1 退化" if cap * v <= 1.0 + 1e-12 else ""
        print(f"  n_side={v:2d}: {c:5d} ({c/len(n_side):6.2%}){deg}")

    deg = cap * n_side <= 1.0 + 1e-12
    print(f"\n退化的调仓点占比: {deg.mean():.2%}  ({deg.sum()}/{len(deg)})")
    print(f"每侧名字数中位数: {int(np.median(n_side))}  最小 {n_side.min()}  最大 {n_side.max()}")

    yrs = np.array([t.year for t in res.reb_ts])
    print("\n按年：")
    for y in sorted(set(yrs)):
        s = yrs == y
        print(f"  {y}: n_side 中位 {int(np.median(n_side[s])):2d}"
              f"  min {n_side[s].min():2d}  max {n_side[s].max():2d}"
              f"   退化占比 {deg[s].mean():6.2%}   合格名字数中位 {int(np.median(n_elig[s])):3d}")

    # cap 需要多大才在所有调仓点都可行？
    need = 1.0 / max(1, int(n_side.min())) + 1e-9
    print(f"\n要让 sizing 在所有调仓点都 bind，需 cap > 1/min(n_side) = "
          f"1/{int(n_side.min())} = {need:.3f}")
    for k in (5, 6, 8, 10):
        frac = (cap * n_side > 1.0 + 1e-12).mean()
        print(f"  cap={cap:.2f} 时，n_side>= {k} 的调仓点里 bind 的比例: "
              f"{(n_side >= k).mean():.2%} 个调仓点宽度达标")


if __name__ == "__main__":
    main(*sys.argv[1:])
