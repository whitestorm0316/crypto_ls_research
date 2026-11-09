r"""全局壳的状态：登录页整屏独立、参数面板可收起。

两条都是**用户直接看得见的缺陷**，不是美化：

1. 登录表单原先渲染在 `#content` 里 ⇒ 未登录时顶栏（含「运行回测」按钮）和
   412px 空侧栏照样可见，登录框被挤在右边。`body.authmode` 把整个壳收起来。
2. 侧栏 `412px` 是常驻的，可交易台/交易记录页一个回测参数都用不到 —— 白占
   32% 宽度。给开关，并把选择记在 localStorage。

第 2 条有一条容易写错的规则：**用户一旦手动点过开关，切 tab 就不许再自动改**。
它是唯一需要真跑才能验的东西，所以下面有一条行为测试。
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
CSS = ROOT / "webapp" / "static" / "style.css"
HTML = ROOT / "webapp" / "static" / "index.html"
NODE = shutil.which("node")


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _rule(src: str, selector: str) -> str:
    """取一条 CSS 规则的声明块（selector 需逐字匹配）。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", src)
    assert m, f"找不到 CSS 规则：{selector}"
    return m.group(1)


# ---------------------------------------------------------------- 登录页

@pytest.mark.parametrize("fn", ["authLogin", "authSetup"])
def test_login_and_setup_turn_on_the_full_screen_mode(fn):
    src = _app()
    i = src.find(f"function {fn}(")
    assert i != -1, f"找不到 {fn}"
    body = src[i:src.find("\n}\n", i)]
    assert "authMode(true)" in body, f"{fn} 没有切到整屏登录态 —— 顶栏和空侧栏还会露出来"


def test_boot_turns_the_full_screen_mode_off():
    src = _app()
    i = src.find("async function boot() {")
    body = src[i:src.find("\n}\n", i)]
    assert "authMode(false)" in body, (
        "boot 没有退出 authmode：登录后顶栏与侧栏会一直是隐藏的")


def test_auth_shell_uses_the_centered_card_not_inline_max_width():
    src = _app()
    i = src.find("function authShell(inner)")
    body = src[i:src.find("\n}\n", i)]
    assert "authcard" in body and "authwrap" in body
    assert "max-width:420px" not in body, "还在用内联宽度 —— 样式应该落在 CSS 里"


def test_css_hides_the_shell_in_auth_mode():
    css = CSS.read_text(encoding="utf-8")
    sel = "body.authmode .topbar,\nbody.authmode .side"
    assert "display: none" in _rule(css, sel), "authmode 没把顶栏与侧栏藏起来"
    assert "1fr" in _rule(css, "body.authmode .main"), "authmode 下主区应占满整行"
    # 登录框必须真的居中，而不是靠 margin 猜
    assert "place-items: center" in _rule(css, ".authwrap")


# ---------------------------------------------------------------- 侧栏收起

def test_there_is_a_toggle_button_in_the_shell():
    html = HTML.read_text(encoding="utf-8")
    assert 'id="sideBtn"' in html, "顶栏没有收起按钮"


def test_collapsing_gives_the_width_back_to_the_content():
    css = CSS.read_text(encoding="utf-8")
    assert "0 1fr" in _rule(css, "body.sidecollapsed .main"), (
        "收起后主区没有拿到 412px")
    assert "display: none" in _rule(css, "body.sidecollapsed .side")


def test_toggle_is_wired_in_boot_and_tab_switch():
    src = _app()
    i = src.find("async function boot() {")
    assert "bindSideToggle()" in src[i:src.find("\n}\n", i)], "boot 没绑定收起按钮"
    i2 = src.find("$$('[data-tab]').forEach")
    assert "syncSideForTab()" in src[i2:i2 + 700], "切 tab 时没同步侧栏状态"


def test_user_preference_beats_the_per_tab_default():
    """`syncSideForTab` 必须让位于显式选择。"""
    src = _app()
    i = src.find("function syncSideForTab()")
    body = src[i:src.find("\n}\n", i)]
    assert "sidePref() !== null" in body and "return" in body, (
        "切 tab 会覆盖用户手动设过的展开/收起")


# ---------------------------------------------------------------- 行为

@pytest.mark.skipif(NODE is None, reason="没有 node")
def test_side_toggle_behaviour():
    """真跑 setSide / syncSideForTab / 点击回调，用假 DOM。"""
    src = _app()
    i0 = src.find("const SIDE_KEY = 'ls.side.collapsed';")
    i1 = src.find("function renderSide() {")
    assert i0 != -1 and i1 != -1 and i0 < i1, "没框出侧栏开关的定义块"
    helper = src[i0:i1]

    prelude = r"""
const cls = new Set();
const body = { classList: {
  add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
  toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); },
} };
const btn = { textContent: '', title: '', attrs: {},
  setAttribute(k, v) { this.attrs[k] = v; } };
const document = { body };
const $ = (s) => (s === '#sideBtn' ? btn : null);
const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};
const S = { tab: 'result' };
"""

    epilogue = r"""
const out = {};
// 1) 无偏好 + 回测结果页 → 展开
bindSideToggle();
out.initialCollapsed = cls.has('sidecollapsed');
out.initialText = btn.textContent;
out.initialAria = btn.attrs['aria-expanded'];

// 2) 无偏好 + 切到交易台 → 自动收起
S.tab = 'live';
syncSideForTab();
out.afterTabLive = cls.has('sidecollapsed');
out.afterTabLiveText = btn.textContent;

// 3) 用户点一下 → 展开，并写进 localStorage
btn.onclick();
out.afterClick = cls.has('sidecollapsed');
out.stored = localStorage.getItem('ls.side.collapsed');

// 4) 已经有显式偏好：切回回测结果页也不许自动展开
S.tab = 'result';
syncSideForTab();
out.afterTabBack = cls.has('sidecollapsed');

// 5) 显式偏好为「收起」时，即使在回测结果页也保持收起
localStorage.setItem('ls.side.collapsed', '1');
bindSideToggle();
out.prefCollapsed = cls.has('sidecollapsed');
console.log('RESULT ' + JSON.stringify(out));
"""

    harness = prelude + helper + epilogue
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"harness 跑不动：{r.stderr[-800:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-400:]}"
    o = json.loads(line[0][len("RESULT "):])

    assert o["initialCollapsed"] is False, "回测结果页默认应展开（要在那儿调参数）"
    assert o["initialText"] == "◧", "展开态图标不对"
    assert o["initialAria"] == "true", "展开态 aria-expanded 不对"
    assert o["afterTabLive"] is True, "切到交易台没自动收起"
    assert o["afterTabLiveText"] == "◨", "收起态图标不对"
    assert o["afterClick"] is False, "点一下没展开"
    assert o["stored"] == "0", "点击后没把选择写进 localStorage"
    assert o["afterTabBack"] is False, (
        "用户手动展开后切回回测结果页被自动收起了 —— 偏好没被尊重")
    assert o["prefCollapsed"] is True, "显式偏好为收起时没有保持收起"
