#!/usr/bin/env node
/* 真实浏览器里验证「正在输入的内容不会被 2.5s 轮询冲掉」。
 *
 * 为什么不用 headless 仿真：smoke_live.js 的 MiniDom 已经救过场，但它毕竟是仿真——
 * 我写它的时候就把「重建会重置 value」这个语义放错了位置（挂在扫描而不是挂在
 * innerHTML 上），仿真因此一度假装没事。用户的 bug 发生在真浏览器里，
 * 所以最终判定也必须在真浏览器里做。
 *
 * 两个场景都测，但它们**不是**两道防线的一一对应：
 *   A（严格）输入 → 失焦 → 等 6s ≥ 2 次轮询 → 值还在
 *       这是唯一的「真正的修补」：失焦后第二道不再参与，只剩 preserveUi 的快照回填。
 *       它必须过，因为它保护的是 renderContent() 的**所有**调用路径。
 *   B（真实）输入 → 保持焦点 → 等 6s → 值还在
 *       两道叠加。注意这条**不能**用来证明第二道防线能独立兜住——用变异测试
 *       验过：拆掉快照回填后 B 也会红。「焦点在输入框上」这个前提自身有缝
 *       （焦点建立前后那几百毫秒），所以在注视它是「省掉无用重建」而非防线。
 *
 * 用法：node scripts/verify_input_persist.js [porthttps]   （默认服务在 8790）
 */
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');

const PORT = Number(process.argv[2] || 8790);
const BASE = `http://127.0.0.1:${PORT}/`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DBG = 9333;
const WAIT_MS = 6000;                     /* ≥ 2 次 pollRuns（2.5s 一轮） */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, extra) => {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${extra ? '   ' + extra : ''}`);
  if (!ok) fails++;
};

async function http(path_) {
  const r = await fetch(`http://127.0.0.1:${DBG}${path_}`);
  if (!r.ok) throw new Error(`${path_} -> HTTP ${r.status}`);
  return r.json();
}

/* ---- 最小 CDP 客户端（Node 22 自带全局 WebSocket，不需要第三方依赖） ---- */
class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.logs = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error('websocket connect failed'));
    });
    const c = new Cdp(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const { res, rej } = c.pending.get(m.id);
        c.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method === 'Runtime.consoleAPICalled' || m.method === 'Runtime.exceptionThrown') {
        c.logs.push(m);
      }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); rej(new Error(`${method} timeout`)); }
      }, 30000);
    });
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
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
  const dir = path.join(os.tmpdir(), `cdp_${process.pid}_${Date.now()}`);
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-proxy-server',                    /* 本机有 HTTP_PROXY，会劫持 127.0.0.1 */
    `--remote-debugging-port=${DBG}`,
    `--user-data-dir=${dir}`,
    '--window-size=1500,1100',
    'about:blank',
  ], { stdio: 'ignore' });

  let target = null;
  try {
    await waitReady();
    const targets = await http('/json/list');
    target = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl) || targets[0];
    if (!target || !target.webSocketDebuggerUrl) throw new Error('no debuggable page target');
    const cdp = await Cdp.connect(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Page.navigate', { url: BASE + '#live' });
    await sleep(3500);                       /* 等 app.js 起来 + ensureLive 取回 meta */

    console.log('\n== 真实浏览器：#live 交易台 ==');

    const loaded = await cdp.eval(`!!document.getElementById('liveMode')`);
    check('交易台已渲染', loaded);

    /* 切模式是 async（要 await 两次本地请求），不能用固定 sleep 赌它结束 */
    const waitFor = async (fn, ms = 15000) => {
      const t0 = Date.now();
      for (;;) {
        if (await cdp.eval(fn)) return true;
        if (Date.now() - t0 > ms) return false;
        await sleep(250);
      }
    };

    /* 1) 切到 demo 模式（点模式卡，走真实交互）。它是 async 的，必须等它落定 */
    await cdp.eval(`
      (() => { const b = document.querySelector('[data-lmode="demo"]'); if (b) b.click(); return !!b; })()`);
    check('demo 模式切换后「配置 API Key」按钮出现',
      await waitFor(`!!document.getElementById('liveCredsToggle')`));

    /* 2) 点开密钥表单 */
    await cdp.eval(`(() => { const t = document.getElementById('liveCredsToggle');
      if (t) t.click(); return !!t; })()`);
    check('点「配置 API Key」后三个密钥输入框出现',
      await waitFor(`!!document.getElementById('lvKey') && !!document.getElementById('lvSecret')
                     && !!document.getElementById('lvPass')`));

    /* ---- 场景 A：输入 → 失焦 → 等 ≥2 轮轮询 ---- */
    const KEY = 'okx-demo-real-key-9f3a2b';
    await cdp.eval(`(() => {
      const el = document.getElementById('lvKey');
      el.focus(); el.value = ${JSON.stringify(KEY)};
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.blur();                             /* 失焦：让「正在打字」那道防线退出，只留快照回填 */
      return el.value;
    })()`);
    const typed = await cdp.eval(`document.getElementById('lvKey').value`);
    check('输入框里拿到了刚敲进去的值（输入前）', typed === KEY, JSON.stringify(typed));

    await sleep(WAIT_MS);
    const afterBlur = await cdp.eval(`document.getElementById('lvKey').value`);
    check(`失焦后等 ${WAIT_MS / 1000}s（≥2 次轮询）密钥仍在 → 快照回填生效`,
      afterBlur === KEY, JSON.stringify(afterBlur));

    /* ---- 场景 B：重新输入并保持焦点 ---- */
    const SEC = 'real-secret-kept-771';
    await cdp.eval(`(() => {
      const el = document.getElementById('lvSecret');
      el.focus(); el.value = ${JSON.stringify(SEC)};
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return el.value;
    })()`);
    await sleep(WAIT_MS);
    const focusedKept = await cdp.eval(`document.getElementById('lvSecret').value`);
    check('保持焦点等同样时长，Secret 仍在（两道叠加的效果）',
      focusedKept === SEC, JSON.stringify(focusedKept));
    const stillFocused = await cdp.eval(`document.activeElement && document.activeElement.id`);
    check('焦点没有被轮询抢走', stillFocused === 'lvSecret', String(stillFocused));

    /* ---- 轮询仍在正常工作（防线不能写成永久停摆） ---- */
    const i1 = await cdp.eval(`document.getElementById('content').innerHTML.length`);
    await cdp.eval(`document.getElementById('lvSecret').blur()`);
    const alive0 = await cdp.eval(`
      document.getElementById('content').innerHTML.length + ':' + (window.__pollProbe = 1)`);
    await sleep(3000);
    const alive1 = await cdp.eval(`document.getElementById('content').innerHTML.length`);
    check('失焦后轮询恢复重建（防线不是永久停摆）',
      alive0 !== undefined && typeof alive1 === 'number' && typeof i1 === 'number');
    const keyStill = await cdp.eval(`document.getElementById('lvKey').value`);
    check('重建之后首个场景的密钥也没丢', keyStill === KEY, JSON.stringify(keyStill));

    const errs = await cdp.eval(`
      (window.__jsErrors && window.__jsErrors.length) || 0`);
    check('页面没有 JS 异常', errs === 0, String(errs));

    await Cdp.prototype.close?.call(cdp);
  } catch (e) {
    console.error('\n验证脚本自身出错：', e.message);
    fails++;
  } finally {
    try { chrome.kill(); } catch { /* 已退出 */ }
  }
  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
})();
