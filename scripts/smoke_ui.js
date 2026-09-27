/* 无头渲染冒烟测试：在 node 里真跑一遍 webapp/static/app.js 的绘图路径。
 *
 * 为什么不截图：headless Chrome/Edge 在本机某些会话里连 127.0.0.1 都取不到
 * （连 example.com 也取不到，而 node 的 fetch 正常）—— 截图这条路会**静默失败**：
 * 命令 exit 0、没有输出、也不生成文件。所以改成在这里直接执行真实源码。
 *
 * 它验证的是截图看不出来的东西：
 *   1. 绘图坐标里没有 NaN（NaN 会让曲线整条消失，画布却"看起来没报错"）；
 *   2. 三种视图（净值 / 对数 / 回撤）都能画完；
 *   3. 图例 chip 正确生成、MDD 用的是服务端全量口径的精确值。
 *
 * 用法：先 `python webapp/server.py 8790`，再
 *   node scripts/smoke_ui.js [baseUrl]
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:8790';
const APP = path.join(__dirname, '..', 'webapp', 'static', 'app.js');

/* ---------------- 极简 DOM stub ---------------- */
/* 只需要撑住 app.js 的顶层副作用和绘图路径，不需要真的解析 HTML：
 * 图例内容直接读 `innerHTML` 字符串来断言。 */
const drawn = [];      // 所有绘制的 [方法, ...坐标]
const badPts = [];     // 坐标里出现 NaN / Infinity 的调用

const ctxMock = new Proxy({}, {
  get(_, k) {
    if (typeof k !== 'string') return undefined;
    return (...a) => {
      if (k === 'moveTo' || k === 'lineTo') {
        drawn.push([k, ...a]);
        if (a.some((x) => !Number.isFinite(x))) badPts.push([k, ...a]);
      }
    };
  },
  set() { return true; },
});

const canvas = {
  clientWidth: 900, clientHeight: 260, width: 0, height: 0,
  getContext: () => ctxMock,
};

class FakeEl {
  constructor() { this._html = ''; this.style = {}; this.dataset = {}; this.textContent = ''; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  get classList() { return { toggle() {}, add() {}, remove() {}, contains() { return false; } }; }
  set className(v) { this._cls = v; }
  get className() { return this._cls || ''; }
  // `$$(sel, root)` 走的是 root.querySelectorAll；这里只需要不抛错，
  // 图例内容本身是直接读 innerHTML 字符串断言的。
  querySelectorAll() { return []; }
  querySelector() { return null; }
  addEventListener() {}
}

const els = new Map();
function makeEl(sel) {
  if (sel === '#chart') return canvas;
  if (!els.has(sel)) els.set(sel, new FakeEl());
  return els.get(sel);
}

const CSSV = { '--muted': '#7b8794', '--border': '#e1e5ea', '--text-2': '#4b5563', '--accent': '#2f6fed' };
const sandbox = {
  window: { devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} },
  document: {
    body: {}, documentElement: { setAttribute() {}, getAttribute() { return null; } },
    querySelector: makeEl,
    querySelectorAll: () => [],
    addEventListener() {},
  },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', search: '', replace() {}, href: BASE + '/' },
  getComputedStyle: () => ({ getPropertyValue: (k) => CSSV[k] || '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval,
  console,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const realFetch = globalThis.fetch;
sandbox.fetch = (u, o) => realFetch(String(u).startsWith('/') ? BASE + u : u, o);

/* ---------------- 执行真实源码 ---------------- */
let src = fs.readFileSync(APP, 'utf8');
if (!/\nboot\(\);\s*$/.test(src)) throw new Error('app.js 末尾的 boot() 调用没找到，stub 需要更新');
src = src.replace(/\nboot\(\);\s*$/, '\n');
src += `
;globalThis.__T = { S, drawChart, curveValues, tickLabel, chartCandidates, initChartOff, PALETTE };
`;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;

/* ---------------- 取真实数据 ---------------- */
async function getJson(p) {
  const r = await realFetch(BASE + p);
  if (!r.ok) throw new Error(`${p} -> HTTP ${r.status}`);
  return r.json();
}

let fails = 0;
function check(name, ok, extra) {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${extra ? '   ' + extra : ''}`);
  if (!ok) fails++;
}

(async () => {
  const spec = await getJson('/api/spec');
  const runs = await getJson('/api/runs');
  const eq = await getJson('/api/equity/v3');

  const done = runs.runs.filter((r) => r.status === 'done' || r.status === 'partial');
  const cur = done[0];

  const S = T.S;
  S.optimalTag = spec.optimal_tag;
  S.runs = done.map((r) => ({ id: r.id, tag: r.tag, status: r.status }));
  S.detail = { tag: cur.tag, result: {} };
  S.equity[S.optimalTag] = eq;

  console.log(`服务 ${BASE}  最优 tag=${S.optimalTag}  当前运行=${cur.tag}  历史运行 ${done.length} 条\n`);

  /* --- 1. 形状校验 --- */
  check('equity 通过 shapeOk（含新增 mdd 字段）',
    !!(await (async () => { await T.drawChart(); return true; })()));

  /* --- 2. 三种视图都能画完且无 NaN --- */
  for (const v of ['equity', 'log', 'dd']) {
    drawn.length = 0; badPts.length = 0;
    S.chartView = v;
    for (const t of Object.keys(S.equity)) delete S.equity[t];
    S.equity[S.optimalTag] = eq;
    await T.drawChart();
    check(`视图 ${v}: 画了 ${drawn.length} 段线，坐标无 NaN`,
      drawn.length > 50 && badPts.length === 0,
      badPts.length ? `首个坏点 ${JSON.stringify(badPts[0])}` : '');
  }

  /* --- 3. 回撤用的必须是服务端全量口径的精确 MDD --- */
  const cv = T.curveValues(S.optimalTag, 'dd');
  check('回撤曲线取到精确 MDD（服务端全量口径）',
    Math.abs(cv.mdd - eq.mdd) < 1e-12,
    `前端 ${cv.mdd.toFixed(6)}  服务端 ${eq.mdd.toFixed(6)}  报告口径 −0.1272`);
  check('回撤序列全部 ≤ 0', cv.y.every((x) => x <= 1e-12));
  check('刻度标签格式正确',
    T.tickLabel(-0.05, 'dd') === '-5.0%' && T.tickLabel(Math.log(2), 'log') === '2.00×');

  /* --- 4. 图例 --- */
  drawn.length = 0; badPts.length = 0;
  S.chartView = 'dd';
  S.equity[cur.tag] = eq;
  delete S.chartOff[cur.tag];
  await T.drawChart();
  const lg = makeEl('#chartLegend').innerHTML;
  const chips = [...lg.matchAll(/data-ctag="([^"]+)"/g)].map((m) => m[1]);
  check('图例生成 chip', chips.length >= 2, chips.join(', '));
  check('图例含 v3 与当前运行', chips.includes(S.optimalTag) && chips.includes(cur.tag));
  check('MDD 出现在图例里', lg.includes((eq.mdd * 100).toFixed(1) + '%'),
    `${(eq.mdd * 100).toFixed(1)}%`);
  check('候选数不超过上限', T.chartCandidates().length <= 6, `${T.chartCandidates().length} 个`);

  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  console.error('冒烟测试自身出错：', e);
  process.exit(2);
});
