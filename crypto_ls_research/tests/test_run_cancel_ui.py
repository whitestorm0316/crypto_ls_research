"""回测「取消」的守卫测试。

为什么需要它
------------
后端 `POST /api/runs/<id>/cancel` 从 2026-10 起就存在（`server.py::_cancel`），
但前端**从来没有接过** —— 用户点了「运行回测」就只能等它跑完，慢档几十分钟。
这个缺口之所以能长期存在，正是因为**没有任何测试会因为「按钮不见了」而失败**。

所以这里钉三件事：
  1. 接线还在（URL 与 method 是**相邻对**，只钉其一的话把 fetch 删掉也能过）；
  2. 按钮**只**出现在排队/运行中的行上（否则用户会对已结束的任务点取消）；
  3. 服务端「取消不了」时回的是 **200 + `note`**，不是 4xx ——
     前端必须把它说出来，不能静默当成功。这是最容易写错的一条。

判据来源：`server.py::_cancel` 的实现 —— 它能取消时回
`{"id", "status": "cancelled"}`；不能取消时回
`{"id", "status": <原状态>, "note": "只能取消排队中或正在运行的任务"}`。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "webapp" / "static" / "app.js"


# ---------------------------------------------------------------------------
# 1) 静态：接线与按钮位置
# ---------------------------------------------------------------------------
def _cancel_run_body() -> str:
    src = APP.read_text(encoding="utf-8")
    i = src.find("async function cancelRun(")
    assert i > 0, "app.js 里找不到 cancelRun() —— 取消功能被删掉了？"
    j = src.find("\n}\n", i)
    assert j > i
    return src[i:j]


def test_cancel_targets_the_runs_cancel_route():
    """相邻对：URL 与 method 必须同时钉住。

    只断言「出现了 /cancel」的话，把整段 fetch 删掉、只留一个字符串也能过；
    只断言 method 同理。两个一起才是"真的发了这个请求"。
    """
    body = _cancel_run_body()
    assert re.search(r"/api/runs/'\s*\+\s*encodeURIComponent\(id\)\s*\+\s*'/cancel'", body), (
        "cancelRun 没有拼出 /api/runs/<id>/cancel —— 路由变了或被删了")
    assert re.search(r"method:\s*'POST'", body), (
        "cancelRun 没有用 POST —— 服务端只注册了 POST（server.py 的 do_POST）")


def test_cancel_does_not_trust_http_200_alone():
    """200 + `note` 是「没取消成」。只判 `resp.ok` 会把它当成功。

    这条直接对应 `_cancel` 的第二个返回分支：任务已经结束时它照样回 200。
    """
    body = _cancel_run_body()
    assert re.search(r"\bd\.note\b", body), (
        "cancelRun 没有读 note —— 服务端「取消不了」时回的是 200 + note，"
        "不读它就会把一个失败静默显示成成功")


def test_cancel_does_not_blindly_parse_json():
    """与 deleteRun 同一条纪律：先取 text 再 parse。

    控制台没重启时路由不存在，`BaseHTTPRequestHandler` 回 501 + HTML，
    `resp.json()` 只会抛 `Unexpected token '<'`，把真正原因藏起来。
    """
    body = _cancel_run_body()
    assert "await resp.text()" in body, "cancelRun 没有先取 text"
    assert "JSON.parse(raw)" in body, "cancelRun 没有自己解析，无法区分「不是 JSON」"
    assert not re.search(r"await\s+resp\.json\(\)", body), (
        "cancelRun 仍在直接 resp.json()")


def test_cancel_uses_modal_not_native_confirm():
    """危险操作要能写清「会做什么 / 不会做什么」，原生 confirm 只有一行字。"""
    body = _cancel_run_body()
    assert "ui.modal(" in body, "cancelRun 没有用 ui.modal"
    assert "confirm(" not in body.replace("ui.modal(", ""), (
        "cancelRun 还在用原生 confirm —— 它会阻塞线程且装不下说明")


def test_the_cancel_button_is_rendered_for_unfinished_runs_only():
    """按钮的出现条件必须与「未结束」绑定，而不是无条件渲染。

    判据用相邻对：`cancelable` 的定义（= 不可删除）与按钮模板两处都要在。
    """
    src = APP.read_text(encoding="utf-8")
    assert re.search(r"const cancelable\s*=\s*!delable", src), (
        "cancelable 的判据变了 —— 它必须等价于「不能删除」（= 排队中/运行中）")
    assert re.search(r"\$\{cancelable \? `<button[^`]*data-cancelrun=", src), (
        "取消按钮没有受 cancelable 控制")


def test_the_cancel_button_does_not_also_select_the_run():
    """行本身有 onclick（选中该运行）。按钮必须 stopPropagation。

    否则「点取消」会先选中这条运行、再弹取消框 —— 两个副作用。
    """
    src = APP.read_text(encoding="utf-8")
    i = src.find("$$('[data-cancelrun]')")
    assert i > 0, "找不到取消按钮的事件绑定"
    body = src[i:src.find("\n  });", i)]
    assert "stopPropagation()" in body, (
        "取消按钮没有 stopPropagation —— 点它会连带选中该运行")
    assert "cancelRun(" in body, "绑定里没有调用 cancelRun"


# ---------------------------------------------------------------------------
# 2) 动态：真跑一遍 cancelRun 的三条路径
# ---------------------------------------------------------------------------
_NODE_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');
const APP = process.argv[2];
const UI = path.join(path.dirname(APP), 'ui.js');
const PAYLOAD = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

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
              querySelector: makeEl, querySelectorAll: () => [], addEventListener() {},
              createElement: () => new FakeEl() },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: 'http://127.0.0.1:8790/' },
  getComputedStyle: () => ({ getPropertyValue: () => '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval, console,
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
sandbox.fetch = () => Promise.reject(new Error('unexpected fetch'));

let src = fs.readFileSync(APP, 'utf8');
const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
if (!entry) throw new Error('app.js 末尾的入口调用未找到');
src = src.slice(0, entry.index) + '\n';
src = fs.readFileSync(UI, 'utf8') + '\n' + src;
/* 导出 + 把两个「取消之后才需要」的函数换掉：本测试只关心取消本身的语义。 */
src += `
;globalThis.__T = { S, cancelRun, ui,
  stub: () => { pollRuns = async () => {}; renderContent = () => {}; } };
`;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;
T.stub();

const calls = [];
T.ui.modal = async () => PAYLOAD.modalOk;
T.ui.toast = (msg, kind) => { calls.push({ msg: String(msg), kind: kind || 'info' }); };
sandbox.fetch = async (url, opt) => {
  calls.push({ url: String(url), method: (opt && opt.method) || 'GET' });
  return {
    ok: PAYLOAD.httpOk, status: PAYLOAD.httpStatus,
    text: async () => PAYLOAD.body,
  };
};

T.S.runs = [{ id: 'r1', tag: 't1', label: '测试运行', status: PAYLOAD.runStatus }];
(async () => {
  await T.cancelRun('r1');
  process.stdout.write(JSON.stringify({ calls }));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _run_case(*, modal_ok, http_ok, http_status, body, run_status="running") -> list[dict]:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists():
        pytest.skip("node 不可用")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_NODE_HARNESS)
        js = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"modalOk": modal_ok, "httpOk": http_ok, "httpStatus": http_status,
                   "body": body, "runStatus": run_status}, f)
        payload = f.name
    try:
        p = subprocess.run([node, js, str(APP), payload],
                           capture_output=True, text=True, timeout=60)
    finally:
        Path(js).unlink(missing_ok=True)
        Path(payload).unlink(missing_ok=True)
    assert p.returncode == 0, f"harness 失败：\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)["calls"]


def test_cancel_sends_the_right_request_and_reports_success():
    calls = _run_case(modal_ok=True, http_ok=True, http_status=200,
                      body=json.dumps({"id": "r1", "status": "cancelled"}))
    req = [c for c in calls if "url" in c]
    assert len(req) == 1, f"应该只发一个请求，实际 {req}"
    assert req[0]["url"] == "/api/runs/r1/cancel", req[0]["url"]
    assert req[0]["method"] == "POST", req[0]["method"]
    toast = [c for c in calls if "msg" in c]
    assert toast and toast[-1]["kind"] == "ok", toast


def test_cancel_reports_the_note_when_the_server_refuses():
    """200 + note 必须被当成「没取消成」说出来，且不能显示成成功。

    这是 `_cancel` 的真实行为：任务已经结束时它回 200，不是 4xx。
    """
    note = "只能取消排队中或正在运行的任务"
    calls = _run_case(modal_ok=True, http_ok=True, http_status=200,
                      body=json.dumps({"id": "r1", "status": "done", "note": note}))
    toast = [c for c in calls if "msg" in c]
    assert toast, "服务端说取消不了，前端却一句话都没说"
    assert toast[-1]["kind"] == "warn", f"应该用 warn 而不是 {toast[-1]['kind']}"
    assert note in toast[-1]["msg"], f"没有把服务端的原因转达给用户：{toast[-1]['msg']}"


def test_cancel_does_nothing_when_the_user_declines():
    """拒绝路径：确认框点「继续跑」就**一个请求都不许发**。"""
    calls = _run_case(modal_ok=False, http_ok=True, http_status=200,
                      body=json.dumps({"id": "r1", "status": "cancelled"}))
    assert not [c for c in calls if "url" in c], f"用户取消了却仍发了请求：{calls}"


def test_cancel_reports_a_non_json_error_instead_of_throwing():
    """控制台没重启时回 501 + HTML。必须说人话，不能把 `Unexpected token` 丢给用户。"""
    calls = _run_case(modal_ok=True, http_ok=False, http_status=501,
                      body="<!DOCTYPE html><html><body>Unsupported method</body></html>")
    toast = [c for c in calls if "msg" in c]
    assert toast, "501 时没有任何提示"
    assert toast[-1]["kind"] == "danger", toast[-1]
    assert "501" in toast[-1]["msg"], toast[-1]["msg"]
    assert "重启" in toast[-1]["msg"], (
        f"没有点明「控制台没重启」这个真正原因：{toast[-1]['msg']}")
