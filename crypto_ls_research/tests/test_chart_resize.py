r"""三处 canvas 必须跟着容器尺寸重绘，不能只听 window.resize。

为什么值得一条守卫：画布拿不到宽度**不只**是窗口尺寸变了。侧栏折叠、切回
tab（display:none 期间 clientWidth 是 0）、异步内容把容器撑开 —— 这些都不发
resize，画布就停在 0 宽或旧尺寸上，界面上看到的是一块空白。而空白画布不会
报错、不会进日志，只有人眼能发现，所以只能靠测试钉住。

三个受控对象：
  #chart     回测净值曲线   drawChart()
  #expChart  敞口序列       drawExposure()
  #navChart  实盘净值曲线   drawNavChart()
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
NODE = shutil.which("node")

CANVASES = [
    ("drawChart", "async function drawChart() {"),
    ("drawExposure", "function drawExposure(d) {"),
    ("drawNavChart", "function drawNavChart() {"),
]


def _src() -> str:
    return APP.read_text(encoding="utf-8")


def _body(src: str, header: str) -> str:
    """取一个顶层函数的函数体（到下一个顶格 `}` 为止）。"""
    i = src.find(header)
    assert i != -1, f"找不到函数：{header}"
    j = src.find("\n}\n", i)
    assert j != -1, f"函数体没有闭合：{header}"
    return src[i:j + 3]


# ---------------------------------------------------------------- 静态接线

@pytest.mark.parametrize("name,header", CANVASES)
def test_every_canvas_registers_a_resize_observer(name, header):
    body = _body(_src(), header)
    assert "autoRedraw(" in body, (
        f"{name} 没有挂 autoRedraw —— 这块画布只会在 window.resize 时重绘")


def test_observer_is_registered_before_the_zero_width_bail_out():
    """必须在「尺寸为 0 就早退」之前注册。

    反过来的话：画布隐藏时第一次绘制早退、观察者也没挂上，等它可见时
    没有任何东西会再调一次绘制 —— 正好是「切回 tab 曲线空白」那个 bug。
    """
    body = _body(_src(), "async function drawChart() {")
    i_reg = body.find("autoRedraw(")
    i_bail = body.find("if (!cv.clientWidth")
    assert i_reg != -1 and i_bail != -1, "drawChart 里缺注册或缺早退"
    assert i_reg < i_bail, "autoRedraw 必须在 clientWidth 早退之前调用"


def test_observer_watches_the_container_not_the_canvas():
    """盯 canvas 自己会被 drawXxx 写 cv.width/height 触成回环。"""
    src = _src()
    assert "observe(host)" in src, "没观察容器"
    assert "cv.parentElement || cv" in src, "host 没取父元素"
    assert not re.search(r"\.observe\(\s*cv\s*\)", src), (
        "直接 observe(cv) 会被画布属性写入触发回环")


def test_redraw_is_skipped_when_the_size_did_not_actually_change():
    """回环的最后一道保险：尺寸没变就不重画。"""
    src = _src()
    assert "cv.clientWidth === ent.w" in src and "cv.clientHeight === ent.h" in src


def test_observer_does_not_draw_while_the_canvas_is_hidden():
    src = _src()
    i = src.find("function autoRedraw(cv, fn)")
    assert i != -1
    body = src[i:src.find("\n}\n", i)]
    assert "if (!cv.clientWidth || !cv.clientHeight) return;" in body, (
        "观察者回调里没有隐藏保护 —— 会画出全是 NaN 的坐标")


def test_repeated_registration_is_idempotent():
    """drawChart 每次 hover 都会重画，不能每次都新建一个观察者。"""
    src = _src()
    assert "new WeakMap()" in src, "没有按元素记住观察者"
    assert "if (prev.host === host) return;" in src, "同一容器会重复建观察者"


# ---------------------------------------------------------------- 行为

@pytest.mark.skipif(NODE is None, reason="没有 node")
def test_auto_redraw_behaviour_under_a_fake_resize_observer():
    """真跑 autoRedraw：把 ResizeObserver 换成可手动触发的假货。

    只测接线（上面几条）不够 —— 「尺寸没变不重画」和「换容器要重建」
    这类逻辑只有跑起来才能验。
    """
    src = _src()
    i0 = src.find("const _chartRO = new WeakMap();")
    i1 = src.find("async function drawChart() {")
    assert i0 != -1 and i1 != -1 and i0 < i1, "没框出 autoRedraw 的定义块"
    helper = src[i0:i1]

    harness = r"""
const fs = require('fs');
const vm = require('vm');
class RO {
  constructor(cb) { this.cb = cb; this.disconnected = false; RO.insts.push(this); }
  observe(t) { this.target = t; }
  disconnect() { this.disconnected = true; }
  fire() { if (!this.disconnected) this.cb(); }
}
RO.insts = [];
const sandbox = { ResizeObserver: RO, WeakMap, console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(HELPER + '\n;globalThis.__autoRedraw = autoRedraw;', sandbox);
const autoRedraw = sandbox.__autoRedraw;

const out = {};
const host1 = { id: 'h1' }, host2 = { id: 'h2' };
const cv = { clientWidth: 0, clientHeight: 0, parentElement: host1 };
let calls = 0;
autoRedraw(cv, () => { calls++; });
out.observersAfterFirst = RO.insts.length;

RO.insts[0].fire();                       // 隐藏（0 宽）→ 不许画
out.callsWhileHidden = calls;

cv.clientWidth = 400; cv.clientHeight = 200;
RO.insts[0].fire();                       // 变可见 → 补画一次
out.callsAfterVisible = calls;

RO.insts[0].fire();                       // 尺寸没变 → 不重画
out.callsAfterSameSize = calls;

cv.clientWidth = 520;
RO.insts[0].fire();                       // 尺寸变了 → 再画
out.callsAfterResize = calls;

autoRedraw(cv, () => { calls++; });       // 幂等：同容器不新建
out.observersAfterReRegister = RO.insts.length;

cv.parentElement = host2;
autoRedraw(cv, () => { calls++; });       // 换容器 → 重建
out.observersAfterHostChange = RO.insts.length;
out.oldDisconnected = RO.insts[0].disconnected;

console.log('RESULT ' + JSON.stringify(out));
"""
    harness = harness.replace("HELPER", json.dumps(helper))
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"harness 跑不动：{r.stderr[-800:]}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-400:]}"
    o = json.loads(line[0][len("RESULT "):])

    assert o["observersAfterFirst"] == 1, "第一次注册就该有且只有一个观察者"
    assert o["callsWhileHidden"] == 0, "隐藏时画布是 0 宽，不该画（坐标全是 NaN）"
    assert o["callsAfterVisible"] == 1, "从隐藏变可见必须补画一次"
    assert o["callsAfterSameSize"] == 1, "尺寸没变又画了一遍 —— 会形成回环"
    assert o["callsAfterResize"] == 2, "容器变宽了却没重画"
    assert o["observersAfterReRegister"] == 1, "同一容器重复注册时新建了观察者"
    assert o["observersAfterHostChange"] == 2, "换了容器却没重建观察者"
    assert o["oldDisconnected"] is True, "旧观察者没断开，会继续往旧容器上画"
