"""调度器执行完之后**必须真的推通知**。

背景（2026-10-12，用户报「自动交易进行了，但是好像没发飞书提醒」）
------------------------------------------------------------------
这是我自己引入的架构缺口，而且是**两个独立缺陷叠在一起**，任一个单独存在都会
让通知永远发不出去：

1. **没人调用。** `notify_for()` 原本只在 `auto_trader` 的循环里被调用
   （`auto_trader.py:666`）。我把「每天执行」搬进控制台（`webapp/auto_schedule.py`
   → `live_api.auto_runner`）之后，这条路经过的是 `live_api`，于是**通知完全没人发**。
   交易照常、静默无声 —— 这正是"把执行搬走、把通知留下"的典型后果。
2. **词汇不匹配。** `engine.execute()` 返回的 `action` 恒为 `"rebalance"`，
   真正区分结果的是 `stage`（`sent` / `blocked` / `no_orders` / …）。而
   `notify_on` 认的是 `traded` / `blocked` / `error`。即使补上了调用，
   不翻译也会得到「该结果不推送」。

⚠️ 值得记一笔：**第 2 条让第 1 条变得难以察觉**。用户看到的是"没有通知"，
而最自然的猜测是"通知配置坏了"—— 配置其实是好的（`ready: true`），
`--test` 也会通过。真相是这条代码路径上根本没有那段代码。

本文件把两件事都钉住：**调用存在**，且**翻译正确**。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def api(monkeypatch):
    """`live_api`，且通知器被换成不触网的替身。"""
    import webapp.live_api as m

    sent = []

    class FakeNotifier:
        provider = "fake"

        def describe(self):
            return {"enabled": True, "provider": "fake", "url": "x",
                    "notify_on": ["traded", "blocked", "error"],
                    "modes": ["demo", "live"], "dedup_sec": 0.0,
                    "secret_set": False, "ready": True, "config_path": "x"}

        def enabled_for(self, action, mode=None):
            return action in ("traded", "blocked", "error")

        def send(self, title, text, *, key="", action=""):
            sent.append({"title": title, "text": text,
                         "key": key, "action": action})
            return {"ok": True, "status": 200, "provider": "fake"}

    monkeypatch.setattr(m, "_notifier_now", lambda mode: FakeNotifier())
    m._NOTIFIER_CACHE.clear()
    return m, sent


# ---------------------------------------------------------------------------
# 1. 翻译正确（缺陷 2）
# ---------------------------------------------------------------------------
def test_sent_is_translated_to_traded_and_pushed(api):
    """`stage="sent"` ⇒ 推一条，且归入 `traded`。

    这是用户实际遇到的那条：今天 11:00 真的下了 15 笔，但 `notify=None`。
    """
    m, sent = api
    r = m._notify_result("live", {"stage": "sent", "n_orders": 15,
                                  "n_ok": 15, "n_fail": 0})
    assert r.get("ok"), f"正常成交没有推送：{r}"
    assert len(sent) == 1
    assert sent[0]["action"] == "traded", (
        f"应归入 traded，实际 {sent[0]['action']!r}")


def test_blocked_is_translated_to_blocked(api):
    """风控拦截必须推，且把拦截原因写进正文。

    只写"被拦截"而不说被什么拦的，收到的人还得回来查日志 —— 通知就没意义了。
    """
    m, sent = api
    # 用 `engine.execute()` 的真实形状：拦截项在 `violations` 里、`sev="block"`。
    r = m._notify_result("live", {
        "stage": "blocked",
        "violations": [
            {"sev": "block", "key": "turnover", "title": "单次换手超上限"},
            {"sev": "block", "key": "gross", "title": "总敞口超上限"},
            {"sev": "warn", "key": "live_mode", "title": "实盘模式"},
        ],
        "errors": ["单次换手超上限"],
    })
    assert r.get("ok"), f"拦截没有推送：{r}"
    assert sent[0]["action"] == "blocked"
    assert "换手" in sent[0]["text"], f"正文里没有拦截原因：{sent[0]['text']!r}"
    # warn 不该混进拦截原因里 —— 否则"为什么被拦"会被非拦截项冲淡
    assert "实盘模式" not in sent[0]["text"], (
        f"warn 级别的条目混进了拦截原因：{sent[0]['text']!r}")


def test_no_orders_does_not_claim_a_trade(api):
    """`no_orders` ⇒ **不推**。

    推一条"已调仓 0 笔"会让人以为出了事。它与 `dry_run` 一样属于
    "刻意不推"的那一类（`notify_on` 里没有 `noop`）。
    """
    m, sent = api
    r = m._notify_result("live", {"stage": "no_orders", "n_orders": 0})
    assert not r.get("ok"), "0 笔不该推送"
    assert sent == [], "0 笔却发了消息"


def test_an_empty_stage_is_treated_as_error(api):
    """缺 `stage`（结果结构异常）⇒ 归为 `error` 并推送。

    没有 stage 意味着"我不知道这次发生了什么" —— 那必须吵，不能静。
    """
    m, sent = api
    r = m._notify_result("live", {})
    assert r.get("ok"), f"缺 stage 应归为 error 并推送，实际 {r}"
    assert sent[0]["action"] == "error"


def test_every_stage_engine_can_emit_is_mapped(api):
    """`engine._result` 能产出的每个 stage 都要在表里。

    这条是防"将来有人加了个 stage 忘了加映射"——那种漏会**静默**变成
    `error`（还算好）或漏推（更糟）。这里把 engine 的取值抓出来对比，
    而不是在测试里再抄一份 stage 清单。
    """
    import re
    m, _ = api
    src = open(os.path.join(ROOT, "crypto_ls_research", "execution",
                            "engine.py"), encoding="utf-8").read()
    stages = set(re.findall(r'stage="([a-z_]+)"', src))
    assert stages, "没找到任何 stage=（engine.py 结构变了？）"
    missing = stages - set(m._STAGE_TO_ACTION)
    assert not missing, (
        f"engine 会产出这些 stage 但没映射：{sorted(missing)} —— "
        f"它们会被当成 error 或漏推")


# ---------------------------------------------------------------------------
# 2. 调用存在（缺陷 1）—— 这条才是根因
# ---------------------------------------------------------------------------
def test_auto_runner_actually_notifies(api, monkeypatch):
    """**`auto_runner` 必须调用通知**，不是"通知模块存在"。

    根因就是这条断言缺了：执行搬进控制台之后，没有任何测试要求这条路也发通知。
    一个只测 `notify_for()` 本身的测试会全绿，而生产上一条都发不出去。
    """
    m, sent = api
    submitted = []

    monkeypatch.setattr(m, "_submit",
                        lambda kind, mode, runner, label="", net_of=None:
                        (submitted.append(kind), {"id": f"j{len(submitted)}"})[1])

    def fake_wait(jid, timeout=0):
        if jid == "j1":                       # 刷新
            return {"status": "done", "result": {}}
        return {"status": "done", "result": {"stage": "sent", "n_orders": 15}}

    monkeypatch.setattr(m, "_wait_job", fake_wait)

    m.auto_runner("live")
    assert submitted == ["refresh", "auto"], f"提交序列不对：{submitted}"
    assert len(sent) == 1, (
        f"auto_runner 没有发通知（sent={sent}）—— 这正是 2026-10-12 的 bug")


def test_a_failed_job_does_not_notify_a_trade(api, monkeypatch):
    """作业失败 ⇒ 不推"已成交"。

    失败会有别的通道（心跳/日志）；把失败推成"交易完成"是最坏的一种错报。
    """
    m, sent = api
    monkeypatch.setattr(m, "_submit", lambda *a, **k: {"id": "j1"})
    monkeypatch.setattr(m, "_wait_job",
                        lambda jid, timeout=0: {"status": "failed",
                                                "error": "boom"})
    m.auto_runner("live")
    assert sent == [], f"作业失败却推了通知：{sent}"


def test_notification_failure_does_not_break_the_run(api, monkeypatch):
    """通知抛错绝不能影响交易 —— 与 `Notifier.send()` 同一条纪律。"""
    m, _ = api

    def boom(mode, result):
        raise RuntimeError("通知通道炸了")

    monkeypatch.setattr(m, "_notify_result", boom)
    monkeypatch.setattr(m, "_submit", lambda *a, **k: {"id": "j1"})
    monkeypatch.setattr(m, "_wait_job",
                        lambda jid, timeout=0: {"status": "done",
                                                "result": {"stage": "sent"}})
    # 不抛异常就是通过
    out = m.auto_runner("live")
    assert out == {"id": "j1"}


# ---------------------------------------------------------------------------
# 3. 配置热读（配了 webhook 之后不重启也应生效）
# ---------------------------------------------------------------------------
def test_notifier_is_re_read_when_the_config_changes(monkeypatch):
    """`config/notify.json` 改了要自动生效，不用重启控制台。

    控制台同样会跑很久。有人"先启动、后配 webhook"时必须自动接上，
    否则 `--test`（新进程）通过、而常驻的这条静默不发 —— 看起来像"策略没动"。
    """
    # ⚠️ 不能用 `api` fixture —— 它已经把 `_notifier_now` 整个换成替身了，
    # 那样测的是替身、不是真实的缓存逻辑（第一版就是这么写的，断言恒假）。
    import webapp.live_api as m
    import crypto_ls_research.execution.notify as nf

    calls = []
    real = nf.Notifier.from_config

    def spy(mode=None, log=None):
        calls.append(mode)
        return real(mode=mode)

    monkeypatch.setattr(nf.Notifier, "from_config", staticmethod(spy))
    m._NOTIFIER_CACHE.clear()

    m._notifier_now("live")
    assert len(calls) == 1, "首次应构造通知器"
    m._notifier_now("live")          # 配置没变 ⇒ 走缓存
    assert len(calls) == 1, "配置没变却重建了通知器"

    p = nf.CONFIG_PATH
    if os.path.exists(p):
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 10))
        try:
            m._notifier_now("live")
        finally:
            os.utime(p, (st.st_atime, st.st_mtime))
        assert len(calls) == 2, "配置改了却仍用旧的通知器（配了 webhook 不生效）"

# ---------------------------------------------------------------------------
# 4. 模块重复加载（这条让上面所有断言在生产上都失效）
# ---------------------------------------------------------------------------
def test_live_api_state_is_consistent_under_the_package_path():
    """`live_api` 的模块级状态在**包路径**下必须唯一。

    背景（2026-10-12 实测踩到）：`webapp/server.py` 把 `webapp/` 塞进了 `sys.path`，
    于是 `import live_api`（顶层名）与 `from webapp import live_api`（包名）会加载
    **两份不同的模块**，各有自己的 `_REGISTRY` / `_JOBS` / `_ENGINES`。
    后果是最难发现的那种：调度线程建在 A 份、HTTP 处理器读 B 份 ——
    启动日志显示「已恢复 ['live']」，页面却显示 `running: false`。

    这里钉住的是**修好之后的不变量**：在 server.py 用的那条路径（包路径）下，
    `live_api` 与它依赖的 `auto_schedule` 都只有一份。
    """
    import importlib
    pkg = importlib.import_module("webapp.live_api")
    assert pkg.auto_schedule is importlib.import_module("webapp.auto_schedule"), (
        "live_api 里的 auto_schedule 与 webapp.auto_schedule 不是同一份 —— "
        "两个 Registry 会各自持有调度线程，页面读到的那个可能永远没线程")
    # 同一个 registry 实例 → 线程状态可被 HTTP 处理器看到
    assert isinstance(pkg._REGISTRY, pkg.auto_schedule.Registry)


def test_server_does_not_import_live_api_the_other_way():
    """`server.py` 里不许出现 `from webapp import live_api`。

    上面那条断言"当前是否只有一份"；这条盯住**成因** —— 哪天有人把那行写回来，
    这条会红，而不是等到某个状态"偶发不同步"再回来查。
    """
    import re
    p = os.path.join(ROOT, "webapp", "server.py")
    src = open(p, encoding="utf-8").read()
    # ⚠️ 只匹配**真正生效的代码行**：注释里会引用这个写法（本文件就在注释里提了
    # 好几次它为什么是错的），扫全文件会把那些说明文字也当成违规 —— 第一版就是
    # 这样，报出 4 处"违规"全是我自己的注释。
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    # 只许用包路径导入（`from webapp import live_api`）。`import live_api` 会以
    # 顶层名再注册一份，与包路径那份**状态不共享**。
    bad = [ln for ln in code if re.match(r"^\s*import\s+live_api\s*$", ln)]
    assert not bad, (
        f"server.py 里有 {len(bad)} 处顶层 `import live_api` —— 那会加载第二份模块，"
        f"模块级状态（_REGISTRY/_JOBS）会断开：{bad}")
    assert "from webapp import live_api" in src, (
        "server.py 应显式用包路径导入 live_api")

# ---------------------------------------------------------------------------
# 5. 通知绝不能依赖**环境里的代理**
# ---------------------------------------------------------------------------
def test_notify_bypasses_a_dead_env_proxy(monkeypatch):
    """🔴 通知的 `urlopen` 必须**绕开 `HTTP_PROXY`**。

    背景（2026-10-13 实测，代价是一次漏发的调仓通知）：控制台是从带沙箱代理的
    shell 起的，继承了 `HTTP_PROXY=http://127.0.0.1:<随机端口>`。那代理**只在工具
    调用期间存活**，到 11:00 定时执行时端口已关 ⇒ `urlopen` 报
    `URLError: [Errno 61] Connection refused` ⇒ 订单照下（走交易所 SDK，不经这里），
    通知静默丢失。

    这里不真的发网：把 opener 换成一个"若被要求走代理就报错"的替身，
    断言它**没有被要求走代理**。
    """
    import crypto_ls_research.execution.notify as nf

    seen = {}

    class Recorder:
        def open(self, req, timeout=None):
            # 真正关心的是：请求有没有带上代理配置。`ProxyHandler({})` 的 opener
            # 不会为 https 建 tunnel，所以只要它被用来发请求，就说明没走代理。
            seen["called"] = True
            seen["url"] = getattr(req, "full_url", str(req))

            class R:
                status = 200

                def read(self, n=None):
                    return b'{"code":0}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    monkeypatch.setattr(nf, "_OPENER", Recorder())
    # 故意放一个必然连不上的代理，证明它**不会**被用到
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    st, body = nf._post_json("https://open.feishu.cn/x", {"a": 1}, 1.0)
    assert seen.get("called"), "没有走到我们装的 opener"
    assert st == 200


def test_the_default_opener_ignores_proxy_handlers():
    """`_OPENER` 必须是「空 `ProxyHandler`」构建的。

    上一条把 `_OPENER` 换掉了，所以它证明的是"调用点用 `_OPENER`"；
    这一条证明 **`_OPENER` 本身**不含代理处理器 —— 两条合起来才闭环。
    """
    import urllib.request as ur
    import crypto_ls_research.execution.notify as nf

    # ⚠️ `build_opener(ProxyHandler({}))` 里那个**空**的 ProxyHandler 会被
    # `build_opener` **自动扔掉**（实测：handlers 里根本没有 ProxyHandler）。
    # 所以正确的断言是「没有 ProxyHandler」，而不是「有一个空的」——
    # 第一版就是这么写错的，被这条测试自己抓出来。
    #
    # 对照：默认的 `build_opener()` **会**装一个 ProxyHandler（它读环境变量）。
    default_has = any(isinstance(h, ur.ProxyHandler) for h in ur.build_opener().handlers)
    ours_has = any(isinstance(h, ur.ProxyHandler) for h in nf._OPENER.handlers)
    assert default_has, "前提变了：默认 opener 现在不含 ProxyHandler（那这条测试没意义）"
    assert not ours_has, (
        f"`_OPENER` 含 ProxyHandler ⇒ 会读 HTTP_PROXY，而那个代理只在工具调用期间"
        f"存活 ⇒ 11:00 定时必然 Connection refused。handlers="
        f"{[type(h).__name__ for h in nf._OPENER.handlers]}")


def test_notify_result_is_written_back_to_the_audit(api, monkeypatch):
    """通知结果要落回 `runs.jsonl` 的那一行。

    2026-10-13：用户问「下单后还是没发飞书」，而 `runs.jsonl` 里**连一个 `notify`
    字段都没有** —— 只能去 grep 控制台日志才发现 `Errno 61`。那条信息本该和这次
    调仓记在一起，否则"它告诉我了吗"永远要翻日志。
    """
    import json as _json
    import tempfile
    import crypto_ls_research.execution.store as store_mod

    m, sent = api
    with tempfile.TemporaryDirectory() as d:
        # 用临时目录，别动真实 artifacts
        class _St:
            def __init__(self):
                self.dir = d

            def path(self, name):
                return os.path.join(d, name)

            def runs(self, limit=None):
                return [
                    {"run_id": "r2", "ts": 2, "action": "rebalance"},
                    {"run_id": "r1", "ts": 1, "action": "rebalance"},
                ]

        st = _St()
        monkeypatch.setattr(m, "Store", lambda mode: st)
        # ⚠️ 走**真实入口** `_notify_result`，不要直接调 `_record_notify`。
        # 第一版就是直接调的，于是变异「把 `_notify_result` 里那行写回删掉」
        # **完全没被发现** —— 测的是那个函数自己能用，而不是它真的被调用了。
        # （变异测试当场抓出这条假守卫。）
        m._notify_result("live", {"run_id": "r1", "stage": "sent",
                                  "n_orders": 1, "n_ok": 1, "n_fail": 0})

        rows = [_json.loads(l) for l in open(os.path.join(d, "runs.jsonl")) if l.strip()]
        # 顺序必须保持（文件是时间序，最新在前读、写回要还原成时间序）
        assert [r["run_id"] for r in rows] == ["r1", "r2"], (
            f"写回把顺序搞乱了：{[r['run_id'] for r in rows]}")
        got = next(r for r in rows if r["run_id"] == "r1")
        assert got["notify"]["ok"] is True
        assert got["notify"]["action"] == "traded"
        # 没被通知的那条不该被塞字段
        other = next(r for r in rows if r["run_id"] == "r2")
        assert "notify" not in other
