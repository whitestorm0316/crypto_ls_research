#!/usr/bin/env node
/* 真实浏览器验证两项新功能（headless 的 --screenshot/--dump-dom 会静默失败，所以只用 CDP）：
 *
 *   ① 「刷新账户」按钮：真实存在于「账户与连接」面板头部、可见、被点后**确实**重新
 *      发了一次 /api/live/status（而不是只转了个圈）。
 *   ② 「模式与交易场所」记住选项：切到 demo → localStorage 落盘 → **刷新页面**后
 *      仍然停在 demo（而不是跳回 paper）。
 *
 * 用法：node scripts/verify_refresh_and_mode.js [port]   （默认 8790）
 */
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const PORT = Number(process.argv[2] || 8790);
const BASE = `http://127.0.0.1:${PORT}/`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DBG = 9334;

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

(async () => {
  const dir = path.join(os.tmpdir(), `cdp_vrm_${process.pid}_${Date.now()}`);
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-proxy-server',
    `--remote-debugging-port=${DBG}`,
    `--user-data-dir=${dir}`,
    '--window-size=1500,1100',
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

    /* 记录所有 /api/live/status 请求，供「刷新账户确实重读」这条用 */
    const hook = `
      window.__statusCalls = window.__statusCalls || [];
      if (!window.__fetchPatched) {
        window.__fetchPatched = true;
        const _f = window.fetch;
        window.fetch = function(u, o) {
          try { if (String(u).indexOf('/api/live/status') === 0 || String(u).indexOf('/api/live/status') > -1)
            window.__statusCalls.push(String(u)); } catch (e) {}
          return _f.apply(this, arguments);
        };
      }`;
    const installHook = async () => cdp.eval(hook);

    await cdp.send('Page.navigate', { url: BASE + '#live' });
    await sleep(3500);
    await installHook();

    const waitFor = async (fn, ms = 15000) => {
      const t0 = Date.now();
      for (;;) { if (await cdp.eval(fn)) return true; if (Date.now() - t0 > ms) return false; await sleep(250); }
    };

    console.log('\n== ① 刷新账户按钮 ==');
    check('交易台已渲染', await cdp.eval(`!!document.getElementById('liveMode')`));
    check('「刷新账户」按钮存在于账户面板头部',
      await cdp.eval(`!!document.getElementById('liveAcctRefresh')`));
    check('按钮可见（checkVisibility）',
      await cdp.eval(`(() => { const b = document.getElementById('liveAcctRefresh');
        return !!b && (b.checkVisibility ? b.checkVisibility() : b.offsetParent !== null); })()`));
    check('按钮文案正确',
      (await cdp.eval(`(document.getElementById('liveAcctRefresh')||{}).textContent`)) === '↻ 刷新账户',
      JSON.stringify(await cdp.eval(`(document.getElementById('liveAcctRefresh')||{}).textContent`)));

    const before = await cdp.eval(`(window.__statusCalls||[]).length`);
    await cdp.eval(`(() => { const b = document.getElementById('liveAcctRefresh'); if (b) b.click(); return !!b; })()`);
    await waitFor(`(window.__statusCalls||[]).length > ${before}`, 10000);
    const after = await cdp.eval(`(window.__statusCalls||[]).length`);
    check('点击后确实重新请求了一次 /api/live/status', after > before, `${before} → ${after}`);
    check('刷新后账户面板仍正常渲染（不是空白/报错）',
      await cdp.eval(`(() => { const el = document.getElementById('liveAcct');
        return !!el && el.innerHTML.length > 40; })()`));

    console.log('\n== ② 模式与交易场所记住选项 ==');
    await cdp.eval(`(() => { try { localStorage.removeItem('ls-live-mode'); } catch(e){} return 1; })()`);
    /* 切到 demo（真实点击） */
    await cdp.eval(`(() => { const b = document.querySelector('[data-lmode="demo"]'); if (b) b.click(); return !!b; })()`);
    check('切到 demo 后模式卡亮起',
      await waitFor(`(() => { const b = document.querySelector('[data-lmode="demo"]'); return !!b && b.classList.contains('on'); })()`));
    const stored = await cdp.eval(`(() => { try { return localStorage.getItem('ls-live-mode'); } catch(e){ return null; } })()`);
    check('localStorage 里记住了 demo', stored === 'demo', JSON.stringify(stored));

    /* 真正刷新页面，看模式是否还停在 demo 而不是跳回 paper */
    await cdp.send('Page.navigate', { url: BASE + '#live' });
    await sleep(3500);
    await installHook();
    check('刷新页面后交易台重新渲染', await waitFor(`!!document.getElementById('liveMode')`));
    check('刷新后仍停在 demo（没有跳回 paper）',
      await waitFor(`(() => { const b = document.querySelector('[data-lmode="demo"]'); return !!b && b.classList.contains('on'); })()`));
    check('刷新后 paper 卡不再是选中态',
      await cdp.eval(`(() => { const b = document.querySelector('[data-lmode="paper"]'); return !b || !b.classList.contains('on'); })()`));

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
