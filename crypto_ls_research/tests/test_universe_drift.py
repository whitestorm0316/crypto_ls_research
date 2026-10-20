"""宇宙体检（universe_drift）的守卫测试。

三组：
1. 模块行为——用注入的 fake client，不碰真网络/真盘。覆盖：池规则镜像
   （top-120 ADV 线）、退市残留 / 缺分类 / 缺规格三计数互不串、TTL 命中
   不打网络、force 强制重测、网络失败绝不抛 + 退避期内不重试 + stale 兜底。
2. API 接线——`/api/live/status` 与 `/api/live/plan` 必须真的带上
   `universe_drift`（相邻对断言：既验挂载、又验它挂在正确的路由上下文里）。
3. 前端真渲染——用 node vm 加载**真实** app.js（stub 同 scripts/smoke_ui.js），
   渲染 `liveDriftHtml()` 三种状态并断言 div 配平。grep 通过但页面空白的教训
   见 test_auth；这里不允许只靠字符串存在性放行。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crypto_ls_research.execution import universe_drift
from crypto_ls_research.execution.universe_drift import (
    RETRY_SECONDS, TTL_SECONDS, drift_report,
)

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# fake OKX：build_candidate_pool 只需要 `.instruments("SWAP")` / `.tickers("SWAP")`
# ---------------------------------------------------------------------------
T0_MS = 1_591_000_000_000        # 2020-05-22 附近 —— 早于 2022-01-01（early 池）
T1_MS = 1_767_225_600_000        # 2026-01-01

EARLY = "AAA-USDT-SWAP"          # 早期上市、成交额很小
TOP1 = "BBB-USDT-SWAP"           # 新上市、ADV 50M
TOP2 = "CCC-USDT-SWAP"           # 新上市、ADV 20M（= top-120 的 ADV 线）
DEAD = "DDD-USDT-SWAP"           # 只在盘上 —— 退市残留
UNCL = "EEE-USDT-SWAP"           # 盘上有、分类快照没有 —— SystemExit 陷阱
NOSP = "FFF-USDT-SWAP"           # 盘上有、规格快照没有 —— planner 会跳过


def _inst(inst: str, listed_ms: int, state: str = "live") -> dict:
    # 列名是 build_candidate_pool 选列的**硬依赖**（ctVal/ctValCcy/tickSz 也要）——
    # 缺一列它就 KeyError，而这正是 fake 数据最容易漏的地方。
    return {"instId": inst, "settleCcy": "USDT", "state": state,
            "listTime": str(listed_ms), "ctVal": "1", "ctValCcy": inst.split("-")[0],
            "tickSz": "0.01"}


def _tk(inst: str, vol_ccy: str, last: str) -> dict:
    return {"instId": inst, "volCcy24h": vol_ccy, "last": last,
            "bidPx": "1", "askPx": "1"}


class FakeClient:
    """记录调用次数——「没打网络」靠 calls 为空来断言，不靠秒表。"""

    def __init__(self, instruments=None, tickers=None, boom: bool = False):
        self._instruments = instruments or []
        self._tickers = tickers or []
        self._boom = boom
        self.calls: list = []

    def instruments(self, inst_type: str = "SWAP") -> list:
        self.calls.append(("instruments", inst_type))
        if self._boom:
            raise RuntimeError("network is down")
        return self._instruments

    def tickers(self, inst_type: str = "SWAP") -> list:
        self.calls.append(("tickers", inst_type))
        if self._boom:
            raise RuntimeError("network is down")
        return self._tickers


FILLERS = [f"F{i:03d}-USDT-SWAP" for i in range(120)]


def make_good_client() -> FakeClient:
    # 120 只 filler：让 top-120 的语义真实化——否则 EARLY（ADV 只有 20）也挤进
    # top-120，把「ADV 线」拉低到它的成交额，fixture 就测不出真实意图。
    fillers = [_inst(f"F{i:03d}-USDT-SWAP", T1_MS) for i in range(120)]
    filler_tk = [_tk(f"F{i:03d}-USDT-SWAP", str(19_000_000 - i * 100_000), "1")
                 for i in range(120)]
    return FakeClient(
        instruments=[_inst(EARLY, T0_MS), _inst(TOP1, T1_MS), _inst(TOP2, T1_MS),
                     # UNCL/NOSP 在交易所在场（只是快照缺）——但 ADV 低于线，
                     # 不在 top-120，也不算「新达标」，免得污染别的计数。
                     _inst(UNCL, T1_MS), _inst(NOSP, T1_MS)]
        + fillers,
        tickers=[_tk(EARLY, "10", "2"),               # ADV 20
                 _tk(TOP1, "50000", "1000"),           # ADV 50M
                 _tk(TOP2, "20000", "1000"),           # ADV 20M
                 _tk(UNCL, "1000", "1000"),            # ADV 1M
                 _tk(NOSP, "2000", "1000")]            # ADV 2M
        + filler_tk)


@pytest.fixture
def tmp_cache(tmp_path) -> str:
    return str(tmp_path / "cache")


def base_kwargs(cli, cache_dir: str, now: float) -> dict:
    # 盘上 = 冻结池（EARLY + 全部 filler），即「上次刷新已经把它们都下下来了」，
    # 分类/规格快照也覆盖到它们（快照是跟着冻结池走的）。于是唯一的漂移就是
    # 新上市又达标的 TOP1/TOP2 —— fixture 意图单一。
    return dict(cli=cli, cached=[EARLY] + FILLERS,
                categories={**{i: "crypto" for i in FILLERS}, EARLY: "crypto"},
                specs={**{i: i for i in FILLERS}, EARLY: EARLY},
                cache_dir=cache_dir, now=now)


# ---------------------------------------------------------------------------
# 1) 模块行为
# ---------------------------------------------------------------------------
def test_counts_mirror_the_pool_rule(tmp_cache):
    """新达标 = 池里有、盘上没有；ADV 线 = top-120 里最便宜的一只。"""
    r = drift_report(**base_kwargs(make_good_client(), tmp_cache, now=1000.0))
    assert r["ok"] and not r["from_cache"]
    assert r["n_cached"] == 121 and r["n_live"] == 125
    assert r["n_new"] == 2 and r["n_new_above_cut"] == 2
    # top-120 = TOP1(50M) + TOP2(20M) + F000..F117 → 第 120 名 = F117 = 19M−117×0.1M
    assert r["pool_cut_adv_usd"] == 7_300_000.0
    assert r["new"][0]["inst"] == TOP1 and r["new"][0]["adv_usd"] == 50_000_000.0
    assert r["n_dead"] == 0 and r["n_unclassified"] == 0 and r["n_missing_specs"] == 0
    assert "新达标 2（超 ADV 线 2）" in r["summary"]


def test_three_gaps_are_counted_separately(tmp_cache):
    """退市残留 / 缺分类 / 缺规格 三个计数必须互不串（不能一个把其它顶掉）。

    fixture 语义：UNCL/NOSP 在交易所在场、也已在盘上（frozen pool 里），只是
    分别缺分类快照 / 缺规格快照；DEAD 只在盘上（交易所已除名）。
    """
    cli = make_good_client()
    kw = base_kwargs(cli, tmp_cache, now=1000.0)
    kw.update(cached=[EARLY] + FILLERS + [DEAD, UNCL, NOSP],
              # 语义对齐：DEAD 退市前是正式成员（分类/规格都有）；NOSP 只缺规格、
              # UNCL 只缺分类——这样三个计数才能被独立断言。
              categories={**kw["categories"], NOSP: "crypto", DEAD: "crypto"},
              specs={**kw["specs"], UNCL: UNCL, DEAD: DEAD})
    r = drift_report(**kw)
    assert r["n_dead"] == 1 and r["dead"] == [DEAD]
    assert r["n_unclassified"] == 1 and r["unclassified"] == [UNCL]
    assert r["n_missing_specs"] == 1 and r["missing_specs"] == [NOSP]
    assert r["n_new"] == 2                              # 池比对不受脏盘影响


def test_ttl_hit_does_not_touch_the_network(tmp_cache):
    """TTL 内第二次读盘上快照：即使给一个「一调就炸」的 client 也必须成功。"""
    kw = base_kwargs(make_good_client(), tmp_cache, now=1000.0)
    first = drift_report(**kw)
    assert first["ok"]
    boom = FakeClient(boom=True)
    second = drift_report(**{**kw, "cli": boom, "now": 1000.0 + 60})
    assert second["ok"] and second["from_cache"] is True
    assert second["n_cached"] == first["n_cached"]
    assert boom.calls == []                             # 「没打网络」的直接证据


def test_force_bypasses_ttl(tmp_cache):
    cli1 = make_good_client()
    drift_report(**base_kwargs(cli1, tmp_cache, now=1000.0))
    cli2 = make_good_client()
    r = drift_report(**{**base_kwargs(cli2, tmp_cache, now=1000.0 + 60), "force": True})
    assert r["ok"] and r["from_cache"] is False
    assert len(cli2.calls) >= 3                         # 真的重新测了


def test_expired_ttl_remeasures(tmp_cache):
    kw = base_kwargs(make_good_client(), tmp_cache, now=1000.0)
    drift_report(**kw)
    cli2 = make_good_client()
    r = drift_report(**{**kw, "cli": cli2, "now": 1000.0 + TTL_SECONDS + 1})
    assert r["from_cache"] is False and cli2.calls      # 过期 = 重测


def test_network_failure_never_raises_and_serves_stale(tmp_cache):
    """先成功一次，再断网：返回 ok False + stale True + 上次的 summary，绝不抛。"""
    drift_report(**base_kwargs(make_good_client(), tmp_cache, now=1000.0))
    r = drift_report(**{**base_kwargs(FakeClient(boom=True), tmp_cache, now=2000.0),
                        "force": True})
    assert r["ok"] is False
    assert "RuntimeError" in (r["error"] or "")
    assert r["stale"] is True
    assert "新达标 2" in r["summary"]                    # 上次成功体还在


def test_failure_backoff_does_not_retry_the_network(tmp_cache):
    """失败后 15 分钟退避：期间状态轮询再频也不打网络；force 才重试。"""
    boom = FakeClient(boom=True)
    kw = base_kwargs(boom, tmp_cache, now=1000.0)
    r1 = drift_report(**kw)
    assert r1["ok"] is False and r1["stale"] is False
    assert boom.calls                                   # 第一次是真试过了

    good = make_good_client()                           # 网络已恢复的好 client
    r2 = drift_report(**{**base_kwargs(good, tmp_cache, now=1000.0 + 60),
                         "force": False})
    assert r2["ok"] is False and r2["retry_in_min"] > 0
    assert good.calls == []                             # 退避期内不许打网络

    r3 = drift_report(**{**base_kwargs(make_good_client(), tmp_cache,
                                      now=1000.0 + 60), "force": True})
    assert r3["ok"] is True                             # force 才重试


def test_missing_category_file_degrades_but_reports_all_cached(tmp_cache, monkeypatch):
    """分类快照读不到 = 全部盘上合约都可能踩 SystemExit ⇒ 必须全部计为缺分类。"""
    from crypto_ls_research.data import asset_class
    monkeypatch.setattr(
        asset_class, "load_categories",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no snapshot")))
    kw = base_kwargs(make_good_client(), tmp_cache, now=1000.0)
    kw["categories"] = None                    # None 才会去走真实的读取路径
    r = drift_report(**kw)
    assert r["ok"] is True                              # 降级 ≠ 失败
    assert r["n_unclassified"] == 121                   # 全部盘上合约
    assert r["unclassified"][0] == EARLY                # 排序后 AAA 在最前
    assert any("分类" in n for n in r["notes"])


def test_missing_specs_file_degrades_but_reports_all_cached(tmp_cache, monkeypatch):
    from crypto_ls_research.execution import specs as specs_mod
    def _boom(*a, **k):
        raise RuntimeError("specs file unreadable")
    monkeypatch.setattr(specs_mod, "load_specs", _boom)
    kw = base_kwargs(make_good_client(), tmp_cache, now=1000.0)
    kw["specs"] = None                        # None 才会去走真实的读取路径
    r = drift_report(**kw)
    assert r["ok"] is True
    assert r["n_missing_specs"] == 121
    assert r["missing_specs"][0] == EARLY
    assert any("规格" in n for n in r["notes"])


def test_module_never_leaks_private_bookkeeping_keys(tmp_cache):
    r = drift_report(**base_kwargs(make_good_client(), tmp_cache, now=1000.0))
    assert not [k for k in r if k.startswith("_")], "私有键不能漏进 API 载荷"


# ---------------------------------------------------------------------------
# 2) API 接线（相邻对断言：挂载点必须长在正确的路由上下文里）
# ---------------------------------------------------------------------------
def test_status_route_attaches_drift():
    src = (ROOT / "webapp" / "live_api.py").read_text(encoding="utf-8")
    assert "from crypto_ls_research.execution.universe_drift import drift_report" in src
    i = src.find('if path == "/api/live/status":')
    j = src.find('if path == "/api/live/navhist":', i)
    body = src[i:j]
    assert 'st["universe_drift"] = drift_report()' in body, \
        "/api/live/status 没带宇宙体检"


def test_plan_route_attaches_drift_after_preview():
    src = (ROOT / "webapp" / "live_api.py").read_text(encoding="utf-8")
    i = src.find('if path == "/api/live/plan":')
    j = src.find('if path == "/api/live/execute":', i)
    body = src[i:j]
    assert re_search(r'payload\s*=\s*eng\.preview\(', body), "plan 不在跑 preview？"
    assert re_search(r'payload\["universe_drift"\]\s*=\s*drift_report\(\)', body), \
        "plan 载荷没带宇宙体检"
    # 相邻对：drift 必须在 preview **之后**、_LAST 写盘之前（否则页面读不到）
    assert body.find("eng.preview(") < body.find('payload["universe_drift"]') \
        < body.find("_LAST[mode] = payload")


def re_search(p, s):
    import re
    return re.search(p, s)


# ---------------------------------------------------------------------------
# 3) 前端真渲染（node vm 加载真实 app.js）
# ---------------------------------------------------------------------------
NODE_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const APP = process.argv[2];
const PAYLOAD = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

/* stub 与 scripts/smoke_ui.js 同源：够撑住顶层副作用即可。 */
class FakeEl {
  constructor() { this._html = ''; this.style = {}; this.dataset = {}; this.textContent = ''; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  get classList() { return { toggle() {}, add() {}, remove() {}, contains() { return false; } }; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  addEventListener() {}
}
const els = new Map();
const makeEl = (sel) => { if (!els.has(sel)) els.set(sel, new FakeEl()); return els.get(sel); };
const sandbox = {
  window: { devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} },
  document: { body: {}, documentElement: { setAttribute() {}, getAttribute() { return null; } },
              querySelector: makeEl, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: 'http://127.0.0.1:8790/' },
  getComputedStyle: () => ({ getPropertyValue: () => '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval, console,
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
sandbox.fetch = () => Promise.reject(new Error('no network in render test'));

let src = fs.readFileSync(APP, 'utf8');
/* 入口调用被 authRender() 包了一层（未登录时 boot() 不能跑），两种结尾都兼容 */
const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
if (!entry) throw new Error('app.js 末尾的入口调用未找到（authRender/boot）');
src = src.slice(0, entry.index) + '\n';
src += '\n;globalThis.__T = { S, esc, liveDriftHtml, liveSkeleton, liveBarHtml };\n';
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;
T.S.live = T.S.live || {};

function divBalance(html) {
  const re = /<(\/?)div\b[^>]*>/gi; let depth = 0, minDepth = 0, m;
  while ((m = re.exec(html))) { depth += m[1] === '/' ? -1 : 1; if (depth < minDepth) minDepth = depth; }
  return { depth, minDepth };
}
const checks = [];
const check = (name, ok, extra) => checks.push({ name, ok: !!ok, extra: extra == null ? '' : String(extra) });

/* 三种状态各真渲染一遍 */
T.S.live.status = { universe_drift: PAYLOAD.drifted };
const h1 = T.liveDriftHtml();
check('drifted: 显示「宇宙有漂移」', h1.includes('宇宙有漂移'));
check('drifted: 缺分类计数可见', h1.includes('缺分类 ' + PAYLOAD.drifted.n_unclassified));
check('drifted: 缺分类点名了具体合约', h1.includes(PAYLOAD.drifted.unclassified[0]));
check('drifted: 新达标样例可见', h1.includes(PAYLOAD.drifted.new[0].inst));
check('drifted: 有可修缺口 ⇒ 有「补齐宇宙」按钮',
  h1.includes('id="liveDriftUpgrade"') && h1.includes('补齐宇宙'));
{ const b = divBalance(h1); check('drifted: div 配平', b.depth === 0 && b.minDepth >= 0,
  `end=${b.depth} min=${b.minDepth}`); }

/* 只有退市残留（无其他缺口）时不给按钮：残留无害，没有可修的东西 */
T.S.live.status = { universe_drift: PAYLOAD.deadOnly };
const h1b = T.liveDriftHtml();
check('deadOnly: 显示漂移但不给按钮',
  h1b.includes('宇宙有漂移') && !h1b.includes('id="liveDriftUpgrade"'));
{ const b = divBalance(h1b); check('deadOnly: div 配平', b.depth === 0 && b.minDepth >= 0); }

T.S.live.status = { universe_drift: PAYLOAD.clean };
const h2 = T.liveDriftHtml();
check('clean: 绿色一行', h2.includes('✓ 宇宙体检') && h2.includes(PAYLOAD.clean.summary));
check('clean: 没有补齐按钮', !h2.includes('id="liveDriftUpgrade"'));
{ const b = divBalance(h2); check('clean: div 配平', b.depth === 0 && b.minDepth >= 0); }

T.S.live.status = { universe_drift: PAYLOAD.failed };
const h3 = T.liveDriftHtml();
check('failed: 显示「宇宙体检不可用」', h3.includes('宇宙体检不可用'));
check('failed: stale 摘要可见', h3.includes(PAYLOAD.failed.summary));
check('failed: 没有补齐按钮', !h3.includes('id="liveDriftUpgrade"'));
{ const b = divBalance(h3); check('failed: div 配平', b.depth === 0 && b.minDepth >= 0); }

/* 状态没回来时必须安静、不许报错 */
T.S.live.status = null;
check('no status: 渲染为空且不抛', T.liveDriftHtml() === '');

/* 骨架里必须真有挂载点 */
const skel = T.liveSkeleton();
check('skeleton 挂载 #liveDrift', skel.includes('id="liveDrift"'));
{ const b = divBalance(skel); check('skeleton: div 配平', b.depth === 0 && b.minDepth >= 0,
  `end=${b.depth} min=${b.minDepth}`); }

/* livePaintAll 必须重绘它（否则首绘之后状态更新就再也刷不上去） */
const raw = fs.readFileSync(APP, 'utf8');
const i = raw.indexOf('function livePaintAll(');
const body = raw.slice(i, raw.indexOf('\n}', i));
check('livePaintAll 重绘 liveDrift', /put\('liveDrift',\s*liveDriftHtml\(\)\)/.test(body));

console.log(JSON.stringify({ checks }));
process.exit(checks.some((c) => !c.ok) ? 1 : 0);
"""


def _render_checks(tmp_path: Path, payload: dict) -> list:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists():
        pytest.skip("node 不可用，跳过前端真渲染")
    js = tmp_path / "render_drift.js"
    pj = tmp_path / "payload.json"
    js.write_text(NODE_HARNESS, encoding="utf-8")
    pj.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run([node, str(js), str(ROOT / "webapp" / "static" / "app.js"),
                        str(pj)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0 and not p.stdout.strip():
        raise AssertionError(f"渲染 harness 崩了：\n{p.stderr[-2000:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])["checks"]


def _drift_payload() -> dict:
    return {
        "drifted": {"ok": True, "stale": False, "from_cache": False,
                    "age_hours": 0.0, "error": None,
                    "summary": "盘上 4 · 实时 3 · 新达标 2（超 ADV 线 2） · "
                               "退市残留 1 · 缺分类 1 · 缺规格 1",
                    "n_cached": 4, "n_live": 3, "n_new": 2, "n_new_above_cut": 2,
                    "n_dead": 1, "n_unclassified": 1, "n_missing_specs": 1,
                    "new": [{"inst": TOP1, "adv_usd": 50_000_000.0,
                             "listed": "2026-01-01", "above_cut": True}],
                    "dead": [DEAD], "unclassified": [UNCL], "missing_specs": [NOSP],
                    "pool_cut_adv_usd": 20_000_000.0, "notes": []},
        "clean": {"ok": True, "stale": False, "from_cache": True,
                  "age_hours": 2.5, "error": None,
                  "summary": "盘上 3 · 实时 3 · 新达标 0（超 ADV 线 0） · "
                             "退市残留 0 · 缺分类 0 · 缺规格 0",
                  "n_cached": 3, "n_live": 3, "n_new": 0, "n_new_above_cut": 0,
                  "n_dead": 0, "n_unclassified": 0, "n_missing_specs": 0,
                  "new": [], "dead": [], "unclassified": [], "missing_specs": [],
                  "pool_cut_adv_usd": 20_000_000.0, "notes": []},
        "failed": {"ok": False, "stale": True, "from_cache": False,
                   "age_hours": 3.2, "retry_in_min": 9.5,
                   "error": "RuntimeError: network is down",
                   "summary": "盘上 4 · 实时 3 · 新达标 2（超 ADV 线 2） · "
                              "退市残留 1 · 缺分类 1 · 缺规格 1"},
        "deadOnly": {"ok": True, "stale": False, "from_cache": False,
                     "age_hours": 0.0, "error": None,
                     "summary": "盘上 4 · 实时 4 · 新达标 0（超 ADV 线 0） · "
                                "退市残留 1 · 缺分类 0 · 缺规格 0",
                     "n_cached": 4, "n_live": 4, "n_new": 0, "n_new_above_cut": 0,
                     "n_dead": 1, "n_unclassified": 0, "n_missing_specs": 0,
                     "new": [], "dead": [DEAD], "unclassified": [],
                     "missing_specs": [], "pool_cut_adv_usd": 20_000_000.0,
                     "notes": []},
    }


def test_frontend_really_renders_all_three_states(tmp_path):
    """真渲染：三种状态 + div 配平 + 挂载点 + 重绘接线，一次全查。"""
    checks = _render_checks(tmp_path, _drift_payload())
    bad = [c for c in checks if not c["ok"]]
    assert not bad, "真渲染断言失败：\n" + "\n".join(
        f"  FAIL {c['name']}  {c['extra']}" for c in bad)
