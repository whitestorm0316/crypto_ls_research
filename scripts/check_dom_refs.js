/* 检查 app.js 里 `$('#xxx')` 引用的元素是否真的存在。
 *
 * 和 check_js.js 是同一类问题的另一半：DOM 元素不存在时，代码里的
 * `if (!el) return;` / `if (el) el.onclick = ...` 会**静默跳过** ——
 * 按钮点了没反应、面板不更新，控制台却干干净净。改完前端后跑一次。
 *
 * id 的来源有两处：index.html 的静态骨架，以及 app.js 模板串里动态渲染的
 * `id="..."`。所以两边都要扫。
 *
 * 用法：node scripts/check_dom_refs.js webapp/static/index.html webapp/static/app.js
 */
'use strict';
const fs = require('fs');

const [htmlPath, jsPath] = process.argv.slice(2);
if (!htmlPath || !jsPath) {
  console.error('usage: node check_dom_refs.js <index.html> <app.js>');
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, 'utf8');
const js = fs.readFileSync(jsPath, 'utf8');

const defined = new Set();
for (const src of [html, js]) {
  for (const m of src.matchAll(/\bid=["']([A-Za-z0-9_-]+)["']/g)) defined.add(m[1]);
}

// app.js 里所有 `$('#...')` / `$$('#...')` 取到的 id（含复合选择器 `#a #b`）
const refs = new Map();          // id -> 出现次数
for (const m of js.matchAll(/\$\$?\(\s*['"]([^'"]+)['"]/g)) {
  for (const t of m[1].matchAll(/#([A-Za-z0-9_-]+)/g)) {
    refs.set(t[1], (refs.get(t[1]) || 0) + 1);
  }
}

// 动态拼出来的 id（id="${...}"）无法静态校验，列出来提醒人工确认
const dynamic = [...js.matchAll(/\bid=["']\$\{[^}]*\}["']/g)].length
  + [...html.matchAll(/\bid=["']\$\{[^}]*\}["']/g)].length;

let bad = 0;
for (const [id, n] of [...refs].sort()) {
  if (defined.has(id)) continue;
  bad++;
  console.log(`引用了不存在的元素  #${id}   （app.js 里引用 ${n} 次）`);
}
console.log(bad
  ? `\n共 ${bad} 个悬空引用；已定义 id ${defined.size} 个，被引用 id ${refs.size} 个`
  : `OK — ${refs.size} 个被引用的 id 全部有定义（已定义 ${defined.size} 个）`);
if (dynamic) console.log(`注意：有 ${dynamic} 处 id 是动态拼接的，无法静态校验`);
process.exit(bad ? 1 : 0);
