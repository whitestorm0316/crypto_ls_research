/* 静态检查 webapp/static/app.js：找出「被调用但没有定义」的标识符。
 *
 * 背景：这个项目的 app.js 全部写在单文件里，函数漏定义时浏览器只在**运行到那一行**
 * 才抛 ReferenceError —— 而这些调用大多在 async 函数 / .then() 里，错误会被
 * promise 链吞掉，页面就停在一个「正在加载…」的壳上，看不出是漏定义。
 * 本脚本把这一类问题在改完代码后立刻暴露出来。
 *
 * 用法：node scripts/check_js.js [文件路径...]
 *   环境变量 CHECK_JS_DUMP=<行号>  打印 strip 后该行附近的「原始 / 剥离」对照
 *
 * ============================ 改这个脚本前必读 ============================
 * strip 要处理的四种「陷阱」都是本项目真实踩过的，缺一个就会得到
 * **静默的错误答案**（漏报比误报危险得多，页面会卡死而看不出原因）：
 *
 *   1. 正则字面量。`/[&<>"']/g` 里的引号若被当成字符串开头，后面几十行都会消失。
 *      判断 `/` 是正则还是除号，要同时看**前一个符号**和**前一个词**
 *      （`return /x/` 的前导词是 return，不在符号集合里）。
 *   2. 模板串嵌套。真实代码有 `` `...${cond ? `<b>${x}</b>` : ''}...` ``。
 *      只按「反引号配对」会提前退出，把 HTML 的 `</b>` 当成正则开头。
 *      正解是状态机 + 栈：`tpl` 遇到 `${` 切 `code` 并记 brace 深度，
 *      `}` 在深度归零时切回 `tpl`；`code` 里再遇到 `` ` `` 就压栈记下外层 brace。
 *   3. 花括号判断顺序。插值结束的条件必须是「`}` 且**当前**深度为 0」，
 *      先自减再判断会把 `${ {a:1} }` 里的对象字面量误认成插值结束。
 *   4. 块注释里的换行要照抄，否则 strip 后行号整体前移，报错位置指向无关代码。
 *
 * 另外 object 方法简写只在**行首缩进**时才算定义，否则 `${foo()}` 会被误判成
 * “在这里定义了 foo”。strip 结束时会自检 mode/栈，跑偏了会打 warn。
 * =========================================================================
 */
'use strict';
const fs = require('fs');

const files = process.argv.slice(2);
if (!files.length) {
  console.error('usage: node check_js.js <file.js> [...]');
  process.exit(2);
}

// 浏览器 / 语言内置，不算「未定义」
const BUILTIN = new Set([
  'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'new', 'delete',
  'void', 'in', 'of', 'do', 'else', 'function', 'class', 'await', 'yield',
  'console', 'fetch', 'JSON', 'Math', 'Number', 'String', 'Boolean', 'Array',
  'Object', 'Date', 'Promise', 'Set', 'Map', 'WeakMap', 'RegExp', 'Error',
  'TypeError', 'RangeError', 'SyntaxError', 'Symbol', 'BigInt', 'Intl',
  'parseInt', 'parseFloat', 'isFinite', 'isNaN', 'encodeURIComponent',
  'decodeURIComponent', 'encodeURI', 'decodeURI', 'setTimeout', 'setInterval',
  'clearTimeout', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame',
  'structuredClone', 'queueMicrotask', 'alert', 'confirm', 'prompt',
  'URLSearchParams', 'URL', 'Blob', 'FileReader', 'Image', 'AbortController',
  'TextEncoder', 'TextDecoder', 'document', 'window', 'globalThis',
  'getComputedStyle', 'localStorage', 'sessionStorage', 'navigator', 'location',
  'history', 'performance', 'CustomEvent', 'Event', 'Node', 'Element',
  'FormData', 'Headers', 'Request', 'Response', 'crypto', 'btoa', 'atob',
  'Function', 'Proxy', 'Reflect', 'escape', 'unescape', 'Infinity',
  'NaN', 'undefined', 'null', 'true', 'false', 'this', 'super',
  // CSS 函数会出现在样式字符串里，不是 JS 调用
  'var', 'url', 'rgb', 'rgba', 'hsl', 'calc', 'clamp', 'env', 'min', 'max',
]);

/** `/` 跟在这些符号后面时是正则开头 */
const REGEX_PREV_CHAR = new Set(['(', ',', '=', ':', '[', '!', '&', '|', '?', '{', ';',
  '+', '-', '*', '%', '<', '>', '~', '^']);
/** `/` 跟在这些关键字后面时也是正则开头 */
const REGEX_PREV_WORD = new Set(['return', 'typeof', 'case', 'in', 'of', 'do', 'else',
  'delete', 'void', 'instanceof', 'new', 'yield', 'await', 'throw']);

/** 去掉注释与字符串（模板串整段跳过），行数保持不变。 */
function strip(src) {
  const out = [];
  const n = src.length;
  let i = 0;
  let mode = 'code';           // code | line | block | sq | dq | regex | tpl
  let regexOk = true;          // 此处出现 `/` 是正则还是除号
  let word = '';               // 正在累积的标识符，用于识别 return / typeof 这类关键字
  const stack = [];            // {t:'tpl'|'expr', brace}
  let brace = 0;               // 当前 `${...}` 里的花括号深度
  const top = () => stack[stack.length - 1];

  while (i < n) {
    const c = src[i], c2 = src[i + 1];

    if (mode === 'code') {
      if (c === '/' && c2 === '/') { mode = 'line'; i += 2; continue; }
      if (c === '/' && c2 === '*') { mode = 'block'; i += 2; continue; }
      if (c === "'") { mode = 'sq'; out.push(' '); i++; continue; }
      if (c === '"') { mode = 'dq'; out.push(' '); i++; continue; }
      if (c === '`') {                        // 进入模板串，记住外层 brace 以便回来
        stack.push({ t: 'tpl', brace });
        brace = 0;
        mode = 'tpl';
        out.push(' ');
        i++; continue;
      }
      if (c === '/' && regexOk) { mode = 'regex'; out.push(' '); i++; continue; }
      if (c === '{') {
        brace++;
      } else if (c === '}') {
        // 顺序很关键：先判断「当前深度是 0」才是插值结束
        if (brace === 0 && top() && top().t === 'expr') {
          stack.pop();
          mode = 'tpl';
          out.push('}');
          i++; continue;
        }
        brace--;
      }
      out.push(c === '\n' ? '\n' : c);
      if (/[A-Za-z0-9_$]/.test(c)) {
        word += c;
      } else {
        if (word) { regexOk = REGEX_PREV_WORD.has(word); word = ''; }
        if (REGEX_PREV_CHAR.has(c)) regexOk = true;
        else if (c === ')' || c === ']') regexOk = false;
        // 空白不改变判断
      }
      i++; continue;
    }

    if (mode === 'line') {
      if (c === '\n') { mode = 'code'; out.push('\n'); }
      i++; continue;
    }

    if (mode === 'block') {
      if (c === '*' && c2 === '/') { mode = 'code'; i += 2; continue; }
      if (c === '\n') out.push('\n');        // 行号必须对齐
      i++; continue;
    }

    if (mode === 'sq' || mode === 'dq') {
      const q = mode === 'sq' ? "'" : '"';
      if (c === '\\') { i += 2; continue; }
      if (c === q) { mode = 'code'; out.push(' '); regexOk = false; i++; continue; }
      if (c === '\n') out.push('\n');
      i++; continue;
    }

    if (mode === 'regex') {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') { mode = 'code'; out.push(' '); regexOk = false; i++; continue; }
      if (c === '\n') { mode = 'code'; out.push('\n'); regexOk = false; i++; continue; }  // 未闭合 → 当除号
      i++; continue;
    }

    // tpl：跳过模板文本，但 `${...}` 里的表达式按代码处理
    if (c === '\\') { i += 2; continue; }
    if (c === '`') {                          // 模板串结束，恢复外层 brace
      const s = stack.pop();
      brace = s ? s.brace : 0;
      mode = 'code';
      out.push(' ');
      regexOk = false;
      i++; continue;
    }
    if (c === '$' && c2 === '{') {            // 进入插值表达式
      stack.push({ t: 'expr', brace: 0 });
      brace = 0;
      mode = 'code';
      out.push(' ');
      i += 2; continue;
    }
    if (c === '\n') out.push('\n');
    i++; continue;
  }

  if (mode !== 'code' || stack.length) {
    console.error(`[warn] strip 结束时 mode=${mode} 栈深=${stack.length}`
      + ' —— 可能有未闭合的字符串/模板串，下面的结果不可信');
  }
  return out.join('');
}

/** 模板串里 `` `${foo(` `` 这种插值开头的直接调用，补检 strip 跳过的部分。 */
function tplCalls(src) {
  const names = new Set();
  for (const m of src.matchAll(/\$\{\s*([A-Za-z_$][\w$]*)\s*\(/g)) names.add(m[1]);
  return names;
}

let bad = 0;
for (const f of files) {
  const raw = fs.readFileSync(f, 'utf8');
  const src = strip(raw);
  const rawLines = raw.split('\n');
  const srcLines = src.split('\n');
  if (rawLines.length !== srcLines.length) {
    console.error(`[warn] ${f}: strip 后行数 ${srcLines.length} != 原始 ${rawLines.length} —— 行号会错位`);
  }
  if (process.env.CHECK_JS_DUMP) {
    const dumpFile = f.replace(/\.js$/, '') + '.stripped.js';
    fs.writeFileSync(dumpFile, src);
    console.error(`[dump] 已写出 ${dumpFile}`);
    const a = parseInt(process.env.CHECK_JS_DUMP, 10);
    if (a > 0) {
      for (let i = a - 1; i < Math.min(a + 5, srcLines.length); i++) {
        console.error(`  ${i + 1}| ${rawLines[i]}`);
        console.error(`    > | ${srcLines[i]}`);
      }
    }
  }

  const defined = new Set();
  const addAll = (s) => { for (const m of s.matchAll(/[A-Za-z_$][\w$]*/g)) defined.add(m[0]); };

  for (const m of src.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
  for (const m of src.matchAll(/\bclass\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
  for (const m of src.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
  for (const m of src.matchAll(/\b(?:const|let|var)\s*\{([^}]*)\}\s*=/g)) addAll(m[1]);
  for (const m of src.matchAll(/\b(?:const|let|var)\s*\[([^\]]*)\]\s*=/g)) addAll(m[1]);
  for (const m of src.matchAll(/function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)/g)) addAll(m[1]);
  for (const m of src.matchAll(/\(([^)]*)\)\s*=>/g)) addAll(m[1]);
  for (const m of src.matchAll(/([A-Za-z_$][\w$]*)\s*=>/g)) defined.add(m[1]);
  // 对象方法简写 / 属性键 —— 必须行首缩进，见文件头说明 4 条之外的最后一段
  for (const m of src.matchAll(/^[ \t]+([A-Za-z_$][\w$]*)\s*(?::|\()/gm)) defined.add(m[1]);
  for (const m of src.matchAll(/\b(?:for|catch)\s*\(\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);

  const seen = new Set();
  const flag = (name, where) => {
    if (BUILTIN.has(name) || defined.has(name) || seen.has(name)) return;
    seen.add(name);
    bad++;
    console.log(`${f}${where}  调用未定义  ${name}(`);
  };
  srcLines.forEach((ln, i) => {
    for (const m of ln.matchAll(/(^|[^\w$.])([A-Za-z_$][\w$]*)\s*\(/g)) {
      flag(m[2], `:${i + 1}  |  ${(rawLines[i] || '').trim().slice(0, 110)}`);
    }
  });
  for (const name of tplCalls(raw)) flag(name, '  [模板串插值]');

  if (!seen.size) console.log(`${f}: OK — 没有发现未定义的被调用标识符`);
}

process.exit(bad ? 1 : 0);
