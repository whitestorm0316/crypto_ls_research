"""Local web console for the liquidity-flow trend strategy.

Standard library only (no Flask on this machine).  Serves a single-page UI and a
small JSON API, and owns a one-at-a-time backtest queue so that concurrent
submissions cannot fight over the same 6 cores.

Run:  python webapp/server.py 8770
"""
from __future__ import annotations

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
import trades  # noqa: E402
import live_api  # noqa: E402

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
class Handler(BaseHTTPRequestHandler):
    server_version = "ls-console/1.0"

    def log_message(self, *a):                                   # noqa: D102
        pass

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(sanitize(obj), ensure_ascii=False,
                                    allow_nan=False).encode("utf-8"))

    def _err(self, code, msg, **extra):
        self._json({"error": msg, **extra}, code)

    def _file(self, path, ctype="application/octet-stream"):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._err(404, "not found")

    def do_GET(self):                                            # noqa: N802
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
                         "html": "text/html; charset=utf-8"}.get(
                    name.rsplit(".", 1)[-1], "application/octet-stream")
                return self._file(os.path.join(STATIC, name), ctype)
            if p == "/api/spec":
                base = self._baseline()
                hl = self._headline(base)
                return self._json({
                    "spec": SPEC, "presets": self._presets(hl),
                    "bundles": STAGE_BUNDLES,
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
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._err(400, "bad json")
        try:
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[ok] console on http://127.0.0.1:{port}", flush=True)
    print(f"[ok] project root: {ROOT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
