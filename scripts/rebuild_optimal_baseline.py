#!/usr/bin/env python3
"""重建控制台页头要读的那份基线产物（`artifacts/<TAG>/tables/...`）。

什么时候需要它
--------------
`artifacts/` 被 `.gitignore` 排除，所以**换机器 / 新 clone 之后页头一定显示**
「基线数据缺失（未找到已验收配置的产物）」—— 那不是 bug，是产物不在盘上。

页头的每一个数字都读自
`artifacts/<OPTIMAL_TAG>/tables/01_headline_metrics.json`，而这份文件是
`run.research` 的 `base` 阶段产出的 ⇒ 只跑一个阶段、**约 10 秒**即可。

用法
----
    python scripts/rebuild_optimal_baseline.py            # 只补 base（页头够用）
    python scripts/rebuild_optimal_baseline.py --dry-run  # 只打印将要执行的命令
    python scripts/rebuild_optimal_baseline.py --stages base charts   # 再加图

⚠️ **参数的唯一来源是 `webapp/spec.py` 的 `OPTIMAL_CLI` / `OPTIMAL_OVERRIDES`**，
本脚本不写任何字面量（改口径时只需改那一处；写字面量是这仓库反复踩过的坑）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

from spec import OPTIMAL_CLI, OPTIMAL_OVERRIDES, OPTIMAL_TAG  # noqa: E402


def build_argv(stages: list[str]) -> list[str]:
    argv = [sys.executable, "-m", "crypto_ls_research.run.research",
            "--tag", OPTIMAL_TAG, "--stages", *stages]
    for k, v in OPTIMAL_CLI.items():
        flag = "--" + k.replace("_", "-")
        # `rebalance_days` 之类的标量直接传；布尔（如有）转成 store_true。
        if isinstance(v, bool):
            if v:
                argv.append(flag)
        else:
            argv += [flag, str(v)]
    for k, v in OPTIMAL_OVERRIDES.items():
        # ⚠️ `--override` 的值走 `json.loads`，所以**列表必须写成 JSON 数组**：
        #    --override 'factors.subset=["momentum","flow",...]'
        #    写成逗号串会被逐字符解析成 36 个"因子"（实测踩过）。
        argv += ["--override", f"{k}={json.dumps(v, ensure_ascii=False)}"]
    return argv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", nargs="*", default=["base"],
                    help="默认只跑 base —— 页头只需要它，约 10 秒")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="覆盖**已存在**的基线产物（默认拒绝，见下）")
    a = ap.parse_args()

    head = os.path.join(ROOT, "artifacts", OPTIMAL_TAG, "tables",
                        "01_headline_metrics.json")
    exists = os.path.isfile(head)
    print(f"目标 tag : {OPTIMAL_TAG}")
    print(f"页头读的 : {head}")
    print(f"当前存在 : {exists}"
          + (f"（{os.path.getsize(head)} 字节）" if exists else ""))

    # 🔴 **默认不覆盖已有的基线**（2026-10-16 我自己踩的）。
    #
    # `v5_1d_all5` 是**已验收基线**：`artifacts/FINDINGS.md`、`webapp/spec.py`
    # 的注释、以及 `test_failure_attribution.py` 里那 5 个回撤窗口都**引用它的数字**。
    # 重跑它会写 `baseline.pkl` + `tables/`（**charts 不会重写**），于是：
    #   * 那些引用立即失效（实测：W2 回撤从 -0.1082 变 -0.1131，测试变红）；
    #   * 而且**换不回来** —— `baseline.pkl` 是 33MB、`artifacts/**` 又被 gitignore，
    #     没备份的人无法复原。
    #
    # 更麻烦的是重跑**必然**改变数字，即使配置一模一样：
    #   * `store.load_panels()` **刻意忽略 `--end`**（网格总延伸到缓存最新 bar），
    #     所以区间会变长；
    #   * 候选池是「今天的实时挂单」⇒ 新上市的合约会进组合。
    # 两者叠加 ⇒ "重建"得到的**不是**验收时那份。
    if exists and not a.force and not a.dry_run:
        prev = json.load(open(head, encoding="utf-8"))
        print(f"\n  现有基线：Sharpe {prev.get('Sharpe')} / 区间 "
              f"{prev.get('start')} → {prev.get('end')}")
        sys.exit(
            "\n[拒绝覆盖] 基线产物已存在。\n\n"
            "  `v5_1d_all5` 是**已验收基线**，重跑它会让下面这些引用失效：\n"
            "    * `artifacts/FINDINGS.md` 里的数字\n"
            "    * `webapp/spec.py` 注释里的记录值\n"
            "    * `test_failure_attribution.py` 的 5 个回撤窗口\n"
            "  而且**换不回来** —— `baseline.pkl` 有 33MB 且 artifacts/ 被 gitignore。\n\n"
            "  真要重跑（会改变数字，且与历史记录不再可比）：\n"
            "    # 先整目录备份，别只备份 tables/（pkl 也要）\n"
            "    cp -r artifacts/v5_1d_all5 /tmp/v5_backup_$(date +%s)\n"
            f"    {os.path.basename(__file__)} --force\n\n"
            "  只想先看看会跑什么：\n"
            f"    {os.path.basename(__file__)} --dry-run\n")

    argv = build_argv(a.stages)
    print("\n将要执行：\n  " + " \\\n    ".join(
        _wrap(argv)))

    if a.dry_run:
        print("\n（--dry-run：未执行）")
        return

    if exists:
        print("\n⚠️ --force：将覆盖现有基线（先确认已备份整个 tag 目录）")

    r = subprocess.run(argv, cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"\n★ 回测失败（退出码 {r.returncode}）")

    if not os.path.isfile(head):
        sys.exit(f"\n★ 跑完了但 {head} 仍不存在 —— 检查上面的输出")
    d = json.load(open(head, encoding="utf-8"))
    print("\n重建完成。页头将显示：")
    for k in ("start", "end", "CAGR", "Annualized Volatility", "Sharpe",
              "Max Drawdown", "Annual Turnover", "Cost Drag (annual)"):
        if k in d:
            print(f"  {k:26s} {d[k]}")
    print(f"\n⚠️ 区间到 {d.get('end')} —— 与历史记录里的数字**不会完全相同**：")
    print("    候选池是从「今天的实时挂单」建的，新上市的合约会进来、")
    print("    区间也会往后延。**跨时点的回测数字不可直接比。**")


def _wrap(argv: list[str]) -> list[str]:
    """把长命令按参数切行，便于阅读。"""
    out, line = [], argv[0]
    for t in argv[1:]:
        if len(line) + len(t) > 78:
            out.append(line)
            line = "    " + t
        else:
            line += " " + t
    out.append(line)
    return out


if __name__ == "__main__":
    main()
