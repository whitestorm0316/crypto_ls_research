/* 交易台渲染冒烟测试：在 node 里真跑一遍 app.js 的交易台绘制路径。
 *
 * 为什么不截图：headless Chrome/Edge 在本机某些会话里连 127.0.0.1 都取不到，
 * 截图这条路会**静默失败**（exit 0、无输出、不生成文件）。所以直接执行真实源码。
 *
 * 它验证的是截图看不出来的东西：
 *   1. `innerHTML = h` 重建之后，每个「内容靠 JS 后填」的区域**真的被重填了**。
 *      这是本项目已经踩过两次的坑：renderContent() 末尾少一句 livePaintAll()，
 *      而 pollRuns() 每 2.5s 调一次 renderContent()，页面就永远停在「正在加载」。
 *      为此这里的 DOM stub 忠实复现「重建会清空子元素」的语义（见 MiniDom）。
 *   2. 渲染出来的每个数字都不是 NaN / undefined（`fmt` 一旦漏掉 null 检查，
 *      表格里就是一格 NaN，画布却"看起来没报错"）。
 *   3. 四个子视图、三种模式、三种 job 状态都能渲染，不留死分支。
 *   4. 按钮 id 在当前这棵渲染树里真的存在（`$('#liveGo')` 取不到时绑定会静默跳过）。
 *
 * 用法：先 `python webapp/server.py 8790`，再
 *   node scripts/smoke_live.js [baseUrl]
 * 计划数据取自 artifacts/live/preview_paper.json（真实引擎产物，不是手搓的）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:8790';
const ROOT = path.join(__dirname, '..');
/* SMOKE_APP 指向一份改坏的 app.js 副本，用来验证这个测试**真的有牙齿**
 * （变异测试）：删掉 renderContent() 末尾的 livePaintAll() 恢复，第 2 节必须变红。 */
const APP = process.env.SMOKE_APP || path.join(ROOT, 'webapp', 'static', 'app.js');

/* 计划数据用**最新的**真实引擎产物。不能用固定文件名：`preview_paper.json` 是
 * 换手预算修复**之前**留下的，19 笔、带 2 条 block 拦截；拿它做断言会把
 * 「旧缺陷」当成期望值。 */
const PREVIEW = (() => {
  const dir = path.join(ROOT, 'artifacts', 'live');
  const cands = fs.readdirSync(dir).filter((f) => /^preview_paper.*\.json$/.test(f))
    .map((f) => ({ f, m: fs.statSync(path.join(dir, f)).mtimeMs }))
    .sort((a, b) => b.m - a.m);
  if (!cands.length) {
    throw new Error('artifacts/live 下没有 preview_paper*.json —— 先在页面上生成一次计划');
  }
  return path.join(dir, cands[0].f);
})();

/* ========================================================================
   MiniDom —— 一个只为「重建语义」服务的假 DOM
   ------------------------------------------------------------------------
   关键点：`#content`.innerHTML = h 会**丢掉所有子元素里 JS 后来写进去的内容**。
   真实浏览器就是这样，而这正是本项目最贵的那个 bug 的成因。所以：
     * 写入 #content 时，contentHtml 换成新串，同时清空所有 override；
     * 其它元素的 innerHTML 读操作 = 「先看有没有 override，没有就去
       contentHtml 里按标签深度切出这个 id 的区间」——等价于浏览器里
       「重建后你看到的是骨架，而不是上次填的内容」。
   这样 `#liveAcct` 在 renderContent() 之后如果没被重填，断言就会读到占位文本，
   而不是读到上一次的旧内容（后者会让测试永远通过，即假绿）。
   ======================================================================== */
const overrides = new Map();             // id -> {kind:'html'|'text', v:string}
let contentHtml = '';

/* 静态外壳（index.html 的 <body>）。app.js 顶层会 `$('#themeBtn').onclick = ...`
 * / `$('#runBtn').onclick = ...`，这两个 id 在静态 HTML 里，不在任何渲染串里；
 * 少了它，源码在加载阶段就抛 TypeError，整份脚本一行都跑不到。 */
const INDEX = path.join(ROOT, 'webapp', 'static', 'index.html');
const staticHtml = (() => {
  const html = fs.readFileSync(INDEX, 'utf8');
  const m = /<body[^>]*>([\s\S]*)<\/body>/i.exec(html);
  if (!m) throw new Error('index.html 里找不到 <body>，stub 需要更新');
  return m[1];
})();

function sliceByTag(html, id) {
  const at = html.indexOf(`id="${id}"`);
  if (at < 0) return null;
  const lt = html.lastIndexOf('<', at);
  if (lt < 0) return null;
  const m = /^<([A-Za-z][\w-]*)/.exec(html.slice(lt));
  if (!m) return null;
  const tag = m[1].toLowerCase();
  const gt = html.indexOf('>', at);
  if (gt < 0) return null;
  if (html[gt - 1] === '/') return { html: '', tag };
  const start = gt + 1;
  let depth = 0;
  const re = new RegExp(`<(/?)${tag}\\b`, 'gi');
  re.lastIndex = start;
  let mm;
  while ((mm = re.exec(html))) {
    if (mm[1] === '/') {
      if (depth === 0) return { html: html.slice(start, mm.index), tag };
      depth--;
    } else {
      const g2 = html.indexOf('>', mm.index);
      if (g2 > 0 && html[g2 - 1] === '/') continue;
      depth++;
    }
  }
  return { html: html.slice(start), tag };
}

/** 哪个「根」包含这个 id：override 的内容优先（等价于更深的那层子树），
 *  然后是被 JS 重建过的 #content，最后才是静态外壳。 */
function rootFor(id) {
  for (const [, o] of overrides) {
    if (o.kind === 'html' && o.v.includes(`id="${id}"`)) return o.v;
  }
  if (contentHtml.includes(`id="${id}"`)) return contentHtml;
  if (staticHtml.includes(`id="${id}"`)) return staticHtml;
  return null;
}

class FakeEl {
  constructor(id) {
    this.id = id || '';
    this.dataset = {};
    this.style = {};
    this.value = '';
    this.checked = false;
    this.href = '';
    this.download = '';
    this._cls = '';
  }
  get innerHTML() {
    const o = overrides.get(this.id);
    if (o && o.kind === 'html') return o.v;
    const root = rootFor(this.id);
    if (root == null) return '';
    const s = sliceByTag(root, this.id);
    return s ? s.html : '';
  }
  set innerHTML(v) {
    if (this.id === 'content') { contentHtml = String(v); overrides.clear(); resetInputsIn(v); return; }
    overrides.set(this.id, { kind: 'html', v: String(v) });
    resetInputsIn(v);
  }
  get textContent() {
    const o = overrides.get(this.id);
    return (o && o.kind === 'text') ? o.v : '';
  }
  set textContent(v) {
    if (this.id === 'content') { contentHtml = String(v); overrides.clear(); return; }
    overrides.set(this.id, { kind: 'text', v: String(v) });
  }
  get outerHTML() { return this.innerHTML; }
  get classList() {
    const self = this;
    return {
      toggle(c, on) { const s = self._set || (self._set = new Set());
        if (on === undefined) { s.has(c) ? s.delete(c) : s.add(c); } else if (on) { s.add(c); } else { s.delete(c); } },
      add(c) { (self._set || (self._set = new Set())).add(c); },
      remove(c) { (self._set || (self._set = new Set())).delete(c); },
      contains(c) { return !!(self._set && self._set.has(c)); },
    };
  }
  get className() { return this._cls; }
  set className(v) { this._cls = String(v); }
  hasClass(c) { return !!(this._set && this._set.has(c)); }
  insertAdjacentHTML() {}
  appendChild() {}
  removeChild() {}
  click() {}
  focus() {}
  setSelectionRange() {}
  setAttribute() {}
  getAttribute() { return null; }
  querySelector() { return null; }
  querySelectorAll(sel) { return resolveAll(sel); }
  addEventListener() {}
  removeEventListener() {}
}

const els = new Map();
function elById(id) {
  if (!els.has(id)) els.set(id, new FakeEl(id));
  return els.get(id);
}
function elExists(id) {
  return overrides.has(id) || rootFor(id) != null;
}

/* [data-xxx] 选择器：真实扫描当前渲染树，这样 liveBind() 的绑定会被真正执行 */
function queryAllData(attr) {
  const out = [];
  const seen = new Set();
  const roots = [...overrides.values()].filter((o) => o.kind === 'html').map((o) => o.v)
    .concat([contentHtml, staticHtml]);
  const re = new RegExp(`data-${attr}="([^"]*)"`, 'g');
  for (const r of roots) {
    let m;
    while ((m = re.exec(r))) {
      const key = attr + '=' + m[1];
      if (seen.has(key)) continue;
      seen.add(key);
      const e = new FakeEl('');
      e.dataset[attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[1];
      e.dataset[attr] = m[1];
      out.push(e);
    }
  }
  return out;
}

function inpKey(id, dl, dlo) {
  return id ? `id:${id}` : (dl ? `limit:${dl}` : (dlo ? `lord:${dlo}` : null));
}
function inpEl(id, dl, dlo) {
  return elById(id || `__${inpKey(null, dl, dlo)}`);
}

/* ---- <input> 的真实扫描 ----
 * 「重建会把值打回模板给的那一版」这个语义挂在 **innerHTML 赋值**上，不能挂在扫描上：
 * 真实浏览器里 querySelectorAll 没有副作用，是 `innerHTML =` 换掉了元素。一开始我把
 * 重置写进 resyncInputs()，结果 snapshotInputs() 自己一扫描就把值清了 —— 守卫永远
 * 显得没坏，测试也永远不红，这比没有测试更糟。
 */
const inputReg = new Map();
const inputSeen = new Set();

function resyncInputs() {
  inputReg.clear();
  const roots = [...overrides.values()].filter((o) => o.kind === 'html').map((o) => o.v)
    .concat([contentHtml, staticHtml]);
  const re = /<(input|textarea|select)\b([^>]*)>/gi;
  for (const html of roots) {
    let m;
    while ((m = re.exec(html))) {
      const tag = m[1].toUpperCase();
      const attrs = m[2] || '';
      const get = (a) => {
        const r = new RegExp(`${a}="([^"]*)"`, 'i').exec(attrs);
        return r ? r[1] : null;
      };
      const id = get('id');
      const dl = get('data-limit');
      const dlo = get('data-lord');
      const key = inpKey(id, dl, dlo);
      if (!key || inputReg.has(key)) continue;
      const e = inpEl(id, dl, dlo);
      e.tagName = tag;
      e.type = (get('type') || 'text').toLowerCase();
      if (!inputSeen.has(key)) {          /* 首次注册：取模板给的初值 */
        e.value = get('value') == null ? '' : get('value');
        e.checked = /\bchecked\b/i.test(attrs);
        inputSeen.add(key);
      }
      if (dl) e.dataset.limit = dl;
      if (dlo) e.dataset.lord = dlo;
      inputReg.set(key, e);
    }
  }
  return [...inputReg.values()];
}

/* `innerHTML = v` 的语义：这段 html 里的每个受控输入都回到模板给的那一版 */
function resetInputsIn(html) {
  const re = /<(input|textarea|select)\b([^>]*)>/gi;
  let m;
  while ((m = re.exec(String(html || '')))) {
    const attrs = m[2] || '';
    const get = (a) => {
      const r = new RegExp(`${a}="([^"]*)"`, 'i').exec(attrs);
      return r ? r[1] : null;
    };
    const id = get('id');
    const dl = get('data-limit');
    const dlo = get('data-lord');
    if (!inpKey(id, dl, dlo)) continue;
    const e = inpEl(id, dl, dlo);
    e.tagName = m[1].toUpperCase();
    e.type = (get('type') || 'text').toLowerCase();
    e.value = get('value') == null ? '' : get('value');
    e.checked = /\bchecked\b/i.test(attrs);
  }
}

function resolveAll(sel) {
  const s = String(sel).trim();
  if (/^input,\s*textarea,\s*select$/i.test(s)) return resyncInputs();
  const d = /^\[data-([a-z-]+)\]$/.exec(s);
  if (d) return queryAllData(d[1]);
  const m = /^#([A-Za-z0-9_-]+)$/.exec(s);
  if (m) return elExists(m[1]) ? [elById(m[1])] : [];
  return [];
}

const drawn = [];
const badPts = [];
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
const canvas = Object.assign(elById('chart'), {
  clientWidth: 900, clientHeight: 260, width: 0, height: 0,
  getContext: () => ctxMock,
});

const blobs = [];
const RealURL = URL;
const CSSV = { '--muted': '#7b8794', '--border': '#e1e5ea', '--text-2': '#4b5563',
  '--accent': '#2f6fed', '--ok': '#2e9e5b', '--danger': '#d64545' };

const sandbox = {
  window: { devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} },
  document: {
    body: elById('body'),
    documentElement: { setAttribute() {}, getAttribute() { return null; } },
    activeElement: null,
    getElementById: (id) => (elExists(id) || id === 'content' || id === 'chart' ? elById(id) : null),
    querySelector: (sel) => {
      if (sel === '#chart') return canvas;
      const m = /^#([A-Za-z0-9_-]+)$/.exec(sel);
      if (m) return elExists(m[1]) ? elById(m[1]) : null;
      const d = /^\[data-([a-z-]+)\]$/.exec(sel);
      if (d) { const a = queryAllData(d[1]); return a.length ? a[0] : null; }
      return null;
    },
    querySelectorAll: (sel) => resolveAll(sel),
    createElement: () => new FakeEl(''),
    addEventListener() {},
  },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '#live', search: '', replace() {}, href: BASE + '/#live' },
  history: { replaceState() {} },
  getComputedStyle: () => ({ getPropertyValue: (k) => CSSV[k] || '#000' }),
  setTimeout, clearTimeout, setInterval, clearInterval,
  console,
  Blob,
  URL: new Proxy(RealURL, {
    get(t, k) {
      if (k === 'createObjectURL') return (b) => { blobs.push(b); return 'blob:fake'; };
      if (k === 'revokeObjectURL') return () => {};
      return t[k];
    },
  }),
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
;globalThis.__T = { S, fmt, esc, applyPreset, renderContent, liveSkeleton, livePaintAll,
  liveModeHtml, liveAcctHtml, liveSignalHtml, liveGatesHtml, livePlanHtml, liveJobHtml,
  liveHistHtml, liveLimitsHtml, liveBind, liveExport, tabBar, LIVE_LABEL,
  LIVE_LIMIT_FIELDS, liveLeverageHtml, liveTradeSettings, pollRuns, renderSide,
  liveNetHtml };
`;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });
const T = sandbox.__T;

/* ---------------- 断言工具 ---------------- */
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
/* 渲染出来的文本里出现这三种东西，一律是 bug：NaN / undefined / [object Object] */
const BAD = [/NaN/, /undefined/, /\[object Object\]/, />null</, /\$\u2014?\s*NaN/];
function dirty(html) {
  return BAD.filter((r) => r.test(html)).map(String);
}
function region(id) {
  const el = sandbox.document.getElementById(id);
  return el ? el.innerHTML : '<MISSING>';
}

/* 只有「加载中」占位算「没被重填」。空状态文案（尚无计划 / 暂无记录）是合法的
 * 终态，和骨架里的字符串可能一模一样 —— 所以要靠 override 计数区分「JS 写过」
 * 与「还停在骨架」。 */
const LOADING = ['正在读取…', '正在读取账户…'];

(async () => {
  const spec = await getJson('/api/spec');
  const meta = await getJson('/api/live/meta');
  const status = await getJson('/api/live/status?mode=paper');
  const orders = await getJson('/api/live/orders?mode=paper&limit=300');
  const fills = await getJson('/api/live/fills?mode=paper&limit=300');
  const runs = await getJson('/api/live/runs?mode=paper&limit=300');
  const preview = JSON.parse(fs.readFileSync(PREVIEW, 'utf8'));

  const S = T.S;
  S.spec = spec.spec; S.presets = spec.presets; S.bundles = spec.bundles;
  S.lib = spec.lib_defaults || {}; S.baseline = spec.baseline; S.optimalTag = spec.optimal_tag;
  T.applyPreset('v3_optimal');
  S.runs = [];

  S.tab = 'live';
  S.live.meta = meta;
  S.live.status = status;
  S.live.orders = orders.rows;
  S.live.fills = fills.rows;
  S.live.runs = runs.rows;
  S.live.mode = 'paper';

  console.log(`服务 ${BASE} · 模式 ${S.live.mode} · nav ${status.account && status.account.nav} · `
    + `订单 ${orders.rows.length} / 成交 ${fills.rows.length} / 运行 ${runs.rows.length}`);
  console.log(`计划产物来自 ${path.relative(ROOT, PREVIEW)}（${preview.plan.n_orders} 笔）\n`);

  /* --- 1. 深链进入交易台 --- */
  T.renderContent();
  check('#content 挂上了 .live（否则 .liveOnly 不可见）', elById('content').hasClass('live'));
  check('#content 含 liveRoot 与标签栏',
    contentHtml.includes('id="liveRoot"') && contentHtml.includes('data-tab="live"'));
  for (const id of ['liveMode', 'liveAcct', 'liveSignal', 'liveGates', 'livePlan',
    'liveJob', 'liveHist']) {
    check(`骨架里有 #${id}`, sandbox.document.getElementById(id) != null);
  }

  /* --- 2. 重建即回占位（本项目最贵的那个坑） --- */
  // renderContent() 刚把 #liveRoot 整个重建过，所有 override 都被清空。这 7 个
  // 区域的内容全部由 JS 后填，所以此刻必须已经被重填。判据有两层：
  //   a) JS 确实往这个 id 写过东西（override 存在）——少了 renderContent() 末尾
  //      那句 livePaintAll()，这里立刻为假；
  //   b) 写进去的不是「加载中」占位。
  // 之所以不能只比字符串：空状态文案和骨架里的占位可能逐字相同。
  for (const id of ['liveMode', 'liveAcct', 'liveSignal', 'liveGates', 'livePlan',
    'liveJob', 'liveHist']) {
    check(`重建后 JS 重填了 #${id}`, overrides.has(id),
      overrides.has(id) ? `${region(id).length} 字符` : '仍停在骨架（未被重填）');
  }
  for (const id of ['liveAcct', 'liveSignal', 'liveHist', 'liveMode']) {
    const stuck = LOADING.find((p) => region(id).includes(p));
    check(`重建后 #${id} 不再是「加载中」`, !stuck, stuck ? `仍是「${stuck}」` : '');
  }
  check('重建后 #liveAcct 是账户数据不是空壳', region('liveAcct').includes('净值'));
  check('重建后 #liveMode 已渲染模式卡', region('liveMode').includes('modecard'));

  /* --- 3. 全局卫生：没有 NaN / undefined --- */
  const regionIds = ['liveMode', 'liveAcct', 'liveSignal', 'liveGates', 'livePlan',
    'liveJob', 'liveHist'];
  for (const id of regionIds) {
    const b = dirty(region(id));
    check(`#${id} 不含 NaN/undefined`, b.length === 0, b.join(','));
  }
  for (const id of ['livePlanMeta', 'liveJobMeta', 'liveConnPill']) {
    const t = sandbox.document.getElementById(id);
    if (t) { const b = dirty(t.textContent); check(`#${id} 文本干净`, b.length === 0, b.join(',')); }
  }

  /* --- 4. 模式卡：三种模式都在，且区分「无需密钥 / 未配置」 --- */
  const mh = region('liveMode');
  check('三张模式卡齐全', ['paper', 'demo', 'live'].every((m) => mh.includes(`data-lmode="${m}"`)));
  check('实盘卡带「真实资金」警示', mh.includes('真实资金'));
  check('paper 显示「无需密钥」', mh.includes('无需密钥'));
  check('demo/live 显示「未配置密钥」或密钥提示',
    mh.includes('未配置密钥') || mh.includes('密钥 '));
  check('模拟盘说明点名「模拟盘专用」Key 与 50111 那个坑',
    mh.includes('模拟盘专用') && mh.includes('50111'));
  check('有熔断按钮', mh.includes('id="liveKill"'));

  /* --- 5. 账户区 --- */
  const ah = region('liveAcct');
  // 断言的是「净值被渲染出来了」，不是「净值恰好是 1000」——后者依赖真实 paper 账本
  // 的状态（跑过一次端到端后是 999.72），同一个测试上午过、下午就红。
  check('账户区显示净值', /\$[\d,]+\.?\d*/.test(ah) && !/NaN|undefined/.test(ah),
    ah.match(/净值[\s\S]{0,60}/) || '');
  check('账户区显示持仓数与行情来源',
    ah.includes('持仓数') && ah.includes('行情来源'));

  /* --- 5b. 行情陈旧横幅：两个分支都显式验一遍 --- */
  const dsReal = S.live.status.data_staleness;
  S.live.status.data_staleness = {
    bar: '1h', stale: true, age_hours: 24.5, bar_age_hours: 32.1,
    newest_bar: '2026-09-26T00:00:00+00:00',
  };
  T.livePaintAll();
  const staleH = region('liveAcct');
  check('陈旧时出横幅', staleH.includes('行情数据已过期'));
  check('横幅显示「最新已收盘 bar」的时间戳', staleH.includes('2026-09-26 00:00'));
  check('横幅显示 bar 落后 32.1 小时', staleH.includes('32.1'));
  check('横幅显示缓存文件 24.5 小时未更新', staleH.includes('24.5'));
  check('横幅提示用增量刷新（十几秒）', staleH.includes('增量') || staleH.includes('十几秒'));

  S.live.status.data_staleness = {
    bar: '1h', stale: false, age_hours: 0.1, bar_age_hours: 1.2,
    newest_bar: '2026-09-27T07:00:00+00:00',
  };
  T.livePaintAll();
  check('新鲜时不出横幅', !region('liveAcct').includes('行情数据已过期'));
  S.live.status.data_staleness = dsReal;
  T.livePaintAll();

  /* --- 5c. 名义额必须用服务端的 notional（含 ctVal），前端不许自己乘 ---
   * BTC-USDT-SWAP 的 ctVal 是 0.01：`|张数| × 标记价` 会把 $8.47 显示成 $847，
   * 让整个账户区的「毛敞口」虚高 100 倍。这两个断言就是为了让那种回归立刻变红。 */
  const realAcct = S.live.status.account;
  S.live.status.account = {
    nav: 1000.0, cash: 999.7, unrealised: -0.3, pos_mode: 'net_mode',
    positions: [
      { instId: 'BTC-USDT-SWAP', pos: -0.01, avgPx: 84742.9, markPx: 84742.9,
        upl: 0.0, notional: 8.47 },
      { instId: 'GONE-USDT-SWAP', pos: 5, avgPx: 1.0, markPx: 1.0,
        upl: 0.0, notional: null },
    ],
  };
  T.livePaintAll();
  const ch = region('liveAcct');
  check('单名名义额用服务端 notional（$8 而不是 $847）',
    ch.includes('$8') && !ch.includes('$847'), 'BTC ctVal=0.01');
  check('持仓名义合计也只算 notional（$8）', ch.includes('持仓名义 $8'), '');
  check('缺规格的持仓不估算，显示 — 并说明毛敞口是下界',
    ch.includes('有 1 个持仓算不出名义额') && ch.includes('下界'));
  S.live.status.account = realAcct;
  T.livePaintAll();

  /* --- 5d. 成交表同理 --- */
  const realFills = S.live.fills;
  S.live.sub = 'fills';
  S.live.fills = [
    { ts: 1790000000, instId: 'BTC-USDT-SWAP', side: 'sell', px: 84742.9,
      sz: 0.01, fee: -0.1, notional: 8.47 },
    { ts: 1790000100, instId: 'GONE-USDT-SWAP', side: 'buy', px: 1.0,
      sz: 10, fee: -0.01, notional: null },
  ];
  T.livePaintAll();
  const fh = region('liveHist');
  check('成交表名义额用 notional（$8 而不是 $847）',
    fh.includes('$8') && !fh.includes('$847'), '');
  check('成交缺 notional 时显示 — 不估算', fh.includes('缺 ctVal，不估算'));
  S.live.fills = realFills;
  S.live.sub = 'orders';
  T.livePaintAll();

  /* --- 6. 信号区（尚无计划时的分支） --- */
  check('信号区给出三个动作按钮',
    ['liveRefreshData', 'livePlanBtn', 'livePlanFresh'].every((i) => region('liveSignal').includes(`id="${i}"`)));

  /* --- 7. 灌入真实计划：闸门 / 账单 / 下单表 --- */
  S.live.plan = preview;
  T.livePaintAll();

  const gh = region('liveGates');
  const nWarn = (preview.violations || []).filter((v) => v.sev === 'warn').length;
  const nBlock = (preview.violations || []).filter((v) => v.sev === 'block').length;
  check(`闸门渲染出 ${preview.violations.length} 条（warn ${nWarn} / block ${nBlock}）`,
    gh.includes('gate warn') || gh.includes('gate block') || gh.includes('全部通过'));
  check('「非调仓日」被渲染成警告', gh.includes('当前不是调仓日'));
  check('「行情缓存过期」被渲染成警告', gh.includes('行情缓存') || gh.includes('行情数据已过期'));
  check('paper 模式无 block 时不出现拦截样式', nBlock > 0 || !gh.includes('gate block'));

  const ph = region('livePlan');
  const p = preview.plan;
  check(`下单表有 ${p.n_orders} 行`, (ph.match(/data-lord=/g) || []).length === p.n_orders,
    `${(ph.match(/data-lord=/g) || []).length} 行`);
  check('账单显示原始目标毛敞口 1.202', ph.includes('1.202'), p.raw_target_gross / p.nav);
  check('账单显示预算后 0.200', ph.includes('0.200'));
  check('账单显示换手缩放 16.6%', ph.includes('16.6'));
  check('有「只执行目标变动的 x%」解释横幅',
    ph.includes('只执行目标变动的') && ph.includes('2.2 倍'));
  check(`跳过腿 ${p.skips.length} 条被披露`, ph.includes(`被跳过的目标腿（${p.skips.length}）`));
  check('覆盖率与权重误差被显示', ph.includes('信号覆盖率') && ph.includes('权重误差'));
  check('paper 模式的执行按钮文案正确',
    ph.includes('执行调仓（本地模拟成交）') && ph.includes('id="liveGo"'));
  check('paper 模式不出现实盘确认短语框', !ph.includes('confirmbox'));
  check('有对账 / 清仓预演 / 清仓三个按钮',
    ['liveReconcile', 'liveFlatDry', 'liveFlat'].every((i) => ph.includes(`id="${i}"`)));
  const pb = dirty(ph);
  check('下单表不含 NaN/undefined', pb.length === 0, pb.join(','));

  /* --- 8. 信号区（有计划的完整分支） --- */
  const sh = region('liveSignal');
  check('信号区显示信号日与下次调仓',
    sh.includes('信号日') && sh.includes('下次调仓'));
  check('信号区显示池宽 19 / 多 9 / 空 10',
    sh.includes('>19<') && sh.includes('多 9 / 空 10'));
  check('信号区显示缩放分解（regime/BTC 波动/回撤）',
    sh.includes('regime') && sh.includes('BTC 波动') && sh.includes('回撤'));
  check('信号区目标毛敞口 1.2018', sh.includes('1.2018'));

  /* --- 9. 四个子视图 --- */
  for (const sub of ['orders', 'fills', 'runs', 'limits']) {
    S.live.sub = sub;
    T.livePaintAll();
    const h = region('liveHist');
    // 空表时的「暂无记录」是合法终态，不是渲染失败
    check(`子视图 ${sub} 渲染且干净`,
      (h.length > 40 || h.includes('暂无记录')) && dirty(h).length === 0,
      dirty(h).length ? dirty(h).join(',') : `${h.length} 字符`);
    if (sub === 'limits') {
      // 数量从源码定义取，不写死 —— 否则每加一个限额字段就要改一次测试，
      // 而写死的数字失败时只会说「期望 7 得到 8」，不会告诉你是不是漏渲染了。
      const nFields = (T.LIVE_LIMIT_FIELDS || []).length;
      check(`风控限额表 ${nFields} 个可编辑字段`,
        (h.match(/data-limit=/g) || []).length === nFields && nFields > 0,
        `${(h.match(/data-limit=/g) || []).length} vs ${nFields}`);
      check('杠杆上限是可编辑的限额字段（不再是死字段）',
        (T.LIVE_LIMIT_FIELDS || []).some((f) => f[0] === 'max_leverage')
        && h.includes('data-limit="max_leverage"'));
      const lvh = T.liveLeverageHtml({ max_leverage: 5.0, max_gross_frac: 1.0 });
      check('杠杆面板渲染出保证金模式与杠杆输入',
        lvh.includes('id="lvTdMode"') && lvh.includes('id="lvLever"'));
      check('杠杆面板写明「提高杠杆不放大收益」',
        lvh.includes('不放大收益') && lvh.includes('爆仓风险'));
      check('杠杆面板给出回撤放大的具体数字（报告 §16）',
        lvh.includes('42.9%') && lvh.includes('毛敞口上限'));
      check('杠杆面板无 NaN/undefined', dirty(lvh).length === 0, dirty(lvh).join(','));
      check('限额表包含「总名义额上限」与绝对金额的解释',
        h.includes('总名义额上限') && h.includes('连错账户'));
    } else {
      check(`子视图 ${sub} 空表时给出「暂无记录」`, h.includes('暂无记录') || h.includes('<table'));
    }
  }

  /* --- 9b. 网络延迟徽标（「卡顿」得能被归因，不能只靠猜） --- */
  check('没有请求样本时不显示延迟徽标', T.liveNetHtml({}) === '');
  const netFast = T.liveNetHtml({ samples: 4, requests: 4, retries: 0, slow: 0,
    last_ms: 520, p50_ms: 580, max_ms: 900, verdict: 'ok', proxy: 'http://127.0.0.1:7897' });
  check('延迟正常时给出最近/中位/峰值与请求次数',
    netFast.includes('网络正常') && netFast.includes('580ms') && netFast.includes('请求 4 次')
    && netFast.includes('7897'), netFast.slice(0, 120));
  const netSlow = T.liveNetHtml({ samples: 6, requests: 6, retries: 2, slow: 3,
    last_ms: 2400, p50_ms: 1800, max_ms: 2500, verdict: 'slow' });
  check('交易所慢时明确标注「响应偏慢」并给出慢请求数与重试数',
    netSlow.includes('⚠') && netSlow.includes('偏慢') && netSlow.includes('3 次超 2s')
    && netSlow.includes('重试 2'), netSlow.slice(0, 120));
  check('延迟徽标无 NaN/undefined', dirty(netFast + netSlow).length === 0,
    dirty(netFast + netSlow).join(','));
  S.live.sub = 'orders';
  T.livePaintAll();

  /* --- 10. 执行日志：三种状态 --- */
  const jobStates = [
    { kind: 'plan', status: 'running', label: '生成下单计划', log: [{ t: 1790000000, msg: '① 读取账户' }], elapsed: null },
    { kind: 'plan', status: 'done', label: '生成下单计划', log: [{ t: 1790000000, msg: '① 读取账户' }, { t: 1790000010, msg: '⑦ 完成' }], elapsed: 15.2 },
    { kind: 'execute', status: 'failed', label: '执行下单', log: [], error: 'OKXError: 50111 Invalid OK-ACCESS-KEY', trace: 'Traceback...', elapsed: 1.1 },
  ];
  for (const j of jobStates) {
    S.live.job = j;
    const h = T.liveJobHtml();
    check(`job ${j.status} 渲染出状态 pill 与日志`,
      h.includes(j.status) && dirty(h).length === 0, dirty(h).join(','));
    if (j.status === 'running') check('running 时有转圈动画', h.includes('spin'));
    if (j.status === 'failed') check('failed 时显示错误与 trace', h.includes('50111') && h.includes('Traceback'));
  }
  S.live.job = null;

  /* --- 11. 事件绑定不抛错（真实的 $$ 扫描） --- */
  let bindErr = null;
  try {
    T.liveBind();
  } catch (e) { bindErr = e; }
  check('liveBind() 在真实渲染树上不抛错', !bindErr, bindErr ? String(bindErr.message) : '');
  const modes = queryAllData('lmode');
  check(`data-lmode 卡片被扫描到 ${modes.length} 张`, modes.length === 3, `${modes.length}`);
  const subs = queryAllData('lsub');
  check(`data-lsub 标签被扫描到 ${subs.length} 个`, subs.length === 4, `${subs.length}`);
  const ords = queryAllData('lord');
  check(`data-lord 复选被扫描到 ${ords.length} 个`, ords.length === p.n_orders, `${ords.length}`);

  /* --- 12. 每个按钮 id 真的存在于渲染树里 --- */
  // `$('#id')` 取不到时绑定会被 `if (el)` 静默跳过：按钮点了没反应、控制台干净。
  // 注意 paper 模式本来就没有「密钥配置」开关，所以它不在这张表里（见第 13 节）。
  for (const id of ['liveKill', 'liveRefreshData', 'livePlanBtn', 'livePlanFresh',
    'liveGo', 'liveDry', 'liveReconcile', 'liveFlatDry', 'liveFlat', 'liveExport',
    'lvAll', 'lvForce']) {
    check(`#${id} 在当前渲染树里可达`, sandbox.document.getElementById(id) != null);
  }
  check('paper 模式没有密钥配置入口（无需密钥）',
    sandbox.document.getElementById('liveCredsToggle') == null);

  /* --- 13. demo / live 模式的分支差异 --- */
  S.live.mode = 'demo';
  S.live.credsOpen = true;
  T.livePaintAll();
  const dh = region('liveMode');
  check('demo 模式出现密钥配置开关', dh.includes('id="liveCredsToggle"'));
  check('demo 模式出现密钥输入框',
    ['lvKey', 'lvSecret', 'lvPass', 'liveCredsSave'].every((i) => dh.includes(`id="${i}"`)));
  check('demo 模式提示用「模拟盘专用」API Key', dh.includes('模拟盘专用'));
  check('demo 模式点名 50111 这个坑（实盘 Key 在模拟环境报的错）',
    dh.includes('50111'));
  check('demo 的执行按钮是「向 OKX 模拟盘下单」',
    region('livePlan').includes('向 OKX 模拟盘下单'));
  check('demo 模式没有实盘确认短语框', !region('livePlan').includes('confirmbox'));

  S.live.mode = 'live';
  S.live.status.confirm_phrase = 'LIVE-2026-09-27';
  T.livePaintAll();
  const lh = region('livePlan');
  check('live 模式强制出现确认短语框', lh.includes('confirmbox'));
  check('live 模式展示当日确认短语', lh.includes('LIVE-2026-09-27'));
  check('live 模式按钮为「确认下单」', lh.includes('确认下单'));
  const lmv = region('liveMode');
  check('live 模式提示「不要开提现」', lmv.includes('不要开提现'));
  S.live.mode = 'paper';
  S.live.credsOpen = false;
  S.live.status.confirm_phrase = null;
  T.livePaintAll();

  /* --- 14. 熔断状态 --- */
  S.live.status.kill_switch = true;
  T.livePaintAll();
  check('熔断时模式区按钮变成「解除熔断」', region('liveMode').includes('解除熔断'));
  check('熔断时连接 pill 显示「交易已熔断」',
    sandbox.document.getElementById('liveConnPill').innerHTML.includes('交易已熔断'));
  check('熔断时标签栏标记熔断',
    T.tabBar().includes('熔断'));
  S.live.status.kill_switch = false;
  S.live.status.connected = true;
  T.livePaintAll();
  check('未熔断时连接 pill 显示「已连接」',
    sandbox.document.getElementById('liveConnPill').innerHTML.includes('已连接'));
  check('未熔断时标签栏不标熔断', !T.tabBar().includes('熔断'));

  /* --- 15. 导出 CSV（走真实 Blob 路径） ---
   * `S.live.orders` 来自真实 paper 账本，账本里有 19 笔还是 0 笔取决于这台机器上
   * 刚跑过什么（跑过一次端到端就有单）。断言的是「空则拒绝」这条逻辑，所以前提必须
   * 自己造，不能指望账本恰好是空的 —— 否则同一个测试上午过、下午就红。 */
  const realOrders = S.live.orders;
  S.live.orders = [];
  blobs.length = 0;
  S.live.sub = 'limits';                 // limits 分支要回退到 orders
  T.liveExport();
  check('子视图无记录时导出被拒绝并提示', S.live.note.includes('没有可导出') && blobs.length === 0,
    S.live.note);
  S.live.orders = realOrders;

  S.live.sub = 'orders';
  S.live.orders = [{ ts: 1790000000, instId: 'BTC-USDT-SWAP', side: 'buy', action: 'open',
    sz: 0.5, notional: 50.0, state: 'filled', clOrdId: 'ab12', error: null },
  { ts: 1790000100, instId: 'ETH-USDT-SWAP', side: 'sell', action: 'open',
    sz: 0.25, notional: 25.0, state: 'filled', clOrdId: 'cd34', error: null }];
  blobs.length = 0;
  T.liveExport();
  check('订单 CSV 生成了 blob', blobs.length === 1, `${blobs.length}`);
  if (blobs[0]) {
    const txt = await blobs[0].text();
    const lines = txt.replace(/^\ufeff/, '').trim().split('\n');
    check('CSV 有表头 + 2 行', lines.length === 3, `${lines.length} 行`);
    check('CSV 表头含 instId / clOrdId',
      lines[0].includes('instId') && lines[0].includes('clOrdId'), lines[0]);
    check('CSV 内容含真实合约名', lines[1].includes('BTC-USDT-SWAP'), lines[1]);
  }

  /* --- 15b. 重建不能吃掉「还没提交的输入」 ---
   * pollRuns() 每 2.5s 重建 #content。这些框的值只有在 change（失焦）时才写回 state，
   * 所以正在敲的那个瞬间值只存在于 DOM 里 —— 重建就丢，控制台还一个报错都没有。
   * 用户看到的就是「一输入就被清空」（API Key 框是这个包第三次复发的地方）。 */
  S.live.mode = 'demo';
  S.live.credsOpen = true;
  S.live.sub = 'limits';
  T.livePaintAll();

  const setInput = (id, v) => { const e = elById(id); e.value = v; return e; };
  setInput('lvKey', 'okx-demo-key-abc123');
  setInput('lvSecret', 'very-secret-xyz');
  setInput('lvPass', 'passphrase-9');
  setInput('lvConfirm', 'LIVE-2026-09-27');
  setInput('lvPaperNav', '2500');
  const limEl = resolveAll('input, textarea, select')
    .find((e) => e.dataset && e.dataset.limit === 'max_gross_notional');
  if (limEl) limEl.value = '0.33';

  T.livePaintAll();
  check('livePaintAll 后 API Key 仍在', elById('lvKey').value === 'okx-demo-key-abc123',
    JSON.stringify(elById('lvKey').value));
  check('livePaintAll 后 Secret 仍在', elById('lvSecret').value === 'very-secret-xyz');
  check('livePaintAll 后 Passphrase 仍在', elById('lvPass').value === 'passphrase-9');
  check('livePaintAll 后确认短语仍在', elById('lvConfirm').value === 'LIVE-2026-09-27');
  check('livePaintAll 后纸面本金仍在', elById('lvPaperNav').value === '2500');
  if (limEl) check('livePaintAll 后限额输入仍在', limEl.value === '0.33', limEl.value);

  /* pollRuns 走的是 renderContent()，比 livePaintAll 重建得更狠（整块 #content） */
  T.renderContent();
  check('renderContent（= 每 2.5s 轮询）后 API Key 仍在',
    elById('lvKey').value === 'okx-demo-key-abc123', JSON.stringify(elById('lvKey').value));
  check('renderContent 后 Secret 仍在', elById('lvSecret').value === 'very-secret-xyz');
  check('renderContent 后 Passphrase 仍在', elById('lvPass').value === 'passphrase-9');
  check('renderContent 后确认短语仍在', elById('lvConfirm').value === 'LIVE-2026-09-27');
  check('renderContent 后纸面本金仍在', elById('lvPaperNav').value === '2500');
  if (limEl) check('renderContent 后限额输入仍在', limEl.value === '0.33', limEl.value);

  /* 复选框同样属于「未提交的输入」 */
  const forceEl = elById('lvForce');
  forceEl.checked = true;
  T.renderContent();
  check('renderContent 后复选框的勾选状态仍在', elById('lvForce').checked === true);

  /* 第二道：正在打字时 pollRuns 连重建都不做。
   * 成对断言——第二条防止把这条写成无条件 return（那样 UI 就再也不刷新了）。
   * 注：这条测的是「省掉无用重建」，不是「的第二道防线」——后者需要焦点，
   * 而焦点前后那几百毫秒的缝里轮询照样重建，所以它兜不住用户的输入。 */
  const beforePoll = contentHtml;
  setInput('lvKey', 'dont-touch-me');
  elById('lvKey').type = 'text';
  elById('lvKey').tagName = 'INPUT';
  sandbox.document.activeElement = elById('lvKey');
  await T.pollRuns();
  check('正在输入时 pollRuns 跳过重建（省掉无用重建，不是防线）', contentHtml === beforePoll);
  check('正在输入时输入框内容原样保留', elById('lvKey').value === 'dont-touch-me');

  sandbox.document.activeElement = null;
  await T.pollRuns();
  check('没有焦点时 pollRuns 照常重建（防线不能变成永久停摆）',
    contentHtml !== beforePoll);
  check('无焦点重建后内容仍由第一道防线兜住', elById('lvKey').value === 'dont-touch-me',
    JSON.stringify(elById('lvKey').value));

  S.live.mode = 'paper';
  S.live.credsOpen = false;
  S.live.sub = 'orders';
  T.livePaintAll();

  /* --- 16. 空状态下不留悬空 id --- */
  S.live.plan = null;
  S.live.busy = false;
  T.livePaintAll();
  check('无计划时回到「点生成计划」空态', region('livePlan').includes('点「生成下单计划」开始'));
  check('无计划时闸门回到空态', region('liveGates').includes('尚未生成计划'));
  check('无计划时但按钮仍在（可点）',
    sandbox.document.getElementById('livePlanBtn') != null);
  check('无计划时 #liveGo 不存在（不会出现无源按钮）',
    sandbox.document.getElementById('liveGo') == null);

  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  console.error('冒烟测试自身出错：', e);
  process.exit(2);
});
