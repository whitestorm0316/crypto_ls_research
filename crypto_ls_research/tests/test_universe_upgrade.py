"""宇宙补齐（universe_upgrade）的守卫测试。

「补齐宇宙」是**有副作用**的按钮：重建候选池（不带 --reuse-pool）→ 分类快照
→ 规格快照 → funding。这里用注入的 runner / specs_fn / drift_fn 把四步流程
钉死，不碰真网络/真子进程：

* **argv 的相邻对断言**：download 那步必须**有** `--incremental`/`--end`、
  **没有** `--reuse-pool`——只断言「跑了 download」是抓不到把冻结池语义
  写错的（那正是这个按钮存在的意义）。
* 失败即停：哪一步断了就停在哪，error 写明步骤，后续步骤**不跑**。
* 补齐成功但复查体检失败（网络抖动）不算失败——补齐成果不能被复查吞掉。
* `funding_build` 的 `--end` 默认值必须是 `default_end()`（它曾是硬编码的
  验收窗口上界 `2026-09-26`，与 download/research 同族的静默截断坑）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from crypto_ls_research.execution.universe_upgrade import upgrade_universe

ROOT = Path(__file__).resolve().parents[2]


def make_drift(n_new=6, n_dead=0, n_unclassified=0, n_missing_specs=2):
    def drift(**kw):
        return {"ok": True, "summary": "…", "stale": False, "from_cache": False,
                "n_new": n_new, "n_new_above_cut": n_new, "n_dead": n_dead,
                "n_unclassified": n_unclassified,
                "n_missing_specs": n_missing_specs,
                "new": [], "dead": [], "unclassified": [], "missing_specs": []}
    return drift


class FakeRunner:
    """按 label 记录 argv；可按序号注入失败。"""

    def __init__(self, fail_at=(), fail_rc=1):
        self.argvs = []
        self._fail_at = set(fail_at)
        self._fail_rc = fail_rc

    def __call__(self, argv, timeout, progress):
        self.argvs.append(list(argv))
        idx = len(self.argvs) - 1
        if idx in self._fail_at:
            return self._fail_rc, "boom at step"
        return 0, "ok-tail"


# ---------------------------------------------------------------------------
# 流程与 argv
# ---------------------------------------------------------------------------
def test_happy_path_runs_all_four_steps_in_order():
    run = FakeRunner()
    specs = []
    drift_args = []

    def recording_drift(**kw):
        drift_args.append(kw)
        return make_drift()(**kw)

    r = upgrade_universe(runner=run, specs_fn=lambda: (specs.append(1) or 477),
                         drift_fn=recording_drift)
    assert r["ok"] is True and r["error"] is None
    assert [s["label"] for s in r["steps"]] == [
        "① 重建候选池并增量下载 K 线 + OKX 资金费率",
        "② 重建合约分类快照",
        "③ 刷新合约规格快照",
        "④ 重建拼接资金费率（funding_hyb）"]
    assert specs == [1]
    assert len(drift_args) == 2 and all(a.get("force") for a in drift_args)


def test_download_step_never_reuses_the_pool():
    """🔴 全部守卫里最重要的一条：补齐 = 重建池，绝不能带 --reuse-pool。"""
    run = FakeRunner()
    upgrade_universe(runner=run, specs_fn=lambda: 1, drift_fn=make_drift())
    dl = run.argvs[0]
    assert "crypto_ls_research.data.download" in " ".join(dl)
    assert "--reuse-pool" not in dl, "补齐宇宙若复用冻结池，新合约永远进不来"
    # 老合约只补尾巴，别全量重拉
    assert "--incremental" in dl
    assert "--end" in dl
    j = " ".join(dl)
    assert "--bars 1h 15m 5m" in j         # 流水线三档一起补


def test_snapshot_and_funding_steps_have_their_guards():
    run = FakeRunner()
    upgrade_universe(runner=run, specs_fn=lambda: 1, drift_fn=make_drift())
    ac = " ".join(run.argvs[1])
    assert "crypto_ls_research.data.asset_class" in ac and "--build" in ac
    fu = " ".join(run.argvs[2])
    assert "crypto_ls_research.data.funding_build" in fu and "--end" in fu


def test_end_defaults_to_tomorrow_utc():
    import time as _t
    tomorrow = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 86400))
    run = FakeRunner()
    upgrade_universe(runner=run, specs_fn=lambda: 1, drift_fn=make_drift())
    dl = run.argvs[0]
    assert dl[dl.index("--end") + 1] == tomorrow


# ---------------------------------------------------------------------------
# 失败即停
# ---------------------------------------------------------------------------
def test_failure_stops_the_pipeline_and_names_the_step():
    run = FakeRunner(fail_at={1})              # ② 分类快照失败
    r = upgrade_universe(runner=run, specs_fn=lambda: 1, drift_fn=make_drift())
    assert r["ok"] is False
    assert len(run.argvs) == 2                 # ③④ 没跑
    assert "重建合约分类快照" in r["error"] and "exit=1" in r["error"]
    assert r["steps"][-1]["tail"] == "boom at step"


def test_specs_failure_stops_before_funding():
    run = FakeRunner()
    def boom():
        raise RuntimeError("specs file locked")
    r = upgrade_universe(runner=run, specs_fn=boom, drift_fn=make_drift())
    assert r["ok"] is False
    assert len(run.argvs) == 2                 # funding 没跑
    assert "刷新合约规格快照" in r["error"] and "RuntimeError" in r["steps"][-1]["tail"]


def test_after_drift_failure_does_not_sink_a_successful_upgrade():
    """补齐全成功、复查体检却网络失败 ⇒ ok 仍是 True（成果不被复查吞掉）。"""
    run = FakeRunner()
    calls = {"n": 0}
    def flaky_drift(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return make_drift()(**kw)
        return {"ok": False, "error": "RuntimeError: network down"}
    r = upgrade_universe(runner=run, specs_fn=lambda: 1, drift_fn=flaky_drift)
    assert r["ok"] is True and r["error"] is None
    assert r["after"]["ok"] is False


# ---------------------------------------------------------------------------
# funding_build 的 --end 坑（Q58 同族：曾被硬编码为验收窗口上界）
# ---------------------------------------------------------------------------
def test_funding_build_end_defaults_to_default_end():
    src = (ROOT / "crypto_ls_research" / "data" / "funding_build.py").read_text()
    assert "from ..config.settings import default_end" in src
    # 只看非 # 行——自己文档字符串里引用的旧字面量不算违规
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith(("#", '"', "*")))
    assert '"2026-09-26"' not in code, "funding_build 仍硬编码验收窗口上界"
    # build() 与 CLI 两层都必须接住显式 --end
    assert "end = end or default_end()" in src
    assert 'ap.add_argument("--end"' in src and "end=a.end" in src


def test_funding_build_help_offers_end_flag():
    """行为级：CLI 真的暴露 --end（防上面静态断言和 argparse 脱节）。"""
    py = sys.executable
    r = subprocess.run([py, "-m", "crypto_ls_research.data.funding_build", "--help"],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(ROOT))
    assert r.returncode == 0
    assert "--end" in r.stdout


# ---------------------------------------------------------------------------
# API / 前端接线（前端真渲染部分在 test_universe_drift.py 的 node harness）
# ---------------------------------------------------------------------------
def test_route_wired():
    src = (ROOT / "webapp" / "live_api.py").read_text(encoding="utf-8")
    i = src.find('"/api/live/universe/upgrade"')
    assert i > 0, "补齐宇宙路由不存在"
    body = src[i:src.find("if path ==", i)]
    assert "upgrade_universe(progress=log)" in body
    assert '_submit("universe-upgrade"' in body, "必须走任务队列（要流式日志）"


def test_frontend_binding_wired():
    import re
    src = (ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    i = src.find("const up = $('#liveDriftUpgrade');")
    assert i > 0, "liveBind 里没有补齐按钮的绑定"
    body = src[i:src.find("\n  }", i)]
    # 相邻对：confirm 的结果必须真的在**门控** liveSubmit——只查「包含
    # window.confirm」抓不到「!false && confirm(...)」这类旁路（变异当场抓过）。
    # confirm 的文案字符串会跨行，用 [\s\S]*? 而非 [^\n]*。
    assert re.search(r"if \(!window\.confirm\([\s\S]*?\)\) return;", body), \
        "确认框的结果没有 return 门控：确认被旁路了"
    assert "liveSubmit('/api/live/universe/upgrade'" in body


# ---------------------------------------------------------------------------
# 子进程输出必须**边跑边吐**（2026-10-28：用户报「补齐宇宙几十分钟还没好」）
#
# 根因不是任务坏了，而是 `_default_runner` 当时用 `subprocess.run(capture_output=True)`
# ——子进程 stdout 被整段缓冲到结束才返回。① 下载（~30 分钟）和 ④ funding（~15 分钟）
# 恰恰是最长的两步 ⇒ 界面上只有一行「…」，和卡死长得一模一样。
#
# 这一组守卫钉死三件事：① 有输出立刻转发（且强制子进程无缓冲）；② 没输出时发心跳；
# ③ 超时真的杀进程（不然「卡死」会一直挂着）。
# ---------------------------------------------------------------------------
import ast
import os
import time

import pytest

from crypto_ls_research.execution import universe_upgrade as U


def _run(code: str, timeout: float, sink):
    return U._default_runner([sys.executable, "-c", code], timeout, sink)


def test_child_output_is_forwarded_before_the_child_exits():
    """🔴 第一行必须在子进程**结束之前**到达 progress。

    子进程用**不带 flush 的 print**，靠 `PYTHONUNBUFFERED=1` 立即吐出——
    少了那个环境变量，print 会攒满缓冲区，逐行转发就退化回「最后一次性倒出来」。
    """
    seen, stamps = [], []

    def sink(msg):
        seen.append(msg)
        stamps.append(time.time())

    code = ("import time\n"
            "print('first-line')\n"          # 故意不 flush
            "time.sleep(2.0)\n"
            "print('second-line')\n")
    rc, _ = _run(code, 30, sink)
    t_end = time.time()
    assert rc == 0
    assert any("first-line" in m for m in seen), f"第一行没被转发：{seen}"
    assert stamps[0] < t_end - 1.0, (
        "第一行是等到子进程结束才到达的 —— 说明仍在整段缓冲（capture_output 的回潮）")


def test_silent_child_gets_a_heartbeat(monkeypatch):
    """没有任何输出的步骤（比如分类快照）也必须自证「还活着」。"""
    monkeypatch.setattr(U, "HEARTBEAT_EVERY", 0.4)
    seen = []
    rc, _ = _run("import time; time.sleep(1.6)", 30, seen.append)
    assert rc == 0
    assert any("运行中" in m for m in seen), f"没有心跳：{seen}"


def test_timeout_kills_the_child_instead_of_hanging(tmp_path):
    """超时必须**真的把子进程杀掉**。

    只断言「抛了 TimeoutExpired」是不够的：把 `proc.kill()` 换成 `pass` 变异后
    异常照样抛出来、测试照样绿，而子进程会一直在后台跑（变异当场抓过）。
    所以让子进程先写下自己的 pid，事后确认它已经不存在（僵尸也不行 ——
    那说明没 wait，父进程退出后它会被 init 收走、但在本进程里仍是残留）。
    """
    pidfile = tmp_path / "child.pid"
    code = ("import os, time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n")
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        _run(code, 1.5, lambda m: None)
    assert time.time() - t0 < 15, "超时后没有及时返回"
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return                       # 已死且已被回收 ⇒ 正确
        time.sleep(0.1)
    pytest.fail(f"超时后子进程 {pid} 仍存在（kill/wait 没执行）")


def test_failure_returns_exit_code_and_tail():
    code = "for i in range(12): print('line-%02d' % i)\nraise SystemExit(3)\n"
    rc, tail = _run(code, 30, lambda m: None)
    assert rc == 3
    assert "line-11" in tail, f"失败回报里没有尾部输出：{tail!r}"


def test_runner_never_buffers_the_whole_child(  ):
    """静态：函数体里不许再出现 `subprocess.run(...)`（用 ast 跳过文档字符串）。"""
    src = (ROOT / "crypto_ls_research" / "execution" / "universe_upgrade.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_default_runner")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    names = []
    for c in calls:
        f = c.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            names.append(f"{f.value.id}.{f.attr}")
        elif isinstance(f, ast.Name):
            names.append(f.id)
    assert "subprocess.run" not in names, \
        "又用回 subprocess.run（整段缓冲 ⇒ 长步骤看起来像卡死）"
    assert "subprocess.Popen" in names
    assert "PYTHONUNBUFFERED" in src


def test_each_step_announces_its_expected_duration():
    """长步骤必须先说清「大概多久」——否则用户只能猜它是不是死了。"""
    msgs = []
    upgrade_universe(runner=FakeRunner(), specs_fn=lambda: 1,
                     drift_fn=make_drift(), progress=msgs.append)
    for label in ("①", "④"):
        i = next(k for k, m in enumerate(msgs) if m.startswith(label))
        assert "分钟" in msgs[i + 1], f"{label} 步没有时长提示：{msgs[i:i + 2]}"
