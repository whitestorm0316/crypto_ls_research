"""宇宙体检（只读）——盘上缓存宇宙 vs OKX 实时清单的差距报告。

为什么要有这个模块
------------------
自动交易的宇宙 = `list_cached_insts()` = **盘上的 parquet 文件**，不是 OKX 实时
列表（Q66）。后果是双向的漂移谁都不会自动修：

* **新合约不进**：刷新走 `--reuse-pool` 冻结候选池，新合约永远没有 parquet；
* **退市不剔**：parquet 还在就仍占名额；
* **缺分类**：`inst_category.json` 快照缺了某只盘上合约 ⇒ `resolve_insts`
  fail-loud 抛 `SystemExit`（曾把守护进程整个打死，见 auto_trader/auto_schedule
  的降级修复）；
* **缺规格**：`swap_specs.json` 快照缺了某只 ⇒ planner 按 `no_spec` 跳过
  （有持仓则市价平掉）。

这些都不是 bug，是刻意的保守设计——代价是**必须有人看见漂移**。本模块就是
「看见」：调仓前后台自动跑一遍、控制台显示一行结论。它**只读**：不刷新行情、
不加载 panel、不写任何会被引擎消费的状态，内存占用是 KB 级。

结果缓存 6 小时（`<CACHE>/meta/universe_drift.json`），网络失败**绝不抛**：
降级为 `ok: False` + 15 分钟退避；若盘上有上次成功的快照则带 `stale: True`
返回旧结果。池规则（top-120 ADV ∪ 2022-01-01 前上市）**直接复用**
`download.build_candidate_pool`——同一份定义，不许抄第二遍。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from ..config.settings import CACHE_DIR

BAR = "1h"                    # 宇宙按 bar 分别缓存；报告对 1h 面板（与实盘同源）
TTL_SECONDS = 6 * 3600.0      # 体检结果的新鲜窗口
RETRY_SECONDS = 15 * 60.0     # 网络失败后的退避：状态轮询很频，别每轮都去打 API
LIST_TOP = 15                 # 明细最多列几只（全量计数不变）


def drift_path(cache_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, "meta", "universe_drift.json")


# ---------------------------------------------------------------------------
# 盘上文件的形状：{"_generated_at": ts, "_last_attempt": ts, "body": {...}}。
# `body` 是上次**成功**体检的完整载荷——失败时必须保留它，退避期内才有的
# 「stale」可还。失败本身只更新 `_last_attempt` + `error`。
# ---------------------------------------------------------------------------
def _read_snapshot(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_snapshot(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass                      # 缓存写失败不影响本次返回值


def _measure(cli, cached: List[str], categories: Optional[Dict[str, str]],
             specs: Optional[Dict[str, str]], cat_note: Optional[str],
             spec_note: Optional[str]) -> dict:
    """真正的比对。`cli` 只需要 `.instruments("SWAP")` 和 `.tickers("SWAP")`。"""
    import pandas as pd

    from ..data.download import build_candidate_pool

    pool = build_candidate_pool(cli)                       # top-120 ∪ 早期上市
    inst = pd.DataFrame(cli.instruments("SWAP"))
    inst = inst[(inst.get("settleCcy") == "USDT")
                & inst["state"].isin(["live", "suspend"])]
    live_ids = set(inst["instId"])

    cached_set = set(cached)
    # top-120 的 ADV 线 = 带 "top" 标记的行里最便宜的一只（"early" 单独出现的行不算）。
    top_rows = pool[pool["reason"].str.contains("top")]
    cut = float(top_rows["adv_now"].min()) if len(top_rows) else float("nan")

    new = pool[~pool["instId"].isin(cached_set)].sort_values(
        "adv_now", ascending=False)
    new_rows = [
        {"inst": r.instId,
         "adv_usd": (None if pd.isna(r.adv_now) else round(float(r.adv_now), 0)),
         "listed": str(r.list_dt.date()) if pd.notna(r.list_dt) else None,
         "above_cut": bool(pd.notna(r.adv_now) and float(r.adv_now) >= cut)}
        for r in new.itertuples(index=False)]

    cats = categories or {}
    unclassified = sorted(cached_set - set(cats))
    spcs = specs or {}
    missing_specs = sorted(cached_set - set(spcs))
    dead = sorted(cached_set - live_ids)

    return {
        "n_cached": len(cached_set),
        "n_live": len(live_ids),
        "n_new": len(new_rows),
        "n_new_above_cut": sum(1 for r in new_rows if r["above_cut"]),
        "n_dead": len(dead),
        "n_unclassified": len(unclassified),
        "n_missing_specs": len(missing_specs),
        "pool_cut_adv_usd": (None if cut != cut else round(cut, 0)),
        "new": new_rows[:LIST_TOP],
        "dead": dead[:LIST_TOP],
        "unclassified": unclassified[:LIST_TOP],
        "missing_specs": missing_specs[:LIST_TOP],
        "notes": [n for n in (cat_note, spec_note) if n],
    }


def _summary(m: dict) -> str:
    parts = [f"盘上 {m['n_cached']}", f"实时 {m['n_live']}",
             f"新达标 {m['n_new']}（超 ADV 线 {m['n_new_above_cut']}）",
             f"退市残留 {m['n_dead']}", f"缺分类 {m['n_unclassified']}",
             f"缺规格 {m['n_missing_specs']}"]
    return " · ".join(parts)


def _stale_fields(body: Optional[dict]) -> dict:
    """从上次成功体里挑出前端要显示的字段（stale 兜底显示用）。"""
    if not isinstance(body, dict) or not body.get("ok"):
        return {}
    keys = ("summary", "n_cached", "n_live", "n_new", "n_new_above_cut",
            "n_dead", "n_unclassified", "n_missing_specs", "new", "dead",
            "unclassified", "missing_specs", "generated_at")
    return {k: body[k] for k in keys if k in body}


def drift_report(cli=None, *, cached: Optional[List[str]] = None,
                 categories: Optional[Dict[str, str]] = None,
                 specs: Optional[Dict[str, str]] = None,
                 bar: str = BAR, ttl: float = TTL_SECONDS,
                 force: bool = False, now: Optional[float] = None,
                 cache_dir: Optional[str] = None) -> dict:
    """宇宙体检报告。**永不抛异常**（这是它被挂在 `/api/live/status` 上的前提）。

    参数都留了注入缝，测试不必碰真网络/真盘：
    `cached`（默认扫盘）、`categories` / `specs`（默认读快照文件，读不到
    只降级不失败）、`cli`（默认 OKXClient，遵守 `OKX_PROXY` 环境变量）。
    """
    path = drift_path(cache_dir)
    t = now if now is not None else time.time()

    snap = _read_snapshot(path)
    body = snap.get("body") if isinstance(snap, dict) else None
    gen = snap.get("_generated_at") if isinstance(snap, dict) else None

    # 1) TTL 内的盘上快照直接用（不碰网络）。
    if (not force and isinstance(body, dict) and body.get("ok")
            and isinstance(gen, (int, float)) and t - gen <= ttl):
        out = dict(body)
        out["from_cache"] = True
        out["stale"] = False
        out["age_hours"] = round((t - gen) / 3600.0, 2)
        return out

    # 2) 上次失败还在退避期内：不重打网络。
    last_attempt = snap.get("_last_attempt") if isinstance(snap, dict) else None
    if (not force and isinstance(snap, dict) and not snap.get("ok", True)
            and isinstance(last_attempt, (int, float))
            and t - last_attempt < RETRY_SECONDS):
        return {"ok": False, "error": snap.get("error") or "上次体检失败，退避中",
                "stale": bool(body and body.get("ok")), "from_cache": False,
                "age_hours": (round((t - gen) / 3600.0, 2)
                              if isinstance(gen, (int, float)) else None),
                "retry_in_min": round((RETRY_SECONDS - (t - last_attempt)) / 60.0, 1),
                **_stale_fields(body)}

    # 3) 真正去体检。每一步都可能失败，失败只降级、不外抛。
    try:
        if cached is None:
            from ..data.store import list_cached_insts
            cached = list_cached_insts(bar, cache=cache_dir or CACHE_DIR)
        cat_note = None
        if categories is None:
            try:
                from ..data.asset_class import load_categories
                categories = load_categories()
            except FileNotFoundError as e:
                cat_note = f"分类快照不可读：{e}"
                categories = {}
        spec_note = None
        if specs is None:
            try:
                from .specs import load_specs
                specs = {i: s.inst_id for i, s in load_specs().items()}
            except Exception as e:                             # noqa: BLE001
                spec_note = f"规格快照不可读：{type(e).__name__}: {e}"
                specs = {}
        if cli is None:
            from ..data.okx_client import OKXClient
            cli = OKXClient()
        m = _measure(cli, list(cached), categories, specs, cat_note, spec_note)
        out = {"ok": True, "error": None, "stale": False, "from_cache": False,
               "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
               "age_hours": 0.0, "summary": _summary(m), **m}
        _write_snapshot(path, {"_generated_at": t, "_last_attempt": t,
                               "ok": True, "error": None, "body": out})
        return out
    except Exception as e:                                     # noqa: BLE001
        # 网络失败：绝不抛。历史成功快照保留在 body 里，下次退避期内仍可还。
        out = {"ok": False, "error": f"{type(e).__name__}: {e}",
               "stale": bool(body and body.get("ok")),
               "from_cache": False,
               "age_hours": (round((t - gen) / 3600.0, 2)
                             if isinstance(gen, (int, float)) else None),
               **_stale_fields(body)}
        _write_snapshot(path, {"_generated_at": gen if isinstance(gen, (int, float))
                               else None,
                               "_last_attempt": t, "ok": False,
                               "error": out["error"],
                               "body": body if isinstance(body, dict) else None})
        return out


# ---------------------------------------------------------------------------
def main() -> None:                                            # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(
        description="宇宙体检：盘上缓存 vs OKX 实时清单（只读）")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略 TTL 强制重测一次")
    a = ap.parse_args()
    r = drift_report(force=a.refresh)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":                                     # pragma: no cover
    main()
