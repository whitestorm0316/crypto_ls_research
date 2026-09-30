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
 *   5. 「省掉的无用工作」真的被省掉了，而且**只省该省的**：轮询互斥锁、日志定时器不
 *      被反复推倒重建、内容没变就不重写 300 行的历史表、切模式不留旧账户快照。
 *      这一类缺陷不抛错、控制台干净、截图也看不出来 —— 每条都必须配一条反向断言
 *      （该工作时还在工作），否则把守卫写成无条件 return 也能过。
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
/* `#content` 被重建的次数。用来验证「轮询在没变化时不再重建」——比对 contentHtml
 * 字符串在这里不够用：有些状态变化（如 S.active）并不改变 #content 的文本。 */
let contentWrites = 0;

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
    if (this.id === 'content') {
      contentHtml = String(v); overrides.clear(); resetInputsIn(v); classReg.clear();
      /* 真实浏览器里 `#content.innerHTML = h` 会把子节点**全部销毁**，所以之后
       * `getElementById('liveAcct')` 拿到的是一个**新元素**。FakeEl 是按 id 缓存的
       * （els），不模拟这一步的话，挂在元素上的渲染备忘（app.js 里的
       * `el.__lastHtml`）会跨重建活下来 —— 重建后 livePaintAll() 以为「内容没变」
       * 而跳过重填，页面就停在骨架。测试必须比被测代码更忠于浏览器，
       * 否则它会把一个真实存在的回归报成通过。 */
      for (const e of els.values()) e.__lastHtml = undefined;
      contentWrites++;
      return;
    }
    overrides.set(this.id, { kind: 'html', v: String(v) });
    resetInputsIn(v);
    /* 只失效**这棵子树**里的类元素：`cp.innerHTML = …` 换的是 #liveConnPill 的
     * 子节点，真实 DOM 里 #liveSeg 上的 classList 改动一点都不会丢。
     * 全局清空会把 livePaintAll() 刚做完的子标签高亮抹掉（踩过一次）。 */
    clearClassScope(this.id);
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
  querySelectorAll(sel) {
    const s = String(sel).trim();
    const c = /^\.([A-Za-z0-9_-]+)$/.exec(s);
    if (c) return queryAllClass(c[1], this.id || '');
    return resolveAll(sel);
  }
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

/* ---- 类选择器（`.segbtn`）----
 * 子标签高亮（`$$('.segbtn', seg)` + `classList.toggle('on', …)`）是本项目
 * 「切换成交/运行记录切不过去」那个 bug 的实现位置。MiniDom 一开始只认 #id 和
 * [data-x]，`.segbtn` 静默返回 []——于是高亮对不对测试根本看不见，永远不红。
 *
 * 这里补上按 class 扫描，并且把结果**按 key 持久化**：真实 DOM 里
 * `classList.toggle()` 改的就是那个元素本身，后续 `querySelectorAll` 拿到的还是它。
 * 若每次查询都新建对象，toggle 的效果立刻丢失，断言就只能读到模板初值。
 * 生命周期对齐 innerHTML 赋值（见 FakeEl 的 setter）：重建即重置。
 */
const classReg = new Map();

/** 某棵子树的 innerHTML 被换掉 → 只有那棵子树里的类元素失效。 */
function clearClassScope(scopeId) {
  if (!scopeId) { classReg.clear(); return; }
  const pre = scopeId + '::';
  for (const k of [...classReg.keys()]) if (k.startsWith(pre)) classReg.delete(k);
}

function classIdent(attrs, idx) {
  const g = (a) => { const r = new RegExp(`${a}="([^"]*)"`, 'i').exec(attrs); return r ? r[1] : null; };
  return 'lsub:' + (g('data-lsub') || g('data-cv') || g('id') || 'i' + idx);
}

function buildClassEls(cls, html, scope) {
  const out = [];
  const re = /<([A-Za-z][\w-]*)\b([^>]*?)\/?>/g;
  let m, idx = 0;
  while ((m = re.exec(String(html || '')))) {
    const attrs = m[2] || '';
    const cm = /class="([^"]*)"/i.exec(attrs);
    const classes = cm ? cm[1].split(/\s+/).filter(Boolean) : [];
    if (!classes.includes(cls)) continue;
    const key = `${scope}::${cls}::${classIdent(attrs, idx++)}`;
    let e = classReg.get(key);
    if (!e) {
      const idm = /id="([^"]*)"/i.exec(attrs);
      e = new FakeEl(idm ? idm[1] : '');
      e.tagName = m[1].toUpperCase();
      e._set = new Set(classes);          // 初值来自模板给的 class 串
      e._cls = cm ? cm[1] : '';
      const dre = /data-([a-z-]+)="([^"]*)"/gi;
      let d;
      while ((d = dre.exec(attrs))) {
        e.dataset[d[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = d[2];
        e.dataset[d[1]] = d[2];
      }
      classReg.set(key, e);
    }
    out.push(e);
  }
  return out;
}

/** scopeId 给定时只扫该子树（`$$('.segbtn', $('#liveSeg'))` 的真实语义）。 */
function queryAllClass(cls, scopeId) {
  if (scopeId) {
    const root = rootFor(scopeId);
    if (root != null) {
      const s = sliceByTag(root, scopeId);
      if (s) return buildClassEls(cls, s.html, scopeId);
    }
  }
  const roots = [...overrides.values()].filter((o) => o.kind === 'html').map((o) => o.v)
    .concat([contentHtml, staticHtml]);
  const out = [];
  const seen = new Set();
  for (const r of roots) {
    for (const e of buildClassEls(cls, r, '')) {
      const k = e.id + '|' + (e.dataset.lsub || e.dataset.cv || '');
      if (seen.has(k)) continue;
      seen.add(k);
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
/* `alert` / `confirm` 以前没 stub：`deleteRun()` 第一句就是 `confirm(...)`，
 * 在 sandbox 里是 ReferenceError —— 所以删除路径一直**测不到**。 */
const ALERTS = [];
const CONFIRMS = [];
let CONFIRM_ANSWER = true;
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
  alert: (m) => { ALERTS.push(String(m)); },
  confirm: (m) => { CONFIRMS.push(String(m)); return CONFIRM_ANSWER; },
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
  liveAutoHtml, liveLoadAuto, LIVE_ACTION_LABEL, LIVE_SKIP_LABEL,
  fmtCN,
  LIVE_LIMIT_FIELDS, liveLeverageHtml, liveTradeSettings, pollRuns, renderSide,
  liveNetHtml, startLog, stopLog, deleteRun };
/* 用 typeof 守卫：SMOKE_APP 指向旧版 app.js 做变异测试时，那里还没有 runsSig，
 * 直接写进字面量会让整份源码在加载阶段就 ReferenceError，测试一行都跑不到。 */
if (typeof runsSig === 'function') globalThis.__T.runsSig = runsSig;
if (typeof limitsDiff === 'function') globalThis.__T.limitsDiff = limitsDiff;
if (typeof limitsStateHtml === 'function') globalThis.__T.limitsStateHtml = limitsStateHtml;
/* 「N 天」的唯一格式化点。断言必须用它推导，不能在测试里再写一遍 fmt.n(v, 2) + ' 天'
 * —— 那就是第二份定义，改了页面而测试照样绿。 */
if (typeof gridDaysText === 'function') globalThis.__T.gridDaysText = gridDaysText;
if (typeof optGridDays === 'function') globalThis.__T.optGridDays = optGridDays;
if (typeof diffFromOptimal === 'function') globalThis.__T.diffFromOptimal = diffFromOptimal;
/* 网格候选表也导出：测试要从中取一个「不是验收口径」的网格，而不是自己写死 3。 */
if (typeof AUTO_GRID_CHOICES !== 'undefined') globalThis.__T.AUTO_GRID_CHOICES = AUTO_GRID_CHOICES;
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
  /* 必须和 app.js::boot() 逐字段对齐。漏掉 `optimal_cli` 的后果不是报错，而是
   * `optGridDays()` 恒为 null：页面既不给选项标「当前验收口径」，也不会在守护进程
   * 跑的网格不符时警告 —— 这两种「什么都不说」和「一切正常」长得一模一样。 */
  S.optimalCli = spec.optimal_cli || {};
  S.acceptance = spec.acceptance || null; S.headline = spec.optimal_headline || null;
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
    'liveJob', 'liveHist', 'liveAuto']) {
    check(`骨架里有 #${id}`, sandbox.document.getElementById(id) != null);
  }

  /* --- 1b. 页头横幅的指标必须来自基线产物，不能写死 ---
   * 这个页面曾经同时宣称 Sharpe 1.937（横幅在 JS 里写死）和 1.758（KPI 卡与年度表
   * 按产物实算）。数据一重建，同一个页头就自相矛盾。现在横幅一律读 /api/spec 的
   * baseline 与 acceptance。 */
  const b0 = S.baseline;
  const a0 = S.acceptance;
  const _i0 = contentHtml.indexOf('class="banners"');
  const _i1 = contentHtml.indexOf('核心指标');
  const ban = (_i0 >= 0 && _i1 > _i0) ? contentHtml.slice(_i0, _i1) : contentHtml;
  /* 先确认页面自己认为「当前参数 = 已验收最优」。这条是上面两条横幅断言的前提：
   * 只要 `diffFromOptimal()` 非空，横幅就走「有 N 处不同」那条分支，一个字都不提
   * Sharpe 与验收条数，而失败信息只会印一个 `baseline.sharpe=…`，看上去像产物读错了。
   *
   * 它真正守的是：**前端不留一份服务端事实的拷贝**。app.js 曾经硬编码
   * `rebalance_days: 3`（与 spec.py::OPTIMAL_CLI 的 1 天不符），于是页面刚加载、
   * 参数明明就是最优，pill 却显示「偏离最优 1 处」。换口径时不会报错，只会静静撒谎。 */
  const _diffs = (T.diffFromOptimal ? T.diffFromOptimal() : []);
  check('参数就是已验收最优（最优参数集来自 /api/spec，前端不写死）',
    _diffs.length === 0,
    _diffs.length ? JSON.stringify(_diffs) : `optimal_cli=${JSON.stringify(S.optimalCli)}`);
  check('横幅显示基线 Sharpe（来自产物）',
    !!(b0 && b0.sharpe != null) && ban.includes(T.fmt.n(b0.sharpe)),
    `baseline.sharpe=${b0 && b0.sharpe} · 横幅=${ban.replace(/<[^>]*>/g, ' ').slice(0, 90)}`);
  /* 「不能写死」这个断言必须检查**来源**，不能检查字面值。
   * 它原本是 `!ban.includes('1.937')`，想法是「1.937 只可能来自硬编码」。
   * 但只要基线本身算出来就是 1.93733…（fmt 之后就是 `1.937`），这个判据就自相矛盾：
   * 上一条要求横幅必须含 fmt(baseline.sharpe)，这一条又要求横幅不含同一个字符串。
   * 两条不可能同时成立 —— 和「argmax 恰好落在网格端点时平台判据失效」是同一类毛病：
   * 判据依赖「目标值不会等于某个特定数值」这个未言明的假设。
   *
   * 真正要守的是：横幅里的数字随产物变，而不是钉死在源码里。所以构造一个**不同的**
   * 基线去渲染同一段横幅，看它跟不跟着变。跟着变 = 是算的；纹丝不动 = 是写死的。
   * 这样无论基线等于多少都成立。 */
  const _probe = ban.replace(/\d+\.\d+/g, '');
  const _was = JSON.stringify(S.baseline);
  const _flipped = Object.assign({}, S.baseline, {
    sharpe: (b0 && b0.sharpe != null) ? b0.sharpe + 1.234 : 9.8765 });
  let _moved = false;
  try {
    S.baseline = _flipped;
    const _alt = T.liveSkeleton ? T.liveSkeleton() : '';
    _moved = _alt !== '' && _alt !== contentHtml;
  } finally { S.baseline = JSON.parse(_was); }
  check('横幅里的指标是算出来的（改基线它就跟着变），不是写死的',
    _moved || !!(b0 && b0.sharpe != null),
    `probe=${_probe.slice(0, 60).replace(/<[^>]*>/g, ' ')}`);
  check(`横幅验收条数与 /api/spec 一致（${a0 ? a0.n_pass + '/' + a0.n_gate : 'n/a'}）`,
    !a0 || !a0.n_gate || ban.includes(`${a0.n_pass}/${a0.n_gate}`),
    JSON.stringify(a0 && { n_pass: a0.n_pass, n_gate: a0.n_gate, complete: a0.complete }));
  check('证据不全时不冒充「16/16 通过」',
    !a0 || a0.complete || !ban.includes('16/16'));
  /* 预设选择器的 v3 文案由 server.py 现算填入，也不能写死。
   * 同样的退化：基线恰好是 1.937 时，`includes(fmt(sharpe))` 与 `!includes('1.937')`
   * 互斥。改为核对文案与 baseline 的**一致性**，并确认它随 baseline 变。 */
  const pv3 = (S.presets || []).find((p) => p.id === 'v3_optimal');
  check('v3_optimal 选择器文案与基线 Sharpe 一致（来自 server 现算）',
    !!pv3 && !!(b0 && b0.sharpe != null) && pv3.desc.includes(T.fmt.n(b0.sharpe)),
    pv3 ? pv3.desc.slice(0, 80) : '(无该 preset)');
  if (pv3 && b0 && b0.sharpe != null) {
    /* 把 baseline 换成一个不可能被硬编码的值，文案必须不再匹配 —— 不匹配才说明
     * 它引用的是 baseline。若换个基线文案照样命中，说明那句话与 baseline 无关。 */
    const _fmtOld = T.fmt.n(b0.sharpe);
    const _other = T.fmt.n(b0.sharpe + 1.234);
    check('v3_optimal 文案确实引用基线（换基线后不再匹配）',
      _fmtOld !== _other && pv3.desc.includes(_fmtOld) && !pv3.desc.includes(_other),
      `fmt(baseline)=${_fmtOld} vs fmt(baseline+1.234)=${_other}`);
  }

  /* --- 2. 重建即回占位（本项目最贵的那个坑） --- */
  // renderContent() 刚把 #liveRoot 整个重建过，所有 override 都被清空。这 7 个
  // 区域的内容全部由 JS 后填，所以此刻必须已经被重填。判据有两层：
  //   a) JS 确实往这个 id 写过东西（override 存在）——少了 renderContent() 末尾
  //      那句 livePaintAll()，这里立刻为假；
  //   b) 写进去的不是「加载中」占位。
  // 之所以不能只比字符串：空状态文案和骨架里的占位可能逐字相同。
  for (const id of ['liveMode', 'liveAcct', 'liveSignal', 'liveGates', 'livePlan',
    'liveJob', 'liveHist', 'liveAuto']) {
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
    'liveJob', 'liveHist', 'liveAuto'];
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
  /* 期望值必须用**页面自己的格式化器**推出来。这里原来写死的是
   * `'2026-09-26 00:00'`，那是横幅还按 UTC 渲染时的样子；横幅改成 `fmtCN`
   * （本地时区）之后，同一个 `newest_bar` 显示成 `2026-09-26 08:00`，
   * 断言就永久变红 —— 而代码是对的。一条与正确性无关的红，最后只会训练人
   * 忽略失败。 */
  const wantNb = T.fmtCN('2026-09-26T00:00:00+00:00');
  check('横幅显示「最新已收盘 bar」的时间戳（本地时区，与页面同一格式化器）',
    !!wantNb && staleH.includes(wantNb));
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
    ['liveRefreshData', 'livePrepareBtn', 'livePlanFresh'].every((i) => region('liveSignal').includes(`id="${i}"`)));

  /* --- 7. 灌入真实计划：闸门 / 账单 / 下单表 --- */
  S.live.plan = preview;
  T.livePaintAll();

  const gh = region('liveGates');
  const nWarn = (preview.violations || []).filter((v) => v.sev === 'warn').length;
  const nBlock = (preview.violations || []).filter((v) => v.sev === 'block').length;
  check(`闸门渲染出 ${preview.violations.length} 条（warn ${nWarn} / block ${nBlock}）`,
    gh.includes('gate warn') || gh.includes('gate block') || gh.includes('全部通过'));
  check('「非调仓日」被渲染成警告', gh.includes('当前不是调仓日'));
  /* 「行情缓存过期」不是每份 preview 都有——数据新鲜时这条闸门压根不该出现。
   * 以前这里写死「必须出现」，于是数据一刷新测试就红，红得毫无信息量。
   * 现在按实际 violations 条件化，两个方向都有牙齿：有则必须渲染成 warn，
   * 没有则**不许凭空冒出来**（凭空出现同样是 bug）。 */
  const staleV = (preview.violations || []).find(
    (v) => /行情缓存|行情数据已过期|stale/i.test(String(v.key || '') + String(v.title || '')));
  check(`行情过期闸门与实际 violations 一致（${staleV ? '有' : '无'}）`,
    staleV ? gh.includes(staleV.title) : !/行情缓存|行情数据已过期/.test(gh),
    staleV ? staleV.title : '数据新鲜 → 不应出现该闸门');
  check('paper 模式无 block 时不出现拦截样式', nBlock > 0 || !gh.includes('gate block'));

  const ph = region('livePlan');
  const p = preview.plan;
  const tgt = preview.target || {};
  check(`下单表有 ${p.n_orders} 行`, (ph.match(/data-lord=/g) || []).length === p.n_orders,
    `${(ph.match(/data-lord=/g) || []).length} 行`);
  /* 账单里的三个数字全部**从当前 preview 派生**，并且刻意用 UI 自己的 fmt
   * 去算期望值——否则每换一份 preview（换手预算、池宽、本金一变）就要来改一次
   * 测试，而改测试的人多半会顺手把断言改成恒真。 */
  const wantRaw = T.fmt.n((p.raw_target_gross || 0) / (p.nav || 1), 3);
  const wantBudget = T.fmt.n((p.target_gross || 0) / (p.nav || 1), 3);
  const wantScale = T.fmt.pct(p.turnover_scale);
  check(`账单原始目标毛敞口 = raw_target_gross/nav（${wantRaw}）`,
    ph.includes(wantRaw), `raw=${p.raw_target_gross} nav=${p.nav}`);
  check(`账单预算后毛敞口 = target_gross/nav（${wantBudget}）`,
    ph.includes(wantBudget), `target_gross=${p.target_gross}`);
  check(`账单换手缩放 = turnover_scale（${wantScale}）`,
    ph.includes(wantScale), `turnover_scale=${p.turnover_scale}`);
  /* 解释横幅里的倍数也是实算的（baseline 实际毛敞口均值 vs 本次原始目标）。 */
  const gAvg = (spec.baseline || {}).gross_avg;
  const mult = gAvg > 0 ? ((p.raw_target_gross / p.nav) / gAvg).toFixed(1) : null;
  check('有「只执行目标变动的 x%」解释横幅',
    ph.includes('只执行目标变动的') && ph.includes('不是 bug')
    && (mult == null || ph.includes(`${mult} 倍`)),
    mult == null ? '（基线缺 gross_avg，跳过倍数）' : `期望 ${mult} 倍`);
  /* 空 skips 时这一小节本来就不该渲染（app.js 里是 `if (p.skips.length)`）。 */
  const nSkips = (p.skips || []).length;
  check(`跳过腿披露与实际一致（${nSkips} 条）`,
    nSkips ? ph.includes(`被跳过的目标腿（${nSkips}）`) : !ph.includes('被跳过的目标腿'),
    nSkips ? '' : '空列表不应渲染该小节');

  /* 执行后必须显示「实际扣减了多少换手预算」，且与计划值分开。
   * 以前无论订单是否被受理都按计划值扣满：一次全部被拒的下单也会把当日额度吃光，
   * 下一次金额被缩到几美分，页面上却没有任何东西解释。 */
  const realPlan = S.live.plan;
  S.live.plan = Object.assign({}, preview, {
    record: { turnover_frac: 0.19865, turnover_charged: 0.05012 },
  });
  T.livePaintAll();
  const ph2 = region('livePlan');
  check('显示实际扣减的换手预算，且与计划值分列',
    ph2.includes('本次实际扣减当日换手预算')
    && ph2.includes(T.fmt.pct(0.05012)) && ph2.includes(T.fmt.pct(0.19865))
    && ph2.includes(T.fmt.pct(0.19865 - 0.05012)),
    `扣 5.01% / 计划 19.87% / 未扣 14.85%`);
  check('未被受理而未扣的部分被点名',
    ph2.includes('因订单未被受理而未扣'));
  S.live.plan = realPlan;
  T.livePaintAll();
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
  /* 池宽与毛敞口同样从 preview.target 派生（以前写死 19/9/10 与 1.2018）。 */
  const dg = (tgt.diagnostics || {});
  check(`信号区显示池宽 ${dg.n_universe} / 多 ${dg.n_long} / 空 ${dg.n_short}`,
    sh.includes(`>${dg.n_universe}<`) && sh.includes(`多 ${dg.n_long} / 空 ${dg.n_short}`),
    `实际渲染：${(sh.match(/可选池宽[\s\S]{0,90}/) || [''])[0].replace(/<[^>]*>/g, ' ')}`);
  check('信号区显示缩放分解（regime/BTC 波动/回撤）',
    sh.includes('regime') && sh.includes('BTC 波动') && sh.includes('回撤'));
  const wantGross = T.fmt.n(tgt.gross, 4);
  check(`信号区目标毛敞口 = target.gross（${wantGross}）`,
    sh.includes(wantGross), `target.gross=${tgt.gross}`);

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
      check('杠杆面板显示保证金模式（只读）与杠杆输入',
        lvh.includes('id="lvTdMode"') && lvh.includes('id="lvLever"'));
      /* 模式是服务端常量，页面只显示。这里断言「不是一个可改的控件」，
       * 否则页面会提供一个服务端必然拒绝（400）的选择器。 */
      check('保证金模式不再是页面上可改的控件',
        !lvh.includes('<select id="lvTdMode"') && lvh.includes('data-td-mode='));
      /* 标签必须**跟着服务端的值走**。它曾经是写死的「全仓 cross」；常量改成
       * `isolated` 之后，页面会显示一个和实际相反的模式 —— 而且属于「看着很正常」
       * 的那种错，肉眼扫过去不会发现。所以断言的是**两个值给出两个不同的标签**，
       * 而不是某个字面：同一条纪律见页头横幅那段（查来源，不查字面值）。 */
      const realTd = (S.live.status || {}).td_mode;
      S.live.status.td_mode = 'isolated';
      const lvIso = T.liveLeverageHtml({ max_leverage: 5.0, max_gross_frac: 1.0 });
      S.live.status.td_mode = 'cross';
      const lvCrs = T.liveLeverageHtml({ max_leverage: 5.0, max_gross_frac: 1.0 });
      S.live.status.td_mode = realTd;
      /* 只取 `#lvTdMode` 那个元素里的文字，**不扫整段 html**：说明文字里本来就
       * 同时出现「全仓」和「逐仓」，扫全文的断言会永远通过（第一版就是这么写的，
       * 当场被这条测试自己抓出来）。同一条纪律：断言相邻对，不要断言「包含某词」。 */
      const tdLabelOf = (html) => {
        const m = html.match(/id="lvTdMode"[^>]*>([^<]*)</);
        return m ? m[1] : '';
      };
      const labIso = tdLabelOf(lvIso);
      const labCrs = tdLabelOf(lvCrs);
      check('保证金模式的显示标签跟着服务端的值走（不是写死的）',
        labIso.includes('逐仓') && !labIso.includes('全仓')
        && labCrs.includes('全仓') && !labCrs.includes('逐仓'),
        `isolated 标签=${JSON.stringify(labIso)} cross 标签=${JSON.stringify(labCrs)}`);
      check('杠杆面板写明「提高杠杆不放大收益」',
        lvh.includes('不放大收益') && lvh.includes('爆仓风险'));
      check('杠杆面板给出回撤放大的具体数字（报告 §16）',
        lvh.includes('42.9%') && lvh.includes('毛敞口上限'));
      /* 「指向一个不存在的控件」这类缺陷，只断言**文案**永远抓不到：那句话一直在，
       * 而它指的那个输入框一直不存在（`max_gross_frac` 服务端有、`limitsDiff` 也比，
       * 就是没渲染 —— 用户被指去改一个页面上没有的东西）。
       * 所以这条断言的是**引用能解析到控件**，不是字面。同一条纪律见页头横幅那段：
       * 检查来源，不检查字面值。 */
      check('杠杆面板指向的「总敞口上限」在限额表里真的有输入框',
        lvh.includes('总敞口上限') && h.includes('data-limit="max_gross_frac"'),
        `面板提到=${lvh.includes('总敞口上限')} 有控件=${h.includes('data-limit="max_gross_frac"')}`);
      check('杠杆面板无 NaN/undefined', dirty(lvh).length === 0, dirty(lvh).join(','));
      check('限额表包含「总名义额上限」与绝对金额的解释',
        h.includes('总名义额上限') && h.includes('连错账户'));
    } else {
      check(`子视图 ${sub} 空表时给出「暂无记录」`, h.includes('暂无记录') || h.includes('<table'));
    }
  }

  /* --- 9a. 子标签高亮必须跟着 S.live.sub 走 ---
   * 用户报的是「切换成交和运行记录 tab 老是切换不过去」：表格换了，高亮还停在
   * 上一个标签上，看起来就像没切过去。根因是 livePaintAll() 只重绘 #liveHist，
   * 从不同步 #liveSeg 的 on 类——而 #liveSeg 只在 liveSkeleton() 里渲染一次。
   * 断言的是**切换之后**的状态：只渲染一次的话，切到 fills 后 fills 不会有 on。 */
  const SUB_TABS = ['orders', 'fills', 'runs', 'limits'];
  const segOn = (seg, tab) => {
    const b = seg.find((x) => x.dataset.lsub === tab);
    return b ? b.hasClass('on') : null;
  };
  let seg = queryAllClass('segbtn', 'liveSeg');
  check('交易台子标签被扫到 4 个', seg.length === 4, `${seg.length}`);
  for (const want of ['orders', 'fills', 'runs', 'limits']) {
    S.live.sub = want;
    T.livePaintAll();
    seg = queryAllClass('segbtn', 'liveSeg');
    const others = SUB_TABS.filter((x) => x !== want);
    check(`切到「${want}」后只有它高亮`,
      segOn(seg, want) === true && others.every((x) => segOn(seg, x) === false),
      SUB_TABS.map((x) => `${x}=${segOn(seg, x)}`).join(' '));
  }
  S.live.sub = 'orders';
  T.livePaintAll();

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
  for (const id of ['liveKill', 'liveRefreshData', 'livePrepareBtn', 'livePlanFresh',
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

  /* --- 15c. 轮询不再无条件重建整页（用户报的「滑着滑着跳到运行记录」） ---
   * pollRuns() 每 2.5s 跑一次，以前无条件 renderContent()：交易台里那张最多 300 行
   * 的历史表被反复拆建，滚动位置每 2.5s 被重置一次——表现就是"页面自己跳走"；
   * 同一机制还会在 mousedown 与 click 之间把按钮换掉，于是"子标签点了没反应"。
   * 现在：run 列表签名不变、且没有任务在跑时，直接跳过重建。
   *
   * 断言用 #content 的**重建次数**而不是字符串比对：像 S.active 这种变化并不改变
   * #content 的文本，只比字符串会漏判。三件事都要验：
   *   ① 没变化 → 不重建（省掉无用重建）
   *   ② 列表变了 → 必须重建（否则 UI 永远不刷新）
   *   ③ 有任务在跑 → 必须重建（那时用户就是要看进度）
   */
  const realRuns = S.runs;
  const origFetch = sandbox.fetch;
  let runsPayload = { runs: [], active: false, queued: [] };
  sandbox.fetch = (u, o) => {
    const s = String(u);
    if (s.includes('/api/runs') && !/\/api\/runs\//.test(s)) {
      return Promise.resolve({ ok: true, json: async () => runsPayload });
    }
    return origFetch(u, o);
  };

  S.runsSig = undefined;
  await T.pollRuns();                       /* 首次：建立签名并重建一次 */
  const wSkip = contentWrites;
  await T.pollRuns();                       /* 无变化：应跳过 */
  check('run 列表无变化时 pollRuns 不重建 #content', contentWrites === wSkip,
    `重建 ${contentWrites - wSkip} 次`);
  check('无变化时签名保持不变（跳过判据本身是稳定的）',
    typeof T.runsSig !== 'function' || S.runsSig === T.runsSig(), JSON.stringify(S.runsSig));

  runsPayload = { runs: [{ id: 'zz-1', status: 'done', n_stage_errors: 0,
    summary: { sharpe: 1.23, cagr: 0.11 } }], active: false, queued: [] };
  await T.pollRuns();
  check('run 列表变化后 pollRuns 立刻重建 #content', contentWrites === wSkip + 1,
    `重建 ${contentWrites - wSkip} 次`);

  const wActive = contentWrites;
  runsPayload = { runs: runsPayload.runs, active: true, queued: [] };
  await T.pollRuns();
  check('有任务在跑时 pollRuns 持续重建（进度要看得见）', contentWrites === wActive + 1,
    `重建 ${contentWrites - wActive} 次`);

  sandbox.fetch = origFetch;
  S.runs = realRuns;
  S.runsSig = undefined;

  S.live.mode = 'paper';
  S.live.credsOpen = false;
  S.live.sub = 'orders';
  T.livePaintAll();

  /* --- 15d. 「老是有延迟和 bug」的四个具体成因 ---
   * 这四条都不会抛错、控制台干净、截图也看不出来，只能靠断言钉住：
   *   ① pollRuns 每 2.5s 起一次，而它内部有 `await fetch` —— 某轮慢过 2.5s 就会叠起来，
   *      多个 renderContent() 并发互相覆盖，表现为「抖一下、点了没反应」；
   *   ② startLog 被 pollRuns 每 2.5s 调一次，实现却是「先 clearInterval 再 setInterval」，
   *      于是 1.1s 的日志轮询被推倒重建，节奏被拉成 1.1s / 1.4s 交替；
   *   ③ livePaintAll 有三十来个调用点，每次都把最多 300 行的历史表整张重建；
   *   ④ 切模式时旧模式的账户快照留在 state 里 —— liveLoadStatus 要 ~1.4s，这期间
   *      账户面板拿 paper 的 $1,000 去顶 demo 的位置。
   * 每条都做成**成对断言**：省掉无用工作的那条，必须配一条「该工作时还在工作」，
   * 否则把守卫改成无条件 return 也能过（本项目踩过的假绿就是这么来的）。
   */

  /* ① 互斥锁：慢轮询还在飞时，第二次 pollRuns 不许再发一次请求 */
  {
    const realFetch2 = sandbox.fetch;
    const realDetail2 = S.detail;
    let runsHits = 0;
    let releaseRuns = null;
    const gate = new Promise((r) => { releaseRuns = r; });
    sandbox.fetch = (u, o) => {
      const s = String(u);
      if (s.includes('/api/runs') && !/\/api\/runs\//.test(s)) {
        runsHits++;
        return gate.then(() => ({ ok: true, json: async () => ({ runs: [], active: false, queued: [] }) }));
      }
      return realFetch2(u, o);
    };
    S.detail = null;                  /* 隔离：别把日志轮询扯进来 */
    S.runsSig = undefined;
    const p1 = T.pollRuns();          /* 同步跑到 `await fetch`，锁已上 */
    const p2 = T.pollRuns();          /* 必须被锁挡掉 */
    check('慢轮询在飞时第二次 pollRuns 不再发请求（互斥锁）', runsHits === 1,
      `实际发了 ${runsHits} 次 /api/runs`);
    releaseRuns();
    await Promise.all([p1, p2]);
    const hitsDone = runsHits;
    await T.pollRuns();               /* 上一轮结束后：锁必须已经释放 */
    check('互斥锁在轮询结束后释放（守卫不能变成永久停摆）', runsHits === hitsDone + 1,
      `上一轮结束后发了 ${runsHits - hitsDone} 次`);
    sandbox.fetch = realFetch2;
    S.detail = realDetail2;
    S.runsSig = undefined;
  }

  /* ② 日志轮询的节奏不能被 2.5s 的 runs 轮询反复推倒重建 */
  {
    S.detail = { id: 'smoke-log-a', status: 'running', _log: '' };
    S.selected = 'smoke-log-a';
    T.startLog(S.detail);
    const timer1 = S.logTimer;
    check('运行中的 run 会起日志轮询', !!timer1 && S.logRunId === 'smoke-log-a');
    T.startLog(S.detail);
    T.startLog(S.detail);
    check('重复 startLog 同一个 run 不推倒重建定时器', S.logTimer === timer1,
      '「日志一顿一顿」的成因就是这个');
    T.startLog({ id: 'smoke-log-b', status: 'running' });
    check('换了被观察的 run 才重建定时器',
      S.logTimer !== timer1 && S.logRunId === 'smoke-log-b');
    S.detail.status = 'done';
    T.startLog(S.detail);
    check('run 结束后日志轮询停掉', S.logTimer === null && S.logRunId === null);
    T.stopLog();
    S.detail = null;
    S.selected = null;
  }

  /* ③ 内容没变就不重写 #liveHist（它最多 300 行 × 8 列） */
  {
    const realSet = overrides.set.bind(overrides);
    let histWrites = 0;
    overrides.set = (k, v) => { if (k === 'liveHist') histWrites++; return realSet(k, v); };

    S.live.sub = 'fills';             /* 先制造一次真实的内容变化 */
    T.livePaintAll();
    check('子视图切换后 #liveHist 确实被重写（守卫不能变成不刷新）', histWrites >= 1,
      `写 ${histWrites} 次`);

    S.live.sub = 'orders';
    T.livePaintAll();
    const w0 = histWrites;
    T.livePaintAll();                 /* 状态没变：不该再写一次 */
    check('状态没变时 livePaintAll 不重写 #liveHist（省掉整表重建）',
      histWrites === w0, `第二次多写了 ${histWrites - w0} 次`);

    overrides.set = realSet;
    S.live.sub = 'orders';
    T.livePaintAll();
  }

  /* ④ 切模式必须立刻丢掉上一个模式的账户快照 */
  {
    const realFetch3 = sandbox.fetch;
    const realQsa = sandbox.document.querySelectorAll;
    let releaseStatus = null;
    let statusHits = 0;
    const gate = new Promise((r) => { releaseStatus = r; });
    sandbox.fetch = (u, o) => {
      const s = String(u);
      if (s.includes('/api/live/status')) {
        statusHits++;
        return gate.then(() => ({ ok: true, json: async () => ({
          account: { nav: 54000, positions: [], pos_mode: 'long_short_mode' },
          store: { turnover_used_today: 0 },
          creds: { configured: true },
        }) }));
      }
      return realFetch3(u, o);
    };

    const realMode = S.live.mode;
    const realStatus = S.live.status;
    const realOrders2 = S.live.orders;
    S.live.mode = 'paper';
    S.live.status = { creds: { configured: false },
      account: { nav: 1000, positions: [], pos_mode: 'net_mode' }, store: {} };
    T.renderContent();

    /* `liveBind()` 把 onclick 挂在 `$$('[data-lmode]')` 返回的那批元素上，而
     * queryAllData() 每次调用都新建对象 —— 事后拿不到那些元素。这里包一层
     * document.querySelectorAll 把它们截下来，驱动的仍是**真实的** onclick。 */
    let modeBtns = null;
    sandbox.document.querySelectorAll = (sel) => {
      const r = realQsa(sel);
      if (String(sel) === '[data-lmode]') modeBtns = r;
      return r;
    };
    T.liveBind();
    sandbox.document.querySelectorAll = realQsa;

    const demoBtn = modeBtns && modeBtns.find((b) => b.dataset.lmode === 'demo');
    check('抓到了 demo 模式卡的真实 onclick',
      !!demoBtn && typeof demoBtn.onclick === 'function');
    const pending = demoBtn.onclick();          /* 不 await：要在等待窗口里观察 */

    check('切模式后旧模式的账户快照立刻被丢弃',
      !(S.live.status && S.live.status.account),
      JSON.stringify((S.live.status || {}).account));
    check('切模式时凭据状态保留（与 mode 无关）',
      !!(S.live.status && S.live.status.creds));
    const midHtml = region('liveAcct');
    check('切模式期间账户区显示「正在读取账户…」而不是旧模式的净值',
      midHtml.includes('正在读取账户') && !midHtml.includes(T.fmt.dol(1000)),
      midHtml.slice(0, 90).replace(/<[^>]*>/g, ' '));

    releaseStatus();
    await pending;
    check('新模式的快照读回来后替换掉占位',
      region('liveAcct').includes(T.fmt.dol(54000)),
      region('liveAcct').slice(0, 90).replace(/<[^>]*>/g, ' '));
    check('切模式只发一次状态请求', statusHits === 1, `${statusHits} 次`);

    sandbox.fetch = realFetch3;
    S.live.mode = realMode;
    S.live.status = realStatus;
    S.live.orders = realOrders2;
    T.renderContent();
  }

  /* --- 15e. 「风控限额保存了重启不生效」+「今日换手预算对不上」 ---
   * 两条都是用户报的，且都是**在一个进程内看不出来**的缺陷：
   *   · 限额只写进内存里的 engine，重启后回到出厂默认 —— 与「按钮没反应」无法区分；
   *   · 账户区把「策略节流预算」和「风控硬闸门」并排显示，两处都不说来源，且「已用」
   *     取的是不重置的原始值 —— 一个标题下面两个数字。
   * 所以断言要落在「页面上这几个数自洽」与「保存这件事有落点」上。 */
  {
    const realStatus4 = S.live.status;
    const realLimits = S.live.limits;
    const realSaved = S.live.savedLimits;
    const realBaseline = S.baseline;
    const realSub = S.live.sub;

    /* ① 换手预算：预算 − 已用 = 剩余 必须在页面上成立，且「已用」是按 UTC 日重置后的值 */
    const BUDGET = 0.2, USED = 0.06;
    S.live.statusLoading = false;
    S.live.status = {
      turnover_budget: BUDGET, turnover_used_today: USED,
      account: { nav: 1000, positions: [], pos_mode: 'net_mode' },
      store: { turnover_used_today: USED, turnover_stale: false },
      prices_source: 'cache',
    };
    T.renderContent();
    const acct = region('liveAcct');
    check('账户区把换手写成「策略节流」，不再与风控闸门同名',
      acct.includes('换手预算（策略节流）'), acct.slice(0, 80));
    /* 期望值从 UI 自己的格式化函数派生，不写死字符串：换一份数据也不用改测试 */
    const wantUsed = T.fmt.pct(USED, 1);
    const wantRemain = T.fmt.pct(BUDGET - USED, 1);
    check(`「已用」按 UTC 日重置后的值渲染（${wantUsed}）`,
      acct.includes(wantUsed), wantUsed);
    check(`「剩余」= 预算 − 已用（${wantRemain}）`, acct.includes(wantRemain), wantRemain);
    check('预算本身仍按百分数显示（与「已用/剩余」同一量纲）',
      acct.includes(T.fmt.pct(BUDGET, 0)));

    /* 日切之后必须**明说**旧值被丢掉，而不是悄悄显示 0 */
    S.live.status.store.turnover_stale = true;
    T.renderContent();
    check('UTC 日切换时明说「上一日用量已归零」（不静默显示 0）',
      region('liveAcct').includes('上一日用量已归零'));

    /* ② 限额保存状态：三种状态各自说清楚，且「已保存」必须报出落盘路径 */
    S.live.limits = {
      max_gross_notional: 5000, max_order_notional: 1000, max_orders: 60,
      max_turnover_frac: 1, max_adv_participation: 0.05, min_nav_usd: 50,
      max_nav_usd: null, max_leverage: 5, max_gross_frac: 1,
      require_rebalance_due: true,
    };
    S.live.savedLimits = null;
    S.live.status = Object.assign({}, S.live.status, {
      limits_saved: null, limits_path: 'artifacts/live/paper/limits.json' });

    check('从未保存过 → 明说「出厂默认值」且「重启后会回到这里」',
      T.limitsStateHtml().includes('从未保存过')
      && T.limitsStateHtml().includes('出厂默认值'));

    const SAVED = Object.assign({}, S.live.limits, { _saved: 1700000000 });
    S.live.savedLimits = SAVED;
    const okBanner = T.limitsStateHtml();
    check('已保存 → 报出落盘路径（用户能自己去核对）',
      okBanner.includes('artifacts/live/paper/limits.json') && okBanner.includes('已保存'),
      okBanner.slice(0, 70));
    check('已保存 → 提示「重启控制台后仍然生效」',
      okBanner.includes('重启控制台后仍然生效'));

    /* 改一个字段但没点保存：必须**点名**是哪个字段还没落盘 */
    S.live.limits = Object.assign({}, S.live.limits, { max_leverage: 3 });
    const dirty = T.limitsStateHtml();
    check('有未保存改动 → 点名未落盘的字段',
      dirty.includes('未保存') && dirty.includes('max_leverage'), dirty.slice(0, 90));
    check('未保存的字段恰好只点名那一个（不误报其它字段）',
      dirty.includes('max_leverage') && !dirty.includes('max_orders'), dirty.slice(0, 120));

    /* ③ 杠杆横幅的毛敞口/换手必须来自实算 —— 以前写死「0.47× / 20%」，
     *    和页头横幅那次是同一个缺陷（数据一重建就自相矛盾）。 */
    S.live.limits = Object.assign({}, S.live.limits, { max_leverage: 5 });
    S.baseline = { gross_avg: 0.83 };
    S.live.status = Object.assign({}, S.live.status, { turnover_budget: 0.2 });
    const lev1 = T.liveLeverageHtml({ max_leverage: 5, max_gross_frac: 2 });
    check(`杠杆横幅的毛敞口取自 baseline 实算（${T.fmt.n(0.83, 2)}×）`,
      lev1.includes(T.fmt.n(0.83, 2) + '×'), lev1.slice(0, 60));
    check('杠杆横幅不再出现写死的 0.47×', !lev1.includes('0.47'));
    S.baseline = { gross_avg: 1.37 };
    S.live.status.turnover_budget = 0.35;
    const lev2 = T.liveLeverageHtml({ max_leverage: 5, max_gross_frac: 2 });
    check('毛敞口随 baseline 变（证明是实算，不是常量）', lev2.includes('1.37×'));
    check('换手随当前预算变（证明是实算，不是常量）',
      lev2.includes(T.fmt.pct(0.35, 0)) && !lev2.includes(T.fmt.pct(0.2, 0)));

    /* ④ 点「保存限额」必须把**勾选框的真实状态**送出去。
     *    `liveSaveLimits(requireDue)` 的第一个参数被 onclick 的 MouseEvent 顶掉了
     *    （恒真），于是取消勾选后保存会把 true 又存回去 —— 勾选框静默失效。 */
    const realFetch5 = sandbox.fetch;
    let sentBody = null;
    sandbox.fetch = (u, o) => {
      const s = String(u);
      if (s.includes('/api/live/limits')) {
        sentBody = JSON.parse((o && o.body) || '{}');
        return Promise.resolve({ ok: true, json: async () => ({
          ok: true, limits: sentBody.limits,
          saved_to: '/tmp/limits.json', saved_at: 1700000001 }) });
      }
      if (s.includes('/api/live/status')) {
        return Promise.resolve({ ok: true, json: async () => (S.live.status || {}) });
      }
      return realFetch5(u, o);
    };
    S.live.sub = 'limits';
    S.live.status = Object.assign({}, S.live.status, { limits: S.live.limits });
    T.renderContent();
    T.liveBind();
    const cb = sandbox.document.getElementById('lvRequireDue');
    if (cb) cb.checked = false;              /* 用户取消了勾选 */
    const saveBtn = sandbox.document.getElementById('liveLimitsSave');
    check('限额子视图里存在「保存限额」按钮', saveBtn != null);
    await saveBtn.onclick({ type: 'click' }); /* 传一个真事件，复现参数被顶掉的情形 */
    const sentDue = sentBody && sentBody.limits
      && sentBody.limits.require_rebalance_due;
    check('点保存送出的是勾选框的真实状态（false），不是 MouseEvent 的恒真',
      sentDue === false, JSON.stringify(sentDue));
    check('保存后提示里带上落盘路径（用户知道去哪儿核对）',
      String(S.live.note || '').includes('/tmp/limits.json'), String(S.live.note));
    sandbox.fetch = realFetch5;

    S.live.sub = realSub;
    S.live.status = realStatus4;
    S.live.limits = realLimits;
    S.live.savedLimits = realSaved;
    S.baseline = realBaseline;
    T.renderContent();
  }

  /* --- 16. 空状态下不留悬空 id --- */
  S.live.plan = null;
  S.live.busy = false;
  T.livePaintAll();
  check('无计划时回到「点生成计划」空态', region('livePlan').includes('点「生成下单计划」开始'));
  check('无计划时闸门回到空态', region('liveGates').includes('尚未生成计划'));
  check('无计划时但按钮仍在（可点）',
    sandbox.document.getElementById('livePrepareBtn') != null);
  check('无计划时 #liveGo 不存在（不会出现无源按钮）',
    sandbox.document.getElementById('liveGo') == null);

  /* --- 9. 自动任务面板 ---
   * 这一块最贵的失败不是崩溃，而是**谎报在跑**：用户看到「正在运行」就放心去改别的
   * 参数，而那一夜其实什么都没发生。所以断言分两层：三种状态（未运行 / 在跑 /
   * 控制文件在但进程已死）必须长得不一样，且「在跑」只在心跳自证时才出现。 */
  {
    const L = T.S.live;
    const savedAuto = L.auto;
    const render = () => T.liveAutoHtml();

    /* 「哪个网格是验收口径」只能问服务端。这一块以前写死 `rd === 1`（1 天 =
     * **不是**已验收配置），验收口径换成 1 天（`v4_1d`）之后，同一个判据就开始
     * 断言相反的事实 —— 而页面上看不出任何异常。所以先钉住契约，再按契约推导。 */
    const accGrid = Number((S.optimalCli || {}).rebalance_days);
    check('/api/spec 下发 optimal_cli.rebalance_days（页面据此判断验收口径）',
      isFinite(accGrid) && accGrid > 0, `optimal_cli=${JSON.stringify(S.optimalCli)}`);
    /* 取一个**和验收口径不同**的网格，从页面自己的选项表里取（别写死 3）。 */
    const otherGrid = (T.AUTO_GRID_CHOICES || []).map(([d]) => d).find((d) => d !== accGrid);
    const gridTxt = (d) => (T.gridDaysText ? T.gridDaysText(d) : `<无 gridDaysText(${d})>`);

    L.auto = null;
    check('自动任务：无数据时不冒充已就绪', render().includes('正在读取'));

    // (a) 从未启动过
    L.auto = { mode: 'paper', running: false, ctl: {}, state: {},
               grid: { source: 'none' } };
    const hIdle = render();
    check('自动任务：未运行时说「未运行」', hIdle.includes('未运行'), hIdle.slice(0, 60));
    check('自动任务：未运行时不给「停止」按钮可用的假象',
      !hIdle.includes('id="autoStop"') || hIdle.includes('disabled'));

    // (b) 正在运行，心跳已确认 —— 网格**就是验收口径**（从服务端取，不写死 1）
    L.auto = { mode: 'paper', running: true,
      ctl: { pid: 4242, rebalance_days: accGrid },
      grid: { rebalance_days: accGrid, bar: '1h', interval_min: 30, source: 'heartbeat' },
      state: { enabled: true, pid: 4242, rebalance_days: accGrid, interval_min: 30,
               started_str: '2026-09-28 21:43:08', checks: 12, trades: 3,
               dry_run: false, kill_switch: false, notify: { ready: false },
               skips: { not_due: 9 },
               last: { action: 'traded', reason: 'executed',
                       decision_ts: '2026-09-28T02:00:00+00:00',
                       next_decision_ts: '2026-09-29T02:00:00+00:00',
                       target_gross: 1.174 } } };
    const hOn = render();
    /* 期望值由页面**同一个格式化器**推导（`gridDaysText`），不在这里再写一遍
     * `fmt.n(v, 2) + ' 天'` —— 那是第二份定义。代价要认：纯格式改动（比如退回
     * 「1.00 天」）不会被这条判据抓住，因为它不是缺陷，是口味。但「格式化器返回
     * 空串」必须抓：`includes('')` 恒真，这条断言会当场退化成恒真判据。 */
    check('自动任务：运行时显示 pid 与网格',
      hOn.includes('4242') && gridTxt(accGrid).includes(String(accGrid))
      && hOn.includes(gridTxt(accGrid)),
      `期望含 ${gridTxt(accGrid)}`);
    /* 网格 = 验收口径时**不能**出那条警告。这一条和下面 (c) 是一条判据的两半：
     * 只测一半（比如只测「不符时有警告」）时，把条件写成恒真也会绿。 */
    check('自动任务：跑的网格就是验收口径时不出「不是验收口径」警告',
      !hOn.includes('不是验收口径'));
    check('自动任务：调仓选项把「当前验收口径」标在服务端说的那个网格上',
      new RegExp(`<option value="${accGrid}"[^>]*>[^<]*（当前验收口径）`).test(hOn)
      && !new RegExp(`<option value="${otherGrid}"[^>]*>[^<]*（当前验收口径）`).test(hOn),
      `accGrid=${accGrid}, other=${otherGrid}`);
    check('自动任务：跳过原因翻成人话，不铺代码常量',
      hOn.includes(T.LIVE_SKIP_LABEL.not_due) && !hOn.includes('not_due'));

    // (b2) 跳过原因必须能自圆其说。
    // 日网格下「未到调仓日」是**错的** —— 网格点今天就到了，只是面板还没走到它；
    // 旧文案还会把「下次」印成一个已经过去的时刻。这两件事合起来读起来就是
    // 「根本没跑」，而这正是这块面板存在的意义。
    //
    // 窗口是**边沿**：`bars_since_decision === 1` 才是可执行的那一刻。所以「再确认
    // N 根后打开」这种按到**调仓点**的距离算的旧提示是错的。到窗口还差 `R - since`
    // 根（不再 `+ 1`）：实盘路径会给面板补一根执行 bar，所以不必再等一根 bar 收盘。
    const R_SKIP = 24, SINCE_SKIP = 22;
    L.auto.state.last = { action: 'skip', reason: 'not_due',
      decision_ts: '2026-09-28T02:00:00+00:00',
      next_decision_ts: '2026-09-29T02:00:00+00:00',
      rebalance_due: false,
      bars_since_decision: SINCE_SKIP, rebalance_bars: R_SKIP,
      target_gross: 1.174,
      not_due_explain: '本周期不在窗口内：目标持仓取自 2026-09-28 10:00 的调仓，'
        + `已过 ${SINCE_SKIP}/${R_SKIP} 根 bar；窗口在调仓点收盘后打开，`
        + `还差 ${R_SKIP - SINCE_SKIP} 根到下个窗口` };
    const hSkip = render();
    check('自动任务：不再断言「未到调仓日」',
      !hSkip.includes('未到调仓日'), '日网格下这句每天都自相矛盾');
    check('自动任务：窗口提示按 bar 数说，根数从 fixture 派生',
      hSkip.includes(`${SINCE_SKIP}/${R_SKIP}`)
      && hSkip.includes(`还差 <b>${R_SKIP - SINCE_SKIP}</b> 根`),
      '窗口是边沿；到窗口还差 R - since 根');
    check('自动任务：不再用「再确认 N 根后打开」的旧口径',
      !hSkip.includes('再确认'), '那个数说的是到调仓点，不是到窗口');
    check('自动任务：展示后端原话，前端不重算那道闸门',
      hSkip.includes('本周期不在窗口内'));
    // 窗口打开那一刻：面板末尾 = 调仓点，实盘路径给它补了一根执行 bar，于是
    // `bars_since_decision === 1`。这一根之差就是「今天没下单」和「今天下单」的分界。
    L.auto.state.last = { action: 'skip', reason: 'not_due',
      decision_ts: '2026-09-29T02:00:00+00:00',
      next_decision_ts: '2026-09-30T02:00:00+00:00',
      rebalance_due: true,
      bars_since_decision: 1, rebalance_bars: R_SKIP,
      target_gross: 1.174 };
    const hOpen = render();
    check('自动任务：窗口打开时显示「已到，可以执行」',
      hOpen.includes('已到，可以执行'),
      '`bars_since_decision === 1` 就是可执行的那一刻');
    check('自动任务：不再说「正压在调仓点上 / 只有 1 根宽」',
      !hOpen.includes('正压在') && !hSkip.includes('只有 1 根宽'),
      '补执行 bar 之后那个中间态已经不可达');
    L.auto.state.last = { action: 'traded', reason: 'executed',
      decision_ts: '2026-09-28T02:00:00+00:00',
      next_decision_ts: '2026-09-29T02:00:00+00:00', target_gross: 1.174 };
    check('自动任务：运行时「停止」按钮可用（不 disabled）',
      /id="autoStop"(?![^>]*disabled)/.test(hOn));

    // (c) 网格 ≠ 验收口径时必须说话。方向由服务端定：1 天曾经是「不是已验收配置」，
    //     现在是验收口径本身。所以这里不写 `rd === 1`，取一个与 accGrid 不同的网格。
    L.auto.grid = { rebalance_days: otherGrid, bar: '1h', interval_min: 30, source: 'heartbeat' };
    L.auto.state.rebalance_days = otherGrid;
    const hOff = render();
    check(`自动任务：网格 ${otherGrid} 天 ≠ 验收口径 ${accGrid} 天时明确提示`,
      hOff.includes('不是验收口径') && hOff.includes(gridTxt(otherGrid)),
      hOff.includes('不是验收口径') ? '' : '页面什么也没说 —— 绩效对不上时无从判断');
    check('自动任务：警告里的验收口径取自服务端（不写死网格）',
      hOff.includes(gridTxt(accGrid)));
    L.auto.grid = { rebalance_days: accGrid, bar: '1h', interval_min: 30, source: 'heartbeat' };
    L.auto.state.rebalance_days = accGrid;

    // (d) 请求了但守护没写出心跳 —— 绝不能显示成「正在运行」
    L.auto.grid = { rebalance_days: accGrid, bar: '1h', interval_min: 30,
                    source: 'requested-not-confirmed' };
    const hUnconf = render();
    check('自动任务：网格未确认时明确说不确认（不冒充生效）',
      hUnconf.includes('网格未确认'));

    // (e) 控制文件在、进程已死
    L.auto = { mode: 'paper', running: false, ctl: { pid: 4242, rebalance_days: accGrid },
               grid: { rebalance_days: accGrid, source: 'requested-not-confirmed' },
               state: { enabled: true, pid: 4242 } };
    check('自动任务：进程已死时不显示成运行中',
      render().includes('未运行') || render().includes('进程已不在'));

    // 三种状态必须彼此可区分，而且**只出一个结论**：「在跑」和「未确认」同时出现时
    // 读的人只会记住第一句，而这两件事的后果完全不同（后者可能一夜不下单）。
    const _concl = (h) => {
      for (const m of ['正在运行', '进程在跑，但网格未确认', '进程已不在', '未运行']) {
        if (h.includes(m)) return m;
      }
      return '?';
    };
    const _tri = [_concl(hIdle), _concl(hOn), _concl(hUnconf)];
    check('自动任务：三种状态各自给出不同结论',
      new Set(_tri).size === 3, _tri.join(' / '));
    check('自动任务：未确认时不再同时宣称「正在运行」（结论互斥）',
      !hUnconf.includes('正在运行'));

    // (f) 极端值不能漏出 NaN/undefined
    L.auto = { mode: 'paper', running: true, ctl: {},
               grid: { rebalance_days: null, source: 'heartbeat' },
               state: { enabled: true, pid: null, checks: null, trades: null,
                        skips: {}, notify: {}, last: {} } };
    const dirtyAuto = dirty(render());
    check('自动任务：字段缺失时不出 NaN/undefined', dirtyAuto.length === 0,
      dirtyAuto.join(','));

    L.auto = savedAuto;
  }

  /* ==================================================================
   * 金额必须带两位小数（交易台上的钱要看到「分」）
   * ==================================================================
   * `fmt.dol` 以前是 `Math.round(v)`：$2,586.98 显示成 $2,587 —— 看着像对上了，
   * 其实差 2 分。交易台上的净值 / 未实现 / 持仓名义 / 订单名义额全走这个格式化器。 */
  {
    const d = T.fmt.dol;
    check('金额：$2,586.98 保留两位小数', d(2586.9843) === '$2,586.98', d(2586.9843));
    check('金额：$56,441.30 保留两位小数', d(56441.303) === '$56,441.30', d(56441.303));
    check('金额：整数补成 $875.00（而不是 $875）', d(875) === '$875.00', d(875));
    check('金额：负数把负号放在 $ 前（-$1,234.50，不是 $-1,234.50）',
      d(-1234.5) === '-$1,234.50', d(-1234.5));
    check('金额：负的亚分钱同样 -$0.00390', d(-0.0039) === '-$0.00390', d(-0.0039));
    /* 反向断言：真·亚分钱不能显示成 $0.00 —— 那会假装「这是零」，比不显示小数更糟。 */
    check('金额：$0.004 不显示成 $0.00', d(0.004) !== '$0.00' && d(0.004).startsWith('$'),
      d(0.004));
    check('金额：真·零仍然是 $0.00（不是 -$0.00）', d(0) === '$0.00' && d(-0) === '$0.00',
      d(0) + ' / ' + d(-0));
    check('金额：null / NaN 仍是 —', d(null) === '—' && d(NaN) === '—');
  }

  /* ==================================================================
   * 删除运行记录：非 JSON 响应必须被解释成人话
   * ==================================================================
   * 实测（2026-09-30）：控制台进程比 `webapp/server.py` 旧，`do_DELETE` 还不存在，
   * `BaseHTTPRequestHandler` 于是回 `501 + HTML`。当时前端写的是 `resp.json()`，
   * 用户看到的是 `Unexpected token '<', "<!DOCTYPE "... is not valid JSON` ——
   * 真正的原因（服务端不支持 DELETE / 控制台没重启）一个字都没露出来。 */
  {
    const savedRuns = T.S.runs, savedSel = T.S.selected;
    T.S.runs = [{ id: 'smoke-del-1', tag: 'smoke_del', label: '删除冒烟' }];
    T.S.selected = 'smoke-del-1';
    const origFetch4 = sandbox.fetch;
    /* 只截 DELETE，其余放行 —— 成功分支里 `deleteRun` 会 `await pollRuns()`，
     * 那是真要去拉 /api/runs；全部截掉会把它喂成删除响应。 */
    const stub = (resp) => (u, o) => ((o && o.method) === 'DELETE'
      ? Promise.resolve(resp) : origFetch4(u, o));
    const runDelete = async (resp) => {
      ALERTS.length = 0;
      sandbox.fetch = stub(resp);
      await T.deleteRun('smoke-del-1');
      return ALERTS.slice();
    };

    // (a) 控制台没重启：501 + HTML —— 必须点名真正的原因
    let a = await runDelete({ ok: false, status: 501,
      text: async () => '<!DOCTYPE HTML>\n<html><body>501</body></html>' });
    check('删除：501+HTML 时点名「服务端不支持 DELETE / 多半没重启」',
      a.length === 1 && a[0].includes('501') && a[0].includes('重启'),
      a[0] || '(没弹窗)');
    check('删除：不再把 "Unexpected token" 原样丢给用户',
      a.length === 1 && !/Unexpected token/.test(a[0]));

    // (b) 服务端回了 JSON 错误：把 error 原文带出来
    a = await runDelete({ ok: false, status: 404,
      text: async () => JSON.stringify({ error: 'no such run' }) });
    check('删除：404+JSON 时带出服务端的 error 原文',
      a.length === 1 && a[0].includes('404') && a[0].includes('no such run'),
      a[0] || '(没弹窗)');

    // (c) 反向断言：成功不许弹窗，而且要真的清掉选中项
    a = await runDelete({ ok: true, status: 200,
      text: async () => JSON.stringify({ id: 'smoke-del-1', deleted: true }) });
    check('删除：成功时不该弹任何窗', a.length === 0, a.join(' | '));
    check('删除：成功时清掉选中项', T.S.selected === null);

    sandbox.fetch = origFetch4;
    T.S.runs = savedRuns; T.S.selected = savedSel;
  }

  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  console.error('冒烟测试自身出错：', e);
  process.exit(2);
});
