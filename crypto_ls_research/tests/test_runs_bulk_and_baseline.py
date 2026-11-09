r"""回测跑完要自己出数、批量删除、一键重跑基线。

三件事都在 `app.js`，且三件都有一个共同的失败模式：**看着工作、其实没工作**。

1. **跑完不刷新指标**
   `pollRunsOnce()` 以前只把 `S.detail.status` 改掉就重绘。可是选中一条正在跑的
   任务时，`S.detail` 是那一刻的快照 —— 里面没有 `result` / `metrics`。于是任务
   跑完、状态变绿、核心指标却全是「—」，用户必须手动刷新整页才看得到数字。
   现在：状态进入终态且与手里这份详情的状态不同 ⇒ 重拉一次详情（只补一次）。

2. **批量删除**
   逐条发 `DELETE /api/runs/<id>`，复用服务端已有的安全规则（拒绝未结束的任务、
   基线产物只摘列表不删目录、产物 rename 进 `.trash`），不新造一个批处理接口。

3. **重跑基线**
   参数一律取自 `/api/spec`（`default` 即最优值、`optimal_cli` 即验收 CLI），
   **不看当前面板**；阶段固定取阶段最多的 bundle（验收判据要靠那些 stage 的产物）。
   tag 用 `optimal_tag`，前端不写死版本号。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "webapp" / "static" / "app.js"
CSS = ROOT / "webapp" / "static" / "style.css"
NODE = shutil.which("node") or "/Users/chenqifeng/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin/node"
DEFAULT_MODULES = "/Users/chenqifeng/.workbuddy-ai/binaries/node/workspace/node_modules"


def _src() -> str:
    return APP.read_text(encoding="utf-8")


def _has_jsdom() -> bool:
    if not pathlib.Path(NODE).exists():
        return False
    env = dict(os.environ)
    env["NODE_PATH"] = env.get("NODE_PATH") or DEFAULT_MODULES
    r = subprocess.run([NODE, "-e", "require.resolve('jsdom')"],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode == 0


# ============================================================ 静态
def test_poll_refetches_the_detail_when_the_run_finishes():
    src = _src()
    i = src.find("async function pollRunsOnce()")
    assert i > 0
    body = src[i:src.find("\n}\n", i)]
    # 相邻对：终态判断、状态变化判断、重拉调用三者必须同时在一个条件里。
    assert re.search(r"RUN_TERMINAL\.has\(cur\.status\)\s*&&\s*S\.detail\.status\s*!==\s*cur\.status", body), \
        "任务结束后没有判断「状态变了」——要么不补数据，要么每 2.5s 重拉一次"
    assert "await reloadDetail(" in body, "任务结束后没有重拉详情：核心指标会一直是「—」"


def test_terminal_states_cover_everything_that_stops_changing():
    src = _src()
    m = re.search(r"const RUN_TERMINAL = new Set\(\[([^\]]*)\]\)", src)
    assert m, "找不到 RUN_TERMINAL"
    got = set(re.findall(r"'(\w+)'", m.group(1)))
    # cancelled 也是终态：产物不完整，但记录要定格，不能永远显示「运行中」。
    assert got == {"done", "partial", "failed", "cancelled"}, f"终态集合不对：{got}"


def test_reload_detail_keeps_the_log_panel():
    """日志由 startLog() 按 offset 增量追加；重拉详情时清空 `_log` 会让它整块消失。"""
    src = _src()
    i = src.find("async function reloadDetail(")
    assert i > 0
    body = src[i:src.find("\n}\n", i)]
    assert "_log" in body, "重拉详情时没有保留 _log —— 用户正看着的日志会突然空掉"


def test_bulk_delete_reuses_the_single_run_delete_route():
    src = _src()
    i = src.find("async function deleteMany(")
    assert i > 0, "没有 deleteMany"
    body = src[i:src.find("\n}\n", i)]
    assert re.search(r"'/api/runs/'\s*\+\s*encodeURIComponent\(r\.id\)", body), \
        "批量删除没有走单条 DELETE 路由"
    assert re.search(r"method:\s*'DELETE'", body), "批量删除没发 DELETE"
    # 501+HTML 时 resp.json() 会把原因藏成 Unexpected token '<'
    assert "resp.text()" in body and "await resp.json()" not in body, \
        "批量删除直接 json() 了：控制台没重启时真实原因会被藏掉"


def test_bulk_selection_skips_unfinished_runs():
    src = _src()
    i = src.find("function bulkPicked()")
    assert i > 0
    body = src[i:src.find("\n}\n", i)]
    assert "'queued'" in body and "'running'" in body, \
        "bulkPicked 没有排除未结束的任务 —— 会把删不掉的算进「已选 N 条」"


def test_bulk_mode_state_lives_in_S_not_in_the_dom():
    """`pollRuns()` 每 2.5s 整块重建列表；选择状态放 DOM 里一刷新就丢。"""
    src = _src()
    assert re.search(r"bulk:\s*false,\s*picked:\s*\{\}", src), "S 里没有 bulk/picked"
    assert "$('#bulkCount')" in src and "syncBulkBar()" in src


def test_bulk_rows_get_an_extra_grid_column():
    """多一个勾选框 = 多一个栅格列，CSS 必须跟着加，否则第 7 个元素被挤到下一行。"""
    css = CSS.read_text(encoding="utf-8")
    m = re.search(r"\.runs\.bulk \.runrow \{\s*grid-template-columns:\s*([^;}]+)", css)
    assert m, "没有 .runs.bulk .runrow 的栅格规则"
    assert len(m.group(1).split()) == 7, f"批量模式列数不对：{m.group(1)}"


def test_baseline_tag_is_never_hardcoded_in_the_frontend():
    """换口径时写死的版本号会继续断言旧口径（项目里已经踩过一次）。"""
    src = _src()
    assert "v5_1d_all5" not in src, "app.js 里写死了验收 tag —— 必须走 /api/spec 的 optimal_tag"
    assert "S.optimalTag" in src


def test_baseline_uses_the_widest_stage_bundle_not_the_current_selection():
    src = _src()
    i = src.find("function baselineStages()")
    assert i > 0, "没有 baselineStages()"
    body = src[i:src.find("\n}\n", i)]
    assert "'full'" in body, "没有优先取「完整验收」bundle"
    assert "S.stages" not in body, "重跑基线跟着面板上当前的 bundle 走 —— 验收判据会算不出来"


def test_baseline_params_come_from_the_spec_not_the_panel():
    src = _src()
    i = src.find("async function runBaseline(")
    assert i > 0
    body = src[i:src.find("\n}\n", i)]
    assert "defaults()" in body, "没有用 spec 的 default（= 最优值）构造参数"
    assert re.search(r"stages:\s*bs\.stages", body), "提交时没用 baselineStages() 的阶段"
    assert re.search(r"tag,?\s*$|tag,\n", body) or "tag }" in body or "tag," in body, \
        "提交时没带 tag"


# ============================================================ 行为（jsdom）
pytestmark_js = pytest.mark.skipif(not _has_jsdom(), reason="没有 node 或 jsdom")

PRELUDE = r"""
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const ROOT = process.argv[1] || process.cwd();
const STATIC = path.join(ROOT, 'webapp', 'static');

function boot() {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8790/', runScripts: 'outside-only' });
  const w = dom.window;
  const calls = [];
  w.fetch = async (url, opt) => {
    const u = String(url);
    calls.push({ url: u, method: (opt && opt.method) || 'GET',
                 body: opt && opt.body ? JSON.parse(opt.body) : null });
    let body = {};
    if (u === '/api/runs') body = { runs: [], active: null, queued: [] };
    else if (/^\/api\/runs\/[^/]+$/.test(u)) body = { id: 'x', tag: 't', status: 'done' };
    else if (u.indexOf('/log') > 0) body = { offset: 0, text: '' };
    return { ok: true, status: 200, text: async () => JSON.stringify(body),
             json: async () => body };
  };
  process.on('unhandledRejection', () => {});
  let src = fs.readFileSync(path.join(STATIC, 'ui.js'), 'utf8') + '\n'
          + fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
  const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
  if (!entry) throw new Error('app.js 末尾的入口调用没找到');
  src = src.slice(0, entry.index) + '\n';
  src += '\n;window.__T = { S, ui, pollRunsOnce, runsSig, reloadDetail,'
       + ' bulkPicked, deleteMany, runBaseline, baselineStages, defaults };';
  w.eval(src);
  return { w, T: w.__T, calls };
}
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 0));
"""


def _run(script: str) -> dict:
    env = dict(os.environ)
    env["NODE_PATH"] = env.get("NODE_PATH") or DEFAULT_MODULES
    r = subprocess.run([NODE, "-e", PRELUDE + script], capture_output=True, text=True,
                       env=env, timeout=180, cwd=str(ROOT))
    assert r.returncode == 0, f"harness 跑不动：{(r.stderr or r.stdout)[-1500:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-900:]}"
    return json.loads(line[0][len("RESULT "):])


@pytestmark_js
def test_finished_run_shows_metrics_without_a_page_reload():
    """本次报的核心问题：跑完指标不出现，要手动刷新。"""
    out = _run(r"""
(async () => {
  const { w, T, calls } = boot();
  const S = T.S;
  // 面板停在一条「正在跑」的任务上：详情是那一刻的快照，没有 result。
  S.runs = [{ id: 'r1', tag: 'ui_x', status: 'done' }];
  S.selected = 'r1';
  S.detail = { id: 'r1', tag: 'ui_x', status: 'running', _log: '旧日志', result: null };
  S.runsSig = T.runsSig();            // 让 poll 补完详情后早退，不触发整页重绘
  w.fetch = async (url) => {
    const u = String(url);
    calls.push({ url: u, method: 'GET' });
    const body = (u === '/api/runs')
      ? { runs: [{ id: 'r1', tag: 'ui_x', status: 'done' }], active: null, queued: [] }
      : { id: 'r1', tag: 'ui_x', status: 'done',
          result: { sharpe: 1.926, cagr: 0.3876, mdd: -0.1331, metrics: [] } };
    return { ok: true, status: 200, text: async () => JSON.stringify(body),
             json: async () => body };
  };
  calls.length = 0;
  await T.pollRunsOnce();
  const out = {
    refetched: calls.some((c) => c.url === '/api/runs/r1'),
    status: S.detail.status,
    hasResult: !!(S.detail.result && S.detail.result.sharpe != null),
    logKept: S.detail._log,
  };
  // 再轮一次：已经补过了，不该重复拉
  calls.length = 0;
  S.runsSig = T.runsSig();
  await T.pollRunsOnce();
  out.refetchedAgain = calls.some((c) => c.url === '/api/runs/r1');
  console.log('RESULT ' + JSON.stringify(out));
  process.exit(0);
})();
""")
    assert out["refetched"] is True, "任务结束后没有重拉详情 —— 指标会一直是「—」"
    assert out["hasResult"] is True, "重拉之后仍然没有 result"
    assert out["logKept"] == "旧日志", "重拉详情把日志面板清空了"
    assert out["refetchedAgain"] is False, "每轮轮询都重拉详情，会打爆服务端"


@pytestmark_js
def test_bulk_delete_issues_one_delete_per_finished_run_only():
    out = _run(r"""
(async () => {
  const { w, T, calls } = boot();
  const S = T.S;
  S.optimalTag = 'v5_1d_all5';
  S.runs = [
    { id: 'a', tag: 'ta', status: 'done', label: 'A' },
    { id: 'b', tag: 'tb', status: 'failed', label: 'B' },
    { id: 'c', tag: 'tc', status: 'running', label: 'C' },
  ];
  S.picked = { a: 1, b: 1, c: 1 };      // c 在跑：不该被算进去
  const picked = T.bulkPicked();
  const p = T.deleteMany(picked).catch(() => 'threw');
  await tick(120);
  const ok = w.document.querySelector('.uimodal [data-uim-ok]');
  const out = { picked, okText: ok && ok.textContent,
                listed: w.document.querySelectorAll('.uimodal .bulklist li').length };
  if (ok) ok.click();
  await p;
  out.deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url);
  out.bulkOff = S.bulk === false && Object.keys(S.picked).length === 0;
  console.log('RESULT ' + JSON.stringify(out));
  process.exit(0);
})();
""")
    assert out["picked"] == ["a", "b"], f"运行中的任务被算进已选：{out['picked']}"
    assert out["okText"] == "删除 2 条", f"确认按钮文案不对：{out['okText']}"
    assert out["listed"] == 2, "弹层没有列出要删的条目"
    assert out["deletes"] == ["/api/runs/a", "/api/runs/b"], \
        f"DELETE 请求不对：{out['deletes']}"
    assert out["bulkOff"] is True, "删完没有退出选择模式"


@pytestmark_js
def test_baseline_posts_the_accepted_tag_and_the_widest_stages():
    out = _run(r"""
(async () => {
  const { w, T, calls } = boot();
  const S = T.S;
  S.optimalTag = 'v5_1d_all5';
  S.optimalCli = { bar: '1h', rebalance_days: 1, asset_class: 'crypto',
                   start: '2021-01-01', end: '2026-10-16', capital: 10000 };
  S.spec = { global: [{ key: '_cli.bar', default: '1h' }], groups: [] };
  S.lib = {}; S.ov = {}; S.cli = { bar: '15m', rebalance_days: 3 };   // 面板是「跑偏」的
  S.bundles = [
    { id: 'fast', label: '快速回测', stages: ['base', 'charts'] },
    { id: 'full', label: '完整验收', stages: ['base', 'decomp', 'exec', 'charts'] },
  ];
  S.stages = ['base', 'charts'];      // 面板当前选的是快速档
  S.presets = [];
  const p = T.runBaseline().catch((e) => 'threw: ' + e.message);
  await tick(120);
  const ok = w.document.querySelector('.uimodal [data-uim-ok]');
  const out = { opened: !!ok, confirm: ok && ok.textContent };
  if (ok) ok.click();
  await p;
  const post = calls.find((c) => c.url === '/api/run' && c.method === 'POST');
  out.body = post && post.body;
  console.log('RESULT ' + JSON.stringify(out));
  process.exit(0);
})();
""")
    assert out["opened"] is True, "点「重跑基线」没有先确认"
    body = out["body"] or {}
    assert body.get("tag") == "v5_1d_all5", f"没带验收 tag：{body.get('tag')}"
    assert body.get("stages") == ["base", "decomp", "exec", "charts"], \
        f"阶段没取「完整验收」：{body.get('stages')}"
    # 面板上的 cli 是 15m / 3 天，基线必须用 optimal_cli 的 1h / 1 天
    assert body.get("cli", {}).get("bar") == "1h", f"cli 取了面板的值：{body.get('cli')}"
    assert body.get("cli", {}).get("rebalance_days") == 1, \
        f"cli 取了面板的值：{body.get('cli')}"
