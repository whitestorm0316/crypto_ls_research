"""当前标的池 + 流动性强弱快照（只读，给控制台看）。

与引擎**同源**
--------------
门槛用的量就是 `factors/engine.py` 里那个 `adv`：

    adv = amount.rolling(liq_window_days * bars_per_day).mean()

它是**每根 bar 的平均成交额**，不是日成交额。所以 `min_avg_amount_usd = 3e6`
在 1h 上等价于「每小时 $3M ≈ 每天 $72M」。页面必须同时印出两个数 —— 只写
「30 日成交额门槛 3M」会被读成「日成交额 3M」，整整差 24 倍（实测：按每 bar
算是 26 只过线，按日算会变成 196 只，池子直接宽 7 倍）。

这里不加载 panel、不碰网络、不写任何引擎会读的状态。计算一次约 2–4 秒
（268 个 parquet，只读 `amount` / `close` 两列），所以结果按 5 分钟缓存到
`<CACHE>/meta/pool_snapshot.json`；`refresh=True` 跳过缓存。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config.settings import BARS_PER_DAY, CACHE_DIR, SECONDS_PER_BAR, UniverseConfig

BAR = "1h"
TTL_SECONDS = 300.0          # 5 分钟：流动性按小时变，不必每次请求都重算
CACHE_NAME = "pool_snapshot.json"


# ---------------------------------------------------------------------------
def snapshot_path(cache_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, "meta", CACHE_NAME)


def _load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, key: str, body: dict, ts: float) -> None:
    """写缓存。失败不影响本次返回值（页面照常显示刚算出来的那份）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        d = _load_cache(path)
        entries = d.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        # 只保留最近 4 个键：池子参数一共也没几种组合，留着旧的是为了让
        # 在 crypto/all 之间来回切时不用每次重算 2 秒。
        entries[key] = {"ts": ts, "body": body}
        if len(entries) > 4:
            for k in sorted(entries, key=lambda x: entries[x].get("ts") or 0)[:-4]:
                entries.pop(k, None)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
def _read_one(path: str) -> Optional[pd.DataFrame]:
    """只读两列。`amount` 是**计价币成交额**（USDT），`close` 用来算最小下单额。

    返回 `None` 只表示「文件不在 / 读不了」；**空文件返回空表** —— 这两件事在
    页面上是不同的结论（「盘上没有这只」vs「有这只但没有行情」）。
    """
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path, columns=["amount", "close"])
    except Exception:                                          # noqa: BLE001
        return None


def _pool_rows(bar: str, insts: List[str], cache_dir: str,
               ucfg: UniverseConfig, now: pd.Timestamp,
               specs: Dict[str, object], cats: Dict[str, str]):
    """返回 (rows, last_bar)。`last_bar` 是最后一根**已收线**的 bar。"""
    bpd = BARS_PER_DAY[bar]
    step = pd.Timedelta(seconds=SECONDS_PER_BAR[bar])
    win = max(2, int(round(ucfg.liq_window_days * bpd)))
    min_periods = max(2, win // 4)          # 与 factors/engine.py 一致
    cdir = os.path.join(cache_dir, "candles", bar)

    frames: Dict[str, pd.DataFrame] = {}
    empty: List[str] = []
    for inst in insts:
        df = _read_one(os.path.join(cdir, f"{inst}.parquet"))
        if df is None:
            continue
        # 有文件但一行都没有：**不能直接丢掉**。丢掉的表现是「盘上明明有这只、
        # 页面上却查不到」，用户会以为筛选条件写错了。留一行 `no_data`。
        if not len(df):
            empty.append(inst)
        else:
            frames[inst] = df
    if not frames:
        return [], None

    # 正在形成的那根 bar 不算：它的 amount 只累积了一部分，会让「24h 成交额」
    # 与「放量/缩量」在整点附近跳一下。
    last = max(df.index[-1] for df in frames.values())
    if last + step > now:
        last = last - step
    grid = pd.date_range(end=last, periods=win, freq=step)

    out: List[dict] = []
    for inst, df in frames.items():
        amt = df["amount"].astype("float64").reindex(grid)
        adv = amt.rolling(win, min_periods=min_periods).mean().iloc[-1]
        turn_24h = float(amt.tail(bpd).sum(min_count=1))
        px = float(df["close"].iloc[-1]) if len(df) else float("nan")
        n_bars = int(len(df))
        age_days = n_bars / bpd
        sp = specs.get(inst)
        out.append({
            "inst": inst,
            "cat": cats.get(inst) or "unknown",            "adv": None if not np.isfinite(adv) else float(adv),
            "adv_day": None if not np.isfinite(adv) else float(adv) * bpd,
            "turn_24h": None if not np.isfinite(turn_24h) else turn_24h,
            "px": None if not np.isfinite(px) else px,
            "age_days": age_days,
            "n_bars": n_bars,
            "ct_val": getattr(sp, "ct_val", None),
            "min_sz": getattr(sp, "min_sz", None),
            "lot_sz": getattr(sp, "lot_sz", None),
            "has_spec": sp is not None,
        })
    for inst in empty:
        out.append({
            "inst": inst, "cat": cats.get(inst) or "unknown",
            "adv": None, "adv_day": None, "turn_24h": None, "px": None,
            "age_days": 0.0, "n_bars": 0,
            "ct_val": getattr(specs.get(inst), "ct_val", None),
            "min_sz": getattr(specs.get(inst), "min_sz", None),
            "lot_sz": getattr(specs.get(inst), "lot_sz", None),
            "has_spec": specs.get(inst) is not None,
        })
    return out, last


def _decide(rows: List[dict], ucfg: UniverseConfig) -> None:
    """就地判定 in_pool / reason / rank。规则与 `universe/pit.py` 逐条对齐。"""
    min_hist = max(1.0, float(ucfg.min_history_days))
    for r in rows:
        if r["adv"] is None or r["px"] is None or not (r["px"] > ucfg.min_price):
            r["reason"] = "no_data"
        elif r["age_days"] < min_hist:
            r["reason"] = "too_young"
        elif r["adv"] < ucfg.min_avg_amount_usd:
            r["reason"] = "below_adv"
        else:
            r["reason"] = "ok"
        r["in_pool"] = False
        r["rank"] = None

    eligible = [r for r in rows if r["reason"] == "ok"]
    eligible.sort(key=lambda r: -r["adv"])
    topn = int(ucfg.pit_topn or 0)
    for i, r in enumerate(eligible, 1):
        r["rank"] = i
        if topn and i > topn:
            r["reason"] = "rank_cut"
        else:
            r["in_pool"] = True


def _summary(rows: List[dict], bpd: int) -> dict:
    pool = [r for r in rows if r["in_pool"]]
    advs = np.array([r["adv_day"] for r in pool if r["adv_day"] is not None], dtype="float64")
    chg = []
    for r in pool:
        if r["adv_day"] and r["turn_24h"] is not None and r["adv_day"] > 0:
            chg.append(r["turn_24h"] / r["adv_day"] - 1.0)
    cats: Dict[str, int] = {}
    for r in pool:
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
    top10 = float(advs[np.argsort(-advs)][:10].sum()) if advs.size else 0.0
    total = float(advs.sum()) if advs.size else 0.0
    return {
        "n_cached": len(rows),
        "n_eligible": sum(1 for r in rows if r["rank"] is not None),
        "n_pool": len(pool),
        "adv_day_total": total,
        "adv_day_median": float(np.median(advs)) if advs.size else None,
        "adv_day_p25": float(np.percentile(advs, 25)) if advs.size else None,
        "adv_day_p75": float(np.percentile(advs, 75)) if advs.size else None,
        "top10_share": (top10 / total) if total > 0 else None,
        "chg_median": float(np.median(chg)) if chg else None,
        "n_expanding": sum(1 for c in chg if c > 0),
        "cats": cats,
        "bpd": bpd,
    }


# ---------------------------------------------------------------------------
def compute(bar: str = BAR, asset_class: str = "crypto",
            ucfg: Optional[UniverseConfig] = None,
            cache_dir: Optional[str] = None,
            now: Optional[pd.Timestamp] = None,
            insts: Optional[List[str]] = None) -> dict:
    """真正算一遍。失败会抛 —— 由 `snapshot()` 兜。"""
    from ..data.asset_class import load_categories
    from ..data.store import list_cached_insts
    from ..execution.specs import load_specs
    from ..data.asset_class import filter_insts

    ucfg = ucfg or UniverseConfig()
    cache_dir = cache_dir or CACHE_DIR
    if now is None:
        now = pd.Timestamp.now(tz="UTC")
    elif now.tzinfo is None:
        now = now.tz_localize("UTC")

    notes: List[str] = []
    universe = insts if insts is not None else list_cached_insts(bar, cache=cache_dir)
    n_on_disk = len(universe)

    cats: Dict[str, str] = {}
    try:
        cats = load_categories()
    except FileNotFoundError as e:
        notes.append(f"分类快照不可读（{e}），类别列会显示 unknown")

    if asset_class not in (None, "", "all"):
        scoped, unknown = filter_insts(universe, cats, asset_class)
        if unknown:
            notes.append(f"{len(unknown)} 只没有分类，已排除：{unknown[:6]}")
        universe = scoped

    try:
        specs = load_specs()
    except Exception as e:                                     # noqa: BLE001
        notes.append(f"规格快照不可读（{type(e).__name__}: {e}），最小下单额不可算")
        specs = {}

    rows, last = _pool_rows(bar, universe, cache_dir, ucfg, now, specs, cats)
    # 类别在盘上是 '1'..'5'，页面要的是 crypto/equity/...；映射表只存在
    # `asset_class.CATEGORY_LABELS` 一处，这里不另写一份。
    from ..data.asset_class import CATEGORY_LABELS
    for r in rows:
        r["cat"] = CATEGORY_LABELS.get(str(r["cat"]), str(r["cat"]))
    _decide(rows, ucfg)
    rows.sort(key=lambda r: (-(r["adv"] if r["adv"] is not None else -1.0)))

    bpd = BARS_PER_DAY[bar]
    for r in rows:
        r["adv_day"] = None if r["adv"] is None else r["adv"] * bpd
        r["chg"] = (None if (r["adv_day"] in (None, 0) or r["turn_24h"] is None)
                    else r["turn_24h"] / r["adv_day"] - 1.0)
        # 与 `risk.adv_cap_delta` 同口径：budget = adv × max_adv_participation，
        # 用的是**每 bar 的 adv**，不是折日成交额。写成 adv_day×5% 会大 24 倍。
        r["adv_cap"] = (None if r["adv"] is None
                        else r["adv"] * 0.05)
        r["min_notional"] = (None if (r["ct_val"] is None or r["px"] is None)
                             else float(r["ct_val"]) * r["px"] * float(r["min_sz"] or 0))

    return {
        "ok": True,
        "bar": bar,
        "asset_class": asset_class or "all",
        "asof": (last.isoformat() if last is not None else None),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": {
            "min_avg_amount_usd": ucfg.min_avg_amount_usd,
            "pit_topn": ucfg.pit_topn,
            "min_history_days": ucfg.min_history_days,
            "liq_window_days": ucfg.liq_window_days,
            "bars_per_day": bpd,
            "adv_cap_frac": 0.05,
        },
        "summary": _summary(rows, bpd),
        "n_on_disk": n_on_disk,
        "rows": rows,
        "notes": notes,
    }


def snapshot(bar: str = BAR, asset_class: str = "crypto",
             ucfg: Optional[UniverseConfig] = None,
             cache_dir: Optional[str] = None,
             refresh: bool = False, ttl: float = TTL_SECONDS,
             now: Optional[pd.Timestamp] = None,
             insts: Optional[List[str]] = None) -> dict:
    """带 TTL 缓存的快照。**永不抛**（页面要能显示错误而不是白屏）。"""
    cache_dir = cache_dir or CACHE_DIR
    key = f"{bar}|{asset_class or 'all'}"
    path = snapshot_path(cache_dir)
    ts = time.time()

    if not refresh:
        ent = (_load_cache(path).get("entries") or {}).get(key)
        if isinstance(ent, dict) and isinstance(ent.get("body"), dict) \
                and ts - float(ent.get("ts") or 0) <= ttl:
            body = dict(ent["body"])
            body["from_cache"] = True
            body["age_sec"] = round(ts - float(ent["ts"]), 1)
            return body

    try:
        body = compute(bar=bar, asset_class=asset_class, ucfg=ucfg,
                       cache_dir=cache_dir, now=now, insts=insts)
        body["from_cache"] = False
        body["age_sec"] = 0.0
        _save_cache(path, key, {k: v for k, v in body.items()
                                if k not in ("from_cache", "age_sec")}, ts)
        return body
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "bar": bar, "asset_class": asset_class,
                "from_cache": False, "rows": [], "summary": {}, "notes": []}


def main() -> None:                                            # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="当前标的池与流动性快照")
    ap.add_argument("--bar", default=BAR)
    ap.add_argument("--asset-class", default="crypto")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--rows", type=int, default=30)
    a = ap.parse_args()
    r = snapshot(bar=a.bar, asset_class=a.asset_class, refresh=a.refresh)
    if not r.get("ok"):
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    s = r["summary"]
    print(f"asof={r['asof']} 池={s['n_pool']}/{s['n_cached']} 过门槛={s['n_eligible']} "
          f"中位日额=${s['adv_day_median']:,.0f} 放量中位={s['chg_median']:+.1%}")
    for x in r["rows"][: a.rows]:
        print(f"  {'✓' if x['in_pool'] else ' '} {x['inst']:22s} {x['reason']:10s} "
              f"adv/bar=${(x['adv'] or 0):,.0f} 日=${(x['adv_day'] or 0):,.0f} "
              f"24h=${(x['turn_24h'] or 0):,.0f} chg={((x['chg'] or 0) * 100):+.1f}%")


if __name__ == "__main__":                                     # pragma: no cover
    main()
