/* 真实浏览器验证：自动任务面板。
 *
 * 为什么必须在真浏览器里跑一次，而不是只看 smoke_live.js：
 * 这块面板同时踩过本项目最贵的两个坑 ——
 *   ① `innerHTML =` 重建会把用户**正在输入**的内容冲掉（历史上复发三次）；
 *   ② 轮询每 10 秒重建一次，而「检查间隔（分钟）」正是一个可编辑的 input。
 * 断言在单测里全绿也说明不了什么：MiniDom 里没有真焦点、没有真的输入法、
 * 也没有「先打一半字再等一次轮询」这个时序。
 *
 * 直连 CDP，不用 puppeteer：Node 22 内置 WebSocket。
 * 用法：node scripts/verify_auto_panel.js [port]
 */
const http = require('http');
const WEB = `http://127.0.0.1:${process.argv[2] || 8790}`;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');

let fails = 0;
const check = (name, ok, detail) => {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${detail && !ok ? '   ' + detail : ''}`);
  if (!ok) fails++;
};

function getJson(url) {
  return new Promise((res, rej) => {
    http.get(url, (r) => {
      let b = ''; r.on('data', (c) => b += c);
      r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

async function main() {
  const port = 9222 + Math.floor(Math.random() * 300);
  const prof = path.join(os.tmpdir(), 'wbauto-' + Date.now());
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
    '--no-proxy-server', '--no-first-run', '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  let ver = null;
  for (let i = 0; i < 60; i++) {
    try { ver = await getJson(`http://127.0.0.1:${port}/json/version`); break; }
    catch (e) { await new Promise((r) => setTimeout(r, 250)); }
  }
  if (!ver) { console.error('Chrome 未能就绪'); chrome.kill(); process.exit(2); }

  const list = await getJson(`http://127.0.0.1:${port}/json/list`);
  const target = list.find((t) => t.type === 'page');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const i = ++id; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  await new Promise((r) => ws.addEventListener('open', r));

  const consoleErrors = [];
  await send('Runtime.enable');
  await send('Runtime.consoleAPICalled', {});
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(m.params.exceptionDetails.text
        + ' ' + (m.params.exceptionDetails.exception || {}).description);
    }
  });

  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: expr, awaitPromise: true, returnByValue: true,
    });
    if (r.result && r.result.exceptionDetails) {
      throw new Error(r.result.exceptionDetails.text + ' :: ' + expr);
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  const waitFor = async (expr, ms = 20000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      try { if (await evaluate(expr)) return true; } catch (e) { /* 页面还没就绪 */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    return false;
  };

  await send('Page.enable');
  await send('Page.navigate', { url: WEB + '/#live' });

  check('页面加载出交易台', await waitFor(
    `!!document.getElementById('liveRoot')`));

  check('自动任务面板被渲染（不是停在占位）', await waitFor(
    `(() => { const e = document.getElementById('liveAuto');`
    + `return e && e.innerText.length > 30 && !e.innerText.includes('正在读取'); })()`));

  const txt = await evaluate(`document.getElementById('liveAuto').innerText`);
  check('面板说明了当前是否在运行', /正在运行|未运行|进程已跑|进程已不在/.test(txt),
    txt.slice(0, 120));
  check('面板显示了调仓间隔选择器',
    await evaluate(`!!document.getElementById('autoGrid')`));
  check('选择器里有 1 天 / 2 天 / 3 天 三个档',
    (await evaluate(`Array.from(document.getElementById('autoGrid').options).map(o=>o.value).join(',')`))
      === '1,2,3');

  /* --- 核心风险：轮询重建 + 正在输入 ---
   * 打一半字，然后强制触发一次 livePaintAll()（等价于一次轮询重建），
   * 值必须还在。历史上前端因为「值只在 change 时才写回 state」而丢掉输入。 */
  await evaluate(`(() => {
    const i = document.getElementById('autoInterval');
    i.focus(); i.value = '45'; i.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await evaluate(`livePaintAll()`);
  await new Promise((r) => setTimeout(r, 400));
  const after = await evaluate(
    `(() => { const i = document.getElementById('autoInterval');`
    + `return i ? i.value : '(元素没了)'; })()`);
  check('重建后未提交的输入值仍在（不会「一输入就清空」）', after === '45', after);

  await evaluate(`(() => { const i = document.getElementById('autoInterval');`
    + `i.value='45'; i.dispatchEvent(new Event('change')); })(), true`);
  await evaluate(`livePaintAll()`);
  await new Promise((r) => setTimeout(r, 300));
  check('change 后值仍正确', (await evaluate(
    `document.getElementById('autoInterval').value`)) === '45');

  /* --- 网格选择器的值要在重建后保持 --- */
  await evaluate(`(() => { const s = document.getElementById('autoGrid');`
    + `s.value = '1'; s.dispatchEvent(new Event('change')); })(), true`);
  await evaluate(`livePaintAll()`);
  await new Promise((r) => setTimeout(r, 300));
  const gv = await evaluate(`document.getElementById('autoGrid').value`);
  check('网格选择器重建后保持所选档位', gv === '1', gv);

  /* --- 真实心跳必须能从服务端读到并显示 --- */
  const api = await getJson(WEB + '/api/live/auto?mode=paper');
  check('服务端读得到自动任务状态', !!api && !!api.grid, JSON.stringify(api).slice(0, 120));
  if (api && api.running) {
    check('在跑时页面显示同一 pid（页面与服务端一致）',
      txt.includes(String((api.state || {}).pid)),
      `pid=${(api.state || {}).pid} / 页面=${txt.slice(0, 150)}`);
  }

  /* --- 没有未捕获异常 --- */
  check('控制台无未捕获异常', consoleErrors.length === 0, consoleErrors.join(' | '));

  /* --- 截图存档，供人工复核（自动化断言看不出布局塌陷） --- */
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  if (shot.result && shot.result.data) {
    const out = 'artifacts/shots/auto_panel.png';
    fs.mkdirSync('artifacts/shots', { recursive: true });
    fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
    console.log(`  截图已存：${out}`);
  }

  ws.close();
  chrome.kill();
  console.log(fails ? `\n${fails} 项未通过` : '\n全部通过');
  process.exit(fails ? 1 : 0);
}

main().catch((e) => { console.error('验证脚本自身出错：', e); process.exit(2); });
