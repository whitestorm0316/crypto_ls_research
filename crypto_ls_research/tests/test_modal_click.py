r"""弹层的按钮必须**真的能点**。

为什么单独一个文件
------------------
2026-11-09 用户报「删除二次确认点了没用」：`ui.modal` 弹出来了，点「删除」什么
都不发生，也不报错。

根因是**选择器和标记对不上**：

    btn({ data: { uimOk: '1' } })  →  `<button data-uimOk="1">`
                                     HTML 解析器小写成 `data-uimok`
    modal 里 querySelector('[data-uim-ok]')  →  null

于是 `okBtn` / `cancelBtn` 都是 null，两个按钮的事件**一个都没绑上**；只有
× / Esc / 点背景能关（它们直接调 `finish`，不经过这两个按钮）。受影响的不止
删除：取消回测、偏离最优继续跑、补齐宇宙、快捷键帮助全都是同一个 `ui.modal`。

为什么以前的守卫没抓到
----------------------
`test_run_cancel_ui.py` / `test_three_states.py` 里的行为测试都把 `ui.modal`
**替换成 stub**（`ui.modal = async () => true`），测的是「调用方拿到 true 之后
干什么」，从来没测过「点按钮能不能拿到 true」。`test_ui_components.py` 只检查
渲染出来的字符串配平、无脏文本，不看事件绑定。

⇒ 这一层只能用**真 DOM**。托管 node 环境里带 `jsdom`，这里就用它：把 `ui.js`
加载进一个真 window，`innerHTML` 走真解析器，然后 `.click()`。

覆盖
----
- 确认 / 取消 / × / Esc / 点背景 五条关闭路径各自的返回值；
- `requirePhrase` 的闸门（没输入 / 输错 → 确认按钮 disabled；输对 → 放行）；
- `hideCancel` 只留一个按钮；
- `btn()` 的 data 键必须 kebab-case（就是这次 bug 的源头）；
- 端到端：`deleteRun()` 弹层 → 点「删除」→ 真的发出 `DELETE /api/runs/<id>`。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "webapp" / "static"
NODE = shutil.which("node") or "/Users/chenqifeng/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin/node"

#: 托管 node 的全局包目录（jsdom 在这里）。
DEFAULT_MODULES = "/Users/chenqifeng/.workbuddy-ai/binaries/node/workspace/node_modules"


def _node_env() -> dict:
    env = dict(os.environ)
    env["NODE_PATH"] = env.get("NODE_PATH") or DEFAULT_MODULES
    return env


def _has_jsdom() -> bool:
    if not pathlib.Path(NODE).exists():
        return False
    r = subprocess.run([NODE, "-e", "require.resolve('jsdom')"],
                       capture_output=True, text=True, env=_node_env(), timeout=60)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _has_jsdom(), reason="没有 node 或 jsdom")


def _run(script: str) -> dict:
    """跑一段 node 脚本，脚本最后要 `console.log('RESULT ' + JSON.stringify(...))`。"""
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                       env=_node_env(), timeout=180, cwd=str(ROOT))
    assert r.returncode == 0, f"harness 跑不动：{(r.stderr or r.stdout)[-1200:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-800:]}"
    return json.loads(line[0][len("RESULT "):])


#: 把 ui.js 装进一个真 window。注意 `w.eval` 里是严格模式（ui.js 自带
#: `'use strict'`）⇒ 顶层 `const ui` 不会落到 window 上，必须在**同一段**脚本里
#: 显式导出。
PRELUDE = r"""
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const ROOT = process.argv[1] || process.cwd();
const STATIC = path.join(ROOT, 'webapp', 'static');

function bootDom(html, scripts) {
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8790/', runScripts: 'outside-only' });
  const w = dom.window;
  w.eval(scripts);
  return w;
}
const UI_SRC = fs.readFileSync(path.join(STATIC, 'ui.js'), 'utf8');
function uiWindow() {
  return bootDom('<!doctype html><html><body></body></html>', UI_SRC + '\n;window.__ui = ui;');
}
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def test_clicking_confirm_resolves_true_and_closes():
    out = _run(PRELUDE + r"""
(async () => {
  const w = uiWindow();
  const p = w.__ui.modal({ title: '删除这条运行记录？', danger: true,
    body: '<p>会删掉产物</p>', confirmText: '删除', cancelText: '保留' });
  await tick();
  const ok = w.document.querySelector('.uimodal [data-uim-ok]');
  const cancel = w.document.querySelector('.uimodal [data-uim-cancel]');
  const before = { ok: !!ok, okText: ok && ok.textContent,
                   cancel: !!cancel, cancelText: cancel && cancel.textContent };
  ok.click();
  const val = await p;
  console.log('RESULT ' + JSON.stringify({ before, val,
    closed: !w.document.querySelector('.uimodal-back') }));
})();
""")


def test_clicking_cancel_resolves_false():
    out = _run(PRELUDE + r"""
(async () => {
  const w = uiWindow();
  const p = w.__ui.modal({ title: 't', confirmText: '删除', cancelText: '保留' });
  await tick();
  w.document.querySelector('.uimodal [data-uim-cancel]').click();
  console.log('RESULT ' + JSON.stringify({ val: await p,
    closed: !w.document.querySelector('.uimodal-back') }));
})();
""")
    assert out["val"] is False, "点「保留」没有关掉弹层 / 返回值不是 false"
    assert out["closed"] is True


def test_x_button_and_escape_and_backdrop_all_cancel():
    out = _run(PRELUDE + r"""
(async () => {
  const w = uiWindow();
  const out = {};
  // ×
  let p = w.__ui.modal({ title: 't' });
  await tick();
  w.document.querySelector('.uimodal-x').click();
  out.x = await p;
  // Esc
  p = w.__ui.modal({ title: 't' });
  await tick();
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
  out.esc = await p;
  // 点背景
  p = w.__ui.modal({ title: 't' });
  await tick();
  const back = w.document.querySelector('.uimodal-back');
  back.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
  out.backdrop = await p;
  out.leftover = w.document.querySelectorAll('.uimodal-back').length;
  console.log('RESULT ' + JSON.stringify(out));
})();
""")
    assert out["x"] is False, "× 关不掉"
    assert out["esc"] is False, "Esc 关不掉"
    assert out["backdrop"] is False, "点背景关不掉"
    assert out["leftover"] == 0, "弹层没从 DOM 里移除（会一直叠着）"


def test_require_phrase_gates_the_confirm_button():
    out = _run(PRELUDE + r"""
(async () => {
  const w = uiWindow();
  const p = w.__ui.modal({ title: '危险', requirePhrase: '我已确认', confirmText: '执行' });
  await tick();
  const ok = w.document.querySelector('.uimodal [data-uim-ok]');
  const input = w.document.querySelector('#uimPhrase');
  const out = { initiallyDisabled: ok.disabled, hasInput: !!input };
  ok.click();                       // 未输入就点：不该关掉
  out.stillOpenAfterBlindClick = !!w.document.querySelector('.uimodal-back');
  input.value = '已确认';            // 少两个字
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  out.wrongDisabled = ok.disabled;
  input.value = '我已确认';
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  out.rightDisabled = ok.disabled;
  ok.click();
  out.val = await p;
  console.log('RESULT ' + JSON.stringify(out));
})();
""")
    assert out["hasInput"] is True, "requirePhrase 没渲染输入框"
    assert out["initiallyDisabled"] is True, "没输入时确认按钮就能点 —— 闸门是假的"
    assert out["stillOpenAfterBlindClick"] is True, "未输入却把弹层关掉了"
    assert out["wrongDisabled"] is True, "输错短语也放行"
    assert out["rightDisabled"] is False, "输对了还不放行"
    assert out["val"] == "我已确认", "返回的不是输入的短语"


def test_hide_cancel_leaves_a_single_button():
    out = _run(PRELUDE + r"""
(async () => {
  const w = uiWindow();
  const p = w.__ui.modal({ title: '快捷键', hideCancel: true, confirmText: '知道了' });
  await tick();
  const btns = [...w.document.querySelectorAll('.uimodal-foot .btn')];
  const out = { n: btns.length, labels: btns.map((b) => b.textContent),
                hasCancel: !!w.document.querySelector('[data-uim-cancel]') };
  btns[0].click();
  out.val = await p;
  console.log('RESULT ' + JSON.stringify(out));
})();
""")
    assert out["n"] == 1, f"hideCancel 下仍渲染了 {out['n']} 个按钮：{out['labels']}"
    assert out["hasCancel"] is False
    assert out["val"] is True


def test_btn_writes_kebab_case_data_attributes():
    """这次 bug 的源头：`{ uimOk: '1' }` 曾生成 `data-uimOk`。"""
    out = _run(PRELUDE + r"""
(() => {
  const w = uiWindow();
  const html = w.__ui.btn({ label: 'x', data: { uimOk: '1', runId: 'abc' } });
  const d = w.document.createElement('div');
  d.innerHTML = html;
  console.log('RESULT ' + JSON.stringify({
    html,
    names: [...d.firstChild.attributes].map((a) => a.name),
    camelLeft: /data-[a-z]*-[A-Z]/.test(html) || /data-[a-z]*[A-Z]/.test(html),
  }));
})();
""")
    assert "data-uim-ok" in out["names"], f"data 键没转 kebab：{out['names']}"
    assert "data-run-id" in out["names"], "多词键也要转"
    assert "data-uimok" not in out["names"], "还留着 camelCase 被小写后的残形"


def test_delete_run_really_issues_the_delete_after_confirming():
    """端到端：`deleteRun()` → 弹层 → 点「删除」→ 真的发 DELETE。

    这条是「点了没用」的直接回归。之前 `test_run_cancel_ui.py` 把 `ui.modal`
    换成 stub，所以只能证明「拿到 true 之后会发请求」，证明不了「点得动」。
    """
    out = _run(r"""
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');
const ROOT = process.argv[1] || process.cwd();
const STATIC = path.join(ROOT, 'webapp', 'static');

const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const dom = new JSDOM(html, { url: 'http://127.0.0.1:8790/', runScripts: 'outside-only' });
const w = dom.window;

const calls = [];
w.fetch = async (url, opt) => {
  calls.push({ url: String(url), method: (opt && opt.method) || 'GET' });
  const body = String(url).indexOf('/api/runs') === 0 ? '{"runs":[],"ok":true}' : '{}';
  return { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) };
};
process.on('unhandledRejection', () => {});

let src = fs.readFileSync(path.join(STATIC, 'ui.js'), 'utf8') + '\n'
        + fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
// 末尾的入口调用（authRender/boot）要摘掉，否则会去打 /api/auth/me
const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
if (!entry) throw new Error('app.js 末尾的入口调用没找到');
src = src.slice(0, entry.index) + '\n';
src += '\n;window.__T = { S, deleteRun };';

w.eval(src);
const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  const T = w.__T;
  T.S.runs = [{ id: 'r1', tag: 'ui_x', label: '测试运行', status: 'done' }];
  const p = T.deleteRun('r1').catch((e) => 'threw: ' + (e && e.message || e));
  await tick();
  const ok = w.document.querySelector('.uimodal [data-uim-ok]');
  const seen = { dialogOpened: !!ok, okText: ok && ok.textContent };
  if (ok) ok.click();
  const after = await p;
  seen.deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url);
  seen.after = typeof after === 'string' ? after : 'ok';
  console.log('RESULT ' + JSON.stringify(seen));
})();
""")
    assert out["dialogOpened"] is True, "deleteRun 没弹出确认框"
    assert out["okText"] == "删除", f"确认按钮文案不对：{out['okText']}"
    assert out["deletes"] == ["/api/runs/r1"], (
        f"点了「删除」却没发出 DELETE（实际请求：{out['deletes']}）—— 按钮没绑上事件")
