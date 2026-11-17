"""标的池 / 流动性快照（`universe/snapshot.py`）+ 新页面接线的守卫。

三组：
1. **模块口径**——合成 parquet 缓存，不碰真盘真网。重点是那条 24 倍的坑：
   门槛用的是「每根 bar 的平均成交额」，不是日成交额。按日算过线的会从 16 只
   变成 196 只。另覆盖 rank 截断、上市年龄、正在形成的 bar、放量比、
   ADV 上限、缓存与永不抛。
2. **接线**——`/api/universe` 路由、前端第 4 个 tab、CSS 四态显隐。
   相邻对断言：既验存在、又验挂在正确的上下文里。
3. **前端真渲染**——node vm 加载**真实** app.js，用真实载荷渲染表格，
   断言行数、剔除行的默认隐藏、「显示全部」、放量条方向、div 配平。
   grep 通过但页面空白的教训见 test_auth —— 不允许只靠字符串存在性放行。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config.settings import UniverseConfig
from crypto_ls_research.universe import snapshot as snap

ROOT = Path(__file__).resolve().parents[2]
BAR = "1h"
BPD = 24
NOW = pd.Timestamp("2026-11-17T08:30:00Z")      # 08:00 那根还没收线
LAST = pd.Timestamp("2026-11-17T08:00:00Z")

SMALL = "SMALL-USDT-SWAP"      # 每 bar 2.9M —— 日频口径会过线，每 bar 不会
BIG = "BIG-USDT-SWAP"
MID = "MID-USDT-SWAP"
THIRD = "THIRD-USDT-SWAP"      # 过门槛但被 rank 截断
YOUNG = "YOUNG-USDT-SWAP"      # 只有 10 天历史


def _frame(per_bar: float, n: int, last: pd.Timestamp = LAST,
           tail24: float | None = None) -> pd.DataFrame:
    """n 根 1h bar，`per_bar` 是每根 bar 的成交额；`tail24` 覆盖最后 24 根。"""
    idx = pd.date_range(end=last, periods=n, freq="1h")
    amt = np.full(n, float(per_bar))
    if tail24 is not None:
        amt[-BPD:] = float(tail24)
    return pd.DataFrame({"amount": amt, "close": np.full(n, 100.0)}, index=idx)


def _build_cache(root: Path, frames: dict) -> Path:
    cdir = root / "candles" / BAR
    cdir.mkdir(parents=True, exist_ok=True)
    for inst, df in frames.items():
        df.to_parquet(cdir / f"{inst}.parquet")
    return root


def _cfg(**kw) -> UniverseConfig:
    base = dict(min_history_days=30.0, liq_window_days=30.0,
                min_avg_amount_usd=3_000_000.0, pit_topn=100)
    base.update(kw)
    return UniverseConfig(**base)


def _run(tmp_path: Path, frames: dict, cfg: UniverseConfig | None = None,
         now: pd.Timestamp = NOW, **kw) -> dict:
    cache = _build_cache(tmp_path / "cache", frames)
    return snap.compute(bar=BAR, asset_class="all", ucfg=cfg or _cfg(),
                        cache_dir=str(cache), now=now, insts=list(frames), **kw)


def _by_inst(res: dict) -> dict:
    return {r["inst"]: r for r in res["rows"]}


# ---------------------------------------------------------------------------
# 1) 模块口径
# ---------------------------------------------------------------------------
def test_gate_uses_the_per_bar_adv_not_the_daily_one(tmp_path):
    """🔴 这条是本页最容易读错的地方。

    `SMALL` 每根 bar 成交 2.9M ⇒ 折日 69.6M，按「日成交额 3M」的口径**过线**；
    但引擎的 `adv` 是**每 bar 均值**，2.9M < 3M ⇒ 必须被剔除。
    如果哪天有人把门槛改成日频，这条会立刻红。
    """
    res = _run(tmp_path, {SMALL: _frame(2.9e6, 720)})
    r = _by_inst(res)[SMALL]
    assert r["adv"] == pytest.approx(2.9e6, rel=1e-9)
    assert r["adv_day"] == pytest.approx(2.9e6 * BPD, rel=1e-9)
    assert r["adv_day"] > 3_000_000, "折日口径下它是过线的 —— 所以这个用例才有意义"
    assert r["in_pool"] is False
    assert r["reason"] == "below_adv"
    assert res["summary"]["n_pool"] == 0


def test_rank_cut_and_liquidity_order(tmp_path):
    res = _run(tmp_path, {BIG: _frame(10e6, 720), MID: _frame(5e6, 720),
                          THIRD: _frame(4e6, 720)}, cfg=_cfg(pit_topn=2))
    rows = _by_inst(res)
    assert rows[BIG]["rank"] == 1 and rows[BIG]["in_pool"] is True
    assert rows[MID]["rank"] == 2 and rows[MID]["in_pool"] is True
    assert rows[THIRD]["rank"] == 3 and rows[THIRD]["in_pool"] is False
    assert rows[THIRD]["reason"] == "rank_cut"
    assert res["summary"]["n_eligible"] == 3
    assert res["summary"]["n_pool"] == 2


def test_too_young_is_excluded_even_with_huge_volume(tmp_path):
    res = _run(tmp_path, {YOUNG: _frame(50e6, 10 * BPD)}, cfg=_cfg(pit_topn=100))
    r = _by_inst(res)[YOUNG]
    assert r["age_days"] == pytest.approx(10.0)
    assert r["in_pool"] is False
    assert r["reason"] == "too_young"


def test_forming_bar_is_dropped_from_the_window(tmp_path):
    """正在形成的那根 bar 只累积了一部分成交额，算进去会让放量比在整点附近跳一下。"""
    df = _frame(4e6, 720)
    df.iloc[-1, df.columns.get_loc("amount")] = 1.0     # 08:00 那根刚开始
    res = _run(tmp_path, {BIG: df})
    r = _by_inst(res)[BIG]
    assert res["asof"].startswith("2026-11-17T07:00"), res["asof"]
    assert r["adv"] == pytest.approx(4e6, rel=1e-9)
    assert r["turn_24h"] == pytest.approx(24 * 4e6, rel=1e-9)


def test_chg_is_24h_turnover_over_the_daily_mean(tmp_path):
    """放量比必须能独立算出来：24h 实际成交额 ÷ 折日均值 − 1。

    ⚠️ 不要在这里写死期望值：08:00 那根是**正在形成**的，会被丢掉，窗口整体
    后移一根，最后 24 根里只有 23 根落在 8M 段上。期望值从 fixture 自己推出来
    （取 `-25:-1`，即「丢掉最后一根之后的 24 根」），这样口径变了也不会假绿。
    """
    df = _frame(4e6, 720, tail24=8e6)
    res = _run(tmp_path, {BIG: df})
    r = _by_inst(res)[BIG]
    expect_24h = float(df["amount"].iloc[-25:-1].sum())
    assert r["turn_24h"] == pytest.approx(expect_24h, rel=1e-9)
    assert r["adv"] > 4e6, "最后 24 根里有一部分是 8M，均值必然高于 4M"
    assert r["chg"] == pytest.approx(r["turn_24h"] / r["adv_day"] - 1.0, rel=1e-12)
    assert r["chg"] > 0


def test_adv_cap_is_five_percent_of_the_per_bar_adv(tmp_path):
    """`risk.adv_cap_delta` 的预算 = adv × 5%，用的是**每 bar** 的 adv。
    写成 adv_day × 5% 会大 24 倍，而这个错不会报任何异常。"""
    res = _run(tmp_path, {BIG: _frame(10e6, 720)})
    r = _by_inst(res)[BIG]
    assert r["adv_cap"] == pytest.approx(10e6 * 0.05, rel=1e-9)
    assert r["adv_cap"] != pytest.approx(r["adv_day"] * 0.05, rel=1e-3)


def test_summary_counts_and_concentration(tmp_path):
    frames = {BIG: _frame(100e6, 720), MID: _frame(50e6, 720),
              THIRD: _frame(10e6, 720), SMALL: _frame(1e6, 720)}
    res = _run(tmp_path, frames, cfg=_cfg(pit_topn=100))
    s = res["summary"]
    assert s["n_cached"] == 4
    assert s["n_eligible"] == 3
    assert s["n_pool"] == 3
    assert s["adv_day_total"] == pytest.approx((100 + 50 + 10) * 1e6 * BPD, rel=1e-9)
    assert s["top10_share"] == pytest.approx(1.0)      # 只有 3 只，全在 top10
    assert res["n_on_disk"] == 4


def test_missing_history_is_reported_as_no_data_not_as_zero(tmp_path):
    """没有行情的标的不能当成「成交额 0」——那是两件事，后者会让人以为它被
    流动性门槛剔掉了。"""
    empty = pd.DataFrame({"amount": pd.Series(dtype="float64"),
                          "close": pd.Series(dtype="float64")},
                         index=pd.DatetimeIndex([], tz="UTC"))
    res = _run(tmp_path, {BIG: _frame(10e6, 720), "EMPTY-USDT-SWAP": empty})
    r = _by_inst(res)["EMPTY-USDT-SWAP"]
    assert r["reason"] == "no_data"
    assert r["adv"] is None and r["adv_day"] is None


def test_snapshot_caches_then_refresh_bypasses(tmp_path):
    cache = _build_cache(tmp_path / "cache", {BIG: _frame(10e6, 720)})
    kw = dict(bar=BAR, asset_class="all", ucfg=_cfg(), cache_dir=str(cache),
              now=NOW, insts=[BIG])
    a = snap.snapshot(**kw)
    b = snap.snapshot(**kw)
    c = snap.snapshot(refresh=True, **kw)
    assert a["ok"] and a["from_cache"] is False
    assert b["ok"] and b["from_cache"] is True
    assert c["ok"] and c["from_cache"] is False
    assert a["summary"] == b["summary"]


def test_snapshot_never_raises_on_a_missing_cache(tmp_path):
    r = snap.snapshot(bar=BAR, asset_class="all", cache_dir=str(tmp_path / "nope"),
                      now=NOW)
    assert r["ok"] is False
    assert r["error"]
    assert r["rows"] == []


def test_unknown_category_does_not_break_the_scope(tmp_path):
    """`filter_insts` 对没有分类的标的返回 unknown。页面不能因为分类快照缺一条
    就整页失败 —— 记一条 note 就够（引擎那边是 fail-loud，两边的取舍不同）。"""
    cache = _build_cache(tmp_path / "cache", {BIG: _frame(10e6, 720)})
    res = snap.compute(bar=BAR, asset_class="crypto", ucfg=_cfg(),
                       cache_dir=str(cache), now=NOW, insts=[BIG])
    # 真盘上的分类快照可能认识也可能不认识 BIG；两种都要能出结果
    assert res["ok"] is True
    assert isinstance(res["notes"], list)


# ---------------------------------------------------------------------------
# 2) 接线（静态相邻对断言）
# ---------------------------------------------------------------------------
SERVER = ROOT / "webapp" / "server.py"
APP = ROOT / "webapp" / "static" / "app.js"
CSS = ROOT / "webapp" / "static" / "style.css"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_server_routes_api_universe():
    s = _src(SERVER)
    assert 'if p == "/api/universe":' in s
    assert "self._universe(q)" in s
    # 方法真的定义了，而且挂在 handler 上（不是写在别处的孤儿函数）
    assert "    def _universe(self, q):" in s
    # 定义里的口径注释必须在 —— 这条注释是防止有人把它改成日频的唯一提示
    i = s.index("    def _universe(self, q):")
    body = s[i:i + 2600]
    assert "每根 bar" in body
    assert "UniverseConfig()" in body


def test_server_merges_held_positions_without_failing():
    s = _src(SERVER)
    i = s.index("    def _universe(self, q):")
    body = s[i:i + 2600]
    assert 'body["held"] = self._held_positions(mode)' in body
    j = s.index("    def _held_positions(mode):")
    held = s[j:j + 1200]
    # 读不到持仓只能当「没有」，不能让整页 500
    assert "except BaseException" in held
    assert "return {}" in held


def test_frontend_has_a_fourth_tab_wired_to_the_route():
    s = _src(APP)
    assert 'data-tab="universe"' in s
    assert "function ensureUniverse(" in s
    assert "'/api/universe'" in s or "/api/universe" in s
    # 路由参数必须来自 spec，不许在前端写死资产类别
    i = s.index("async function ensureUniverse(")
    body = s[i:i + 1400]
    assert "S.optimalCli" in body and "asset_class" in body
    # 先读 text 再 parse：501+HTML 会让 resp.json() 抛 Unexpected token '<'
    assert "await r.text()" in body
    assert "await r.json()" not in body


def test_polling_repaints_the_universe_page_instead_of_resetting_it():
    """`pollRuns()` 每 2.5s 重建整块 #content。少了这一行，页面每 2.5 秒退回
    骨架 —— 而且不报错。"""
    s = _src(APP)
    i = s.index("if (S.tab === 'universe') {")
    body = s[i:i + 400]
    assert "paintUniverse()" in body
    assert "ensureUniverse()" in body
    # 与交易台那条相邻：两个分支都要在 renderContent 的尾部
    assert s.index("if (S.tab === 'live') {") < i


def test_universe_container_is_rendered_in_the_page_shell():
    s = _src(APP)
    assert '<div class="universeOnly" id="uniRoot">' in s
    assert "el.classList.toggle('universe', S.tab === 'universe')" in s


def test_threshold_is_never_hardcoded_in_the_frontend():
    s = _src(APP)
    assert "3000000" not in s and "3_000_000" not in s
    i = s.index("function universeBodyHtml()")
    body = s[i:i + 5000]
    # 门槛必须从载荷里读
    assert "p.min_avg_amount_usd" in body


def test_css_shows_exactly_one_tab_at_a_time():
    css = _src(CSS)
    assert ".resultOnly, .tradesOnly, .liveOnly, .universeOnly { display: block; }" in css
    assert ".tradesOnly, .liveOnly, .universeOnly { display: none; }" in css
    for st in ("trades", "live", "universe"):
        assert f"#content.{st} .universeOnly {{" in css
    assert "#content.universe .universeOnly { display: block; }" in css
    # 每个状态的四个面板都要被显式处理：漏一条 = 两页叠在一起
    for st in ("trades", "live", "universe"):
        block = css[css.index(f"#content.{st} .resultOnly"):]
        block = block[:block.index("}") + 1]
        for cls in ("resultOnly", "tradesOnly", "liveOnly", "universeOnly"):
            assert f"#content.{st} .{cls}" in css, f"{st} 状态漏了 .{cls}"


def test_wide_table_scrolls_inside_its_own_wrapper():
    """10 列的表不能把 `.content` 撑破（`1fr` 是 `minmax(auto,1fr)`）。"""
    css = _src(CSS)
    assert ".uniwrap { overflow-x: auto; }" in css
    assert "table.uni { min-width:" in css


# ---------------------------------------------------------------------------
# 3) 前端真渲染（node vm 加载真实 app.js）
# ---------------------------------------------------------------------------
NODE_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');
const APP = process.argv[2];
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
              querySelector: makeEl, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: 'http://127.0.0.1:8790/' },
  getComputedStyle: () => ({ getPropertyValue: () => '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval, console,
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
sandbox.fetch = () => Promise.reject(new Error('no network in render test'));

let src = fs.readFileSync(APP, 'utf8');
const entry = src.match(/\n(authRender|boot)\(\);\s*$/);
if (!entry) throw new Error('app.js 末尾的入口调用未找到（authRender/boot）');
src = src.slice(0, entry.index) + '\n';
const UI = path.join(path.dirname(APP), 'ui.js');
if (!fs.existsSync(UI)) throw new Error('找不到 ui.js：' + UI);
src = fs.readFileSync(UI, 'utf8') + '\n' + src;
src += '\n;globalThis.__T = { S, universeBodyHtml, universeSkeleton, uniBadge, liqBar };\n';
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;

function divBalance(html) {
  const re = /<(\/?)div\b[^>]*>/gi; let depth = 0, minDepth = 0, m;
  while ((m = re.exec(html))) { depth += m[1] === '/' ? -1 : 1; if (depth < minDepth) minDepth = depth; }
  return { depth, minDepth };
}
const checks = [];
const check = (n, ok, extra) => checks.push({ name: n, ok: !!ok, extra: extra == null ? '' : String(extra) });

/* --- 空载荷：骨架要有挂载点 --- */
const skel = T.universeSkeleton();
check('骨架挂载 #uniBody', skel.includes('id="uniBody"'));
check('骨架有重新计算按钮', skel.includes('id="uniReload"'));
{ const b = divBalance(skel); check('骨架 div 配平', b.depth === 0 && b.minDepth >= 0, `end=${b.depth} min=${b.minDepth}`); }

/* --- 真载荷 --- */
T.S.uni = PAYLOAD;
T.S.uniShowAll = false;
T.S.uniFilter = '';
const h = T.universeBodyHtml();
const rowsIn = (h.match(/<tr class="[^"]*"/g) || []).length;
check('默认只渲染在池的行', rowsIn === PAYLOAD.summary.n_pool, `rows=${rowsIn}`);
check('门槛印成了每 bar + 折日两个数', h.includes('$3.00M') && h.includes('$72.00M'), h.slice(0, 200));
check('池内合计日成交额可见', h.includes('$16.25B'));
check('放量中位用相对百分比而不是 pp', h.includes('-21.2%') && !h.includes('pp'), h.slice(0, 400));
/* 在池行不该带「在池」chip（每行重复、纯占宽度）。唯一允许出现的一次是
   KPI 卡片的标题「在池」。 */
check('在池行不带「在池」chip（每行重复纯占宽度）',
  (h.match(/>在池</g) || []).length === 1, `n=${(h.match(/>在池</g) || []).length}`);
check('持仓行有 持仓 chip', h.includes('>持仓<'));
{ const b = divBalance(h); check('载荷 div 配平', b.depth === 0 && b.minDepth >= 0, `end=${b.depth} min=${b.minDepth}`); }

/* --- 显示全部：剔除行出现，且带原因 --- */
T.S.uniShowAll = true;
const h2 = T.universeBodyHtml();
const rowsAll = (h2.match(/<tr class="[^"]*"/g) || []).length;
check('显示全部时行数 = 总行数', rowsAll === PAYLOAD.rows.length, `rows=${rowsAll}/${PAYLOAD.rows.length}`);
check('剔除行标出原因', h2.includes('成交额不足') && h2.includes('上市太短'));
check('剔除行有 off 类', h2.includes('class="off"'));
{ const b = divBalance(h2); check('全部 div 配平', b.depth === 0 && b.minDepth >= 0); }

/* --- 筛选 --- */
T.S.uniShowAll = false;
T.S.uniFilter = 'btc';
const h3 = T.universeBodyHtml();
const rowsF = (h3.match(/<tr class="[^"]*"/g) || []).length;
check('筛选后只剩命中项', rowsF === 1, `rows=${rowsF}`);
T.S.uniFilter = '';

/* --- 放量条方向：正数向右、负数向左 --- */
const bp = T.liqBar(0.20), bn = T.liqBar(-0.20), bz = T.liqBar(null);
check('正放量：填充从 50% 起', /left:50\.0%/.test(bp), bp.slice(0, 160));
check('负缩量：填充在 50% 左侧', /left:([0-4]\d\.\d)%/.test(bn), bn.slice(0, 160));
check('缺失值不给条形', bz.includes('—'));

/* --- 徽标：没取到载荷时不许显示 0 只 --- */
T.S.uni = null;
check('未加载时 tab 徽标为空', T.uniBadge() === '');
T.S.uni = PAYLOAD;
check('加载后 tab 徽标是池子规模', T.uniBadge().includes(String(PAYLOAD.summary.n_pool)));

/* --- 错误载荷 --- */
T.S.uni = { ok: false, error: 'FileNotFoundError: no candle cache', rows: [], summary: {} };
const he = T.universeBodyHtml();
check('失败时画红色横幅而不是空白', he.includes('标的池计算失败') && he.includes('no candle cache'));
{ const b = divBalance(he); check('失败 div 配平', b.depth === 0 && b.minDepth >= 0); }

console.log(JSON.stringify({ checks }));
process.exit(checks.some((c) => !c.ok) ? 1 : 0);
"""


def _payload() -> dict:
    """一份形状与 `/api/universe` 完全一致的载荷（数值取真盘快照的缩样）。"""
    rows = [
        {"inst": "BTC-USDT-SWAP", "cat": "crypto", "adv": 265_070_904.0,
         "adv_day": 6_361_701_698.0, "turn_24h": 6_717_988_432.0, "px": 64307.3,
         "age_days": 2146.4, "n_bars": 51513, "ct_val": 0.01, "min_sz": 0.01,
         "lot_sz": 0.01, "has_spec": True, "reason": "ok", "in_pool": True,
         "rank": 1, "chg": 0.056, "adv_cap": 13_253_545.0,
         "min_notional": 6.43, "held": True, "held_sz": 0.5},
        {"inst": "ETH-USDT-SWAP", "cat": "crypto", "adv": 252_612_195.0,
         "adv_day": 6_062_692_679.0, "turn_24h": 5_553_731_698.0, "px": 1819.26,
         "age_days": 2146.4, "n_bars": 51513, "ct_val": 0.01, "min_sz": 0.01,
         "lot_sz": 0.01, "has_spec": True, "reason": "ok", "in_pool": True,
         "rank": 2, "chg": -0.084, "adv_cap": 12_630_610.0,
         "min_notional": 1.82, "held": True, "held_sz": 3.0},
        {"inst": "ZEC-USDT-SWAP", "cat": "crypto", "adv": 51_665_865.0,
         "adv_day": 1_239_980_765.0, "turn_24h": 998_115_774.0, "px": 915.36,
         "age_days": 376.25, "n_bars": 9030, "ct_val": 0.01, "min_sz": 0.01,
         "lot_sz": 0.01, "has_spec": True, "reason": "ok", "in_pool": True,
         "rank": 3, "chg": -0.195, "adv_cap": 2_583_293.0,
         "min_notional": 9.15, "held": False, "held_sz": None},
        {"inst": "TINY-USDT-SWAP", "cat": "crypto", "adv": 1_000_000.0,
         "adv_day": 24_000_000.0, "turn_24h": 20_000_000.0, "px": 1.0,
         "age_days": 400.0, "n_bars": 9600, "ct_val": 1.0, "min_sz": 1.0,
         "lot_sz": 1.0, "has_spec": True, "reason": "below_adv", "in_pool": False,
         "rank": None, "chg": -0.167, "adv_cap": 50_000.0,
         "min_notional": 1.0, "held": False, "held_sz": None},
        {"inst": "NEW-USDT-SWAP", "cat": "crypto", "adv": 9_000_000.0,
         "adv_day": 216_000_000.0, "turn_24h": 200_000_000.0, "px": 2.0,
         "age_days": 10.0, "n_bars": 240, "ct_val": 1.0, "min_sz": 1.0,
         "lot_sz": 1.0, "has_spec": True, "reason": "too_young", "in_pool": False,
         "rank": None, "chg": -0.074, "adv_cap": 450_000.0,
         "min_notional": 2.0, "held": False, "held_sz": None},
    ]
    return {
        "ok": True, "bar": "1h", "asset_class": "crypto",
        "asof": "2026-11-17T08:00:00+00:00",
        "generated_at": "2026-11-17T09:00:00Z",
        "params": {"min_avg_amount_usd": 3_000_000.0, "pit_topn": 100,
                   "min_history_days": 30.0, "liq_window_days": 30.0,
                   "bars_per_day": 24, "adv_cap_frac": 0.05},
        "summary": {"n_cached": 5, "n_eligible": 3, "n_pool": 3,
                    "adv_day_total": 16_249_000_000.0,
                    "adv_day_median": 174_078_009.0,
                    "adv_day_p25": 90_611_584.0, "adv_day_p75": 400_490_497.0,
                    "top10_share": 0.967, "chg_median": -0.212,
                    "n_expanding": 1, "cats": {"crypto": 3}, "bpd": 24},
        "n_on_disk": 268, "rows": rows, "notes": [],
        "held": {"BTC-USDT-SWAP": 0.5, "ETH-USDT-SWAP": 3.0},
        "from_cache": False, "age_sec": 0.0,
    }


def _render_checks(tmp_path: Path, payload: dict) -> list:
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    if not Path(node).exists():
        pytest.skip("node 不可用，跳过前端真渲染")
    js = tmp_path / "render_uni.js"
    pj = tmp_path / "payload.json"
    js.write_text(NODE_HARNESS, encoding="utf-8")
    pj.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run([node, str(js), str(APP), str(pj)],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0 and not p.stdout.strip():
        raise AssertionError(f"渲染 harness 崩了：\n{p.stderr[-2000:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])["checks"]


def test_universe_page_renders_in_a_real_js_engine(tmp_path):
    checks = _render_checks(tmp_path, _payload())
    bad = [c for c in checks if not c["ok"]]
    assert not bad, "；".join(f"{c['name']} ({c['extra']})" for c in bad)
    assert len(checks) >= 18
