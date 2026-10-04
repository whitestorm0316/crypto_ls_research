#!/usr/bin/env node
/* 真实浏览器验证「账户净值历史」面板（headless 的 --screenshot/--dump-dom 会静默失败，所以只用 CDP）：
 *
 *   ① 面板真的在交易台里渲染出 <canvas id="navChart">，且尺寸非 0（不是被布局挤成 0 宽）；
 *   ② 画布**真的被画了东西**——回读像素，不是一张空白 canvas（防「函数跑了但什么都没画」）；
 *   ③ 「曲线 / K线」两个视图按钮可切，切换后画布内容确实变了（防「按钮绑了但重画没发生」）；
 *   ④ 「7天 / 30天 / 90天 / 全部」区间按钮可切，切换后确实重新请求了一次
 *      /api/live/navhist（防「按钮是装饰」），且读数条 navMeta 有内容；
 *   ⑤ 「重建」按钮能触发重取；
 *   ⑥ 全程没有 JS 异常。
 *
 * 用法：node scripts/verify_navhist.js [port]   （默认 8790）
 */
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const PORT = Number(process.argv[2] || 8790);
const BASE = `http://127.0.0.1:${PORT}/`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DBG = 9336;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, extra) => {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${extra ? '   ' + extra : ''}`);
  if (!ok) fails++;
};

async function http(p) {
  const r = await fetch(`http://127.0.0.1:${DBG}${p}`);
  if (!r.ok) throw new Error(`${p} -> HTTP ${r.status}`);
  return r.json();
}

class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.exceptions = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws connect failed')); });
    const c = new Cdp(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const { res, rej } = c.pending.get(m.id);
        c.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method === 'Runtime.exceptionThrown') {
        c.exceptions.push(m.params && m.params.exceptionDetails
          ? (m.params.exceptionDetails.exception?.description
             || m.params.exceptionDetails.text || 'exception')
          : 'exception');
      }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(`${method} timeout`)); } }, 30000);
    });
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      throw new Error('JS: ' + (r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails)));
    }
    return r.result.value;
  }
}

async function waitReady() {
  for (let i = 0; i < 60; i++) {
    try { return await http('/json/version'); } catch { await sleep(400); }
  }
  throw new Error('chrome did not open its debug port');
}

/* 回读画布像素得到一个「指纹」：非零像素数 + RGB 求和 + 逐位 hash。
 * 空白 canvas → nonzero=0；两张不同的图 → sig 不同。 */
const PIXEL_PROBE = (id) => `(() => {
  const cv = document.getElementById(${JSON.stringify(id)});
  if (!cv) return { err: 'no canvas' };
  const w = cv.width, h = cv.height;
  if (!w || !h) return { err: 'zero size', w, h, cssW: cv.clientWidth, cssH: cv.clientHeight };
  let ctx;
  try { ctx = cv.getContext('2d', { willReadFrequently: true }); } catch (e) { return { err: 'ctx ' + e.message }; }
  if (!ctx) return { err: 'no ctx' };
  let nonzero = 0, sum = 0, hash = 0, nan = 0;
  const d = ctx.getImageData(0, 0, w, h).data;
  for (let i = 0; i < d.length; i += 4) {
    const a = d[i + 3];
    if (!Number.isFinite(d[i]) || !Number.isFinite(d[i + 1])) { nan++; continue; }
    if (a) {
      nonzero++;
      sum += d[i] + d[i + 1] + d[i + 2];
      hash = (hash * 31 + d[i] * 7 + d[i + 1] * 13 + d[i + 2] * 17) % 2147483647;
    }
  }
  return { w, h, cssW: cv.clientWidth, cssH: cv.clientHeight, nonzero, sum, hash, nan };
})()`;

(async () => {
  const dir = path.join(os.tmpdir(), `cdp_navh_${process.pid}_${Date.now()}`);
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-proxy-server',
    `--remote-debugging-port=${DBG}`,
    `--user-data-dir=${dir}`,
    '--window-size=1500,1400',
    'about:blank',
  ], { stdio: 'ignore' });

  let cdp = null;
  try {
    await waitReady();
    const targets = await http('/json/list');
    const target = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl) || targets[0];
    if (!target || !target.webSocketDebuggerUrl) throw new Error('no debuggable page target');
    cdp = await Cdp.connect(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');

    /* 记录所有 /api/live/navhist 请求，供「区间按钮确实重取」这条用 */
    const hook = `
      window.__navCalls = window.__navCalls || [];
      if (!window.__fetchPatched) {
        window.__fetchPatched = true;
        const _f = window.fetch;
        window.fetch = function(u, o) {
          try { if (String(u).indexOf('/api/live/navhist') > -1) window.__navCalls.push(String(u)); } catch (e) {}
          return _f.apply(this, arguments);
        };
      }`;
    const installHook = async () => cdp.eval(hook);

    await cdp.send('Page.navigate', { url: BASE + '#live' });
    await sleep(3500);
    await installHook();

    const waitFor = async (fn, ms = 20000) => {
      const t0 = Date.now();
      for (;;) { if (await cdp.eval(fn)) return true; if (Date.now() - t0 > ms) return false; await sleep(300); }
    };

    console.log('\n== ① 面板与画布真的渲染出来 ==');
    check('交易台已渲染', await cdp.eval(`!!document.getElementById('liveMode')`));
    check('「账户净值历史」画布在 DOM 里', await waitFor(`!!document.getElementById('navChart')`));
    const geo = await cdp.eval(`(() => { const cv = document.getElementById('navChart');
      if (!cv) return null;
      const r = cv.getBoundingClientRect();
      return { cssW: cv.clientWidth, cssH: cv.clientHeight, w: cv.width, h: cv.height,
        rectW: Math.round(r.width), rectH: Math.round(r.height),
        inLive: !!(cv.closest && cv.closest('#liveNav') === document.getElementById('liveNav')) }; })()`);
    check('画布 CSS 尺寸非 0（没被挤成一条线）', !!geo && geo.cssW > 120 && geo.cssH > 80,
      JSON.stringify(geo));
    check('画布挂在 #liveNav 容器里（没逃出面板）', !!geo && geo.inLive === true);
    check('画布退避倍率生效（drawing buffer ≥ CSS）', !!geo && geo.w >= geo.cssW && geo.h >= geo.cssH,
      geo ? `${geo.w}x${geo.h} vs css ${geo.cssW}x${geo.cssH}` : '');

    console.log('\n== ② 画布真的被画了东西（不是空白） ==');
    check('画布已绘出像素', await waitFor(`(() => { const p = ${PIXEL_PROBE('navChart')};
      return p && !p.err && p.nonzero > 500; })()`));
    const curve = await cdp.eval(PIXEL_PROBE('navChart'));
    check('曲线视图：非零像素量合理', curve && !curve.err && curve.nonzero > 500,
      curve && !curve.err ? `nonzero=${curve.nonzero} sum=${curve.sum}` : JSON.stringify(curve));
    check('像素里没有 NaN 污染', curve && !curve.err && (curve.nan | 0) === 0,
      curve && !curve.err ? `nan=${curve.nan}` : '');

    console.log('\n== ③ 曲线 / K线 视图可切且确实重画 ==');
    check('默认停在「曲线」视图',
      await cdp.eval(`(() => { const b = document.querySelector('[data-navview="line"]');
        return !!b && b.classList.contains('on'); })()`));
    await cdp.eval(`(() => { const b = document.querySelector('[data-navview="candle"]'); if (b) b.click(); return !!b; })()`);
    check('点「K线」后该按钮亮起',
      await waitFor(`(() => { const b = document.querySelector('[data-navview="candle"]');
        return !!b && b.classList.contains('on'); })()`));
    await sleep(500);
    const candle = await cdp.eval(PIXEL_PROBE('navChart'));
    check('K线视图画布仍非空且无 NaN', candle && !candle.err && candle.nonzero > 500 && (candle.nan | 0) === 0,
      candle && !candle.err ? `nonzero=${candle.nonzero}` : JSON.stringify(candle));
    check('切到 K线后画布内容确实变了（不是没重画）',
      curve && candle && !curve.err && !candle.err && curve.hash !== candle.hash,
      curve && candle ? `${curve.hash} → ${candle.hash}` : '');
    /* 切回曲线，确认是「来回可切」而不是单向 */
    await cdp.eval(`(() => { const b = document.querySelector('[data-navview="line"]'); if (b) b.click(); return !!b; })()`);
    await sleep(500);
    check('切回「曲线」按钮状态正确',
      await waitFor(`(() => { const b = document.querySelector('[data-navview="line"]');
        return !!b && b.classList.contains('on'); })()`));

    console.log('\n== ④ 区间按钮真的重取数据 ==');
    const metaText = await cdp.eval(`(document.getElementById('navMeta')||{}).textContent || ''`);
    check('读数条 navMeta 有内容（数据源/区间/校验）', metaText.trim().length > 0, JSON.stringify(metaText.slice(0, 60)));
    const c0 = await cdp.eval(`(window.__navCalls||[]).length`);
    await cdp.eval(`(() => { const b = document.querySelector('[data-navdays="7"]'); if (b) b.click(); return !!b; })()`);
    await waitFor(`(window.__navCalls||[]).length > ${c0}`, 25000);
    const c1 = await cdp.eval(`(window.__navCalls||[]).length`);
    check('点「7天」确实重新请求了 /api/live/navhist', c1 > c0, `${c0} → ${c1}`);
    const lastUrl = await cdp.eval(`(() => { const a = window.__navCalls||[]; return a[a.length-1] || ''; })()`);
    check('请求带上了 days=7', /[?&]days=7(&|$)/.test(lastUrl || ''), lastUrl);
    check('切 7 天后该按钮亮起',
      await waitFor(`(() => { const b = document.querySelector('[data-navdays="7"]');
        return !!b && b.classList.contains('on'); })()`));
    /* 面板没有因为重取而崩掉 */
    check('换区间后画布仍在且非空',
      await waitFor(`(() => { const p = ${PIXEL_PROBE('navChart')}; return p && !p.err && p.nonzero > 200; })()`));
    /* 收尾：切回 30 天 */
    await cdp.eval(`(() => { const b = document.querySelector('[data-navdays="30"]'); if (b) b.click(); return !!b; })()`);
    await sleep(1500);

    console.log('\n== ⑤ 「重建」按钮：重取 + 期间不许把面板清空 ==');
    const c2 = await cdp.eval(`(window.__navCalls||[]).length`);
    await cdp.eval(`(() => { const b = document.getElementById('navReload'); if (b) b.click(); return !!b; })()`);
    await waitFor(`(window.__navCalls||[]).length > ${c2}`, 25000);
    const c3 = await cdp.eval(`(window.__navCalls||[]).length`);
    check('点「重建」确实重新请求了一次', c3 > c2, `${c2} → ${c3}`);
    /* 回归守卫：重建期间旧曲线**必须还在**（这里曾经把 L.nav 清空，
     * 面板会从一张图塌成一行字；冷探测下要等满 6 秒超时，很难看）。 */
    check('重建进行中，旧曲线仍留在面板里（没有被清空）',
      await cdp.eval(`(() => { const cv = document.getElementById('navChart');
        return !!cv && cv.clientWidth > 120; })()`));
    check('重建进行中，面板给了「重建中…」提示',
      await cdp.eval(`!!document.querySelector('#liveNav .navbusy')`));
    await waitFor(`(() => { const cv = document.getElementById('navChart');
      const b = document.querySelector('#liveNav .navbusy');
      return !!cv && !b; })()`, 25000);
    const settled = await cdp.eval(PIXEL_PROBE('navChart'));
    check('重建完成后画布重新画出内容', settled && !settled.err && settled.nonzero > 500,
      settled && !settled.err ? `nonzero=${settled.nonzero}` : JSON.stringify(settled));

    console.log('\n== ⑥ 切走再切回交易台，面板照旧（没被别的 tab 弄坏） ==');
    check('净值历史面板没有逃出 liveOnly 容器',
      await cdp.eval(`(() => { const el = document.getElementById('liveNav');
        if (!el) return false; return !!el.closest('.liveOnly'); })()`));
    await cdp.eval(`(() => { const b = document.querySelector('[data-tab="result"]'); if (b) b.click(); return !!b; })()`);
    check('已切到「回测结果」，交易台画布随之离场',
      await waitFor(`!document.getElementById('navChart')`, 8000));
    await cdp.eval(`(() => { const b = document.querySelector('[data-tab="live"]'); if (b) b.click(); return !!b; })()`);
    check('切回交易台后画布重新出现', await waitFor(`!!document.getElementById('navChart')`, 20000));
    check('切回后画布仍画出了内容（不是空 canvas）',
      await waitFor(`(() => { const p = ${PIXEL_PROBE('navChart')}; return p && !p.err && p.nonzero > 500; })()`));

    const errs = cdp.exceptions.length;
    check('页面没有 JS 异常', errs === 0, errs ? JSON.stringify(cdp.exceptions.slice(0, 2)) : '');
  } catch (e) {
    console.error('\n验证脚本自身出错：', e.message);
    fails++;
  } finally {
    try { chrome.kill(); } catch { /* 已退出 */ }
  }
  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})();
