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

import json
import os
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
from crypto_ls_research.execution.limits import LiveLimits, live_confirm_phrase
from crypto_ls_research.execution.store import Store, archives, reset_mode
from crypto_ls_research.execution.universe_drift import drift_report
from crypto_ls_research.execution import auto_ctl
from crypto_ls_research.execution import navhist
from crypto_ls_research.execution.auto_trader import (
    AUTO_ENABLED_KEY, resolve_auto_enabled,
)
# ⚠️ 用**相对 import**：`webapp/server.py` 把 `webapp/` 加进了 `sys.path`，所以本模块
# 可能以顶层名 `live_api` 被加载。写成 `from webapp import auto_schedule` 会强制
# 走「包」路径，与 `import auto_schedule`（顶层路径）**各加载一份**，而
# `auto_schedule` 的 `Registry` 是**有实例状态**的 —— 两份就意味着两个 registry。
# 相对 import 在同一父包下解析到同一个子模块，从根上避免这件事。
try:
    from . import auto_schedule
except ImportError:                                              # pragma: no cover
    # 以顶层名（`import live_api`）加载时没有父包，退回绝对 import。
    import auto_schedule

MODES = ("paper", "demo", "live")
MODE_LABEL = {"paper": "本地纸面", "demo": "OKX 模拟盘", "live": "OKX 实盘"}

#: A NAV mark is appended at most this often by the 「刷新账户」 button.  The
#: engine already marks on every rebalance and reconcile; this only adds the
#: intraday resolution the history chart needs, and without a floor a
#: click-happy UI would append a mark per click.
NAV_MARK_MIN_GAP = 300.0

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


# ---------------------------------------------------------------------------
# 每日调度：由控制台进程自己执行，不需要任何外部守护进程。
#
# 为什么可行：控制台是双重 fork + `setsid()` 起的（父进程 launchd），能长期活着。
# 而原来那条「守护进程」路有个反复出问题的脆弱点 —— 从 Web 点按钮派生的子进程
# 会随调用方的进程树被回收（README §一·补 那一整节）。控制台自己没这个问题。
# ---------------------------------------------------------------------------
_REGISTRY = auto_schedule.Registry()


def auto_runner(mode: str) -> dict:
    """到点时执行的动作：**走和网页按钮完全同一条路径**。

    刻意不在这里直接调 `engine.execute()`：那条路会绕过 `_submit` 的作业队列，
    于是网页和定时器可能同时在下单（两个 execute 各读一遍同一本书、各发全量单）。
    提交进同一个队列，`execute` 自己的跨进程锁 + 队列的串行化一起兜住这件事。

    两步（刷新行情 → 计划并下单）与 `livePrepare()` 按钮一致，这样"自动"和
    "手动点一次"的结果是同一份产物，而不是两套逻辑。
    """
    from crypto_ls_research.execution.engine import DEFAULT_SIGNAL
    bar = DEFAULT_SIGNAL["bar"]

    def refresh(log):
        return LiveEngine.refresh_market_data(bar=bar, progress=log)

    j1 = _submit("refresh", mode, refresh, label=f"自动任务：刷新 {bar} 行情")

    # 刷新是异步作业，必须等它结束再算计划 —— 否则计划会基于还没刷新的缓存，
    # 拿到的是一根旧 bar（这正是"自动下单买了昨天的仓位"那类事故）。
    _wait_job(j1["id"], timeout=300.0)

    def plan_and_execute(log):
        eng = engine(mode)
        # 与网页「下单」按钮同样的入参：dry_run=False（真下）、force=False
        # （不越过到期闸门）、confirm 由 live 模式的服务端短语提供。
        plan = eng.preview(force_signal=False, progress=log)
        log(f"计划 {plan.get('plan', {}).get('n_orders')} 笔，提交执行…")
        return eng.execute(confirm=(live_confirm_phrase() if mode == "live" else None),
                           force=False, dry_run=False, progress=log)

    job = _submit("auto", mode, plan_and_execute, label="自动任务：下单")
    # 等执行结束，然后把结果**解析成通知词汇**推出去。
    #
    # ⚠️ 这里有一个曾经漏掉的洞（2026-10-12 用户报「自动交易进行了但没发飞书」）：
    # `notify_for` 原本**只在 `auto_trader` 的循环里**被调用。执行搬到控制台之后，
    # 这条路经过的是 `live_api`，于是**通知完全没人发** —— 交易正常、静默无声。
    # 这正是"把执行搬走、把通知留下"会造成的典型后果。
    done = _wait_job(job["id"], timeout=600.0)
    if done and done.get("status") == "done":
        try:
            _notify_result(mode, done.get("result") or {})
        except Exception as e:                                    # noqa: BLE001
            # 通知绝不能影响交易 —— 这条纪律与 `Notifier.send()` 一致。
            print(f"[auto] {mode} 通知发送失败（不影响交易）：{type(e).__name__}: {e}",
                  flush=True)
    return job


#: `engine.execute()` 的 `stage` → 通知配置认的 `action`。
#: **两个词汇表不一致**，这是第二个独立的缺陷：`notify_on` 认的是
#: `traded` / `blocked` / `error`（那是 `auto_trader` 的 `action`），而
#: `engine.execute()` 返回的 `action` 恒为 `"rebalance"`、真正区分结果的是
#: `stage`。不映射的话，即使调了 `notify_for` 也会得到「该结果不推送」。
#:
#: `no_orders` 刻意不映射成 `traded`：没有订单不是"成交了"，推一条"已调仓 0 笔"
#: 会让人以为出了什么事。
_STAGE_TO_ACTION = {
    "sent": "traded",
    "blocked": "blocked",
    "confirm_failed": "blocked",
    "no_orders": "noop",
    "dry_run": "dry_run",
}


#: 通知器缓存：(config_mtime, Notifier)。**按 mtime 热读**，与 `auto_trader`
#: 的行为一致 —— 控制台同样会跑很久，有人"配了 webhook 但没重启"时必须自动生效，
#: 否则 `--test`（新进程）通过、而常驻的这条静默不发，看起来像"策略什么都没做"。
_NOTIFIER_CACHE: Dict[str, tuple] = {}


def _notifier_now(mode: str):
    from crypto_ls_research.execution.notify import CONFIG_PATH, Notifier

    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None
    cached = _NOTIFIER_CACHE.get(mode)
    if cached and cached[0] == mtime:
        return cached[1]
    n = Notifier.from_config(mode=mode)
    _NOTIFIER_CACHE[mode] = (mtime, n)
    d = n.describe()
    print(f"[auto] {mode} 通知配置已读取："
          + (f"{d['provider']} → {d['url']}" if d.get("ready") else "未启用/未就绪"),
          flush=True)
    return n


#: `engine.execute()` 的返回是**嵌套**的：数字在 `record` / `summary` / `target` 里。
#:
#: 🔴 2026-10-14 用户收到「订单 — 笔 → 成交 — · 失败 —」—— 全部是破折号。
#: 原因就是我把**外层 dict** 直接交给了 `format_rebalance()`，而它读的是**扁平**的键
#: （`n_orders` / `n_ok` / `nav` / `decision_ts`）⇒ 每个字段都 `None` ⇒ 格式化器一律
#: 打「—」。**这不是格式化器的问题，是我喂错了形状。**
#:
#: ⚠️ 两条纪律：
#:   1. `record` 与 `summary` 都要取，**`n_ok`/`n_fail` 只在 `record` 里**
#:      （成交数是执行后才有的），`nav` 两处都有。
#:   2. 报告数字前先看**格式化器读什么键**（同 `blocked_by` 那一脚）。
def _flatten_result(result: dict, mode: str) -> dict:
    """把 `execute()` 的嵌套结果摊平成 `format_rebalance()` 认的形状。"""
    rec = result.get("record") or {}
    smy = result.get("summary") or {}
    tgt = result.get("target") or {}
    out: dict = {
        "mode": mode,
        "stage": result.get("stage"),
        "decision_ts": tgt.get("decision_ts") or rec.get("decision_ts"),
        "next_decision_ts": tgt.get("next_decision_ts") or rec.get("next_decision_ts"),
        "nav": smy.get("nav", rec.get("nav")),
        "n_orders": smy.get("n_orders", rec.get("n_orders")),
        "n_ok": rec.get("n_ok"),
        "n_fail": rec.get("n_fail"),
        "turnover_frac": smy.get("turnover_frac", rec.get("turnover_frac")),
        "order_notional": smy.get("order_notional", rec.get("order_notional")),
        "gross_target": rec.get("gross_target"),
        "gross_realised": rec.get("gross_realised"),
        # ⚠️ `target.gross` 是**权重和**（≈1.36× NAV 这样的倍数），而
        # `record["gross_target"]` 是**USDT 名义额**（≈226.8）。两者数值量级
        # 差 NAV 倍，混用会让消息印出「目标毛敞口 226.799× NAV」这种荒谬的倍数
        # （实测撞到过）。所以分开两个键，各配各的标签。
        "target_gross_frac": tgt.get("gross"),
        "target_gross_usdt": rec.get("gross_target"),
        "run_id": rec.get("run_id"),
        "violations": result.get("violations") or [],
        "errors": rec.get("errors") or [],
    }
    # 账户快照（净值/保证金/可用）—— `execute` 不算它，由 `account()` 给。
    # 通知里带上是为了回答"下完这单账户还剩多少"，而那是每次都要看的数。
    acct = result.get("account")
    if not isinstance(acct, dict):
        try:
            acct = engine(mode).account()
        except Exception:                                        # noqa: BLE001
            acct = {}
    out["account"] = acct if isinstance(acct, dict) else {}
    return out


def _notify_result(mode: str, result: dict) -> dict:
    """把一次自动执行的产物推给通知通道。

    只做**翻译 + 转发**：`notify_for` 仍然是唯一的判定点（它决定该不该推、
    去重、格式化）。这里不重复它的逻辑，否则就有了第二份定义。
    """
    from crypto_ls_research.execution.notify import notify_for

    stage = str(result.get("stage") or "")
    out = _flatten_result(result, mode)
    out["action"] = _STAGE_TO_ACTION.get(stage, stage or "error")
    # `blocked` 时把拦截原因带进正文，否则收到"被拦截"却不知道被什么拦的。
    # ⚠️ 字段名是 `blocked_by` —— 那是 `format_rebalance()` 实际读的键。
    # 我第一版填的是 `errors`，结果消息里只有「未发送任何订单」、一条原因都没有
    # （被测试当场抓住）。**别猜字段名，去看格式化器读什么。**
    if out["action"] == "blocked":
        if not out.get("blocked_by"):
            titles = [str(d.get("title") or d.get("key") or d)
                      for d in (result.get("violations") or [])
                      if str(d.get("sev") or "") == "block"]
            out["blocked_by"] = titles or [str(e) for e in (result.get("errors") or [])] \
                or ["风控拦截"]
        out.setdefault("error", "；".join(str(b) for b in out["blocked_by"]))
    if out["action"] == "traded":
        out.setdefault("reason", "rebalance")

    n = _notifier_now(mode)
    r = notify_for(out, n)
    # **把结果写回审计与心跳**，否则"它告诉我了吗"只能靠翻控制台日志回答。
    # 2026-10-13：用户问「下单后还是没发飞书」，而 `runs.jsonl` 里连一个 `notify`
    # 字段都没有 —— 只能去 grep 日志才发现是 `Errno 61`。那条信息应该和这次调仓
    # 记在一起。（`auto_trader` 的 `_notify()` 一直把 `out["notify"]` 落盘，
    # 是控制台这条路漏了。）
    try:
        _record_notify(mode, result, r, out["action"])
    except Exception as e:                                        # noqa: BLE001
        print(f"[auto] {mode} 通知结果写入审计失败（不影响交易）："
              f"{type(e).__name__}: {e}", flush=True)
    if r.get("ok"):
        print(f"[auto] {mode} 已推送通知（{r.get('provider')}）：{out['action']}",
              flush=True)
    else:
        print(f"[auto] {mode} 未推送（{r.get('skipped') or r.get('error')}）"
              f"，action={out['action']}", flush=True)
    return r


def _record_notify(mode: str, result: dict, r: dict, action: str) -> None:
    """把通知结果附到这次调仓的审计记录上（按 `run_id` 找回那一行）。

    `runs.jsonl` 是只追加的，所以"改一行"= 重写整个文件。文件很小（每次调仓一条），
    而且这里已经在下单路径之外（下单早完成了），所以这点代价换"能事后回答
    『通知发了吗』"是值得的。

    写失败**不算错**：它绝不能影响交易，调用方已经把异常吞掉了；审计缺一个字段
    远不如把一次成功的调仓标成失败严重。
    """
    run_id = result.get("run_id")
    if not run_id:
        return
    st = Store(mode)
    # ⚠️ `store.runs()` 默认**最新在前**，写回时必须按原顺序（文件是时间序），
    # 否则重写一次会把 `runs.jsonl` 倒过来。
    rows = list(reversed(st.runs(limit=None)))
    hit = False
    for row in rows:
        if row.get("run_id") == run_id:
            row["notify"] = dict(r)
            row["notify"]["action"] = action
            hit = True
            break
    if not hit:
        return
    path = st.path("runs.jsonl")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def _wait_job(jid: str, timeout: float = 300.0) -> Optional[dict]:
    """等一个作业结束。超时返回 None（调用方自行决定要不要继续）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _JOBS.get(jid)
        if job and job.get("status") in ("done", "failed"):
            return job
        time.sleep(0.5)
    return None


def ensure_scheduler(mode: str) -> dict:
    """确保该账户的调度线程在跑（幂等）。**开关关着也装线程** —— 线程自己会跳过，
    这样页面上拨开关立即生效，不需要重启控制台。"""
    st = Store(mode)
    _REGISTRY.ensure(
        mode, auto_runner,
        state_path=lambda: st.path(auto_schedule.STATE_FILE),
        is_enabled=lambda: resolve_auto_enabled(st.load_auto_config()),
        log=lambda m: print(m, flush=True))


def start_enabled_schedulers() -> list:
    """控制台启动时调用：把所有**开关打开**的账户的调度器建起来。

    没有这一步，改代码重启一次就再也不自动了 —— 而页面上开关还显示「已开启」，
    是最坏的那一种（看着在跑，其实什么都没接上）。
    """
    started = []
    for mode in MODES:
        try:
            if resolve_auto_enabled(Store(mode).load_auto_config()):
                ensure_scheduler(mode)
                started.append(mode)
        except Exception as e:                                    # noqa: BLE001
            print(f"[auto] 恢复 {mode} 调度失败：{type(e).__name__}: {e}", flush=True)
    return started


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
    ("do not touch leverage") matters.  Both end up meaning *don't send the
    leverage request* -- but the second one is an explicit **off**, so it must
    also clear a value left over from an earlier request instead of keeping it.

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
            # 数字 → 下一次下单时设置一次；空 / 0 / 'off' → `None` = 关掉。两者都必须
            # 真正落到 `eng.set_leverage` 上，否则"关掉"会退化成"沿用上一次的值"。
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


def _nav_of(st: dict) -> Optional[float]:
    """The account NAV out of a `/status` payload, or None if unreadable."""
    acct = st.get("account") or {}
    for v in (acct.get("nav"), st.get("equity_usdt")):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and f > 0:
            return f
    return None


def record_nav_mark(mode: str, st: dict, *, gap: float = NAV_MARK_MIN_GAP,
                    now: Optional[float] = None) -> Optional[float]:
    """Append a NAV mark for the history chart, at most one per `gap` seconds.

    Deliberately *not* on every `/status` poll: the console polls status while
    a job runs, and a mark per poll would turn `equity.jsonl` into a request
    log.  This is called from the explicit 「刷新账户」 action, so the desk's
    history grows by one point per refresh at most -- and the engine's own
    rebalance/reconcile marks keep accumulating either way.

    Returns the mark it wrote, or None when it was throttled / unreadable.
    """
    nav = _nav_of(st)
    if nav is None:
        return None
    store = Store(mode)
    rows = store.equity()
    t = time.time() if now is None else float(now)
    if rows:
        try:
            last = float(rows[-1].get("ts") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if last and t - last < gap:
            return None
    store.add_equity(nav, ts=t)
    return nav


def _truthy(v: object) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


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
        # `effective_leverage` 现在可能是 `None` = **下单时不改杠杆**（这是默认）。
        # 它仍然不等于"实际在用的杠杆"：真实杠杆按 `(合约, 保证金模式, 方向)` 存在
        # 交易所，页面只能显示我们要不要去设它。
        st["leverage"] = eng.effective_leverage
        st["leverage_managed"] = eng.set_leverage is not None
        st["leverage_cap"] = eng.limits.max_leverage
        st["creds"] = creds_status()
        st["jobs"] = _recent_jobs(8)
        st["has_preview"] = mode in _LAST
        st["archives"] = archives(mode)
        # 宇宙体检（Q66）：调仓引擎读的是**盘上 parquet**，新合约不进、退市不剔、
        # 缺分类会 fail-loud。这里只读地报差距；模块自带 TTL 缓存 + 永不抛，
        # 所以挂在每轮状态轮询上是安全的。
        st["universe_drift"] = drift_report()
        # `record=1` comes from the explicit 「刷新账户」 button, never from the
        # automatic poll -- see `record_nav_mark`.  Read-only callers get a
        # read-only endpoint: they simply omit the flag.
        if _truthy(one("record")) and st.get("connected"):
            marked = record_nav_mark(mode, st)
            st["nav_marked"] = marked is not None
        return st, 200

    if path == "/api/live/navhist":
        # The account's own NAV history.  It is *rebuilt* (see
        # `navhist.reconstruct`) because neither OKX nor the local ledger stores
        # it: OKX only exposes the current balance and the ledger only the
        # current book.  `rt=1` (default) also pulls the live 1h candles from
        # OKX's public endpoint so the curve reaches the bar still forming;
        # when that is unreachable it falls back to the local cache and says so
        # in `source`/`warnings` instead of quietly drawing a stale line.
        mode = one("mode", "paper")
        if mode not in MODES:
            return {"error": f"unknown mode {mode!r}", "modes": list(MODES)}, 400
        bar = str(one("bar", "1h") or "1h")
        days = one("days")
        try:
            days = int(days) if days not in (None, "") else None
        except (TypeError, ValueError):
            return {"error": f"days must be an integer, got {days!r}"}, 400
        want_live = _truthy(one("rt", "1"))
        try:
            hist = navhist.build(
                mode, bar=bar, days=days, use_okx=want_live,
                client=navhist.okx_client() if want_live else None)
        except ValueError as e:
            return {"error": str(e)}, 400
        except Exception as e:                                     # noqa: BLE001
            # A rebuild that explodes must not take the whole desk down with it:
            # the panel can render the error, the orders still go out.
            return {"error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc()[-800:]}, 500
        return hist.to_dict(), 200

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
        return _auto_view(mode), 200

    return None


def _auto_view(mode: str) -> dict:
    """Everything the page needs to render the auto panel for one account.

    `enabled` (the persisted switch) and `running` (is a daemon alive) are
    **independent**, and conflating them is how a page ends up claiming trading
    is on when it is not:

      * `enabled=True, running=False` -> configured, but nothing will happen.
      * `enabled=False, running=True` -> a process is up and will correctly do
        nothing; this is the *safe* state, not a broken one.

    So both are reported, plus a derived `will_trade` that is the honest answer
    to "is this account going to place orders unattended?".
    """
    st = Store(mode)
    cfg = st.load_auto_config()
    enabled = resolve_auto_enabled(cfg)

    # `running` 现在指**后端调度线程**，不再指那个独立守护进程。
    # 旧字段（守护进程的心跳/控制文件）仍然带回，因为页面别处还在用它们显示
    # 网格与检查间隔；但「会不会自动交易」这个问题只由 `enabled and running`
    # 回答，而 `running` 的语义已经换成控制台自己的调度器。
    sched = _REGISTRY.get(mode)
    running = bool(sched and sched.is_alive())
    return {
        "mode": mode,
        "enabled": enabled,
        "running": running,
        "will_trade": bool(enabled and running),
        "at": (sched.at if sched else auto_schedule.DEFAULT_AT),
        "ran_today": bool(sched and sched.already_ran_today()),
        "last": (sched.last if sched else {}),
        "scheduler": next((r for r in _REGISTRY.status() if r["mode"] == mode), None),
        "state": auto_ctl.read_state(mode),
        "ctl": auto_ctl.read_ctl(),
        "grid": auto_ctl.grid_for(mode),
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "config_path": st.auto_path(),
    }


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
            # 计划里也带一份宇宙体检（TTL 内几乎免费）：调仓前最后一眼
            # 「这次决策的宇宙和交易所现状差多远」。
            payload["universe_drift"] = drift_report()
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

    if path == "/api/live/auto/enable":
        return _auto_set_enabled(body, True)
    if path == "/api/live/auto/disable":
        return _auto_set_enabled(body, False)

    if path == "/api/live/auto/notify-test":
        # 通知自检：**由控制台进程自己发一条**。
        #
        # 为什么需要这个端点（2026-10-13）：用户在 11:00 之后才问"为什么没发飞书"，
        # 而那时唯一的证据是控制台日志里一行 `URLError: Errno 61`。如果不等到明天
        # 11:00 就能自己验一次，这类问题能在部署当下被发现，而不是事后翻日志。
        #
        # ⚠️ 关键：它必须在**控制台进程**里执行。我在工具调用里另起一个进程发，
        # 走的是我那次的代理环境 —— 那恰恰会掩盖问题（代理在我的调用期间是活的）。
        mode = body.get("mode", "live")
        if mode not in MODES:
            return {"ok": False, "error": f"bad mode {mode!r}"}, 400
        n = _notifier_now(mode)
        d = n.describe()
        r = n.send("✅ 通知自检 · 控制台",
                   "这条由**控制台进程自己**发出。收到即说明 11:00 的定时调仓也发得出。",
                   key=f"selftest-{time.time()}", action="traded")
        return {"ok": bool(r.get("ok")), "notify": r, "config": d}, 200

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


def _auto_set_enabled(body: dict, on: bool) -> Tuple[dict, int]:
    """Persist the per-account unattended switch.

    Three rules, each of which exists because of a specific way this can hurt:

    1. **Switching `live` ON requires the same typed phrase the order path
       uses** (`LIVE-<today>`).  This switch is what turns "a process is
       running" into "real orders at 11:00 every day, unattended".  A checkbox
       is too cheap for that; the phrase is the same "are you awake" control
       already used before a manual live order, so it is one thing to learn,
       not two.
    2. **Switching OFF is always allowed and never gated.**  Anything that makes
       it harder to stop trading than to start it is backwards.
    3. **The response reports what was written, re-read from disk.**  Echoing
       the request would let a failed write look like a successful one -- and
       the failure mode here is the worst kind: the page says "已开启" while the
       file still says off (or vice versa).
    """
    mode = body.get("mode", "paper")
    if mode not in MODES:
        return {"ok": False, "error": f"bad mode {mode!r}"}, 400

    # 用户裁定（2026-10-10）：**不要确认短语，就一个普通开关**。
    # 便利性与"不会误碰就每天自动下真钱单"之间的取舍由他做了选择 ——
    # 页面必须把代价写在按钮旁边（见 `liveAutoHtml` 的说明），而不是靠一道
    # 会被当成麻烦的闸门来暗示。
    st = Store(mode)
    cfg = st.load_auto_config()
    cfg[AUTO_ENABLED_KEY] = bool(on)
    cfg["changed_ts"] = time.time()
    cfg["changed_by"] = "console"
    st.save_auto_config(cfg)

    # 开关一落盘就把调度线程备好（**不是在开启时才装**：线程自己每轮读开关，
    # 所以关→开 不需要任何额外动作，开→关 也立即生效）。
    ensure_scheduler(mode)

    # Re-read rather than echo, and report the *resolved* boolean (not the raw
    # JSON value) so the page and the scheduler can never disagree about what
    # "enabled" means.
    view = _auto_view(mode)
    return {"ok": True, "mode": mode, "requested": bool(on),
            "enabled": view["enabled"], "running": view["running"],
            "will_trade": view["will_trade"],
            "config_path": st.auto_path()}, 200
