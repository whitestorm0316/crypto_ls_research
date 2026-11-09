"""订单表枚举翻译的守卫测试。

为什么需要它
------------
订单表的 `state` / `action` 两列以前**原样铺出去**，用户在盘面上读到的是
`filled` / `flip_close` / `increase`。同一个毛病项目已经为自动任务修过一次
（`LIVE_ACTION_LABEL`），订单表漏了。

这里钉住三条：
  1. 已知值翻译成**含义**（`increase` = 加仓，不是"增加"；`flip_close` 与
     `flip_open` 必须能区分 —— 它们是反手的两笔，混在一起会让一笔反手
     看起来像重复下单）；
  2. **未知值原样显示**，不许吞成「—」或「未知」。交易所新增一个状态时，
     把真值露出来才能发现；吞掉它等于让新状态永远隐形；
  3. 未成交的单必须有视觉区分（它还占着敞口但可以撤，和已成交不是一回事）。
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
UI = ROOT / "webapp" / "static" / "ui.js"


# ---------------------------------------------------------------------------
# 1) 静态：翻译表被真的用上了
# ---------------------------------------------------------------------------
def _orders_branch() -> str:
    src = APP.read_text(encoding="utf-8")
    i = src.find("if (key === 'orders') {")
    assert i > 0, "订单表分支找不到了"
    j = src.find("if (key === 'fills') {", i)
    assert j > i
    return src[i:j]


def test_order_table_translates_state_and_action():
    """相邻对：两个翻译表都必须被真的调用，而不是只定义了没人用。"""
    body = _orders_branch()
    assert "labelOf(ORDER_ACTION_LABEL" in body, "动作列没有走翻译表"
    assert "labelOf(ORDER_STATE_LABEL" in body, "状态列没有走翻译表"


def test_order_table_no_longer_prints_raw_enums():
    """裸枚举不许再出现在这两个单元格里。"""
    body = _orders_branch()
    assert not re.search(r"esc\(r\.action \|\| ''\)", body), "动作列仍在直接输出裸值"
    assert not re.search(r"esc\(r\.state \|\| ''\)", body), "状态列仍在直接输出裸值"


def test_unfilled_orders_are_visually_distinct():
    """未成交（挂单/部分成交）必须有独立样式，不能和已成交长得一样。"""
    body = _orders_branch()
    assert "ostate" in body, "订单状态没有任何视觉区分"
    assert re.search(r"r\.state === 'live'", body), "没有识别挂单状态"
    assert re.search(r"r\.state === 'partially_filled'", body), "没有识别部分成交"


# ---------------------------------------------------------------------------
# 2) 动态：翻译函数的行为（含未知值）
# ---------------------------------------------------------------------------
_NODE_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');
const APP = process.argv[2];

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
const makeEl = (s) => { if (!els.has(s)) els.set(s, new FakeEl()); return els.get(s); };
const sandbox = {
  window: { devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} },
  document: { body: {}, documentElement: { setAttribute() {}, getAttribute() { return null; } },
              querySelector: makeEl, querySelectorAll: () => [], addEventListener() {},
              createElement: () => new FakeEl() },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: 'http://x/' },
  getComputedStyle: () => ({ getPropertyValue: () => '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval, console,
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
sandbox.fetch = () => Promise.reject(new Error('no network'));

let src = fs.readFileSync(APP, 'utf8');
const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
if (!entry) throw new Error('app.js 末尾入口未找到');
src = src.slice(0, entry.index) + '\n';
src = fs.readFileSync(path.join(path.dirname(APP), 'ui.js'), 'utf8') + '\n' + src;
src += '\n;globalThis.__T = { labelOf, ORDER_STATE_LABEL, ORDER_ACTION_LABEL };\n';
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;

const probe = {
  state_known: ['filled', 'live', 'partially_filled', 'canceled', 'failed']
    .map((k) => [k, T.labelOf(T.ORDER_STATE_LABEL, k)]),
  action_known: ['open', 'close', 'increase', 'reduce', 'flip_close', 'flip_open']
    .map((k) => [k, T.labelOf(T.ORDER_ACTION_LABEL, k)]),
  unknown: T.labelOf(T.ORDER_STATE_LABEL, 'brand_new_state_from_exchange'),
  empty: T.labelOf(T.ORDER_STATE_LABEL, ''),
  nil: T.labelOf(T.ORDER_STATE_LABEL, null),
};
process.stdout.write(JSON.stringify(probe));
"""


def _probe() -> dict:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists():
        pytest.skip("node 不可用")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_NODE_HARNESS)
        js = f.name
    try:
        p = subprocess.run([node, js, str(APP)], capture_output=True, text=True, timeout=60)
    finally:
        Path(js).unlink(missing_ok=True)
    assert p.returncode == 0, f"harness 失败：\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)


def test_known_states_and_actions_are_translated_to_chinese():
    d = _probe()
    for k, v in d["state_known"]:
        assert re.search(r"[\u4e00-\u9fff]", v), f"state {k} 没翻译：{v!r}"
    for k, v in d["action_known"]:
        assert re.search(r"[\u4e00-\u9fff]", v), f"action {k} 没翻译：{v!r}"


def test_flip_legs_stay_distinguishable():
    """反手是两笔（平 + 开）。翻译成一模一样会让一笔反手看起来像重复下单。"""
    d = _probe()
    acts = dict(d["action_known"])
    assert acts["flip_close"] != acts["flip_open"], (
        f"反手的两笔翻译成了同一个词：{acts['flip_close']!r} —— 会看起来像重复下单")


def test_unknown_enum_is_shown_not_swallowed():
    """未知值必须原样露出来。

    显示成「—」或「未知」的话，交易所新增一个状态时页面上永远看不见它，
    而持仓和订单会悄悄地处于一个没人认识的状态。
    """
    d = _probe()
    assert d["unknown"] == "brand_new_state_from_exchange", (
        f"未知状态被吞成了 {d['unknown']!r} —— 新状态会永远隐形")


def test_empty_state_renders_a_dash():
    d = _probe()
    assert d["empty"] == "—" and d["nil"] == "—", (
        f"空值应该显示 —，实际 empty={d['empty']!r} nil={d['nil']!r}")
