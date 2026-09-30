"""HTTP surface for the trading desk.

Mounted by `webapp/server.py`.  Two things shape the design:

* **A plan needs ~40-60 s** on a cold signal cache, because the target book is
  produced by running the real backtest.  So every mutating or expensive
  operation is a *job*: the request returns a job id immediately and the page
  polls `/api/live/job/<id>`, streaming the progress lines the engine emits.
  Jobs run on a single worker thread, so a click-happy UI cannot spawn four
  concurrent backtests on a 6-core box.
* **Secrets never leave the server.**  `POST /api/live/creds` is write-only;
  the only thing any response ever contains is a masked hint.

Read-only views (`orders`, `fills`, `runs`, `equity`) are synchronous -- they
just read the JSON/JSONL files the engine wrote.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

from crypto_ls_research.config.settings import MARGIN_MODE
from crypto_ls_research.execution.credentials import (
    creds_status, delete_creds, kill_switch_on, kill_switch_path, save_creds,
    set_kill_switch,
)
from crypto_ls_research.execution.engine import DEFAULT_SIGNAL, LiveEngine
from crypto_ls_research.execution.limits import LiveLimits
from crypto_ls_research.execution.store import Store, archives, reset_mode
from crypto_ls_research.execution import auto_ctl

MODES = ("paper", "demo", "live")
MODE_LABEL = {"paper": "本地纸面", "demo": "OKX 模拟盘", "live": "OKX 实盘"}

_LOCK = threading.RLock()
_ENGINES: Dict[str, LiveEngine] = {}
_JOBS: Dict[str, dict] = {}
_JOBQ: "queue.Queue[str]" = queue.Queue()
_LAST: Dict[str, dict] = {}          # mode -> last plan/preview payload
_WORKER: Optional[threading.Thread] = None
_SEQ = {"n": 0}


# ---------------------------------------------------------------------------
def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker, daemon=True,
                                       name="live-job-worker")
            _WORKER.start()


def _worker() -> None:
    while True:
        jid = _JOBQ.get()
        job = _JOBS.get(jid)
        if job is None:
            _JOBQ.task_done()
            continue
        job["status"] = "running"
        job["started"] = time.time()

        def log(msg: str) -> None:
            job["log"].append({"t": time.time(), "msg": str(msg)})
            job["stage"] = str(msg)

        try:
            job["result"] = job["_runner"](log)
            net = job.pop("_net_of", None)
            if net is not None and isinstance(job["result"], dict):
                job["result"].setdefault("net", net())
            job["status"] = "done"
        except Exception as e:                                    # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            job["trace"] = traceback.format_exc()[-2000:]
            log(f"失败：{job['error']}")
        finally:
            job["finished"] = time.time()
            job["elapsed"] = round(job["finished"] - job["started"], 2)
            job.pop("_runner", None)
            _JOBQ.task_done()


def _submit(kind: str, mode: str, runner: Callable, label: str = "",
            net_of: Optional[Callable] = None) -> dict:
    _ensure_worker()
    with _LOCK:
        _SEQ["n"] += 1
        jid = f"j{int(time.time())}_{_SEQ['n']}"
        job = {"id": jid, "kind": kind, "mode": mode, "label": label or kind,
               "status": "queued", "created": time.time(), "log": [],
               "stage": "排队中…", "_runner": runner, "elapsed": None,
               "_net_of": net_of,
               "error": None, "result": None}
        _JOBS[jid] = job
        if len(_JOBS) > 60:                       # keep the map bounded
            for k in sorted(_JOBS, key=lambda x: _JOBS[x]["created"])[:20]:
                if _JOBS[k]["status"] in ("done", "failed"):
                    _JOBS.pop(k, None)
    _JOBQ.put(jid)
    return _job_view(job)


def _job_view(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
def engine(mode: str, limits: Optional[dict] = None,
           td_mode: Optional[str] = None,
           set_leverage: object = None) -> LiveEngine:
    """Get (or build) the engine for `mode`, applying the desk's settings.

    `set_leverage` is `object` rather than `Optional[float]` on purpose: the
    distinction between "not supplied" (`None`) and "supplied as empty/0"
    ("do not touch leverage") matters, and collapsing them would make an
    explicit "turn it off" indistinguishable from "never mentioned".

    `td_mode` is the opposite: it is accepted only so that an explicit
    *conflict* is rejected loudly.  The margin mode is a pinned constant
    (`settings.MARGIN_MODE`), not a per-request setting -- see `route_post`,
    which turns a conflicting value into a 400.  Silently ignoring the field
    would be worse than honouring it: a console that posts a mode the server
    does not use is the "UI says one thing, the request does another" defect.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    with _LOCK:
        eng = _ENGINES.get(mode)
        if eng is None:
            # `None` -- not `LiveLimits.from_dict(None)` -- so a freshly built
            # engine reads the caps saved on disk.  Passing the defaults here
            # would make `LiveEngine`'s "saved beats factory" fallback dead code,
            # and a restart would quietly revert the risk limits (it did).
            eng = LiveEngine(mode=mode,
                             limits=LiveLimits.from_dict(limits) if limits else None)
            _ENGINES[mode] = eng
        elif limits:
            eng.limits = LiveLimits.from_dict(limits)
        # `td_mode` is deliberately *not* applied here: `LiveEngine` derives it
        # from the constant, and a conflicting value has already been rejected.
        if set_leverage is not None:
            eng.set_leverage = _clean_leverage(set_leverage)
        return eng


def _clean_leverage(v: object) -> Optional[float]:
    """'' / 0 / None / 'off' -> None (leave the exchange as it is)."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in ("", "0", "off", "none"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 1 else None


def _recent_jobs(n: int = 12) -> List[dict]:
    with _LOCK:
        rows = sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)
        return [_job_view(j) for j in rows[:n]]


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------
def route_get(path: str, q: dict) -> Optional[Tuple[dict, int]]:
    one = lambda k, d=None: (q.get(k) or [d])[0] if isinstance(q.get(k), list) \
        else (q.get(k) if q.get(k) is not None else d)   # noqa: E731

    if path == "/api/live/meta":
        return {"modes": [{"id": m, "label": MODE_LABEL[m],
                           "note": _MODE_NOTE[m]} for m in MODES],
                "kill_switch": kill_switch_on(), "kill_path": kill_switch_path(),
                "creds": creds_status(),
                "limits_default": LiveLimits().to_dict(),
                # Per mode, because the absolute caps are account-specific: a
                # $1k paper book and a $54k demo account cannot share a
                # `max_gross_notional`.  `null` = never saved for that mode.
                "limits_saved": {m: Store(m).load_limits() for m in MODES},
                "default_signal": DEFAULT_SIGNAL}, 200

    if path == "/api/live/status":
        mode = one("mode", "paper")
        eng = engine(mode)
        st = eng.status()
        # `set_leverage` is what we will *ask* the exchange for.  It is not the
        # same as the leverage actually in force (which lives per-instrument on
        # OKX and can differ per name), so label it honestly.
        st["leverage"] = eng.set_leverage
        st["leverage_cap"] = eng.limits.max_leverage
        st["creds"] = creds_status()
        st["jobs"] = _recent_jobs(8)
        st["has_preview"] = mode in _LAST
        st["archives"] = archives(mode)
        return st, 200

    if path == "/api/live/jobs":
        return {"jobs": _recent_jobs(int(one("n", 12) or 12))}, 200

    if path.startswith("/api/live/job/"):
        jid = path.rsplit("/", 1)[-1]
        with _LOCK:
            job = _JOBS.get(jid)
        if job is None:
            return {"error": "no such job"}, 404
        return _job_view(job), 200

    if path == "/api/live/preview":
        mode = one("mode", "paper")
        if mode not in _LAST:
            return {"error": "还没有生成过计划", "missing": True}, 404
        return _LAST[mode], 200

    if path == "/api/live/orders":
        mode = one("mode", "paper")
        rows = list(Store(mode).orders().values())
        rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        return {"rows": rows[: int(one("limit", 200) or 200)], "mode": mode}, 200

    if path == "/api/live/fills":
        mode = one("mode", "paper")
        return {"rows": Store(mode).fills(limit=int(one("limit", 200) or 200)),
                "mode": mode}, 200

    if path == "/api/live/runs":
        mode = one("mode", "paper")
        return {"rows": Store(mode).runs(limit=int(one("limit", 40) or 40)),
                "mode": mode}, 200

    if path == "/api/live/equity":
        mode = one("mode", "paper")
        return {"rows": Store(mode).equity(limit=int(one("limit", 4000) or 4000)),
                "mode": mode}, 200

    if path == "/api/live/creds":
        return creds_status(), 200

    if path == "/api/live/auto":
        mode = one("mode", "paper")
        return {"mode": mode, "state": auto_ctl.read_state(mode),
                "ctl": auto_ctl.read_ctl(),
                "running": auto_ctl.is_running(mode),
                "grid": auto_ctl.grid_for(mode)}, 200

    return None


_MODE_NOTE = {
    "paper": "不需要 API Key。成交按 OKX 实时公开价模拟，成本用回测同一套模型。"
             "适合验证整条链路（信号→计划→下单→对账→净值）。",
    "demo": "OKX 模拟盘：真实撮合引擎 + 模拟资金。必须用「模拟盘专用」API Key"
            "（在 OKX → 交易 → 模拟交易 → 个人中心 → 模拟盘 API 创建）；"
            "拿实盘 Key 填进来会返回 50111 Invalid OK-ACCESS-KEY —— 看起来像 key "
            "打错了，其实是环境选错了。",
    "live": "实盘：真金白银。需要输入当日确认短语才会真正下单，并受绝对名义额上限约束。",
}


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------
def route_post(path: str, body: dict) -> Optional[Tuple[dict, int]]:
    body = body or {}

    # The margin mode is a pinned constant, so a client that posts a *different*
    # one is rejected rather than silently ignored.  A 400 (not a 500) because
    # this is a client mistake, and the message has to say what to do about it.
    _td = body.get("td_mode")
    if _td is not None and str(_td) != MARGIN_MODE:
        return ({"error": f"保证金模式已钉死为 {MARGIN_MODE}（settings.MARGIN_MODE），"
                          f"不接受 td_mode={_td!r}。"},
                400)

    if path == "/api/live/plan":
        mode = body.get("mode", "paper")
        fresh = bool(body.get("fresh"))
        only = body.get("only") or None
        limits = body.get("limits")
        eng = engine(mode, limits, body.get("td_mode"), body.get("leverage"))

        def run(log):
            payload = eng.preview(force_signal=fresh, only=only, progress=log)
            _LAST[mode] = payload
            return payload

        return _submit("plan", mode, run,
                       label=("重新计算信号" if fresh else "生成下单计划"),
                       net_of=eng.net_latency), 200

    if path == "/api/live/execute":
        mode = body.get("mode", "paper")
        eng = engine(mode, body.get("limits"), body.get("td_mode"),
                     body.get("leverage"))
        dry = bool(body.get("dry_run", True))
        confirm = body.get("confirm")
        force = bool(body.get("force"))
        only = body.get("only") or None
        fresh = bool(body.get("fresh"))

        def run(log):
            payload = eng.execute(confirm=confirm, force=force, dry_run=dry,
                                  only=only, force_signal=fresh, progress=log)
            _LAST[mode] = {**_LAST.get(mode, {}), "last_execute": payload}
            return payload

        return _submit("execute", mode, run,
                       label=("预演下单（不发单）" if dry else f"实际下单 · {MODE_LABEL[mode]}"),
                       net_of=eng.net_latency), 200

    if path == "/api/live/flatten":
        mode = body.get("mode", "paper")
        eng = engine(mode, body.get("limits"), body.get("td_mode"),
                     body.get("leverage"))

        def run(log):
            payload = eng.flatten(confirm=body.get("confirm"),
                                  dry_run=bool(body.get("dry_run", True)),
                                  progress=log)
            log(f"结果：{payload['stage']}")
            payload["plan"] = payload.get("plan")
            _LAST[mode] = {**_LAST.get(mode, {}), "last_flatten": payload}
            return payload

        return _submit("flatten", mode, run, label="清仓",
                       net_of=eng.net_latency), 200

    if path == "/api/live/reconcile":
        mode = body.get("mode", "paper")
        eng = engine(mode)

        eng._net_notice = None           # reconcile has its own log lines

        def run(log):
            log("向交易所/本地账本对账…")
            r = eng.reconcile()
            log(f"更新订单 {r.get('orders')} 条、新增成交 {r.get('fills')} 笔")
            return r

        return _submit("reconcile", mode, run, label="对账",
                       net_of=eng.net_latency), 200

    if path == "/api/live/refresh-data":
        bar = body.get("bar", DEFAULT_SIGNAL["bar"])

        def run(log):
            return LiveEngine.refresh_market_data(bar=bar, progress=log)

        return _submit("refresh", "paper", run, label=f"刷新 {bar} 行情"), 200

    if path == "/api/live/auto/start":
        return _auto_start(body)
    if path == "/api/live/auto/stop":
        return _auto_stop(body)

    if path == "/api/live/creds":
        mode = body.get("mode")
        if mode not in ("demo", "live"):
            return {"error": "只能为 demo / live 保存密钥"}, 400
        try:
            save_creds(mode, body.get("api_key", ""), body.get("secret_key", ""),
                       body.get("passphrase", ""))
        except ValueError as e:
            return {"error": str(e)}, 400
        with _LOCK:                      # force the engine to reload credentials
            _ENGINES.pop(mode, None)
        return {"ok": True, "creds": creds_status()}, 200

    if path == "/api/live/creds/delete":
        mode = body.get("mode")
        if mode not in ("demo", "live"):
            return {"error": "只能删除 demo / live 的密钥"}, 400
        delete_creds(mode)
        with _LOCK:
            _ENGINES.pop(mode, None)
        return {"ok": True, "creds": creds_status()}, 200

    if path == "/api/live/kill":
        want = bool(body.get("on"))
        now_on = set_kill_switch(want)
        out = {"ok": True, "kill_switch": now_on, "requested": want}
        if now_on != want:
            # Never claim a state change we could not make.  The lock file
            # survives -> trading is still blocked -> that is the safe failure,
            # but the user must be told, not deceived.
            out["note"] = ("未能解除：熔断锁文件仍然存在（删除被拒）。交易仍被拦截。"
                           if not want else "未能熔断：锁文件没有写成功。")
        return out, 200

    if path == "/api/live/limits":
        mode = body.get("mode", "paper")
        if mode not in MODES:
            return {"error": "bad mode"}, 400
        lim = LiveLimits.from_dict(body.get("limits"))
        eng = engine(mode)
        eng.limits = lim
        # The caps have to outlive this process.  They used to be assigned to the
        # in-memory engine and nowhere else, so the button reported "已生效" and
        # a restart brought back `LiveLimits()` defaults -- reported as
        # "风控限额保存了重启不生效".  Write first, then report the path so the
        # user can verify it themselves.
        saved = eng.store.save_limits(lim.to_dict())
        return {"ok": True, "limits": lim.to_dict(),
                "saved_to": eng.store.limits_path(),
                "saved_at": saved["_saved"]}, 200

    if path == "/api/live/reset":
        mode = body.get("mode", "paper")
        if mode not in MODES:
            return {"error": "bad mode"}, 400
        nav = body.get("nav")
        archived = reset_mode(mode)
        with _LOCK:
            _ENGINES.pop(mode, None)
            _LAST.pop(mode, None)
        if mode == "paper" and nav:
            Store("paper").write_state(
                {"mode": "paper", "cash": float(nav), "nav0": float(nav),
                 "positions": {}, "pos_mode": "net_mode", "created": time.time()})
        return {"ok": True, "mode": mode, "nav": nav, "archived": archived}, 200

    return None


# ---------------------------------------------------------------------------
# Unattended auto-trader (see `execution/auto_ctl.py`)
# ---------------------------------------------------------------------------
def _auto_start(body: dict) -> Tuple[dict, int]:
    mode = body.get("mode", "paper")
    if mode not in MODES:
        return {"ok": False, "error": f"bad mode {mode!r}"}, 400
    grid = auto_ctl.grid_for(mode)
    # Default the grid to whatever is already in force, so "restart the daemon"
    # cannot silently move a 1d schedule back to 3d just because the field was
    # omitted from the request.
    rd = body.get("rebalance_days")
    if rd is None:
        rd = grid.get("rebalance_days")
    if rd is None:
        rd = DEFAULT_SIGNAL["rebalance_days"]
    try:
        rd = float(rd)
    except (TypeError, ValueError):
        return {"ok": False, "error": "调仓间隔必须是数字（天）"}, 400
    try:
        interval = float(body.get("interval_min") or 30.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "检查间隔必须是数字（分钟）"}, 400

    res = auto_ctl.start(mode, rebalance_days=rd, interval_min=interval,
                         bar=body.get("bar") or DEFAULT_SIGNAL["bar"],
                         dry_run=bool(body.get("dry_run", False)),
                         allow_live=bool(body.get("allow_live", False)))
    if not res.get("ok"):
        return res, 200
    waited = auto_ctl.wait_for_heartbeat(mode, timeout=25.0)
    # Report the heartbeat, not the request.  `confirmed` is the field the page
    # keys off, so the two cannot be confused.
    res["confirmed"] = bool(waited.get("ok"))
    res["state"] = waited.get("state") or auto_ctl.read_state(mode)
    res["grid"] = auto_ctl.grid_for(mode)
    if not waited.get("ok"):
        res["warning"] = waited.get("error")
    return res, 200


def _auto_stop(body: dict) -> Tuple[dict, int]:
    mode = body.get("mode", "paper")
    if mode not in MODES:
        return {"ok": False, "error": f"bad mode {mode!r}"}, 400
    return auto_ctl.stop(mode), 200
