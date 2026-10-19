"""Local web console for the liquidity-flow trend strategy.

Standard library only (no Flask on this machine).  Serves a single-page UI and a
small JSON API, and owns a one-at-a-time backtest queue so that concurrent
submissions cannot fight over the same 6 cores.

Run:  python webapp/server.py 8770
"""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spec import (  # noqa: E402
    OPTIMAL_CLI, OPTIMAL_HEADLINE, OPTIMAL_TAG, PRESETS, SPEC, STAGE_BUNDLES,
    build_run_args, check_traps,
)
from crypto_ls_research.config.settings import CACHE_DIR, default_end  # noqa: E402
import trades  # noqa: E402
import auth  # noqa: E402

# ⚠️ **`live_api` 必须只有一份，且用包路径导入。**
#
# 背景（2026-10-12 实测踩到）：本文件上面把 `webapp/` 塞进了 `sys.path`，于是
# `import live_api` 会以**顶层名**注册模块；而任何一处 `from webapp import live_api`
# 会以 `webapp.live_api` 再加载**第二份**。两份各有自己的模块级状态
# （`_REGISTRY` / `_JOBS` / `_ENGINES` / `_ENGINES`），后果是
# 「调度线程建在 A 份、HTTP 处理器读 B 份」—— 启动日志显示 `已恢复 ['live']`，
# 页面却显示 `running: false`。看着在跑，其实没接上，是本仓库最忌讳的失败形态。
#
# 现在两条路都堵上：
#   1. 本文件**只用这一种**导入方式（下面的测试会盯着不许出现另一种）；
#   2. `webapp/live_api.py` 对 `auto_schedule` 用**相对导入**，不再自造第二条路径。
#
# 保持包路径（而不是顶层名）的理由：`crypto_ls_research` 那边也是包，包路径下
# 依赖解析不会因为 `sys.path` 的顺序而漂移。
from webapp import live_api  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ARTIFACTS = os.path.join(ROOT, "artifacts")
RUNS_DIR = os.path.join(ARTIFACTS, "ui_console")
RUNS_JSON = os.path.join(RUNS_DIR, "runs.json")
os.makedirs(RUNS_DIR, exist_ok=True)

PY = sys.executable
ACTIVE = {"proc": None, "run_id": None}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sanitize(o):
    """Make numpy / NaN / Inf JSON-safe."""
    if isinstance(o, dict):
        return {str(k): sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, int):
        return o
    try:
        f = float(o)
    except (TypeError, ValueError):
        return str(o)
    if not math.isfinite(f):
        return None
    return f


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_runs(runs):
    tmp = RUNS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RUNS_JSON)


def load_runs():
    runs = read_json(RUNS_JSON, {}) or {}
    # a process that died with the server leaves a stale "running" flag
    for r in runs.values():
        if r.get("status") in ("running", "queued"):
            r["status"] = "interrupted"
    return runs


RUNS = load_runs()
RUNS_LOCK = threading.Lock()
JOBQ: "queue.Queue[str]" = queue.Queue()


def new_run_id():
    return "ui_" + time.strftime("%Y%m%d_%H%M%S")


def log_path(run_id):
    return os.path.join(RUNS_DIR, run_id + ".log")


def tag_dir(tag):
    return os.path.join(ARTIFACTS, tag)


# How many pass/fail gates a *complete* full-stage run produces.  That is the
# yardstick for "do we have all the evidence on disk right now", so a partial
# re-run is reported as partial instead of quietly looking like a pass.
#
# The recorded v3 write-up said "16/16", and `acceptance_checks` does return 16
# **rows** for a complete run -- but three of them are deliberately `pass=None`
# *informational* rows (the funding term's sign is not stable, so the criterion
# that judges it is not a gate; see `tests/test_acceptance_funding.py`).  Gates and
# rows are different units, and comparing a gate count against the row count made
# `complete` unreachable: a full run on disk reported "证据不全：完整需 16 项" while
# holding all 13 gates.  Count gates.
#
# 2026-09-29: 13 -> 16.  The 13 was the count for a run **without `--stages mc`**,
# and the three placebo nulls were not merely unevaluated -- they produced no rows
# at all, so the summary read a clean 13/13 with the only falsifiable criterion
# missing.  `acceptance_checks` now emits those rows explicitly when the artifact
# is absent (see `tests/test_acceptance_completeness.py`), and the accepted tag
# `v5_1d_all5` is the first on disk that actually ran the stage, so the complete
# count is 16.  A tag that skipped `mc` still reports 13 gates -- and now says so.
_ACCEPT_EXPECTED = 16
_ACCEPT_CACHE: dict = {}


def _tag_stamp(tag):
    """Newest mtime under a tag's tables/ — cheap cache key for artifact reads."""
    d = os.path.join(tag_dir(tag), "tables")
    try:
        return max(os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d))
    except Exception:
        return 0.0


def collect_metrics(run) -> dict:
    """Everything the result panel needs, or None if the stage never produced it."""
    tag = run["tag"]
    tdir = os.path.join(tag_dir(tag), "tables")
    head = read_json(os.path.join(tdir, "01_headline_metrics.json"))
    meta = read_json(os.path.join(tdir, "00_run_meta.json"))
    scope = read_json(os.path.join(tdir, "00b_universe_scope.json"))
    if head is None:
        return None
    keep = [
        ("Sharpe", "Sharpe", "n"),
        ("CAGR", "CAGR", "pct"),
        ("Annualized Volatility", "年化波动", "pct"),
        ("Max Drawdown", "最大回撤", "pct"),
        ("Max DD Duration (days)", "回撤持续（天）", "n1"),
        ("Sortino", "Sortino", "n"),
        ("Calmar", "Calmar", "n"),
        ("Win Rate (daily)", "日胜率", "pct"),
        ("Annual Turnover", "年换手（倍）", "n1"),
        ("Cost Drag (annual)", "成本拖累（年）", "pct"),
        ("Trading Cost (total, frac)", "交易成本（总）", "pct"),
        ("Funding P&L (total, frac)", "资金费盈亏（总）", "pct"),
        ("Gross Exposure (avg)", "平均总敞口", "n"),
        ("Beta Exposure (avg abs)", "平均 |beta 敞口|", "n"),
        ("Long Share of Gross", "多头占总毛收益", "pct"),
        ("Avg Max Participation", "平均最大 ADV 参与率", "sci"),
    ]
    metrics = []
    for src, label, fmt in keep:
        metrics.append({"label": label, "value": head.get(src), "fmt": fmt})
    yearly = read_year_structure(tag)
    return sanitize({
        "headline": head,
        "metrics": metrics,
        "meta": meta,
        "scope": scope,
        "sharpe": head.get("Sharpe"),
        "cagr": head.get("CAGR"),
        "mdd": head.get("Max Drawdown"),
        "yearly": yearly,
        "has_equity": os.path.exists(os.path.join(tag_dir(tag), "baseline.pkl")),
        "charts": sorted(os.listdir(os.path.join(tag_dir(tag), "charts")))
        if os.path.isdir(os.path.join(tag_dir(tag), "charts")) else [],
    })


def read_year_structure(tag):
    """Per-year Sharpe, computed with the *same* convention as `metrics.compute_metrics`.

    That convention is: aggregate net_ret to daily sums, then
    `mean/std(ddof=1) * sqrt(365)`.  Computing it straight off the 1h bars produces
    different numbers (e.g. 2021 = 1.023 vs the canonical 1.055) because intraday
    noise changes the denominator -- and a console that disagrees with the report on
    a headline number is worse than no console.

    `equity` is rebuilt from `(1 + net_ret).cumprod()` rather than sliced out of
    `bars["equity"]`: the saved equity curve is full-sample, and slicing it would
    annualise the whole-sample move over a sub-window (this project once produced a
    fake 475% CAGR exactly that way).
    """
    pkl = os.path.join(tag_dir(tag), "baseline.pkl")
    if not os.path.exists(pkl):
        return None
    try:
        import pickle
        import numpy as np
        with open(pkl, "rb") as f:
            b = pickle.load(f)
        s = b["result"].bars["net_ret"]
        s = s[np.isfinite(s.to_numpy(dtype="float64"))]
        if s.empty:
            return None
        daily = s.resample("1D").sum()
        daily = daily[np.isfinite(daily.to_numpy(dtype="float64"))]
        if len(daily) < 30:
            return None

        def block(x):
            sd = float(x.std(ddof=1))
            mean = float(x.mean())
            years = max((x.index[-1] - x.index[0]).total_seconds() / 86400.0 / 365.25, 1e-6)
            eq = float(np.prod(1.0 + x.to_numpy(dtype="float64")))
            return {
                "sharpe": (mean / sd * math.sqrt(365.0)) if sd > 0 else None,
                "vol": sd * math.sqrt(365.0),
                "cagr": (eq ** (1.0 / years) - 1.0) if eq > 0 else -1.0,
                "n_days": int(len(x)),
            }

        out = [dict(year=int(y), **block(daily[daily.index.year == y]))
               for y in sorted(set(daily.index.year))]
        return sanitize({"years": out, "full": block(daily)})
    except Exception:
        return None


def lib_defaults():
    """Library-level defaults, so the UI can send overrides for *differences only*.

    Sending the full parameter set would work but makes every run's command line
    unreadable and hides which knob actually moved.
    """
    try:
        from crypto_ls_research.config.settings import BacktestConfig
        cfg = BacktestConfig()
        out = {}
        for sec in ("universe", "factors", "portfolio", "risk", "execution", "costs"):
            for k, v in vars(getattr(cfg, sec)).items():
                if k == "profiles":
                    continue
                out[f"{sec}.{k}"] = sanitize(list(v) if isinstance(v, tuple) else v)
        return out
    except Exception:
        return {}


def read_equity(tag, max_points=900):
    """Downsampled cumulative equity for the chart, read from the saved result.

    **下采样必须保极值。** 等间隔抽点会跳过峰谷：v3 的真实最大回撤是 -12.72%，
    隔 56 个 bar 抽一个点之后只剩 -10.01%，页面上就会给出一个**比报告好看 2.7pp**
    的假数字。所以这里每桶保留 min / max / 末值三个点，另外再用**全量**序列算一次
    精确 MDD 一起返回 —— 图上的数字直接用它，不去猜下采样后的形状。
    """
    pkl = os.path.join(tag_dir(tag), "baseline.pkl")
    if not os.path.exists(pkl):
        return None
    try:
        import pickle
        import numpy as np
        with open(pkl, "rb") as f:
            b = pickle.load(f)
        bars = b["result"].bars
        eq = bars["equity"]
        v_all = eq.to_numpy(dtype="float64")
        keep = np.isfinite(v_all)
        eq, idx = eq[keep], eq.index[keep]
        if eq.size == 0:
            return None
        v = eq.to_numpy(dtype="float64")

        # 全量口径的精确 MDD —— 与 analysis.metrics.max_drawdown 同一算法
        peak = np.maximum.accumulate(v)
        dd = np.where(peak > 0, v / peak - 1.0, 0.0)
        k = int(np.argmin(dd))
        mdd = float(dd[k])
        mdd_date = str(idx[k].date())
        peak_date = str(idx[int(np.argmax(v[: k + 1]))].date())

        step = max(1, int(math.ceil(eq.size / max_points)))
        if step == 1:
            sel = list(range(eq.size))
        else:
            picked = set()
            for a in range(0, eq.size, step):
                b2 = min(a + step, eq.size)
                seg = v[a:b2]
                picked.add(int(a + np.argmin(seg)))
                picked.add(int(a + np.argmax(seg)))
                picked.add(b2 - 1)
            sel = sorted(picked)
        return {"t": [int(t.timestamp() * 1000) for t in idx[sel]],
                "v": [float(x) for x in v[sel]],
                "start": str(idx[0].date()), "end": str(idx[-1].date()),
                "final": float(v[-1]),
                "n_points": int(eq.size),
                "downsampled": bool(step > 1),
                "mdd": mdd, "mdd_date": mdd_date, "peak_date": peak_date}
    except Exception:
        return None


def _load_bars(tag):
    """Return the raw `bars` DataFrame from `baseline.pkl`, or None."""
    pkl = os.path.join(tag_dir(tag), "baseline.pkl")
    if not os.path.exists(pkl):
        return None
    try:
        import pickle
        with open(pkl, "rb") as f:
            return pickle.load(f)["result"].bars
    except Exception:
        return None


def _downsample_pair(idx, v, max_points=600):
    """Downsample a series to <= max_points, preserving per-bucket min/max/tail.

    Same rule as `read_equity` (min/max/tail per bucket) so the exposure/heatmap
    lines never skip a peak or trough.  Returns parallel lists (t_ms, values).
    """
    import numpy as np
    arr = np.asarray(v, dtype="float64")
    keep = np.isfinite(arr)
    arr, idx = arr[keep], idx[keep]
    if arr.size == 0:
        return [], []
    n = arr.size
    step = max(1, int(math.ceil(n / max_points)))
    if step == 1:
        sel = list(range(n))
    else:
        picked = set()
        for a in range(0, n, step):
            b2 = min(a + step, n)
            seg = arr[a:b2]
            picked.add(int(a + int(np.argmin(seg))))
            picked.add(int(a + int(np.argmax(seg))))
            picked.add(b2 - 1)
        sel = sorted(picked)
    return ([int(t.timestamp() * 1000) for t in idx[sel]],
            [float(x) for x in arr[sel]])


def read_equity_detail(tag, max_points=600):
    """Extra series for the result panel: drawdown ranges, monthly returns,
    gross/net/beta exposure time-series.  All computed from `baseline.pkl` so the
    numbers match the report exactly (not re-derived from the downsampled equity).
    """
    bars = _load_bars(tag)
    if bars is None or "equity" not in bars:
        return None
    import numpy as np
    try:
        eq = bars["equity"]
        keep = np.isfinite(eq.to_numpy(dtype="float64"))
        eq, idx = eq[keep], eq.index[keep]
        if eq.size == 0:
            return None
        v = eq.to_numpy(dtype="float64")

        # --- Top-N drawdown ranges (full resolution, same algo as report) ---
        peak = np.maximum.accumulate(v)
        dd = np.where(peak > 0, v / peak - 1.0, 0.0)
        ranges = []
        # walk the drawdown series and capture each excursion below a threshold
        under = dd < -0.005            # ignore sub-0.5% ripples
        i = 0
        while i < dd.size:
            if not under[i]:
                i += 1
                continue
            j = i
            while j < dd.size and under[j]:
                j += 1
            seg = dd[i:j]
            depth = float(seg.min())
            trough = int(i + int(np.argmin(seg)))
            ranges.append({
                "start": str(idx[i].date()),
                "trough": str(idx[trough].date()),
                "end": str(idx[j - 1].date()),
                "depth": depth,
                "days": int((idx[j - 1] - idx[i]).total_seconds() / 86400.0) + 1,
            })
            i = j
        ranges.sort(key=lambda r: r["depth"])
        drawdowns = ranges[:5]

        # --- Monthly returns (sum of daily net_ret), full resolution ---
        monthly = []
        if "net_ret" in bars:
            s = bars["net_ret"]
            keep = np.isfinite(s.to_numpy(dtype="float64"))
            s = s[keep]
            daily = s.resample("1D").sum()
            if daily.size:
                m = daily.resample("1ME").apply(
                    lambda x: float(np.prod(1.0 + x.to_numpy(dtype="float64")) - 1.0))
                for ts, r in m.items():
                    monthly.append({"y": int(ts.year), "m": int(ts.month),
                                    "ret": float(r)})

        # --- Exposure series (downsampled, min/max/tail) ---
        def series(name):
            if name not in bars:
                return None
            s = bars[name]
            keep = np.isfinite(s.to_numpy(dtype="float64"))
            s, si = s[keep], s.index[keep]
            t, vals = _downsample_pair(si, s.to_numpy(dtype="float64"), max_points)
            return {"t": t, "v": vals}

        return {
            "drawdowns": drawdowns,
            "monthly": monthly,
            "gross": series("gross_exposure"),
            "net": series("net_exposure"),
            "beta": series("beta_exposure"),
        }
    except Exception:
        return None


def stage_report(run):
    """Parse the log for per-stage timing and the stage-failure marker.

    `run.research` wraps every stage in try/except and keeps going, so the exit
    code stays 0 even when a stage never produced its tables.  `!! stage` is the
    only reliable failure signal.
    """
    p = run.get("log")
    if not p or not os.path.exists(p):
        return {"stages": [], "errors": []}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception:
        return {"stages": [], "errors": []}
    stages = [{"name": m.group(1), "secs": int(m.group(2))}
              for m in re.finditer(r"^\s*\[([a-z]+)\]\s+(\d+)s\s*$", txt, re.M)]
    errors = [m.group(0).strip() for m in
              re.finditer(r"^\s*!! stage .*$", txt, re.M)]
    total = re.search(r"^### total (\d+)s\s*$", txt, re.M)
    return {"stages": stages, "errors": errors,
            "total_secs": int(total.group(1)) if total else None}


# ---------------------------------------------------------------------------
# the queue worker
# ---------------------------------------------------------------------------
def worker():
    while True:
        run_id = JOBQ.get()
        try:
            _execute(run_id)
        except Exception:                                        # noqa: BLE001
            with RUNS_LOCK:
                r = RUNS.get(run_id)
                if r:
                    r["status"] = "failed"
                    r["error"] = traceback.format_exc()[-2000:]
                    write_runs(RUNS)
        finally:
            JOBQ.task_done()


def _execute(run_id):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            return
        run["status"] = "running"
        run["started"] = time.time()
        write_runs(RUNS)
        argv = run["argv"]

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8:replace"
    env["PYTHONPATH"] = ROOT
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)

    lp = log_path(run_id)
    rc = None
    with open(lp, "w", encoding="utf-8", errors="replace") as lf:
        lf.write("$ " + " ".join([PY] + argv) + "\n\n")
        lf.flush()
        proc = subprocess.Popen([PY] + argv, cwd=ROOT, stdout=lf,
                                stderr=subprocess.STDOUT, env=env)
        ACTIVE["proc"] = proc
        ACTIVE["run_id"] = run_id
        rc = proc.wait()
        ACTIVE["proc"] = None
        ACTIVE["run_id"] = None

    rep = stage_report({"log": lp})
    res = collect_metrics(run)
    with RUNS_LOCK:
        r = RUNS.get(run_id)
        r["exit_code"] = rc
        r["finished"] = time.time()
        r["elapsed"] = int(r["finished"] - r.get("started", r["finished"]))
        r["stage_errors"] = rep["errors"]
        r["total_secs"] = rep.get("total_secs")
        if rc != 0:
            r["status"] = "failed"
        elif rep["errors"]:
            # exit code 0 with a failed stage is the dangerous case: tables the
            # report references may simply not exist.
            r["status"] = "partial"
        else:
            r["status"] = "done"
        r["summary"] = None
        if res:
            r["summary"] = {"sharpe": res["sharpe"], "cagr": res["cagr"],
                            "mdd": res["mdd"]}
        write_runs(RUNS)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
#: 不需要登录就能访问的路径。**白名单**，不是黑名单 ——
#: 新增端点默认是"要登录"的，忘了加白名单只会让页面暂时登不上；
#: 反过来（默认放行）则是一个静默的、能下单的洞。
#:
#: `/api/auth/*` 是登录流程本身；根路径与静态资源必须放行，否则登录页自己
#: 都加载不出来。
#:
#: 🔴 **路径要和 `index.html` 实际请求的一致。** 我第一版写的是 `/app.js` /
#: `/style.css`，而页面里写的是 **`/static/app.js`** / **`/static/style.css`**
#: ⇒ 登录页的 CSS/JS 全被 401 挡掉，浏览器里只剩一具没有样式的空壳，
#: 表现为"页面打不开"而控制台日志一切正常（2026-10-16 用户就是这么撞上的）。
#: 现在用**前缀** `/static/`，并且用测试从 `index.html` 里**抓**引用的路径来对账。
_PUBLIC_STATIC_PREFIX = ("/static/",)
_PUBLIC_STATIC = ("/", "/index.html", "/favicon.ico")

#: 登录流程本身的白名单（都在 `/api/` 下）。
_PUBLIC_API = (
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/verify",
    "/api/auth/logout",
)

AUTH = auth.AuthStore()
LOGIN = auth.LoginFlow()
SESSION_COOKIE = "ls_session"


def _client_is_local(addr) -> bool:
    """请求是不是来自本机。用于"只在 HTTPS 下放行外网"那类判断。"""
    try:
        return (addr or ("", 0))[0] in ("127.0.0.1", "::1", "localhost")
    except (TypeError, IndexError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ls-console/1.0"

    def log_message(self, *a):                                   # noqa: D102
        pass

    # -- auth --------------------------------------------------------------
    def _cookie_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v or None
        return None

    def _authed(self) -> bool:
        return AUTH.resolve_session(self._cookie_token())

    def _cookie(self, token: str, *, clear: bool = False) -> str:
        """会话 Cookie。

        `HttpOnly` 挡住 XSS 读走 token；`SameSite=Strict` 挡住**跨站请求伪造**
        （否则你打开任意一个网页，它就能悄悄替你 POST 一次下单）；
        `Secure` 只在 TLS 下加 —— 明文 HTTP 上加 Secure 会让浏览器直接丢掉
        Cookie，于是**本机开发时永远登不上**（一个"安全设置导致功能全废"的典型）。
        """
        v = "" if clear else token
        attrs = [f"{SESSION_COOKIE}={v}", "Path=/", "HttpOnly", "SameSite=Strict",
                 f"Max-Age={0 if clear else auth.SESSION_TTL}"]
        if getattr(self.server, "tls", False):
            attrs.append("Secure")
        return "; ".join(attrs)

    def _needs_auth(self, path: str) -> bool:
        """这个请求要不要先登录？**默认要。**"""
        if not path.startswith("/api/"):
            # 静态资源放行（登录页自己要用）。
            # ⚠️ 用**前缀**匹配 `/static/`，不要逐条列举文件名 ——
            # 列举就会和 `index.html` 里实际写的路径漂移（我第一版列的是
            # `/app.js`，而页面请求的是 `/static/app.js`，结果登录页的 JS 被挡）。
            if any(path.startswith(p) for p in _PUBLIC_STATIC_PREFIX):
                return False
            return path not in _PUBLIC_STATIC
        return not any(path == p or path.startswith(p + "/")
                       for p in _PUBLIC_API)

    # -- auth routes -------------------------------------------------------
    def _auth_route(self, path: str, body: dict):
        """`/api/auth/*` 的所有处理。**每个分支都写清它信什么、不信什么。**"""
        act = path[len("/api/auth/"):]

        if act == "status":
            # 公开。**只回"有没有配好"和"我登没登"** —— 不回用户名、不回任何
            # 可用于缩小搜索空间的东西。
            return self._json({
                "configured": AUTH.is_configured(),
                "authed": self._authed(),
                "locked_for": round(AUTH.locked_for(), 1),
                "user": (AUTH.user().get("name") if self._authed() else None),
            })

        if act == "setup":
            # 首次设置：**只在还没账号时可用**。
            if AUTH.is_configured():
                return self._err(409, "账号已存在")
            name = str(body.get("user") or "").strip()
            pw = str(body.get("password") or "")
            try:
                secret = AUTH.create_user(name, pw)
            except ValueError as e:
                return self._err(400, str(e))
            # TOTP 密钥**只在这一刻**回给浏览器（让用户扫进验证器）。
            # 之后任何接口都不再返回它。
            return self._json({
                "ok": True,
                "user": name,
                "secret": secret,
                "otpauth": auth.otpauth_url(secret, name, "LS 交易台"),
                "hint": "用验证器 App 扫这个链接（或手输密钥），然后把 6 位码填进来",
            })

        if act == "login":
            # 第一步：口令。**过了只发一张短命票据，不发会话。**
            locked = AUTH.locked_for()
            if locked > 0:
                return self._err(429, f"失败次数过多，请 {int(locked)} 秒后再试",
                                 locked_for=round(locked, 1))
            name = str(body.get("user") or "")
            pw = str(body.get("password") or "")
            u = AUTH.user()
            # 用户名和口令**一起**判定，且失败回同一句话：分别报"用户不存在"
            # 会给出一条枚举用户名的路径。
            if not (u.get("name") == name and auth.verify_password(pw, u.get("pw"))):
                st = AUTH.note_fail()
                return self._err(401, "用户名或口令不对", locked_for=round(
                    st.get("locked_for", 0.0), 1))
            return self._json({"ok": True, "step": 2,
                               "ticket": LOGIN.start(u.get("name")),
                               "hint": "请输入验证器上的 6 位码"})

        if act == "verify":
            # 第二步：TOTP。票据是一次性的，**claim 之后立刻作废**。
            pending = LOGIN.claim(str(body.get("ticket") or ""))
            if not pending:
                return self._err(401, "票据无效或已过期，请重新输入口令")
            u = AUTH.user()
            # 用过的"步数"从盘上读进来（跨请求、跨重启都有效），验完再把
            # 新用掉的那几个写回去。⚠️ 只在**成功**时写：失败的尝试不该
            # 消耗掉一个合法码的位置。
            used = AUTH.totp_used()
            ok, why = auth.verify_totp(u.get("totp") or "",
                                       str(body.get("code") or ""), used=used)
            if not ok:
                AUTH.note_fail()
                return self._err(401, f"验证码错误（{why}）")
            for c in used:
                AUTH.remember_totp(c)
            AUTH.note_ok()
            tok = AUTH.new_session(self.headers.get("User-Agent") or "")
            return self._json({"ok": True, "user": u.get("name")},
                              cookies=[self._cookie(tok)])

        if act == "logout":
            AUTH.drop_session(self._cookie_token())
            return self._json({"ok": True}, cookies=[self._cookie("", clear=True)])

        return self._err(404, "not found")

    def _gate(self) -> bool:
        """返回 True = 已放行。False = 已经回了响应，调用方必须立刻 return。"""
        path = urlparse(self.path).path

        # 静态资源**永远**放行，与"有没有建账号""登没登录"都无关。
        #
        # 🔴 这条分支原本被漏掉了：`not AUTH.is_configured()` 那一支**自己又写了
        # 一遍白名单判断**（`path in _PUBLIC_STATIC`），而它只认文件名、不认
        # `/static/` 前缀 ⇒ 首次使用（还没建账号）时 `/static/app.js` 回 401 ⇒
        # 页面的 JS 根本加载不出来 ⇒ 屏幕上只有一具空壳，**连"创建账号"按钮都没有**。
        # （2026-10-16 用户截图报的就是这个。）
        #
        # 教训：同一条规则写两遍，两遍就会不一致。现在只留 `_needs_auth` 一处定义，
        # 下面两个分支都问它。
        if not self._needs_auth(path):
            return True

        if not AUTH.is_configured():
            # 还没建账号：只允许走首次设置流程，别的一律挡掉并告诉前端去设置。
            if path in ("/api/auth/status", "/api/auth/setup"):
                return True
            self._err(401, "尚未创建账号", need_setup=True)
            return False

        if self._authed():
            return True
        # ⚠️ 回 **JSON 401**，不是 302 重定向：前端一律 `resp.json()`，
        # 一个 HTML 重定向页会让它抛 `Unexpected token '<'`，而真正的原因
        # （没登录）被完全藏住 —— 这是本仓库已经踩过一次的同类坑。
        self._err(401, "未登录或会话已过期", need_login=True)
        return False

    # -- plumbing ----------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, cookies: Optional[list] = None):
        """`cookies` 用来在**同一个响应**里下达/清除会话 Cookie。

        刻意不另开一个 `_json_with_headers`：登录成功必须"回 JSON + 种 Cookie"
        原子发生，分成两步容易出现"发了 Cookie 却没回 body"这类半成品。
        """
        body = json.dumps(sanitize(obj), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg, **extra):
        self._json({"error": msg, **extra}, code)

    def _file(self, path, ctype="application/octet-stream"):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._err(404, "not found")

    def send_error(self, code, message=None, explain=None):
        """任何 `/api/` 下的错误都必须回 **JSON**，不能是 HTML。

        `BaseHTTPRequestHandler` 对**没有实现的方法**会走
        `handle_one_request` -> `send_error(501, ...)`，回一张
        `Content-Type: text/html` 的错误页。前端 `resp.json()` 在这上面抛
        `Unexpected token '<', "<!DOCTYPE "... is not valid JSON` —— 用户只看到
        「JSON 解析失败」，看不到「服务端不支持这个方法」，排查方向直接跑偏。

        实测（2026-09-30）：控制台进程是 09-29 18:51 起的，而 `do_DELETE` 是
        09-30 00:46 才进仓库的。改过 `webapp/` 却没重启 -> 进程里没有
        `do_DELETE` -> `DELETE /api/runs/<id>` 回 501 + HTML -> 删除按钮报
        「JSON 解析失败」。所以 `/api/` 的错误一律换成 JSON 信封；
        静态资源/页面的错误仍走父类的 HTML 错误页（浏览器直接打开时要好看）。
        """
        path = urlparse(getattr(self, "path", "") or "").path
        if path.startswith("/api/"):
            try:
                return self._err(code, message or explain or "error")
            except Exception:                                    # noqa: BLE001
                # 头可能已经发出去一半，此时再让父类补一张 HTML 只会更乱。
                return None
        return super().send_error(code, message, explain)

    def do_GET(self):                                            # noqa: N802
        # ⚠️ 鉴权在**唯一的入口**做，不散在每个端点里 —— 散着做一定会漏掉一个，
        # 而漏掉的那个很可能是 `/api/live/execute`。
        if not self._gate():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                name = os.path.basename(p)
                ctype = {"css": "text/css; charset=utf-8",
                         "js": "application/javascript; charset=utf-8",
                         "html": "text/html; charset=utf-8",
                         # favicon 必须是这个 MIME：回 octet-stream 时 Chrome
                         # 会**静默不显示**（不报错，标签页就是没有图标）。
                         "svg": "image/svg+xml"}.get(
                    name.rsplit(".", 1)[-1], "application/octet-stream")
                return self._file(os.path.join(STATIC, name), ctype)
            if p == "/api/spec":
                base = self._baseline()
                hl = self._headline(base)
                return self._json({
                    "spec": self._spec_payload(), "presets": self._presets(hl),
                    "bundles": STAGE_BUNDLES,
                    "data": self._data_status(),
                    "optimal_tag": OPTIMAL_TAG,
                    # The accepted revision's *grid*, so the page can say which
                    # configuration the acceptance actually describes instead of
                    # hardcoding it in JS.  `/api/live/auto` reports the grid the
                    # daemon is running; these two are compared in the UI.
                    "optimal_cli": dict(OPTIMAL_CLI),
                    # Live numbers win over the recorded ones; the recorded dict is
                    # kept only as the fallback for fields with no artifact.
                    "optimal_headline": hl,
                    "baseline": base,
                    "acceptance": self._acceptance(),
                    "tags": self._tags(),
                    "lib_defaults": lib_defaults(),
                    "python": PY,
                })
            if p == "/api/runs":
                with RUNS_LOCK:
                    out = [self._run_brief(r) for r in RUNS.values()]
                out.sort(key=lambda r: r["created"], reverse=True)
                return self._json({"runs": out, "active": ACTIVE["run_id"],
                                   "queued": list(JOBQ.queue)})
            if p.startswith("/api/runs/"):
                rid = p.split("/")[3]
                with RUNS_LOCK:
                    r = RUNS.get(rid)
                if r is None:
                    return self._err(404, "no such run")
                if p.endswith("/log"):
                    return self._log_tail(r, int(q.get("offset", ["0"])[0]))
                return self._json(self._run_detail(r))
            if p.startswith("/api/chart/"):
                parts = p.split("/")
                if len(parts) != 5:
                    return self._err(400, "bad chart path")
                _, _, _, tag, name = parts
                if os.path.basename(name) != name or os.path.basename(tag) != tag:
                    return self._err(400, "bad path")
                return self._file(os.path.join(ARTIFACTS, tag, "charts", name),
                                  "image/png")
            if p.startswith("/api/equity/"):
                parts = p.split("/")
                tag = parts[3]
                if os.path.basename(tag) != tag:
                    return self._err(400, "bad tag")
                if len(parts) >= 5 and parts[4] == "detail":
                    d = read_equity_detail(tag)
                    if d is not None:
                        return self._json(d)
                    return self._err(404, "no detail for " + tag)
                e = read_equity(tag)
                if e:
                    return self._json(e)
                # `fetch` does NOT reject on 404, so a JSON error body reaches the
                # chart code as if it were data.  Say plainly whether the tag is
                # merely still running, and the client keeps the curve blank.
                pending = any(r.get("tag") == tag
                              and r.get("status") in ("queued", "running")
                              for r in RUNS.values())
                return self._err(404, "run in progress" if pending
                                 else "no equity for " + tag, pending=pending)
            if p.startswith("/api/trades/"):
                return self._trades(p, q)
            if p == "/api/auth/status":
                # 公开（白名单），前端用它决定显示"登录页"还是"首次设置页"。
                return self._auth_route(p, {})
            if p.startswith("/api/live"):
                r = live_api.route_get(p, q)
                if r is not None:
                    return self._json(r[0], r[1])
                return self._err(404, "not found")
            if p == "/api/check":
                return self._json({"warnings": []})
            return self._err(404, "not found")
        except BaseException:                                    # noqa: BLE001
            # See the note in `do_POST`: a non-`Exception` abort must still
            # produce a response, otherwise the client sees a dead connection.
            try:
                self._err(500, traceback.format_exc()[-1200:])
            except Exception:                                    # noqa: BLE001
                pass

    def do_POST(self):                                           # noqa: N802
        # ⚠️ 鉴权在**唯一的入口**做，不散在每个端点里 —— 散着做一定会漏掉一个，
        # 而漏掉的那个很可能是 `/api/live/execute`。
        if not self._gate():
            return
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._err(400, "bad json")
        try:
            if u.path.startswith("/api/auth/"):
                return self._auth_route(u.path, body)
            if u.path == "/api/check":
                return self._json({"warnings": check_traps(
                    body.get("overrides") or {}, body.get("cli") or {})})
            if u.path == "/api/run":
                return self._submit(body)
            if u.path.startswith("/api/runs/") and u.path.endswith("/cancel"):
                rid = u.path.split("/")[3]
                return self._cancel(rid)
            if u.path.startswith("/api/live"):
                r = live_api.route_post(u.path, body)
                if r is not None:
                    return self._json(r[0], r[1])
                return self._err(404, "not found")
            return self._err(404, "not found")
        except BaseException:                                    # noqa: BLE001
            # `BaseException`, not `Exception`, on purpose.  A host-level guard
            # can abort a handler with something that is not an `Exception`
            # (observed on this machine: the safe-delete bulk guard firing on
            # `shutil.rmtree`), and an *unanswered* request is indistinguishable
            # from "the server died" -- the browser sees a dropped connection
            # and there is nothing to diagnose.  A JSON 500 costs nothing.
            try:
                self._err(500, traceback.format_exc()[-1200:])
            except Exception:                                    # noqa: BLE001
                pass

    def do_DELETE(self):                                         # noqa: N802
        # ⚠️ 鉴权在**唯一的入口**做，不散在每个端点里 —— 散着做一定会漏掉一个，
        # 而漏掉的那个很可能是 `/api/live/execute`。
        if not self._gate():
            return
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/runs/"):
                rid = u.path.split("/")[3]
                return self._delete_run(rid)
            return self._err(404, "not found")
        except BaseException:                                    # noqa: BLE001
            try:
                self._err(500, traceback.format_exc()[-1200:])
            except Exception:                                    # noqa: BLE001
                pass

    # -- API pieces --------------------------------------------------------
    def _baseline(self):
        """The live headline for the optimal tag, read from its artifacts.

        Everything the UI shows about "the current optimum" must come from here.
        The console used to *also* ship a hardcoded `OPTIMAL_HEADLINE`
        (Sharpe 1.937) that no client read, while the banner hardcoded the same
        number in JS -- so after the data was rebuilt the page claimed 1.937 in
        the header and 1.758 in the yearly table.  One number, one source.
        """
        head = read_json(os.path.join(ARTIFACTS, OPTIMAL_TAG, "tables",
                                      "01_headline_metrics.json"))
        if head is None:
            return None
        return sanitize({
            "tag": OPTIMAL_TAG,
            "sharpe": head.get("Sharpe"), "cagr": head.get("CAGR"),
            "mdd": head.get("Max Drawdown"),
            "dd_days": head.get("Max DD Duration (days)"),
            "cost_drag": head.get("Cost Drag (annual)"),
            "funding": head.get("Funding P&L (total, frac)"),
            "ann_turnover": head.get("Annual Turnover"),
            "vol": head.get("Annualized Volatility"),
            "gross_avg": head.get("Gross Exposure (avg)"),
            "start": head.get("start"), "end": head.get("end"),
            "yearly": read_year_structure(OPTIMAL_TAG),
        })

    def _acceptance(self):
        """Evaluate the acceptance criteria from the artifacts, not from a string.

        `OPTIMAL_HEADLINE.acceptance` says "16/16 通过".  That was true for the
        original full-stage run; this machine only re-ran the `base` stage, so
        most of the criteria have no artifact to read and cannot be evaluated at
        all.  Reporting a remembered "16/16" next to a freshly computed baseline
        is exactly the kind of self-contradiction this console must not have, so
        the count is computed here and the missing evidence is named.
        """
        key = (OPTIMAL_TAG, _tag_stamp(OPTIMAL_TAG))
        hit = _ACCEPT_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            from crypto_ls_research.run.optimize_report import acceptance_checks
            rows = acceptance_checks(OPTIMAL_TAG)
        except Exception:
            rows = []
        gates = [r for r in rows if r.get("pass") is not None]
        out = sanitize({
            "rows": [{"criterion": r.get("criterion"), "value": r.get("value"),
                      "pass": r.get("pass")} for r in rows],
            "n_pass": sum(1 for r in gates if r.get("pass")),
            "n_gate": len(gates),
            "n_total": len(rows),
            "complete": bool(gates) and len(gates) == _ACCEPT_EXPECTED,
            "expected": _ACCEPT_EXPECTED,
        })
        _ACCEPT_CACHE.clear()
        _ACCEPT_CACHE[key] = out
        return out

    def _headline(self, base):
        """The recorded v3 headline with every live-measurable field overridden.

        `OPTIMAL_HEADLINE` in spec.py is the *historical record* of the accepted
        run (needed for fields with no artifact, and as a fallback when the tag
        has not been re-run).  Anything we can measure from `baseline` must come
        from `baseline`, otherwise the API publishes two different Sharpe ratios
        for the same configuration.
        """
        out = dict(OPTIMAL_HEADLINE)
        if base:
            for k in ("sharpe", "cagr", "mdd", "dd_days", "cost_drag",
                      "funding", "ann_turnover"):
                if base.get(k) is not None:
                    out[k] = base[k]
            out["tag"] = base.get("tag")
        out["live"] = bool(base)
        acc = self._acceptance()
        if acc and acc.get("n_gate"):
            out["acceptance"] = f"{acc['n_pass']}/{acc['n_gate']} 通过"
            if not acc.get("complete"):
                out["acceptance"] += f"（证据不全：完整需 {acc['expected']} 项）"
        out["acceptance_live"] = acc
        return sanitize(out)

    def _presets(self, headline):
        """PRESETS with the accepted entry's description filled from the live headline.

        The picker used to restate "Sharpe 1.937 / CAGR 25.09% / MDD −12.72%" while
        the header banner computed a different number from the artifacts -- the same
        page, two answers.  Copy before mutating: PRESETS is module state that
        `check_traps` and the run builder also read.
        """
        out = []
        for p in PRESETS:
            q = dict(p)
            q["cli"] = dict(p.get("cli") or {})
            q["overrides"] = dict(p.get("overrides") or {})
            if q.get("id") == "v3_optimal" and headline:
                bits = []
                if headline.get("sharpe") is not None:
                    bits.append(f"Sharpe {headline['sharpe']:.3f}")
                if headline.get("cagr") is not None:
                    bits.append(f"CAGR {headline['cagr'] * 100:.2f}%")
                if headline.get("mdd") is not None:
                    bits.append(f"MDD {headline['mdd'] * 100:.2f}%")
                if headline.get("acceptance"):
                    bits.append(f"验收 {headline['acceptance']}")
                if bits:
                    q["desc"] = "、".join(bits) + "。" + q["desc"]
                    if not headline.get("live"):
                        q["desc"] += "（未找到产物，以上为记录值）"
            out.append(q)
        return out

    def _tags(self):
        if not os.path.isdir(ARTIFACTS):
            return []
        out = []
        for n in sorted(os.listdir(ARTIFACTS)):
            d = os.path.join(ARTIFACTS, n, "tables")
            if os.path.isfile(os.path.join(d, "01_headline_metrics.json")):
                out.append(n)
        return out

    # -- readiness ---------------------------------------------------------
    def _spec_payload(self):
        """`SPEC` with the window defaults recomputed **for this request**.

        `SPEC` is module state, built once at import.  A console left running for
        a week would otherwise keep offering the date it booted with as the
        default `--end` -- which is the same failure as the hard-coded
        `2026-09-26` it replaced, only slower and harder to notice.

        Deep copy, because `check_traps`/`build_run_args` also read `SPEC`; the
        project has already been bitten once by mutating module state in place.
        """
        spec = copy.deepcopy(SPEC)

        def walk(node):
            """`SPEC` 是 {"groups": [...], "global": [...]}，字段散在两层里 ——
            按固定路径找会漏（第一版就是 `spec.get("fields")`，什么都找不到，
            于是"跟今天走"的默认值根本没生效，而且**看起来一切正常**）。"""
            if isinstance(node, dict):
                if node.get("key") == "_cli.end":
                    node["default"] = default_end()
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(spec)
        return spec

    def _data_status(self):
        """Is there anything to backtest on?

        On a fresh clone `data_cache/` is **empty** -- data is gitignored -- and
        the console used to know nothing about it: page fine, buttons fine, and
        the first sign of trouble was a `FileNotFoundError` from the run you
        just queued.  Whatever is missing, and the command that fixes it, has to
        be visible *before* the button is pressed.

        Deliberately stdlib-only: reading the last bar would need pandas, and
        this process is already the memory-sensitive one (see the OOM notes in
        MEMORY.md).  `cache_mtime` is labelled for what it is -- the newest write
        to the cache, not the last bar's timestamp.
        """
        cdir = os.path.join(CACHE_DIR, "candles", "1h")
        files = ([f for f in os.listdir(cdir) if f.endswith(".parquet")]
                 if os.path.isdir(cdir) else [])
        mtime = None
        if files:
            try:
                newest = max(files, key=lambda f: os.path.getmtime(os.path.join(cdir, f)))
                mtime = time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(os.path.getmtime(
                                          os.path.join(cdir, newest))))
            except OSError:
                mtime = None

        missing = []
        if not files:
            missing.append({
                "what": "K 线缓存（data_cache/candles/1h 是空的）",
                "fix": "./scripts/init_macos.command",
            })
        if not os.path.isfile(os.path.join(CACHE_DIR, "meta", "inst_category.json")):
            missing.append({
                "what": "资产分类快照 data_cache/meta/inst_category.json",
                "fix": "python -m crypto_ls_research.data.asset_class --build",
            })
        if not os.path.isdir(os.path.join(CACHE_DIR, "funding_hyb")) and \
                not os.path.isdir(os.path.join(CACHE_DIR, "funding")):
            missing.append({
                "what": "资金费序列（funding/ 与 funding_hyb/ 都不存在，净收益会虚高）",
                "fix": "python -m crypto_ls_research.data.funding_build",
            })

        return {
            "ok": not missing,
            "candles": len(files),
            "cache_mtime": mtime,
            "has_baseline": self._baseline() is not None,
            "missing": missing,
        }

    def _run_brief(self, r):
        return {
            "id": r["id"], "label": r.get("label"), "tag": r["tag"],
            "status": r["status"], "created": r["created"],
            "elapsed": r.get("elapsed"), "exit_code": r.get("exit_code"),
            "n_stage_errors": len(r.get("stage_errors") or []),
            "summary": r.get("summary"),
            "preset": r.get("preset"),
        }

    def _run_detail(self, r):
        d = self._run_brief(r)
        d["log"] = log_path(r["id"])
        d["argv"] = r.get("argv")
        d["overrides"] = r.get("overrides")
        d["cli"] = r.get("cli")
        d["stages_requested"] = r.get("stages")
        d["stage_errors"] = r.get("stage_errors")
        d["stage_report"] = stage_report({"log": log_path(r["id"])})
        d["result"] = collect_metrics(r)
        d["delta"] = self._delta(d["result"])
        return d

    def _delta(self, res):
        if not res:
            return None
        b = self._baseline()
        if not b:
            return None
        out = {}
        for k in ("sharpe", "cagr", "mdd"):
            if res.get(k) is not None and b.get(k) is not None:
                out[k] = res[k] - b[k]
        by = {}
        arr = (res.get("yearly") or {}).get("years") or []
        barr = {y["year"]: y["sharpe"]
                for y in ((b.get("yearly") or {}).get("years") or [])}
        for y in arr:
            if y["year"] in barr and y["sharpe"] is not None and barr[y["year"]] is not None:
                by[y["year"]] = y["sharpe"] - barr[y["year"]]
        out["yearly"] = by
        return out

    def _log_tail(self, r, offset):
        lp = log_path(r["id"])
        try:
            with open(lp, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, offset))
                txt = f.read()
                return self._json({"text": txt, "offset": f.tell(),
                                   "status": r["status"]})
        except OSError:
            return self._json({"text": "", "offset": 0, "status": r["status"]})

    def _trades(self, path, q):
        """`/api/trades/<tag>[/attribution|blotter|fills|export/<what>.csv]`."""
        parts = [x for x in path.split("/") if x]
        if len(parts) < 3:
            return self._err(400, "bad trades path")
        tag = parts[2]
        if os.path.basename(tag) != tag:
            return self._err(400, "bad tag")
        if not trades.has_result(tag):
            pending = any(r.get("tag") == tag
                          and r.get("status") in ("queued", "running")
                          for r in RUNS.values())
            return self._err(404, "no saved result for " + tag, pending=pending)

        def one(k, d=None):
            v = q.get(k)
            return v[0] if v else d

        filt = {}
        for k in ("year", "inst", "action", "side", "q", "min_notional"):
            v = one(k)
            if v not in (None, ""):
                filt[k] = v
        if one("include_dust") in ("1", "true", "True"):
            filt["include_dust"] = True

        try:
            if len(parts) == 3:
                return self._json({
                    "tag": tag,
                    "summary": trades.trade_summary(tag),
                    "options": trades.trade_options(tag),
                    "attribution": trades.trade_attribution(tag, sort=one("sort", "net")),
                })
            what = parts[3]
            if what == "attribution":
                return self._json({"tag": tag, "rows": trades.trade_attribution(
                    tag, sort=one("sort", "net"))})
            if what == "blotter":
                return self._json(trades.trade_blotter(
                    tag, page=int(one("page", 0) or 0),
                    per_page=int(one("per_page", 60) or 60), filt=filt))
            if what == "fills":
                return self._json(trades.trade_fills(
                    tag, page=int(one("page", 0) or 0),
                    per_page=int(one("per_page", 80) or 80), filt=filt))
            if what == "export" and len(parts) == 5:
                name = parts[4]
                kind = name[:-4] if name.endswith(".csv") else name
                if kind == "fills":
                    body = trades.fills_csv(tag, filt)
                elif kind == "attribution":
                    body = trades.attribution_csv(tag)
                elif kind == "blotter":
                    body = trades.blotter_csv(tag)
                else:
                    return self._err(400, "unknown export: " + kind)
                b = ("\ufeff" + body).encode("utf-8")   # BOM so Excel reads UTF-8
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{tag}_{kind}.csv"')
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return None
            return self._err(404, "not found")
        except Exception:                                        # noqa: BLE001
            return self._err(500, traceback.format_exc()[-1200:])

    def _submit(self, body):
        stages = body.get("stages") or ["base", "charts"]
        stages = [s for s in stages if s]
        if not stages or stages[0] != "base":
            stages = ["base"] + [s for s in stages if s != "base"]
        cli = dict(body.get("cli") or {})
        ov = dict(body.get("overrides") or {})
        preset = body.get("preset") or "custom"
        rid = new_run_id()
        n = 1
        while rid in RUNS:
            rid = new_run_id() + f"_{n}"
            n += 1
        # 用户可以在运行框里指定 tag（否则永远产不出 `v5_1d_all5` 这种有意义的名字）。
        # 只接受安全的标识符，且不允许覆盖盘上已有产物（`_submit` 前已有同名目录会静默复用）。
        tag = None
        raw_tag = (body.get("tag") or "").strip()
        if raw_tag:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", raw_tag):
                return self._err(400, "tag 只允许字母/数字/._- 且以字母数字开头")
            tag = raw_tag
        if not tag:
            tag = rid
        argv = build_run_args(ov, cli, stages, tag, int(body.get("n_jobs") or 4))
        run = {
            "id": rid, "tag": tag, "label": body.get("label") or preset,
            "preset": preset, "status": "queued", "created": time.time(),
            "argv": argv, "cli": cli, "overrides": ov, "stages": stages,
            "log": log_path(rid),
        }
        with RUNS_LOCK:
            RUNS[rid] = run
            write_runs(RUNS)
        JOBQ.put(rid)
        return self._json({"id": rid, "status": "queued",
                           "warnings": check_traps(ov, cli)})

    def _cancel(self, rid):
        with RUNS_LOCK:
            r = RUNS.get(rid)
            if r is None:
                return self._err(404, "no such run")
            if r["status"] == "running" and ACTIVE["run_id"] == rid and ACTIVE["proc"]:
                ACTIVE["proc"].terminate()
                r["status"] = "cancelled"
                write_runs(RUNS)
                return self._json({"id": rid, "status": "cancelled"})
            if r["status"] == "queued":
                r["status"] = "cancelled"
                write_runs(RUNS)
                return self._json({"id": rid, "status": "cancelled"})
        return self._json({"id": rid, "status": r["status"],
                           "note": "只能取消排队中或正在运行的任务"})

    def _delete_run(self, rid):
        """Delete a finished run from the list and, when safe, its artifacts.

        Only `done` / `partial` / `failed` / `cancelled` runs may be removed;
        a queued or running task must be cancelled first, otherwise the worker
        would keep writing into a directory the UI just told the user is gone.

        Artifacts are only removed when the run's tag is *owned* by this run --
        i.e. the tag directory is not also referenced by another surviving run
        and is not the accepted/optimal tag.  Two runs can share a tag (the user
        re-ran the same name), and `v3` / `v5_1d_all5` are the accepted baselines
        the rest of the console reads from, so deleting those on disk would break
        the headline.  In those cases we drop the list entry + log but leave the
        directory in place.

        The artifact directory is *renamed into `artifacts/.trash/`*, not
        `shutil.rmtree`-ed.  This machine's host installs a safe-delete guard
        (`sitecustomize.py`) that aborts `shutil.rmtree` with `SystemExit` once a
        session has deleted too many files -- and a `SystemExit` is not an
        `Exception`, so the HTTP handler's `except Exception` cannot catch it and
        the request dies mid-flight with no response.  Renaming never trips it
        (same pattern as `store.reset_mode`), and keeps the bytes recoverable
        until the user empties `.trash` by hand.
        """
        with RUNS_LOCK:
            r = RUNS.get(rid)
            if r is None:
                return self._err(404, "no such run")
            if r["status"] in ("queued", "running"):
                return self._err(409, "先取消再进行删除")
            tag = r.get("tag")
            tag_owned = bool(tag) and tag != OPTIMAL_TAG
            if tag_owned:
                # another surviving run with the same tag => dir still in use
                for other_id, other in RUNS.items():
                    if other_id != rid and other.get("tag") == tag:
                        tag_owned = False
                        break
            del RUNS[rid]
            write_runs(RUNS)
        removed_dir = None
        if tag_owned:
            d = tag_dir(tag)
            if os.path.isdir(d):
                # rename -> artifacts/.trash/<tag>__<rid>/ ; never recursive-delete
                try:
                    trash = os.path.join(ARTIFACTS, ".trash")
                    os.makedirs(trash, exist_ok=True)
                    dest = os.path.join(trash, f"{tag}__{rid}")
                    n = 1
                    while os.path.exists(dest):
                        dest = os.path.join(trash, f"{tag}__{rid}_{n}")
                        n += 1
                    os.replace(d, dest)
                    removed_dir = dest
                except BaseException:                        # noqa: BLE001
                    removed_dir = None                       # best effort, never fatal
        lp = log_path(rid)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except BaseException:                            # noqa: BLE001
                pass
        return self._json({"id": rid, "deleted": True,
                           "removed_dir": removed_dir})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    threading.Thread(target=worker, daemon=True).start()
    if os.environ.get("LS_CLEAR_INTERRUPTED"):
        with RUNS_LOCK:
            for r in RUNS.values():
                if r["status"] == "interrupted":
                    r["status"] = "failed"
            write_runs(RUNS)
    # 每日自动交易的调度器就活在这个进程里 —— 不需要外部守护进程。
    # **启动时按开关重建**：改代码重启之后，之前打开的账户仍然会自动交易。
    # 没有这一步，"重启一次就再也不自动了"而页面上开关还显示「已开启」，
    # 那是最坏的一种失败（看着在跑，其实什么都没接上）。
    try:
        # ⚠️ 必须用**下面那个 `import live_api`**，不能写 `from webapp import live_api`。
        #
        # 本文件第 33 行是 `import live_api`（因为 `webapp/` 目录本身被塞进了
        # `sys.path`），于是模块以**顶层名 `live_api`** 注册。而
        # `from webapp import live_api` 会以 `webapp.live_api` 再 import **第二份**，
        # 两份各有自己的模块级状态（`_REGISTRY` / `_JOBS` / `_ENGINES`）。
        # 结果：调度线程建在了 A 份里，而 HTTP 处理器读的是 B 份 ——
        # 日志说「已恢复 ['live']」，页面却显 `running: false`。
        # 这正是"看着在跑、其实什么都没接上"，也就是本段注释想防的那种失败。
        started = live_api.start_enabled_schedulers()
        print(f"[ok] 每日调度：已恢复 {started or '（没有账户开启）'}", flush=True)
    except Exception as e:                                        # noqa: BLE001
        # 调度器起不来不该拦住控制台：页面还要能用来手动下单与排查。
        print(f"[!!] 每日调度启动失败（控制台仍可用）：{type(e).__name__}: {e}",
              flush=True)

    host, tls_ctx = _resolve_bind(port)
    srv = ThreadingHTTPServer((host, port), Handler)
    # 处理器读它决定要不要给 Cookie 加 `Secure`（只在 TLS 下加）。
    srv.tls = tls_ctx is not None
    if tls_ctx is not None:
        srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
    scheme = "https" if srv.tls else "http"
    print(f"[ok] console on {scheme}://{host}:{port}", flush=True)
    print(f"[ok] project root: {ROOT}", flush=True)
    if host not in ("127.0.0.1", "::1"):
        print(f"[!!] 正在监听 **{host}** —— 可从本机之外访问。"
              f"确认防火墙/安全组只放行你需要的来源。", flush=True)
    srv.serve_forever()


def _resolve_bind(port: int):
    """监听地址 + TLS 上下文。**明文绑定到非本机一律拒绝启动。**

    为什么这条要硬拒、而不是打个警告
    --------------------------------
    口令和 TOTP 码都要经这条连接传。明文 HTTP 在局域网上是**直接可读**的，
    而这里登进去之后能做的是**下真钱单**。一个"点了继续"的警告框拦不住任何人，
    真正的结果是：有一天真的把口令发在明文里了。

    所以规则是：
      * 默认 `127.0.0.1`（只有本机）—— 明文可以，别人碰不到。
      * 想绑别的地址（`0.0.0.0` / 具体 IP / 域名）就必须**同时**给证书。
      * 没证书又绑外网 ⇒ **直接 sys.exit**，并说清两条出路。

    证书从环境变量给（别把私钥写进仓库）：
      LS_TLS_CERT / LS_TLS_KEY  —— PEM 路径
    """
    host = os.environ.get("LS_HOST", "127.0.0.1").strip() or "127.0.0.1"
    cert = os.environ.get("LS_TLS_CERT")
    key = os.environ.get("LS_TLS_KEY")

    is_local = host in ("127.0.0.1", "::1", "localhost")
    if is_local and not cert:
        return host, None                      # 本机明文：可以

    if not (cert and key):
        sys.exit(
            f"\n[拒绝启动] LS_HOST={host} 但没给 TLS 证书。\n\n"
            f"  口令与二重认证码会**明文**经过这条连接，而登进来能下真钱单。\n"
            f"  两条出路：\n"
            f"    1) 只有本机用 —— 不要设 LS_HOST（默认 127.0.0.1）；\n"
            f"    2) 要从外面访问 —— 生成自签证书：\n"
            f"         openssl req -x509 -newkey rsa:2048 -nodes -days 825 \\\n"
            f"           -keyout ls-key.pem -out ls-cert.pem -subj '/CN=<你的域名或IP>'\n"
            f"       然后：\n"
            f"         LS_HOST={host} LS_TLS_CERT=ls-cert.pem LS_TLS_KEY=ls-key.pem \\\n"
            f"           python -m webapp.server {port}\n\n"
            f"  ⚠️ 自签证书浏览器会报「不安全」——那是**预期**的，手工确认一次即可。\n"
            f"     想要浏览器不报警就得用真域名 + Let's Encrypt（需要公网可达）。\n")

    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2          # 1.0/1.1 已不安全
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:
        sys.exit(f"[拒绝启动] 证书加载失败（{cert} / {key}）：{e}")
    return host, ctx


if __name__ == "__main__":
    main()
