r"""`ui.js` 组件库的守卫测试。

为什么需要它
------------
`ui.js` 是一个**全局 IIFE + 单文件**的组件库，它有两类失败是**静默**的：

1. **顶层名字撞车。** `app.js` 已经定义了顶层 `const esc`。`ui.js` 里如果再写
   一个顶层 `const esc`，两个 classic script 共享同一个全局词法环境 ⇒ 浏览器
   直接抛 `Identifier 'esc' has already been declared`，**整页白屏**，而且
   控制台之外的任何地方都看不到原因。`ui.js` 因此整体包在 IIFE 里、只暴露 `ui`。
   这条纪律靠人记是不可靠的，所以在这里用静态断言钉死。

2. **渲染出脏字符。** `NaN` / `undefined` / `[object Object]` 出现在 HTML 里，
   画布和表格看起来"没报错"，但用户读到的是垃圾。本项目已经被这一类坑过
   （`smoke_ui.js` 里的 `dirty()` 就是为此写的），这里用同一套判据。

判据来源与 `scripts/smoke_ui.js` 一致：`BAD = [/NaN/, /undefined/, /\[object Object\]/]`。
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
UI = ROOT / "webapp" / "static" / "ui.js"
APP = ROOT / "webapp" / "static" / "app.js"
INDEX = ROOT / "webapp" / "static" / "index.html"


# ---------------------------------------------------------------------------
# 1) 静态：顶层名字不能和 app.js 撞车
# ---------------------------------------------------------------------------
#: 顶层声明：`const X =` / `let X =` / `var X =` / `function X(` / `class X`
_TOP_DECL = re.compile(
    r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
_TOP_FN = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
_TOP_CLASS = re.compile(r"^class\s+([A-Za-z_$][\w$]*)\s*[{ ]", re.M)


def _top_level_names(src: str) -> set[str]:
    """收集**行首**（无缩进）的顶层声明名。

    只认行首是刻意的：`ui.js` 的内容都在 IIFE 里缩进，所以这正好把
    "IIFE 内部的局部变量"排除在外 —— 那些和 app.js 重名是无害的。
    """
    names: set[str] = set()
    for rx in (_TOP_DECL, _TOP_FN, _TOP_CLASS):
        names.update(rx.findall(src))
    return names


def test_ui_exposes_exactly_one_top_level_name():
    """ui.js 顶层只允许 `ui` 一个名字。

    多一个就多一次和 app.js 撞车的机会；而撞车的表现是**白屏**，不是报错。
    """
    got = _top_level_names(UI.read_text(encoding="utf-8"))
    assert got == {"ui"}, (
        f"ui.js 顶层出现了多余的名字：{sorted(got - {'ui'})}。"
        "请把它们挪进 IIFE —— 顶层 const 与 app.js 重名会让整页白屏"
        "（Identifier 'esc' has already been declared）。")


def test_no_top_level_collision_with_app_js():
    """ui.js 与 app.js 的顶层名字集合必须**不相交**。

    这是上一条的兜底：即使有人把 `ui` 改名，只要新增的名字撞上 app.js 就失败。
    """
    a = _top_level_names(UI.read_text(encoding="utf-8"))
    b = _top_level_names(APP.read_text(encoding="utf-8"))
    clash = sorted(a & b)
    assert not clash, (
        f"ui.js 与 app.js 的顶层声明重名：{clash}。"
        "两个 classic script 共享全局词法环境，重名会让页面直接白屏。")


def test_index_loads_ui_before_app():
    """`index.html` 必须**先**加载 ui.js。

    反了的话 app.js 里的 `ui.card(...)` 会在执行到那一行时抛 ReferenceError，
    而多数调用点在 async/模板里，错误会被 promise 链吞掉 —— 页面停在一个空壳上。
    """
    html = INDEX.read_text(encoding="utf-8")
    i_ui = html.find("/static/ui.js")
    i_app = html.find("/static/app.js")
    assert i_ui != -1, "index.html 没有引用 /static/ui.js"
    assert i_app != -1, "index.html 没有引用 /static/app.js"
    assert i_ui < i_app, "ui.js 必须在 app.js 之前加载"


# ---------------------------------------------------------------------------
# 2) 动态：真渲染每个组件，查脏输出与 div 平衡
# ---------------------------------------------------------------------------
_NODE_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const UI = process.argv[2];

/* 最小 DOM stub：ui.js 的纯函数组件只拼字符串，只有 toast/modal 需要 DOM。 */
function makeEl() {
  return {
    _html: '', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    setAttribute() {}, getAttribute() { return null; },
    appendChild() {}, removeChild() {}, addEventListener() {},
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
    isConnected: true, parentNode: null, focus() {}, textContent: '',
  };
}
const sandbox = {
  document: { createElement: () => makeEl(), body: makeEl(),
              addEventListener() {}, removeEventListener() {} },
  setTimeout, clearTimeout, console,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

/* `const ui` 不会挂到 sandbox 上（顶层 lexical 声明不进 global object），
 * 所以必须在**脚本内部**导出一份。浏览器里跨 script 可见，vm 里不行。 */
const src = fs.readFileSync(UI, 'utf8') + '\n;globalThis.__ui = ui;\n';
vm.runInContext(src, sandbox, { filename: 'ui.js' });
const ui = sandbox.__ui;

const cases = {
  'btn.primary': () => ui.btn({ label: '运行回测', kind: 'primary', id: 'runBtn' }),
  'btn.danger.sm': () => ui.btn({ label: '清仓', kind: 'danger', size: 'sm',
                                  data: { act: 'flat' } }),
  'btn.disabled': () => ui.btn({ label: '不可用', disabled: true }),
  'badge.ok': () => ui.badge('已连接', 'ok'),
  'badge.danger': () => ui.badge('熔断', 'danger'),
  'badge.unknown_kind': () => ui.badge('x', 'not-a-kind'),
  'card.full': () => ui.card({ title: '账户', sub: 'live',
                               actions: ui.btn({ label: '刷新', size: 'sm', kind: 'ghost' }),
                               body: '<p>hi</p>' }),
  'card.minimal': () => ui.card({ body: '<p>hi</p>' }),
  'stat.normal': () => ui.stat({ label: 'Sharpe', value: '1.94',
                                 delta: '+0.21 vs 基线', dir: 'up' }),
  'stat.null_value': () => ui.stat({ label: 'CAGR', value: null }),
  'table.normal': () => ui.table({ cols: [{ t: '时间' }, { t: '名义额', num: true }],
                                   rows: [['<span>11:00</span>', '$66.23']] }),
  'table.empty': () => ui.table({ cols: [{ t: 'a' }], rows: [],
                                  empty: { title: '还没有运行过', desc: '点右上角运行回测' } }),
  'skeleton.table': () => ui.skeleton({ kind: 'table', rows: 3, cols: 4 }),
  'skeleton.card': () => ui.skeleton({ kind: 'card' }),
  'skeleton.lines': () => ui.skeleton({ rows: 3 }),
  'empty.with_action': () => ui.empty({ icon: '▤', title: '暂无挂单',
                                        desc: '当前没有未成交订单',
                                        action: ui.btn({ label: '刷新' }) }),
  'error.full': () => ui.error({ title: '读账户失败', detail: 'HTTP 401',
                                 fix: '重新登录', action: ui.btn({ label: '重试' }) }),
  'seg': () => ui.seg({ items: [{ v: '7', label: '7天', on: true },
                                { v: '30', label: '30天' }], dataKey: 'navdays' }),
  'esc.xss': () => ui.badge('<script>alert(1)</script>', 'ok'),
};

const out = {};
for (const [k, fn] of Object.entries(cases)) out[k] = String(fn());
process.stdout.write(JSON.stringify(out));
"""

_BAD = [re.compile(r"NaN"), re.compile(r"undefined"), re.compile(r"\[object Object\]")]


def _render_all() -> dict:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists():
        pytest.skip("node 不可用，跳过组件渲染")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_NODE_HARNESS)
        js = f.name
    try:
        p = subprocess.run([node, js, str(UI)], capture_output=True,
                           text=True, timeout=60)
    finally:
        Path(js).unlink(missing_ok=True)
    assert p.returncode == 0, f"组件渲染脚本失败：\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)


def test_no_component_renders_dirty_text():
    """任何组件都不能把 NaN / undefined / [object Object] 渲染进 HTML。"""
    bad = {}
    for name, html in _render_all().items():
        hits = [rx.pattern for rx in _BAD if rx.search(html)]
        if hits:
            bad[name] = (hits, html[:200])
    assert not bad, f"这些组件渲染出了脏字符：{bad}"


def test_every_component_has_balanced_divs():
    """div 不配平会让后续内容整块错位（浏览器会自动纠正，于是更难发现）。"""
    bad = {}
    for name, html in _render_all().items():
        depth = 0
        for m in re.finditer(r"<(/?)div\b[^>]*>", html, re.I):
            depth += -1 if m.group(1) else 1
        if depth != 0:
            bad[name] = depth
    assert not bad, f"这些组件的 div 不配平：{bad}"


def test_text_is_escaped():
    """所有文本参数都必须转义 —— 这些字符串来自交易所的合约名与错误信息。"""
    out = _render_all()
    html = out["esc.xss"]
    assert "<script>" not in html, f"badge 没有转义尖括号：{html}"
    assert "&lt;script&gt;" in html, f"badge 的转义结果不对：{html}"


def test_unknown_badge_kind_falls_back_to_neutral():
    """未知 kind 必须退化成 neutral，而不是丢掉 class 变成一个没有底色的裸标签。"""
    out = _render_all()
    assert "uibadge neutral" in out["badge.unknown_kind"], out["badge.unknown_kind"]


def test_empty_table_renders_an_empty_state_not_a_broken_header():
    """空数据要渲染空状态（含原因与下一步），不能只留一个表头。"""
    out = _render_all()
    html = out["table.empty"]
    assert "<table" not in html, f"空数据不该渲染表格骨架：{html}"
    assert "还没有运行过" in html and "点右上角运行回测" in html, html
