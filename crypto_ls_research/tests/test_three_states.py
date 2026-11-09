r"""三态统一：`alert`/`confirm` 全部退场，加载态用骨架，空态说清下一步。

为什么这三件事要一起钉住：

* 原生 `confirm()` / `alert()` **阻塞整个 JS 线程**。交易台上每 2.5 秒一次轮询，
  一个原生弹窗挡住的正是最需要实时的那几秒；而且差异/风险只能挤在纯文本里。
* 「正在读取…」和「没有数据」在视觉上是同一件事（都是一行灰字），用户分不清
  是坏了还是在等。骨架条让「在等」自己看得见。
* 空态只说「暂无记录」，用户不知道是自己没跑、还是坏了、下一步该点什么。

⚠️ 有一条**兼容约束**必须写在测试里：`smoke_live.js` 与 `verify_auto_panel.js`
用 `includes('正在读取')` / `includes('正在读取账户')` 判断「还在加载吗」，
`smoke_live.js` 还断言 `includes('点「生成下单计划」开始')` 与
`includes('尚未生成计划')`。所以改造时**文案一个字都不能改** —— 只换外壳。
下面最后两条断言就是钉这个的。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "webapp" / "static" / "app.js"
UI = ROOT / "webapp" / "static" / "ui.js"
NODE = shutil.which("node")


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉行注释与块注释 —— 注释里提到 `confirm()` 不算调用。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


# ---------------------------------------------------------------- 原生弹窗退场

def test_no_native_blocking_dialogs_are_left():
    code = _strip_comments(_app())
    bad = []
    for m in re.finditer(r"\b(alert|confirm)\s*\(", code):
        line = code[:m.start()].count("\n") + 1
        bad.append(f"{m.group(1)}() @ 第 {line} 行")
    assert not bad, (
        "还有原生阻塞对话框（会冻住轮询与动画）：\n  " + "\n  ".join(bad))


def test_the_run_dialog_shows_the_diff_as_a_table():
    """跑回测前提醒「偏离最优」这件事要保留，但别用纯文本堆。"""
    src = _app()
    i = src.find("async function runNow()")
    body = src[i:src.find("\n}\n", i)]
    assert "ui.modal(" in body, "runNow 没有用模态框提醒偏离"
    assert "diffFromOptimal()" in body
    assert "<table" in body, "差异应该列成表，而不是一行行拼字符串"
    assert "await" in body[:body.find("ui.modal(")], "模态框是异步的，必须 await"


def test_universe_upgrade_stays_gated_and_is_marked_dangerous():
    """补齐宇宙会改宇宙 —— 确认必须保留，且要用 danger 样式。"""
    src = _app()
    i = src.find("const up = $('#liveDriftUpgrade');")
    assert i != -1
    body = src[i:i + 900]
    assert "ui.modal(" in body, "补齐宇宙的确认被删了"
    assert "danger: true" in body, "这是会改变宇宙的动作，应该用危险样式"
    assert "if (!ok) return;" in body, "用户取消后必须真的停下来"
    assert "universe/upgrade" in body


# ---------------------------------------------------------------- 加载态

def test_loading_placeholders_use_the_skeleton_helper():
    src = _app()
    assert "function loadPh(" in src
    assert src.count("loadPh(") >= 8, "加载占位没用统一的 loadPh"
    assert 'class="empty">正在读取' not in src, (
        "还有裸的「正在读取」占位 —— 加载和空态看起来一样")


def test_skeleton_and_caption_are_both_rendered():
    src = _app()
    i = src.find("function loadPh(")
    body = src[i:src.find("\n}\n", i)]
    assert "ui.skeleton(" in body, "loadPh 里没有骨架"
    assert "uiload-cap" in body, "loadPh 里没有说明文字（harness 依赖它）"


# ---------------------------------------------------------------- 空态

def test_main_empty_states_carry_a_next_step():
    src = _app()
    assert "ui.empty(" in src
    # 三个最常看到的空态必须带 desc（下一步做什么）
    for title in ["还没有运行过回测", "还没有可分析的结果", "还没有执行记录"]:
        i = src.find("title: '" + title + "'")
        assert i != -1, f"找不到空态：{title}"
        seg = src[i:i + 220]
        assert "desc:" in seg, f"空态「{title}」没有说明下一步"


# ---------------------------------------------------------------- harness 兼容

@pytest.mark.parametrize("text", [
    "正在读取",                    # smoke_live / verify_auto_panel 的「还在加载吗」
    "正在读取账户",                # smoke_live 切模式断言
    "点「生成下单计划」开始",       # smoke_live 无计划空态
    "尚未生成计划",                # smoke_live 闸门空态
])
def test_harness_asserted_text_is_preserved(text):
    """改外壳可以，改文案会让三个 harness 静默失效。"""
    assert text in _app(), f"文案「{text}」被改了 —— smoke_live/verify_auto_panel 会失效"


def test_vertical_skeleton_bars_do_not_collapse():
    """竖排骨架的条不能塌成 0 高。

    这是**真渲染才发现**的缺陷：`.skel` 是 `flex-direction: column` 且高度 auto，
    `.skbar` 的 `flex: 1` 等于 `flex-basis: 0` ⇒ 每根条被压成 0 像素，界面上只剩
    一句「正在读取」，和没有骨架一模一样。表格形态因为有固定高的 `.skrow` 才没
    露出来 —— 所以只看表格那一屏是发现不了的。
    """
    css = (ROOT / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.skel\s*>\s*\.skbar\s*\{([^}]*)\}", css)
    assert m, "缺 `.skel > .skbar` 覆盖规则 —— 竖排的条会塌成 0 高"
    assert "flex: 0 0 auto" in m.group(1), "竖排的条还在用 flex:1，会塌成 0 高"
    # 横向（表格行内）的分布不能被这条覆盖规则破坏
    m2 = re.search(r"^\.skbar\s*\{([^}]*)\}", css, flags=re.M)
    assert m2 and "flex: 1" in m2.group(1), "行内骨架条丢了 flex:1，宽度不再均分"


def test_modal_can_render_a_single_button():
    """只读对话框不该有两个都能关掉它的按钮。"""
    src = UI.read_text(encoding="utf-8")
    assert "o.hideCancel" in src
    assert "cancelBtn &&" in src, "没有 cancel 按钮时会空指针"


# ---------------------------------------------------------------- 行为

@pytest.mark.skipif(NODE is None, reason="没有 node")
def test_live_panel_renders_skeletons_and_stays_balanced():
    harness = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = process.cwd();
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
const canvas = { clientWidth: 900, clientHeight: 260, width: 0, height: 0,
  getContext: () => new Proxy({}, { get: () => () => {}, set: () => true }) };
function makeEl(sel) {
  if (sel === '#chart' || sel === '#navChart') return canvas;
  if (!els.has(sel)) els.set(sel, new FakeEl());
  return els.get(sel);
}
const sandbox = {
  window: { devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} },
  document: { body: {}, documentElement: { setAttribute() {}, getAttribute() { return null; } },
    querySelector: makeEl, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: 'http://x/' },
  getComputedStyle: () => ({ getPropertyValue: () => '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval, console,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' }),
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
let src = fs.readFileSync('webapp/static/ui.js', 'utf8') + '\n'
        + fs.readFileSync('webapp/static/app.js', 'utf8');
const e = src.match(/\n(authRender|boot)\(\);\s*$/);
src = src.slice(0, e.index) + '\n';
src += '\n;globalThis.__T = { S, liveSkeleton, livePlanHtml, liveGatesHtml, liveJobHtml, trLoading };';
vm.createContext(sandbox); vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;

const count = (h, re) => (h.match(re) || []).length;
const bal = (h) => count(h, /<div\b/g) === count(h, /<\/div>/g);
const skel = T.liveSkeleton();
const plan = T.livePlanHtml();
const gates = T.liveGatesHtml();
const job = T.liveJobHtml();
const tr = T.trLoading();
console.log('RESULT ' + JSON.stringify({
  skelBalanced: bal(skel),
  planBalanced: bal(plan), gatesBalanced: bal(gates), jobBalanced: bal(job), trBalanced: bal(tr),
  skeletons: count(skel, /class="skel/g),
  captions: count(skel, /uiload-cap/g),
  skelHasLoadingText: skel.includes('正在读取'),
  planText: plan.includes('点「生成下单计划」开始'),
  gatesText: gates.includes('尚未生成计划'),
  jobText: job.includes('还没有执行记录'),
  planEmptyClass: plan.includes('uiempty'),
  gatesEmptyClass: gates.includes('uiempty'),
  trHasSkeleton: tr.includes('class="skel') && tr.includes('正在读取'),
}));
"""
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, f"harness 跑不动：{r.stderr[-800:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-400:]}"
    o = json.loads(line[0][len("RESULT "):])

    assert o["skelBalanced"], "liveSkeleton 的 div 不配平"
    assert o["planBalanced"] and o["gatesBalanced"] and o["jobBalanced"] and o["trBalanced"], \
        "空态/加载态的 div 不配平"
    assert o["skeletons"] >= 4, f"骨架太少：{o['skeletons']}"
    assert o["captions"] == o["skeletons"], "每个骨架都应该配一句说明"
    assert o["skelHasLoadingText"], "加载文案丢了 —— harness 的「还在加载吗」会失效"
    assert o["planText"] and o["gatesText"] and o["jobText"], "空态文案被改了"
    assert o["planEmptyClass"] and o["gatesEmptyClass"], "空态没走 ui.empty"
    assert o["trHasSkeleton"], "交易记录加载态没有骨架"
