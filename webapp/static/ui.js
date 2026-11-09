/* =========================================================================
   UI 组件库 —— 让「样式」重新变成可引用的东西，而不是手写字符串
   -------------------------------------------------------------------------
   为什么需要这个文件
   ------------------
   改造前 `app.js` 里有 **200 处** `style="..."`，`style.css` 里有 12 种字号、
   9 种 `margin-top`。这不是审美问题，是**没有可引用的刻度**：每写一个新面板都在
   凭手感挑 `margin-top:11px` 还是 `12px`，所以永远统一不了。组件收口之后，
   "一个面板该长什么样"只有一处定义。

   三条硬约束（违反任何一条都会静默坏掉）
   --------------------------------------
   1. **整体包在 IIFE 里，只暴露 `ui` 一个顶层名字。**
      `app.js` 已经定义了顶层 `const esc`。如果这里也写 `const esc = ...`，
      两个 classic script 共享同一个全局词法环境 ⇒ 直接抛
      `Identifier 'esc' has already been declared`，整页白屏。
   2. **不依赖 app.js 的任何东西**（`esc` / `fmt` / `S` 一概不碰）。
      这个文件先于 `app.js` 加载，而且 `scripts/smoke_ui.js` 与
      `test_universe_drift.py` 用 node vm 单独加载源码 —— 反向依赖会让它们崩。
      业务数字仍然由 `app.js` 的 `fmt` 格式化后传进来，保持唯一来源。
   3. **弹层挂 `document.body`，不挂 `#content`。**
      `#content` 会被 `pollRuns()` 每 2.5s 整块重建（`innerHTML =`），
      挂在里面的话，一个正在等待用户确认的对话框会在两秒半后凭空消失。
   ========================================================================= */
'use strict';

const ui = (() => {
  /* ---------------- 基础 ---------------- */
  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  const cls = (...xs) => xs.filter(Boolean).join(' ');

  /* ---------------- 按钮 ---------------- */
  /* kind: primary | secondary(默认) | ghost | danger
   * size: '' | sm
   * 只生成 `<button>`，事件由调用方用 data-* 绑定（与现有 `bindSide` 一致）。 */
  function btn(o) {
    o = o || {};
    const attrs = [
      o.id ? `id="${esc(o.id)}"` : '',
      o.data ? Object.keys(o.data).map((k) => `data-${esc(k)}="${esc(o.data[k])}"`).join(' ') : '',
      o.title ? `title="${esc(o.title)}"` : '',
      o.disabled ? 'disabled' : '',
      o.type ? `type="${esc(o.type)}"` : 'type="button"',
    ].filter(Boolean).join(' ');
    const k = o.kind && o.kind !== 'secondary' ? ' ' + o.kind : '';
    const s = o.size === 'sm' ? ' sm' : '';
    return `<button class="btn${k}${s}" ${attrs}>${o.label || ''}</button>`;
  }

  /* ---------------- 状态徽标 ---------------- */
  /* kind: ok | warn | danger | info | neutral
   * 语义固定，调用方不再自己挑颜色 —— 这是「同一个状态到处不同色」的根因。 */
  const BADGE_KIND = new Set(['ok', 'warn', 'danger', 'info', 'neutral']);
  function badge(text, kind, title) {
    const k = BADGE_KIND.has(kind) ? kind : 'neutral';
    return `<span class="uibadge ${k}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
  }

  /* ---------------- 卡片 ---------------- */
  /* o: { title, sub, actions(HTML), body(HTML), footer(HTML), cls, id } */
  function card(o) {
    o = o || {};
    const head = (o.title || o.actions)
      ? `<div class="head"><h3>${esc(o.title || '')}</h3>`
        + (o.sub ? `<span class="uisub">${esc(o.sub)}</span>` : '')
        + `<span class="spacer"></span>${o.actions || ''}</div>`
      : '';
    return `<div class="panel ${o.cls || ''}"${o.id ? ` id="${esc(o.id)}"` : ''}>`
      + head
      + `<div class="body"${o.bodyId ? ` id="${esc(o.bodyId)}"` : ''}>${o.body || ''}</div>`
      + (o.footer ? `<div class="uifoot">${o.footer}</div>` : '')
      + `</div>`;
  }

  /* ---------------- KPI ---------------- */
  /* o: { label, value, delta, dir('up'|'down'|'flat'), hint, unit }
   * `value` 是**已格式化**的字符串：业务数字的唯一来源仍然是 app.js 的 fmt。 */
  function stat(o) {
    o = o || {};
    const d = o.delta != null
      ? `<div class="d ${o.dir || 'flat'}">${o.delta}</div>` : '';
    return `<div class="kpi"${o.hint ? ` title="${esc(o.hint)}"` : ''}>`
      + `<div class="k">${esc(o.label || '')}${o.unit ? ` <span class="uiunit">${esc(o.unit)}</span>` : ''}</div>`
      + `<div class="v">${o.value == null ? '—' : o.value}</div>${d}</div>`;
  }

  /* ---------------- 表格 ---------------- */
  /* o: { cols:[{t, num}], rows:[[html,...]], dense, empty, cls }
   * `rows` 里放的是**已经转义好的** HTML 片段（含颜色 span 等），所以这里不再 esc。
   * 数字列由 `num:true` 统一右对齐 + tabular-nums —— 这是可读性最便宜的一步。 */
  function table(o) {
    o = o || {};
    const cols = o.cols || [];
    if (!o.rows || !o.rows.length) {
      return empty(o.empty || { title: '暂无数据' });
    }
    const th = cols.map((c) => `<th${c.num ? ' class="num"' : ''}>${esc(c.t || '')}</th>`).join('');
    const body = o.rows.map((r) => '<tr>'
      + r.map((cell, i) => `<td${cols[i] && cols[i].num ? ' class="num"' : ''}>${cell}</td>`).join('')
      + '</tr>').join('');
    return `<div class="tblwrap${o.dense ? ' tbl-dense' : ''}${o.cls ? ' ' + o.cls : ''}">`
      + `<table class="ordtable"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  /* ---------------- 骨架屏 ---------------- */
  /* kind: table | card | lines
   * 替代「正在读取…」纯文本：让等待**可见**，且不会在加载完成后发生布局跳动。 */
  function skeleton(o) {
    o = o || {};
    const kind = o.kind || 'lines';
    const n = o.rows || 4;
    if (kind === 'table') {
      const rows = Array.from({ length: n }, () =>
        `<div class="skrow">${'<span class="skbar"></span>'.repeat(o.cols || 5)}</div>`).join('');
      return `<div class="skel skel-table" aria-busy="true" aria-label="正在加载">${rows}</div>`;
    }
    if (kind === 'card') {
      return `<div class="skel skel-card" aria-busy="true" aria-label="正在加载">`
        + '<span class="skbar lg"></span><span class="skbar"></span><span class="skbar sm"></span></div>';
    }
    const lines = Array.from({ length: n }, (_, i) =>
      `<span class="skbar${i === n - 1 ? ' sm' : ''}"></span>`).join('');
    return `<div class="skel" aria-busy="true" aria-label="正在加载">${lines}</div>`;
  }

  /* ---------------- 空状态 ---------------- */
  /* o: { icon, title, desc, action(HTML) }
   * 空态必须回答三件事：现在是什么状态、为什么、下一步做什么。
   * 只有「暂无记录」四个字，用户不知道是自己没跑、还是坏了。 */
  function empty(o) {
    o = o || {};
    return `<div class="uiempty">`
      + `<div class="uiempty-ico" aria-hidden="true">${o.icon || '·'}</div>`
      + `<div class="uiempty-title">${esc(o.title || '暂无数据')}</div>`
      + (o.desc ? `<div class="uiempty-desc">${esc(o.desc)}</div>` : '')
      + (o.action ? `<div class="uiempty-act">${o.action}</div>` : '')
      + `</div>`;
  }

  /* ---------------- 错误块 ---------------- */
  /* 错误必须带「下一步动作」，否则用户只知道自己错了、不知道怎么办。 */
  function error(o) {
    o = o || {};
    return `<div class="uierr" role="alert">`
      + `<div class="uierr-title">${esc(o.title || '出错了')}</div>`
      + (o.detail ? `<div class="uierr-detail mono">${esc(o.detail)}</div>` : '')
      + (o.fix ? `<div class="uierr-fix">${esc(o.fix)}</div>` : '')
      + (o.action ? `<div class="uierr-act">${o.action}</div>` : '')
      + `</div>`;
  }

  /* ---------------- Toast（替代 alert） ---------------- */
  /* alert() 会**阻塞整个 JS 线程**：轮询停摆、动画冻结，而且在交易台上
   * 一个弹窗挡住盘面是危险的。toast 不阻塞、可堆叠、4 秒自动消失（错误常驻）。 */
  let toastHost = null;
  function _host() {
    if (toastHost && toastHost.isConnected) return toastHost;
    toastHost = document.createElement('div');
    toastHost.className = 'uitoast-host';
    toastHost.setAttribute('role', 'status');
    toastHost.setAttribute('aria-live', 'polite');
    document.body.appendChild(toastHost);
    return toastHost;
  }
  function toast(msg, kind, ttlMs) {
    const k = ['ok', 'warn', 'danger', 'info'].includes(kind) ? kind : 'info';
    const el = document.createElement('div');
    el.className = `uitoast ${k}`;
    el.innerHTML = `<span class="uitoast-msg">${esc(msg)}</span>`
      + '<button class="uitoast-x" type="button" aria-label="关闭">×</button>';
    const close = () => { if (el.parentNode) el.parentNode.removeChild(el); };
    el.querySelector('.uitoast-x').addEventListener('click', close);
    _host().appendChild(el);
    /* 错误不自动消失：它通常需要用户做点什么，一闪而过等于没提示。 */
    const ttl = ttlMs != null ? ttlMs : (k === 'danger' ? 0 : 4000);
    if (ttl > 0) setTimeout(close, ttl);
    return close;
  }

  /* ---------------- Modal（替代 confirm） ---------------- */
  /* 为什么不用原生 confirm：
   *  1. 它阻塞线程（同上）；
   *  2. 它只有一个字符串，**装不下**"这次会做什么/不会做什么"；
   *  3. 危险操作需要**输入确认短语**，原生 confirm 做不到。
   * 返回 Promise<boolean>（或 requirePhrase 时为输入的短语字符串 / null）。 */
  function modal(o) {
    o = o || {};
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'uimodal-back';
      const danger = !!o.danger;
      const phrase = o.requirePhrase || null;
      back.innerHTML = `<div class="uimodal ${danger ? 'danger' : ''}" role="dialog" aria-modal="true">`
        + `<div class="uimodal-head"><h3>${esc(o.title || '确认')}</h3>`
        + `<button class="uimodal-x" type="button" aria-label="关闭">×</button></div>`
        + `<div class="uimodal-body">${o.body || ''}</div>`
        + (phrase
          ? `<div class="uimodal-phrase"><label>输入 <span class="mono">${esc(phrase)}</span> 以确认</label>`
            + '<input class="liveinput" id="uimPhrase" autocomplete="off" spellcheck="false"></div>'
          : '')
        + `<div class="uimodal-foot">`
        + btn({ label: o.cancelText || '取消', kind: 'ghost', data: { uimCancel: '1' } })
        + btn({
          label: o.confirmText || '确认', kind: danger ? 'danger' : 'primary',
          data: { uimOk: '1' }, disabled: !!phrase,
        })
        + `</div></div>`;
      document.body.appendChild(back);

      const okBtn = back.querySelector('[data-uim-ok]');
      const phIn = back.querySelector('#uimPhrase');
      let done = false;
      const finish = (val) => {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        if (back.parentNode) back.parentNode.removeChild(back);
        resolve(val);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') { e.stopPropagation(); finish(phrase ? null : false); }
        if (e.key === 'Enter' && (okBtn && !okBtn.disabled)) { e.preventDefault(); doOk(); }
      };
      const doOk = () => finish(phrase ? (phIn ? phIn.value.trim() : '') : true);

      if (phIn) {
        phIn.addEventListener('input', () => {
          okBtn.disabled = phIn.value.trim() !== phrase;
        });
        setTimeout(() => phIn.focus(), 0);
      } else if (okBtn) {
        setTimeout(() => okBtn.focus(), 0);
      }
      okBtn && okBtn.addEventListener('click', doOk);
      back.querySelector('[data-uim-cancel]').addEventListener('click', () => finish(phrase ? null : false));
      back.querySelector('.uimodal-x').addEventListener('click', () => finish(phrase ? null : false));
      back.addEventListener('mousedown', (e) => { if (e.target === back) finish(phrase ? null : false); });
      document.addEventListener('keydown', onKey, true);
    });
  }

  /* ---------------- 分段控件 ---------------- */
  /* o: { items:[{v,label,on}], dataKey, cls } —— 生成 data-<dataKey>="v" */
  function seg(o) {
    o = o || {};
    const key = o.dataKey || 'seg';
    return `<div class="seg sm${o.cls ? ' ' + o.cls : ''}"${o.id ? ` id="${esc(o.id)}"` : ''}>`
      + (o.items || []).map((it) =>
        `<button class="segbtn${it.on ? ' on' : ''}" data-${esc(key)}="${esc(it.v)}">${esc(it.label)}</button>`).join('')
      + '</div>';
  }

  return {
    esc, btn, badge, card, stat, table, skeleton, empty, error,
    toast, modal, seg,
  };
})();
