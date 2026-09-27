/* =========================================================================
   参数中台 —— 交互逻辑
   默认参数 = 已验收最优配置（v3）。页面会实时告警本仓库已经证伪过的配置。
   ========================================================================= */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- 已验收最优（唯一事实来源在 webapp/spec.py） ---- */
const OPT = {
  cli: { bar: '1h', rebalance_days: 3, asset_class: 'crypto' },
  ov: {
    'factors.subset': ['range_pos', 'hitrate'],
    'portfolio.max_weight_per_instrument': 0.20,
    'execution.max_daily_turnover': 0.20,
  },
};

const S = {
  spec: null, presets: [], bundles: [], lib: {}, baseline: null,
  cli: {}, ov: {}, stages: ['base', 'charts'], bundleId: 'fast', preset: 'custom',
  warnings: [], runs: [], active: null, queued: [],
  selected: null, detail: null, logOffset: 0, logTimer: null, chartTimer: null,
  /* 回测曲线：视图（净值 / 对数净值 / 回撤）+ 每条曲线的显隐 */
  chartView: 'equity', chartOff: {}, chartOffInit: false,
  equity: {}, equityErr: {},
  /* 交易记录页 */
  tab: 'result', tr: null, trErr: null, trTag: null, trInFlight: false,
  fills: null, fillsFilt: {}, fillsPage: 0, blotter: null, blotterPage: 0,
  attrSort: 'net',
  /* 交易台 */
  live: {
    meta: null, status: null, statusErr: null, plan: null, planErr: null,
    job: null, jobTimer: null, mode: 'paper', sub: 'orders',
    orders: null, fills: null, runs: null, sel: {}, confirm: '', force: false,
    limits: null, limitsOpen: false, credsOpen: false, busy: false, note: '',
  },
};

/* ============================== 格式化 ============================== */
const fmt = {
  n: (v, d = 3) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d),
  n1: (v) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(1),
  pct: (v, d = 2) => (v == null || !isFinite(v)) ? '—' : (Number(v) * 100).toFixed(d) + '%',
  sci: (v) => (v == null || !isFinite(v)) ? '—' : Number(v).toExponential(2),
  pp: (v, d = 2) => (v == null || !isFinite(v)) ? '—' : (v >= 0 ? '+' : '') + (Number(v) * 100).toFixed(d) + 'pp',
  sgn: (v, d = 3) => (v == null || !isFinite(v)) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(d),
  usd: (v) => {
    if (v == null || !isFinite(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
    if (a >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
    if (a >= 1) return '$' + v.toFixed(0);
    return '$' + v.toFixed(2);
  },
  dol: (v) => (v == null || !isFinite(v)) ? '—' : '$' + Math.round(v).toLocaleString('en-US'),
  int: (v) => (v == null || !isFinite(v)) ? '—' : Math.round(v).toLocaleString('en-US'),
  val(v, k) {
    if (v == null) return '—';
    if (k === 'pct') return fmt.pct(v);
    if (k === 'n1') return fmt.n1(v);
    if (k === 'sci') return fmt.sci(v);
    return fmt.n(v);
  },
};

/* ============================== 启动 ============================== */
async function boot() {
  try {
    const r = await fetch('/api/spec');
    const d = await r.json();
    S.spec = d.spec; S.presets = d.presets; S.bundles = d.bundles;
    S.lib = d.lib_defaults || {}; S.baseline = d.baseline; S.optimalTag = d.optimal_tag;
  } catch (e) {
    $('#content').innerHTML = `<div class="banner danger"><div class="ico">!</div>
      <div><b>无法连接本地服务</b><p>${esc(e)}</p></div></div>`;
    return;
  }
  applyPreset('v3_optimal');
  // 分享/截图用：?view=dd|log|equity 直接打开某个曲线视图，?tag=<tag> 选中某次运行。
  const qs = new URLSearchParams(location.search);
  const vs = qs.get('view');
  if (vs === 'dd' || vs === 'log' || vs === 'equity') S.chartView = vs;
  // Deep link / screenshots: /#trades opens the trade-record tab directly.
  const wantTrades = location.hash === '#trades';
  const wantLive = location.hash === '#live';
  if (wantTrades) S.tab = 'trades';
  if (wantLive) S.tab = 'live';
  try {
    renderSide();
    renderContent();
    refreshWarnings();
    await pollRuns();
    setInterval(pollRuns, 2500);
    window.addEventListener('resize', debounce(() => drawChart(), 180));
    if (wantTrades) {
      ensureTrades();
    } else if (wantLive) {
      ensureLive();
    } else {
      // 打开页面就把最近一次成功的运行显示出来，而不是留一片空卡片
      const wantTag = qs.get('tag');
      const picked = wantTag ? S.runs.find((r) => r.tag === wantTag) : null;
      const last = picked || S.runs.find((r) => r.status === 'done' || r.status === 'partial');
      if (last) await selectRun(last.id);
    }
  } catch (e) {
    fatal('初始化失败：' + ((e && e.stack) || e));
  }
}

/* 从 spec 的 default 值构造完整参数集（default 即最优值） */
function defaults() {
  const cli = {}, ov = {};
  for (const it of (S.spec.global || [])) {
    if (it.key.startsWith('_cli.')) cli[it.key.slice(5)] = it.default;
  }
  for (const g of S.spec.groups) for (const it of g.items) ov[it.key] = clone(it.default);
  return { cli, ov };
}
const clone = (v) => (Array.isArray(v) ? v.slice() : (v && typeof v === 'object' ? JSON.parse(JSON.stringify(v)) : v));

function applyPreset(id) {
  const p = S.presets.find((x) => x.id === id);
  const d = defaults();
  S.cli = Object.assign({}, d.cli, p ? (p.cli || {}) : {});
  S.ov = Object.assign({}, d.ov, p ? (p.overrides || {}) : {});
  S.preset = id;
}

/* ============================== 侧边栏 ============================== */
function renderSide() {
  const el = $('#side');
  let h = '';

  /* --- 预设 --- */
  h += `<div class="card"><div class="head"><h3>预设</h3>
      <span class="hint">默认已选最优</span></div><div class="body">
      <div class="presets">`;
  for (const p of S.presets) {
    h += `<button class="preset ${S.preset === p.id ? 'active' : ''}" data-preset="${esc(p.id)}">
        <b>${esc(p.label)}${p.badge ? ` <span class="tag">${esc(p.badge)}</span>` : ''}</b>
        <small>${esc(p.desc)}</small></button>`;
  }
  h += `</div>
    <p class="desc" style="margin:10px 0 0">选中预设只会改参数，不会自动开跑 —— 先看告警，再点右上角运行。</p>
    </div></div>`;

  /* --- 运行范围 --- */
  h += `<div class="card"><div class="head"><h3>运行范围</h3></div><div class="body">
      <div class="presets" style="grid-template-columns:1fr">`;
  for (const b of S.bundles) {
    h += `<button class="preset ${S.bundleId === b.id ? 'active' : ''}" data-bundle="${esc(b.id)}">
        <b>${esc(b.label)} <span class="tag">${esc(b.eta)}</span></b>
        <small>${esc(b.desc)}<br><span class="mono">${esc(b.stages.join(' '))}</span></small></button>`;
  }
  h += `</div></div></div>`;

  /* --- 全局 --- */
  h += `<div class="card"><div class="head"><h3>回测范围</h3></div><div class="body">`;
  h += fieldRows((S.spec.global || []).filter((i) => i.level === 'core'), 'cli');
  h += `</div><details class="adv"><summary>高级</summary><div class="body">`;
  h += fieldRows((S.spec.global || []).filter((i) => i.level === 'adv'), 'cli');
  h += `</div></details></div>`;

  /* --- 参数分组：因子优先 --- */
  for (const g of S.spec.groups) {
    const core = g.items.filter((i) => i.level === 'core');
    const adv = g.items.filter((i) => i.level === 'adv');
    const isFac = g.id === 'factors';
    h += `<div class="card"><div class="head"><h3>${esc(g.label)}</h3>
        ${isFac ? '<span class="pill accent" style="height:20px;font-size:11px">收益最大</span>' : ''}
        </div><div class="body">`;
    h += `<p class="desc">${esc(g.desc || '')}</p>`;
    if (isFac) h += factorPicker(g);
    h += fieldRows(core.filter((i) => i.type !== 'multi'), 'ov');
    h += `</div>`;
    if (adv.length) {
      h += `<details class="adv"><summary>高级参数（${adv.length}）</summary><div class="body">`
        + fieldRows(adv, 'ov') + `</div></details>`;
    }
    h += `</div>`;
  }

  el.innerHTML = h;
  bindSide();
  updateOptPill();
}

function factorPicker(g) {
  const item = g.items.find((i) => i.key === 'factors.subset');
  const sel = S.ov['factors.subset'] || [];
  let h = `<div class="facgrid">`;
  for (const f of item.options) {
    const on = sel.includes(f);
    const banned = f === 'rev_short';
    h += `<div class="fac ${on ? 'on' : ''} ${banned ? 'warnf' : ''}" data-factor="${esc(f)}">
        <span class="dot"></span><span>${esc(f)}</span></div>`;
  }
  h += `</div><div class="field wide"><div class="help">${esc(item.help || '')}
      ${sel.length ? `<br>当前子集：<b>${esc(sel.join(' + '))}</b>（共 ${sel.length} 个）` : '<br><b style="color:var(--danger)">一个字都没选</b>'}
      </div></div>`;
  return h;
}

function fieldRows(items, scope) {
  const store = scope === 'cli' ? S.cli : S.ov;
  let h = '';
  for (const it of items) {
    const key = scope === 'cli' ? it.key.slice(5) : it.key;
    const v = store[key];
    const changed = !eq(v, it.default);
    const trap = S.warnings.some((w) => w.key === it.key);
    h += `<div class="field ${changed ? 'changed' : ''} ${trap ? 'trap' : ''}">
        <label for="f_${esc(it.key)}">${esc(it.label)}</label>`;
    const id = `f_${esc(it.key)}`;
    if (it.type === 'select') {
      h += `<select id="${id}" data-scope="${scope}" data-key="${esc(key)}">`
        + it.options.map((o) => `<option value="${esc(o)}" ${String(v) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('')
        + `</select>`;
    } else if (it.type === 'bool') {
      h += `<select id="${id}" data-scope="${scope}" data-key="${esc(key)}">
          <option value="false" ${v === false ? 'selected' : ''}>关闭</option>
          <option value="true" ${v === true ? 'selected' : ''}>启用</option></select>`;
    } else {
      const step = it.step != null ? `step="${it.step}"` : 'step="any"';
      const mn = it.min != null ? `min="${it.min}"` : '';
      const mx = it.max != null ? `max="${it.max}"` : '';
      const shown = it.format === 'pct' && typeof v === 'number' ? (v * 100).toPrecision(6) : v;
      h += `<input id="${id}" type="${it.type === 'text' ? 'text' : 'number'}" ${step} ${mn} ${mx}
          data-scope="${scope}" data-key="${esc(key)}" data-fmt="${esc(it.format || '')}"
          value="${esc(shown)}">`;
    }
    h += `</div>`;
    if (it.help && !it.key.endsWith('subset')) {
      h += `<div class="field wide" style="padding-top:0"><div class="help">${esc(it.help)}</div></div>`;
    }
  }
  return h;
}

function bindSide() {
  $$('[data-preset]').forEach((b) => b.onclick = () => {
    applyPreset(b.dataset.preset); renderSide(); renderContent(); refreshWarnings();
  });
  $$('[data-bundle]').forEach((b) => b.onclick = () => {
    const bundle = S.bundles.find((x) => x.id === b.dataset.bundle);
    S.bundleId = bundle.id; S.stages = bundle.stages.slice();
    renderSide(); renderContent();
  });
  $$('[data-factor]').forEach((d) => d.onclick = () => {
    const f = d.dataset.factor;
    const cur = S.ov['factors.subset'] || [];
    S.ov['factors.subset'] = cur.includes(f) ? cur.filter((x) => x !== f) : cur.concat([f]);
    S.preset = 'custom';
    renderSide(); renderContent(); refreshWarnings();
  });
  $$('[data-key]').forEach((inp) => {
    const ev = inp.tagName === 'SELECT' ? 'change' : 'input';
    inp.addEventListener(ev, () => {
      const scope = inp.dataset.scope, key = inp.dataset.key;
      let v = inp.value;
      if (inp.dataset.fmt === 'pct' && inp.type === 'number') v = Number(v) / 100;
      else if (inp.type === 'number') v = Number(v);
      else if (inp.tagName === 'SELECT' && (v === 'true' || v === 'false')) v = (v === 'true');
      else if (scope === 'cli' && (key === 'rebalance_days' || key === 'capital')) v = Number(v);
      (scope === 'cli' ? S.cli : S.ov)[key] = v;
      S.preset = 'custom';
      markChanged();
      updateOptPill();
      refreshWarnings();
    });
  });
}

function markChanged() {
  $$('.field[data-k]').forEach(() => { });
  const d = defaults();
  $$('#side [data-key]').forEach((inp) => {
    const scope = inp.dataset.scope, key = inp.dataset.key;
    const dv = scope === 'cli' ? d.cli[key] : d.ov[key];
    const cur = scope === 'cli' ? S.cli[key] : S.ov[key];
    inp.closest('.field').classList.toggle('changed', !eq(cur, dv));
  });
}

/* ============================== 告警 ============================== */
let warnTimer = null;
function refreshWarnings() {
  clearTimeout(warnTimer);
  warnTimer = setTimeout(async () => {
    try {
      const r = await fetch('/api/check', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overrides: S.ov, cli: S.cli }),
      });
      S.warnings = (await r.json()).warnings || [];
    } catch { S.warnings = []; }
    renderContent();
    // 给命中的参数打红框
    $$('#side [data-key]').forEach((inp) => {
      const hit = S.warnings.some((w) => w.key.indexOf(inp.dataset.key) >= 0
        || w.key === (inp.dataset.scope === 'cli' ? '_cli.' + inp.dataset.key : inp.dataset.key));
      inp.closest('.field').classList.toggle('trap', hit);
    });
  }, 220);
}

function diffFromOptimal() {
  const out = [];
  for (const k in OPT.ov) if (!eq(S.ov[k], OPT.ov[k])) out.push({ k, want: OPT.ov[k], got: S.ov[k] });
  for (const k in OPT.cli) if (String(S.cli[k]) !== String(OPT.cli[k]))
    out.push({ k: '--' + k.replace('_', '-'), want: OPT.cli[k], got: S.cli[k] });
  return out;
}

function updateOptPill() {
  const el = $('#optPill');
  if (!el) return;
  const n = diffFromOptimal().length;
  if (!n) {
    el.className = 'pill ok';
    el.textContent = '已验收最优 v3 ✓';
    el.title = 'Sharpe 1.937 / CAGR 25.09% / MDD −12.72%';
  } else {
    el.className = 'pill warn';
    el.textContent = `偏离最优 ${n} 处`;
    el.title = '改动会体现在结果卡的 Δ 列';
  }
}

/* 页面里的 JS 错误必须显形 —— 上面的占位符如果一直不消失，多半是脚本炸了 */
function fatal(msg) {
  const c = $('#content');
  if (!c) return;
  c.insertAdjacentHTML('afterbegin',
    banner('danger', '!', '页面脚本出错', pre_escape(msg)));
  const p = $('#optPill');
  if (p) { p.className = 'pill danger'; p.textContent = '脚本出错'; }
}
const pre_escape = (s) => `<span class="mono">${esc(s)}</span>`;
window.addEventListener('error', (e) => fatal((e.message || '') + ' @ ' + (e.filename || '') + ':' + (e.lineno || 0)));
window.addEventListener('unhandledrejection', (e) => fatal('unhandled promise: ' + (e.reason && e.reason.message || e.reason)));

/* ============================== 主面板 ============================== */
function renderContent() {
  const el = $('#content');
  let h = `<div class="banners">`;

  const diffs = diffFromOptimal();
  if (!diffs.length) {
    h += banner('ok', '✓', '当前参数 = 已验收最优配置（v3）',
      `Sharpe <b>1.937</b> / CAGR <b>25.09%</b> / 最大回撤 <b>−12.72%</b>，
       验收 16/16 通过。因子子集 = range_pos + hitrate；换手预算 20%；单名上限 20%；
       1h 频率、3 天调仓、crypto 池。直接点右上角即可复现。`);
  } else {
    h += banner('info', 'i', `当前参数与最优配置有 ${diffs.length} 处不同`,
      `只改这些的话，跑完可以直接看 Δ 列。<br>` +
      diffs.map((d) => `<span class="chip">${esc(d.k)} <b>${esc(String(d.want))}</b> → ${esc(String(d.got))}</span>`).join(' ')
      + `<div style="margin-top:8px"><button class="btn sm ghost" id="backOpt">↺ 回到最优配置</button></div>`);
  }

  for (const w of S.warnings) {
    h += banner(w.sev === 'high' ? 'danger' : 'warn', '⚠', w.title, w.body);
  }

  const d = S.detail;
  if (d && (d.stage_errors || []).length) {
    h += banner('danger', '!', `有 ${d.stage_errors.length} 个 stage 失败，退出码却是 ${d.exit_code}`,
      `研究编排器把每个 stage 包在 try/except 里、失败后继续跑，所以<b>退出码 0 不代表全部成功</b>。`
      + `缺的表会静默消失。<br><span class="mono">${esc(d.stage_errors.slice(0, 3).join(' | '))}</span>`);
  }
  h += `</div>`;

  h += tabBar();
  h += `<div class="resultOnly">`;

  /* --- KPI --- */
  const r = d && d.result;
  const b = S.baseline;
  const KPI = [
    ['Sharpe', r && r.sharpe, b && b.sharpe, 'n', d && d.delta && d.delta.sharpe],
    ['CAGR', r && r.cagr, b && b.cagr, 'pct', d && d.delta && d.delta.cagr],
    ['最大回撤', r && r.mdd, b && b.mdd, 'pct', d && d.delta && d.delta.mdd],
  ];
  const rest = r ? r.metrics.filter((m) => ['年化波动', '回撤持续（天）', 'Calmar', '年换手（倍）', '成本拖累（年）', '资金费盈亏（总）', '平均总敞口', '日胜率'].includes(m.label)) : [];
  h += `<h2 class="sec">核心指标 <small>${r ? '本配置' : '尚未运行'} ${r ? '· 括号内为与 v3 基线之差' : ''}</small></h2>`;
  h += `<div class="kpis">`;
  for (const [k, v, bv, f, dl] of KPI) {
    h += kpiCard(k, fmt.val(v, f), dl, f === 'pct' ? 'pp' : f);
  }
  for (const m of rest) h += kpiCard(m.label, fmt.val(m.value, m.fmt), null, '');
  h += `</div>`;

  /* --- 回测曲线 + 年度 --- */
  h += `<div class="grid2" style="margin-top:14px">
    <div class="panel"><div class="head"><h3>回测曲线</h3>
      <div class="seg" id="chartSeg">
        <button class="segbtn ${S.chartView === 'equity' ? 'on' : ''}" data-cv="equity">净值</button>
        <button class="segbtn ${S.chartView === 'log' ? 'on' : ''}" data-cv="log">对数</button>
        <button class="segbtn ${S.chartView === 'dd' ? 'on' : ''}" data-cv="dd">回撤</button>
      </div>
      <span class="spacer"></span>
      <span class="muted" style="font-size:11.5px" id="chartNote">${r ? '' : '等待运行'}</span></div>
      <div class="body" style="padding:10px 12px 8px">
        <canvas class="chart" id="chart"></canvas>
        <div class="legend" id="chartLegend"></div>
      </div></div>
    <div class="panel"><div class="head"><h3>逐年 Sharpe</h3><span class="spacer"></span>
      <span class="muted" style="font-size:11.5px">与 v3 基线对照</span></div>
      <div class="body" style="padding:4px 10px 10px">${yearTable(d)}</div></div></div>`;
  h += `</div>`;                       /* /resultOnly */

  h += `<div class="tradesOnly">${tradesHtml()}</div>`;

  h += `<div class="liveOnly" id="liveRoot">${liveSkeleton()}</div>`;

  /* --- 运行记录 --- */
  h += `<h2 class="sec">运行记录 <small>${S.runs.length} 条 · 串行执行，不会抢核</small></h2>`;
  h += `<div class="runs">`;
  if (!S.runs.length) h += `<div class="empty">还没有运行过。点右上角「运行回测」，快速档约 50 秒。</div>`;
  for (const run of S.runs) {
    const su = run.summary || {};
    h += `<div class="runrow ${S.selected === run.id ? 'sel' : ''}" data-run="${esc(run.id)}">
      <div class="lbl"><b>${esc(run.label || run.id)}</b>
        <small>${esc(run.tag)} · ${new Date(run.created * 1000).toLocaleString('zh-CN')}${run.elapsed ? ' · ' + run.elapsed + 's' : ''}${run.n_stage_errors ? ` · <span style="color:var(--danger)">${run.n_stage_errors} 个 stage 报错</span>` : ''}</small></div>
      <div class="m">${su.sharpe != null ? 'Sharpe ' + fmt.n(su.sharpe) : ''}</div>
      <div class="m">${su.cagr != null ? 'CAGR ' + fmt.pct(su.cagr) : ''}</div>
      <div class="state ${esc(run.status)}"><span class="dot2"></span>${esc(statusText(run.status))}</div>
    </div>`;
  }
  h += `</div>`;

  /* --- 日志 --- */
  if (d) {
    h += `<div class="panel" style="margin-top:14px"><div class="head">
        <h3>运行日志</h3><span class="spacer"></span>
        <span class="muted mono">${esc(d.tag)}</span></div>
      <pre class="log" id="log"></pre></div>`;
  }

  /* --- 命令 --- */
  if (d && d.argv) {
    h += `<div class="panel" style="margin-top:14px"><div class="head"><h3>等价命令行</h3>
      <span class="spacer"></span><button class="btn sm ghost" id="copyCmd">复制</button></div>
      <div class="body"><div class="chips"><span class="chip" style="white-space:pre-wrap;word-break:break-all">${esc(cmdText(d))}</span></div></div></div>`;
  }

  // `pollRuns()` 每 2.5s 走到这里重建整块 #content。包进 preserveUi()，否则用户
  // 正在敲的任何输入框都会被丢回模板给的旧值——API Key 那种没有 value 回填的框
  // 就是直接变空。浏览器控制台一个报错都没有。
  preserveUi(() => {
  el.innerHTML = h;
  // `#content` carries `.content` for its own scroll/padding; never overwrite
  // `className` outright or the layout silently collapses.
  el.classList.toggle('trades', S.tab === 'trades');
  el.classList.toggle('live', S.tab === 'live');

  const bo = $('#backOpt');
  if (bo) bo.onclick = () => { applyPreset('v3_optimal'); renderSide(); renderContent(); refreshWarnings(); };
  $$('[data-run]').forEach((row) => row.onclick = () => selectRun(row.dataset.run));
  $$('[data-tab]').forEach((t) => t.onclick = () => {
    if (S.tab === t.dataset.tab) return;
    S.tab = t.dataset.tab;
    const hash = S.tab === 'trades' ? '#trades' : (S.tab === 'live' ? '#live' : '#result');
    try { history.replaceState(null, '', hash); } catch { }
    renderContent();
    if (S.tab === 'trades') ensureTrades();
    if (S.tab === 'live') ensureLive();
  });

  const note = $('#chartNote');
  if (note && r) note.textContent = `蓝 = 本配置，灰 = v3 基线`;
  if (S.tab === 'result') drawChart();
  if (d) { const lg = $('#log'); if (lg) lg.innerHTML = logHtml(d._log || ''); startLog(d); }
  if (S.tab === 'trades') {
    // `el.innerHTML = h` 刚刚把 #trBody 重建成了「正在读取…」占位，所以这里必须
    // 让它恢复内容。以前这里只调 bindTrBody()，于是 pollRuns() 每 2.5s 调一次
    // renderContent() 就把已经加载好的表格打回占位 —— 页面永远停在「正在加载」。
    if (S.tr || S.trErr) paintTr();
    else if (!S.trInFlight) ensureTrades();
  }
  if (S.tab === 'live') {
    // 同样的坑：这里刚把 #liveRoot 重建过，必须把已加载的状态重新填回去，
    // 否则 pollRuns()（2.5s）会把交易台打回骨架。
    if (S.live.meta) livePaintAll(); else ensureLive();
  }
  // 结果页才有的视图切换按钮（交易记录页下 #chartSeg 不存在，函数自己会跳过）
  bindChartSeg();
  });                                   /* /preserveUi */
}

function cmdText(d) {
  const q = (s) => (/[\s"]/.test(String(s)) ? `'${s}'` : String(s));
  return 'python ' + (d.argv || []).map(q).join(' ');
}

function kpiCard(k, v, delta, unit) {
  let dl = '';
  if (delta != null && isFinite(delta)) {
    const cls = Math.abs(delta) < 1e-9 ? 'flat' : (delta > 0 ? 'up' : 'down');
    const t = unit === 'pp' ? fmt.pp(delta) : fmt.sgn(delta);
    dl = `<div class="d ${cls}">${t} <span class="muted">vs v3</span></div>`;
  }
  return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>${dl}</div>`;
}

function yearTable(d) {
  const r = d && d.result;
  const by = (S.baseline && S.baseline.yearly && S.baseline.yearly.years) || [];
  if (!by.length) return `<div class="empty">缺 v3 基线年度数据</div>`;
  const cur = {};
  for (const y of ((r && r.yearly && r.yearly.years) || [])) cur[y.year] = y;
  const full = r && r.yearly && r.yearly.full;
  let h = `<table><thead><tr><th>年份</th>
      <th class="num">v3 基线</th><th class="num">本配置 (Δ)</th>
      <th class="num">年化收益</th></tr></thead><tbody>`;
  for (const y of by) {
    const c = cur[y.year];
    const cs = c ? c.sharpe : null;
    const dl = (cs != null && y.sharpe != null) ? cs - y.sharpe : null;
    const cls = dl == null ? 'muted' : (dl > 0 ? 'up' : (dl < 0 ? 'down' : 'muted'));
    const isLocked = y.year >= 2026;
    const cagr = (c && c.cagr != null) ? c.cagr : y.cagr;
    h += `<tr class="${isLocked ? 'mark' : ''}">
      <td>${y.year}${isLocked ? ' <span class="muted">锁定</span>' : ''}</td>
      <td class="num">${fmt.n(y.sharpe)}</td>
      <td class="num">${cs == null ? '<span class="muted">—</span>'
      : `${fmt.n(cs)} <span class="${cls}" style="font-size:11px">(${dl == null ? '' : fmt.sgn(dl)})</span>`}</td>
      <td class="num">${cagr != null ? fmt.pct(cagr) : '—'}</td></tr>`;
  }
  h += `</tbody></table>`;
  if (full && r && r.sharpe != null) {
    const ok = Math.abs(full.sharpe - r.sharpe) < 5e-3;
    h += `<p class="desc" style="margin:9px 0 0">口径自检：由日频收益重算的全样本 Sharpe =
      <b>${fmt.n(full.sharpe)}</b>，指标体系给出的 = <b>${fmt.n(r.sharpe)}</b>
      ${ok ? '（一致 ✓）' : '（<span style="color:var(--danger)">不一致，需查</span>）'}。</p>`;
  }
  h += `<p class="desc" style="margin:7px 0 0">2026 是结构性离群段（CAGR +6.2σ、波动 +5.8σ，
    平均总敞口反而更低、日胜率不变），对外应拿 2021–2025 的分年 Sharpe 校准预期，
    而不是全样本的 <b>1.937</b>。年度 Sharpe 按本项目口径：先日频求和，再 ×√365。</p>`;
  return h;
}

const statusText = (s) => ({
  queued: '排队中', running: '运行中', done: '完成', partial: '部分失败',
  failed: '失败', cancelled: '已取消', interrupted: '被中断',
}[s] || s);

function banner(cls, ico, title, body) {
  return `<div class="banner ${cls}"><div class="ico">${ico}</div>
    <div><b>${esc(title)}</b><p>${body}</p></div></div>`;
}

/* ============================== 交易记录 ============================== */
function tabBar() {
  const n = S.tr && S.tr.summary ? S.tr.summary.n_fills : null;
  const st = S.live.status;
  const armed = st && (st.kill_switch ? ' · 熔断' : (st.connected ? ' · 已连接' : ''));
  return `<div class="tabs">
    <button class="tab ${S.tab === 'result' ? 'on' : ''}" data-tab="result">回测结果</button>
    <button class="tab ${S.tab === 'trades' ? 'on' : ''}" data-tab="trades">交易记录
      ${n != null ? `<span class="badge">${fmt.int(n)} 笔</span>` : ''}</button>
    <button class="tab ${S.tab === 'live' ? 'on' : ''}" data-tab="live">交易台
      ${st ? `<span class="badge">${esc(LIVE_LABEL[S.live.mode] || S.live.mode)}${esc(armed)}</span>` : ''}</button>
    <span class="spacer"></span>
    <span class="muted" style="font-size:11.5px">${S.tab === 'live'
    ? '信号与回测同源；下单前必过风控闸门'
    : '全部数据来自已保存的 baseline.pkl'}</span>
  </div>`;
}

function tradeTagChoices() {
  const seen = new Set();
  const out = [];
  const push = (t, label) => { if (t && !seen.has(t)) { seen.add(t); out.push({ t, label }); } };
  push(S.optimalTag, S.optimalTag + '（已验收最优）');
  for (const r of S.runs) {
    if (r.status === 'done' && !(r.stage_errors || []).length) push(r.tag, r.label || r.tag);
  }
  for (const t of (S.tags || [])) push(t, t);
  return out;
}

function tradesHtml() {
  return `<h2 class="sec">交易记录 <small>成交 = 相邻两次调仓的持仓之差</small></h2>
    <div class="panel"><div class="body" id="trBody" style="padding:14px">${trLoading()}</div></div>`;
}

const trLoading = () => `<div class="empty">正在读取回测结果与交易记录…</div>`;

function trErrHtml(msg) {
  return `<div class="banner danger"><div class="ico">!</div><div>
    <b>交易记录读取失败</b><p>${esc(msg)}</p></div></div>`;
}

function paintTr() {
  const el = $('#trBody');
  if (!el) return;
  el.innerHTML = trBodyHtml();
  bindTrBody();
}

function trBodyHtml() {
  if (S.trErr) return trErrHtml(S.trErr);
  if (!S.tr) return trLoading();
  const s = S.tr.summary || {};
  if (!s.n_fills) {
    return `<div class="banner warn"><div class="ico">!</div><div>
      <b>该 tag 没有交易记录</b><p>只有跑过 <span class="mono">base</span> 阶段并写出
      <span class="mono">baseline.pkl</span> 的运行才有成交明细。</p></div></div>
      ${tagPicker()}`;
  }
  const choices = tradeTagChoices();
  let h = '';
  h += `<div class="filt" style="margin-bottom:12px">
    <label>运行</label>
    <select id="trTag">${choices.map((c) => `<option value="${esc(c.t)}" ${c.t === S.trTag ? 'selected' : ''}>${esc(c.label)} · ${esc(c.t)}</option>`).join('')}</select>
    <span class="spacer"></span>
    <button class="btn sm ghost" data-exp="fills">⭳ 成交明细 CSV</button>
    <button class="btn sm ghost" data-exp="attribution">⭳ 逐名归因 CSV</button>
    <button class="btn sm ghost" data-exp="blotter">⭳ 调仓流水 CSV</button>
  </div>`;

  h += `<div class="banner info"><div class="ico">i</div><div>
    <b>这些数字是怎么来的</b>
    <p>成交明细 = <span class="mono">weight_matrix</span> 相邻两期之差（引擎里就是实际执行的
    <span class="mono">delta</span>）；逐名归因 = <span class="mono">name_gross / name_cost / name_fund</span>
    按合约求和。全部读自已保存的结果，<b>不重跑回测</b>。
    权重是「占净值比例」，名义金额按 <span class="mono">|Δw| × 初始本金 × 当时净值</span> 折算。</p></div></div>`;

  /* --- KPI --- */
  const K = [
    ['调仓次数', fmt.int(s.n_rebalances), `${fmt.n(s.fills_per_rebalance, 1)} 笔/次`],
    ['成交笔数', fmt.int(s.n_fills), `${fmt.n((s.dust || {}).share_of_fills * 100, 1)}% 是碎单`],
    ['涉及合约', fmt.int(s.n_instruments), `池内共 134 个`],
    ['成交名义额', fmt.usd(s.total_notional), `均值 ${fmt.dol(s.avg_fill_notional)}/笔`],
    ['交易成本', fmt.dol(s.total_cost), `${fmt.n(s.cost_bps_of_notional, 2)} bps 成交额`],
    ['样本跨度', fmt.int(s.days) + ' 天', `${s.n_rebalances} 次调仓`],
  ];
  h += `<div class="kpis">`;
  for (const [k, v, sub] of K) h += `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d flat">${esc(sub)}</div></div>`;
  h += `</div>`;

  /* --- 对账 --- */
  const rc = s.reconcile || {};
  const same = Math.abs((rc.turnover_from_book_delta || 0) - (rc.turnover_scheduled || 0)) < 1e-6
    && Math.abs((rc.turnover_scheduled || 0) - (rc.turnover_bars_total || 0)) < 1e-6;
  h += `<h2 class="sec">口径对账 <small>三套换手口径必须完全一致，否则重建就是错的</small></h2>
    <div class="panel"><div class="body" style="padding:6px 14px 12px">
    <table><thead><tr><th>口径</th><th class="num">累计换手（净值倍数）</th><th>含义</th></tr></thead><tbody>
      <tr><td>账本 Δ 重建</td><td class="num">${fmt.n(rc.turnover_from_book_delta, 6)}</td><td class="tk">相邻两次调仓持仓之差求和 —— 本页成交明细的来源</td></tr>
      <tr><td>引擎主动调仓</td><td class="num">${fmt.n(rc.turnover_scheduled, 6)}</td><td class="tk">name_turnover，只含主动执行的 delta</td></tr>
      <tr><td>bars 逐 bar</td><td class="num">${fmt.n(rc.turnover_bars_total, 6)}</td><td class="tk">逐根 K 线累加，含被动平仓</td></tr>
      <tr><td>被动平仓（数据断档）</td><td class="num">${fmt.n(rc.passive_close_notional, 6)}</td><td class="tk">${fmt.int(rc.n_stale_marks)} 次强平标记；这部分不在成交明细里</td></tr>
    </tbody></table>
    <p class="desc" style="margin:10px 0 0">${same
      ? `三套口径<b style="color:var(--ok)">逐位一致</b> → 成交明细与账本是同一份事实。`
      : `<b style="color:var(--danger)">三套口径不一致</b>，页面上的成交明细不可信，需先查账本。`}</p>
    </div></div>`;

  /* --- 碎单披露 --- */
  const du = s.dust || {};
  if (du.n) {
    h += `<div class="banner warn" style="margin-top:12px"><div class="ico">⚠</div><div>
      <b>${fmt.int(du.n)} 笔成交（${fmt.n(du.share_of_fills * 100, 1)}%）金额低于 ${fmt.dol(du.threshold_usd)}</b>
      <p>合计 <b>${fmt.dol(du.notional)}</b>，只占成交总额的
      <b>${fmt.n(du.share_of_notional * 100, 4)}%</b> —— 这是 cap 重归一化与换手预算留下的数值残差，
      <b>不是真实下单</b>。表格默认过滤掉它们，但 CSV 导出<b>一条不漏</b>（含 <span class="mono">dust</span> 列），
      上面的口径对账也用的是全量数据。勾选「含碎单」可原样查看。</p></div></div>`;
  }

  /* --- 逐名归因 --- */
  h += `<h2 class="sec">逐名归因 <small>每个合约在整个样本里贡献了多少，以及为它付了多少成本</small></h2>
    <div class="panel"><div class="head">
      <h3>合约净贡献</h3><span class="spacer"></span>
      <label class="inline">排序</label>
      <select id="attrSort">
        ${[['net', '净贡献 ↓'], ['net_worst', '净贡献 ↑（最亏）'], ['gross', '毛贡献 ↓'],
    ['cost', '成本 ↓'], ['trades', '成交笔数 ↓'], ['name', '合约名']]
      .map(([v, t]) => `<option value="${v}" ${S.attrSort === v ? 'selected' : ''}>${t}</option>`).join('')}
      </select></div>
      <div class="body" style="padding:4px 14px 14px">${attrTableHtml()}</div></div>`;

  /* --- 调仓流水 --- */
  h += `<h2 class="sec">调仓流水 <small>每一次调仓选了什么、换手多少、这一段赚了多少</small></h2>
    <div class="panel"><div class="head">
      <h3>调仓决策</h3><span class="spacer"></span>
      <label class="inline">年份</label>
      <select id="blYear">${['全部'].concat((S.tr.options || {}).years || [])
      .map((y) => `<option value="${y === '全部' ? '' : y}" ${String(S.blYear || '') === String(y === '全部' ? '' : y) ? 'selected' : ''}>${y}</option>`).join('')}</select>
      </div>
      <div class="body" style="padding:4px 14px 14px">${blotterTableHtml()}</div></div>`;

  /* --- 成交明细 --- */
  const o = S.tr.options || {};
  h += `<h2 class="sec">成交明细 <small>每一笔实际下单 —— 时间、合约、方向、从多少到多少、金额、成本</small></h2>
    <div class="panel"><div class="head" style="flex-wrap:wrap;gap:6px">
      <h3>Fills</h3><span class="spacer"></span>
      <div class="filt">
        <label>年份</label><select id="flYear">${['全部'].concat(o.years || [])
      .map((y) => `<option value="${y === '全部' ? '' : y}" ${String(S.fillsFilt.year || '') === String(y === '全部' ? '' : y) ? 'selected' : ''}>${y}</option>`).join('')}</select>
        <label>动作</label><select id="flAction">${['全部'].concat(o.actions || [])
      .map((a) => `<option value="${a === '全部' ? '' : a}" ${String(S.fillsFilt.action || '') === String(a === '全部' ? '' : a) ? 'selected' : ''}>${a}</option>`).join('')}</select>
        <label>方向</label><select id="flSide">${['全部'].concat(o.sides || [])
      .map((a) => `<option value="${a === '全部' ? '' : a}" ${String(S.fillsFilt.side || '') === String(a === '全部' ? '' : a) ? 'selected' : ''}>${a}</option>`).join('')}</select>
        <input id="flQ" placeholder="合约名，如 ETH" value="${esc(S.fillsFilt.q || '')}" style="width:118px">
        <label class="inline"><input type="checkbox" id="flDust" ${S.fillsFilt.include_dust ? 'checked' : ''}> 含碎单</label>
        <button class="btn sm ghost" id="flApply">筛选</button>
        <button class="btn sm ghost" id="flReset">重置</button>
      </div></div>
      <div class="body" style="padding:4px 14px 14px">${fillsTableHtml()}</div></div>`;
  return h;
}

function tagPicker() {
  const choices = tradeTagChoices();
  return `<div class="filt" style="margin-top:12px"><label>运行</label>
    <select id="trTag">${choices.map((c) => `<option value="${esc(c.t)}" ${c.t === S.trTag ? 'selected' : ''}>${esc(c.label)} · ${esc(c.t)}</option>`).join('')}</select>
    <span class="spacer"></span><span class="muted" style="font-size:11.5px">选一个跑过 base 阶段的运行</span></div>`;
}

function attrTableHtml() {
  const rows = (S.tr.attribution || []).slice();
  if (!rows.length) return `<div class="empty">无数据</div>`;
  const key = { net: (r) => -r.net, net_worst: (r) => r.net, gross: (r) => -r.gross, cost: (r) => -r.cost, trades: (r) => -r.n_trades, name: (r) => r.inst }[S.attrSort] || ((r) => -r.net);
  rows.sort(key);
  const hasF = rows.some((r) => r.funding != null);
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.net))) || 1;
  let h = `<div class="tblwrap"><table><thead><tr>
    <th>合约</th><th class="num">净贡献</th><th class="num">毛贡献</th><th class="num">成本</th>
    ${hasF ? '<th class="num">资金费</th>' : ''}<th class="num">成交笔数</th>
    <th class="num">持仓期数</th><th class="num">平均权重</th><th class="num">多空偏</th>
    <th class="num">期胜率</th><th></th></tr></thead><tbody>`;
  for (const r of rows) {
    const cls = r.net > 0 ? 'up' : (r.net < 0 ? 'down' : '');
    const barW = Math.max(1, Math.round(Math.abs(r.net) / maxAbs * 52));
    const bar = r.net >= 0 ? `left:50%;width:${barW}px` : `right:50%;width:${barW}px`;
    h += `<tr><td class="mono">${esc(r.inst.replace('-USDT-SWAP', ''))}</td>
      <td class="num ${cls}"><b>${fmt.n(r.net * 100, 3)}%</b></td>
      <td class="num muted">${fmt.n(r.gross * 100, 3)}%</td>
      <td class="num">${fmt.n(r.cost * 100, 3)}%</td>
      ${hasF ? `<td class="num ${(r.funding || 0) > 0 ? 'up' : 'down'}">${fmt.n(r.funding * 100, 3)}%</td>` : ''}
      <td class="num">${fmt.int(r.n_trades)}</td>
      <td class="num">${fmt.int(r.n_periods_held)}</td>
      <td class="num">${fmt.n(r.avg_abs_w, 4)}</td>
      <td class="num">${r.long_share == null ? '—' : fmt.n(r.long_share * 100, 0) + '%多'}</td>
      <td class="num">${r.win_rate == null ? '—' : fmt.n(r.win_rate * 100, 0) + '%'}</td>
      <td style="width:120px;position:relative;padding:0"><div class="cbar"><i style="${bar}"></i></div></td></tr>`;
  }
  h += `</tbody></table></div>`;
  if (!hasF) {
    h += `<p class="desc" style="margin:9px 0 0"><b>资金费列缺失</b>：这一份
      <span class="mono">baseline.pkl</span> 写于 <span class="mono">name_fund</span> 落盘之前，
      逐名资金费归因拿不到（新跑的运行会有）。</p>`;
  }
  h += `<p class="desc" style="margin:9px 0 0">单位是<b>占净值比例</b>（累计，非年化）。
    净贡献 = 毛贡献 − 成本${hasF ? ' + 资金费现金流' : ''}；全表求和等于账本里的
    <span class="mono">gross_ret / fee+spread+impact${hasF ? ' / funding' : ''}</span> 之和。</p>`;
  return h;
}

function blotterTableHtml() {
  const d = S.blotter;
  if (!d) return `<div class="empty">载入中…</div>`;
  if (!d.rows.length) return `<div class="empty">无数据</div>`;
  let h = `<div class="tblwrap"><table><thead><tr>
    <th>信号时间</th><th>执行时间</th><th class="num">池宽</th>
    <th class="num">多</th><th class="num">空</th><th class="num">敞口</th>
    <th class="num">换手</th><th class="num">成交笔</th><th class="num">成本</th>
    <th class="num">本段毛收益</th><th class="num">本段净收益</th><th class="num">本段资金费</th>
    <th class="num">缩放</th><th class="num">替换</th></tr></thead><tbody>`;
  for (const r of d.rows) {
    h += `<tr><td class="mono">${esc(r.ts)}</td><td class="mono muted">${esc(r.exec_ts)}</td>
      <td class="num">${r.n_universe}</td>
      <td class="num">${r.n_long}</td><td class="num">${r.n_short}</td>
      <td class="num">${fmt.n(r.exposure, 3)}</td>
      <td class="num">${fmt.n(r.turnover_w, 4)}</td>
      <td class="num">${r.n_fills}</td>
      <td class="num">${fmt.n(r.cost_w * 100, 3)}%</td>
      <td class="num ${r.period_gross > 0 ? 'up' : 'down'}">${fmt.n(r.period_gross * 100, 2)}%</td>
      <td class="num ${r.period_net > 0 ? 'up' : 'down'}">${fmt.n(r.period_net * 100, 2)}%</td>
      <td class="num">${fmt.n(r.period_funding * 100, 3)}%</td>
      <td class="num">${fmt.n(r.scale, 3)}</td>
      <td class="num">${r.n_replaced}</td></tr>`;
  }
  h += `</tbody></table></div>`;
  h += `<p class="desc" style="margin:9px 0 0">${esc(d.unit_note || '')}
    <b>换手列</b>逐行看正好等于换手预算（${fmt.n(d.rows[0] && d.rows[0].turnover_w, 4)}）——
    预算在几乎每一根调仓点上都咬住了，这正是「持仓渐进换」的原因。</p>`;
  return h + pagerHtml(d, 'bl');
}

function fillsTableHtml() {
  const d = S.fills;
  if (!d) return `<div class="empty">载入中…</div>`;
  let h = '';
  if (d.dust && d.dust.n) {
    h += `<div class="chips" style="margin-bottom:8px">
      <span class="chip">共 <b>${fmt.int(d.n_fills_all)}</b> 笔</span>
      <span class="chip">当前筛选 <b>${fmt.int(d.total)}</b> 笔</span>
      <span class="chip">成交额 <b>${fmt.usd(d.agg.notional)}</b></span>
      <span class="chip">成本 <b>${fmt.dol(d.agg.cost)}</b></span>
      <span class="chip warnchip">碎单 <b>${fmt.int(d.dust.n)}</b> 笔 / ${fmt.dol(d.dust.notional)}${d.include_dust ? '（已含）' : '（已滤）'}</span>
    </div>`;
  }
  if (!d.rows.length) return h + `<div class="empty">当前筛选下没有成交</div>`;
  h += `<div class="tblwrap"><table><thead><tr>
    <th>执行时间</th><th>合约</th><th>方向</th><th>动作</th>
    <th class="num">成交前权重</th><th class="num">成交后权重</th><th class="num">Δw</th>
    <th class="num">金额</th><th class="num">成本</th><th class="num">成本(bps)</th>
    <th>当前选入</th><th>此前选入</th></tr></thead><tbody>`;
  for (const f of d.rows) {
    const up = f.dw > 0;
    h += `<tr class="${f.dust ? 'dustrow' : ''}">
      <td class="mono muted">${esc(f.exec_ts)}</td>
      <td class="mono">${esc(f.inst.replace('-USDT-SWAP', ''))}</td>
      <td>${f.side === '多' ? '<span class="tag long">多</span>' : '<span class="tag short">空</span>'}</td>
      <td>${esc(f.action)}</td>
      <td class="num muted">${fmt.n(f.w_before, 5)}</td>
      <td class="num">${fmt.n(f.w_after, 5)}</td>
      <td class="num ${up ? 'up' : 'down'}">${fmt.n(f.dw, 5)}</td>
      <td class="num">${fmt.dol(f.notional)}</td>
      <td class="num">${fmt.n(f.cost, 2)}</td>
      <td class="num">${f.cost_bps == null ? '—' : fmt.n(f.cost_bps, 1)}</td>
      <td>${f.selected_now ? '<span class="tag on">是</span>' : '<span class="muted">—</span>'}</td>
      <td>${f.selected_before ? '<span class="muted">是</span>' : '<span class="muted">—</span>'}</td></tr>`;
  }
  h += `</tbody></table></div>`;
  return h + pagerHtml(d, 'fl', [60, 100, 200, 500]);
}

function pagerHtml(d, kind) {
  if (!d || !d.total) return '';
  const from = d.page * d.per_page + 1;
  const to = Math.min(d.total, (d.page + 1) * d.per_page);
  return `<div class="pager">
    <span class="muted">第 ${fmt.int(from)}–${fmt.int(to)} 条 / 共 ${fmt.int(d.total)} 条 · 第 ${d.page + 1}/${d.n_pages} 页</span>
    <span class="spacer"></span>
    <button class="btn sm ghost" data-pg="${kind}:0" ${d.page === 0 ? 'disabled' : ''}>« 首页</button>
    <button class="btn sm ghost" data-pg="${kind}:${d.page - 1}" ${d.page === 0 ? 'disabled' : ''}>‹ 上页</button>
    <button class="btn sm ghost" data-pg="${kind}:${d.page + 1}" ${d.page >= d.n_pages - 1 ? 'disabled' : ''}>下页 ›</button>
    <button class="btn sm ghost" data-pg="${kind}:${d.n_pages - 1}" ${d.page >= d.n_pages - 1 ? 'disabled' : ''}>末页 »</button>
  </div>`;
}

/* --- 数据加载 --- */
function ensureTrades(force) {
  const tag = S.trTag || (S.selected && (S.runs.find((r) => r.id === S.selected) || {}).tag) || S.optimalTag;
  if (S.tr && S.trTag === tag && !force) { paintTr(); loadFills(0, true); loadBlotter(0, true); return; }
  S.tr = null; S.trErr = null; S.trTag = tag; S.fills = null; S.blotter = null;
  S.fillsPage = 0; S.blotterPage = 0;
  S.trInFlight = true;
  paintTr();
  fetch('/api/trades/' + encodeURIComponent(tag))
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      S.tr = j;
      paintTr();
      loadFills(0, true);
      loadBlotter(0, true);
    })
    .catch((e) => { S.trErr = String((e && e.message) || e); paintTr(); })
    .finally(() => { S.trInFlight = false; });
}

function fillsQuery(page, perPage) {
  const f = S.fillsFilt;
  const p = new URLSearchParams();
  p.set('page', String(page));
  p.set('per_page', String(perPage || 60));
  for (const k of ['year', 'action', 'side', 'q']) if (f[k]) p.set(k, f[k]);
  if (f.include_dust) p.set('include_dust', '1');
  return p.toString();
}

function loadFills(page, silent) {
  if (!S.trTag) return;
  S.fillsPage = Math.max(0, page);
  if (!silent) { S.fills = null; paintTr(); }
  const per = (S.fills && S.fills.per_page) || 60;
  fetch(`/api/trades/${encodeURIComponent(S.trTag)}/fills?${fillsQuery(S.fillsPage, per)}`)
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      S.fills = j;
    })
    .catch((e) => { S.fillsErr = String((e && e.message) || e); })
    .finally(() => paintTr());
}

function loadBlotter(page, silent) {
  if (!S.trTag) return;
  S.blotterPage = Math.max(0, page);
  if (!silent) { S.blotter = null; paintTr(); }
  const p = new URLSearchParams({ page: String(S.blotterPage), per_page: '40' });
  if (S.blYear) p.set('year', String(S.blYear));
  if (S.blInst) p.set('inst', S.blInst);
  fetch(`/api/trades/${encodeURIComponent(S.trTag)}/blotter?${p.toString()}`)
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      S.blotter = j;
    })
    .catch((e) => { S.blotterErr = String((e && e.message) || e); })
    .finally(() => paintTr());
}

function bindTrBody() {
  const tg = $('#trTag');
  if (tg) tg.onchange = () => { S.trTag = tg.value; ensureTrades(true); };
  const as = $('#attrSort');
  if (as) as.onchange = () => { S.attrSort = as.value; paintTr(); };
  const by = $('#blYear');
  if (by) by.onchange = () => { S.blYear = by.value || ''; loadBlotter(0); };
  const fy = $('#flYear');
  if (fy) fy.onchange = () => { S.fillsFilt.year = fy.value || ''; loadFills(0); };
  const fa = $('#flAction');
  if (fa) fa.onchange = () => { S.fillsFilt.action = fa.value || ''; loadFills(0); };
  const fs = $('#flSide');
  if (fs) fs.onchange = () => { S.fillsFilt.side = fs.value || ''; loadFills(0); };
  const fd = $('#flDust');
  if (fd) fd.onchange = () => { S.fillsFilt.include_dust = fd.checked; loadFills(0); };
  const fq = $('#flQ');
  if (fq) fq.onkeydown = (e) => { if (e.key === 'Enter') { S.fillsFilt.q = fq.value.trim(); loadFills(0); } };
  const ap = $('#flApply');
  if (ap) ap.onclick = () => { const q = $('#flQ'); if (q) S.fillsFilt.q = q.value.trim(); loadFills(0); };
  const rs = $('#flReset');
  if (rs) rs.onclick = () => { S.fillsFilt = {}; loadFills(0); };
  for (const b of $$('[data-pg]')) {
    b.onclick = () => {
      const [kind, pg] = b.dataset.pg.split(':');
      (kind === 'fl' ? loadFills : loadBlotter)(parseInt(pg, 10));
    };
  }
  for (const s of $$('[data-per]')) {
    s.onchange = () => {
      const n = parseInt(s.value, 10);
      if (s.dataset.per === 'fl') { S.fillsPer = n; loadFills(0); }
      else { S.blPer = n; loadBlotter(0); }
    };
  }
  for (const g of $$('[data-goto]')) {
    g.onkeydown = (e) => {
      if (e.key !== 'Enter') return;
      const d = g.dataset.goto === 'fl' ? S.fills : S.blotter;
      const n = parseInt(g.value, 10);
      if (!d || !isFinite(n)) return;
      const pg = Math.min(Math.max(1, n), d.n_pages) - 1;
      (g.dataset.goto === 'fl' ? loadFills : loadBlotter)(pg);
    };
  }
  for (const b of $$('[data-exp]')) {
    b.onclick = () => {
      const kind = b.dataset.exp;
      const p = new URLSearchParams();
      if (kind === 'fills') {
        const f = S.fillsFilt;
        for (const k of ['year', 'action', 'side', 'q']) if (f[k]) p.set(k, f[k]);
      }
      const qs = p.toString();
      window.open(`/api/trades/${encodeURIComponent(S.trTag)}/export/${kind}.csv${qs ? '?' + qs : ''}`, '_blank');
    };
  }
}

/* ============================== 净值曲线 ============================== */

/* `fetch` 在 4xx/5xx 时**不会** reject，所以 `/api/equity/<tag>` 的错误体
 * （`{"error":"run in progress"}`）、`null`、以及任何形状不对的东西都会一路
 * 传到绘图代码里，然后死在 `d.t[0]` / `d.final.toFixed()` 上 —— 浏览器报的
 * `Cannot read properties of undefined (reading '0')` 就是这么来的。
 * 唯一的办法是**校验形状**，而不是只看请求成不成功。
 * 形状来自 server.py::read_equity()。 */
function shapeOk(d) {
  if (!d || typeof d !== 'object' || Array.isArray(d)) return false;
  if (!Array.isArray(d.t) || !Array.isArray(d.v)) return false;
  if (d.t.length === 0 || d.t.length !== d.v.length) return false;
  if (typeof d.start !== 'string' || typeof d.end !== 'string') return false;
  if (!Number.isFinite(d.final)) return false;
  for (let i = 0; i < d.t.length; i++) {
    if (!Number.isFinite(d.t[i]) || !Number.isFinite(d.v[i])) return false;
  }
  return true;
}

/* 曲线可以同时画多条：v3 基线 + 当前运行 + 用户在图例上点出来的历史运行。
 * 颜色按**候选顺序**分配（不是按当前显示顺序），所以勾选/取消不会让颜色乱跳。 */
const PALETTE = ['#2f6feb', '#d1495b', '#2a9d8f', '#e9a23b', '#8e6fd8', '#4b8b3b',
  '#c2571a', '#0f7c8a'];
const CHART_MAX_SERIES = 6;

/* 能作为曲线来源的 tag：当前运行、v3 基线，以及所有跑完的历史运行。 */
function chartCandidates() {
  const out = [];
  const push = (t) => { if (t && out.indexOf(t) < 0) out.push(t); };
  push(S.detail && S.detail.tag);
  push(S.optimalTag);
  for (const r of S.runs) {
    if (r.status === 'done' || r.status === 'partial') push(r.tag);
  }
  return out.slice(0, CHART_MAX_SERIES);
}

/* 默认只画「当前运行 + v3 基线」：六个 tag 一起画会糊成一片。 */
function initChartOff() {
  if (S.chartOffInit) return;
  const cur = S.detail && S.detail.tag;
  for (const t of chartCandidates()) {
    if (t !== cur && t !== S.optimalTag) S.chartOff[t] = true;
  }
  S.chartOffInit = true;
}

/* 视图 → 绘制值。回撤取「相对各自历史高点」（≤ 0），这样不同窗口的曲线可比。
 * 注意曲线是**下采样过**的，最小值可能仍比真实回撤浅一点，所以显示用的 MDD
 * 优先取服务端在全量序列上算好的 `d.mdd`（与报告口径一致）。 */
function curveValues(tag, view) {
  const d = S.equity[tag];
  if (!d) return null;
  if (view === 'dd') {
    let peak = -Infinity;
    const y = d.v.map((v) => { if (v > peak) peak = v; return peak > 0 ? v / peak - 1 : 0; });
    const mdd = Number.isFinite(d.mdd) ? d.mdd : Math.min.apply(null, y);
    return { d, y, mdd };
  }
  return { d, y: d.v, mdd: null };
}

/* 对数视图要把刻度也搬到 log 空间，否则 1×~4× 的网格会挤在底部 */
const fwd = (view, v) => view === 'log' ? Math.log(v) : v;
function tickLabel(u, view) {
  if (view === 'dd') return (u * 100).toFixed(1) + '%';
  if (view === 'log') return Math.exp(u).toFixed(2) + '×';
  return u.toFixed(2) + '×';
}

function bindChartSeg() {
  const seg = $('#chartSeg');
  if (!seg) return;
  $$('.segbtn', seg).forEach((b) => {
    b.onclick = () => {
      S.chartView = b.dataset.cv;
      $$('.segbtn', seg).forEach((x) => x.classList.toggle('on', x === b));
      drawChart();
    };
  });
}

function bindLegend() {
  const lg = $('#chartLegend');
  if (!lg) return;
  $$('[data-ctag]', lg).forEach((c) => {
    c.onclick = () => {
      const t = c.dataset.ctag;
      if (S.chartOff[t]) delete S.chartOff[t];
      else S.chartOff[t] = true;
      drawChart();
    };
  });
}

function paintLegend(cands, shown, view) {
  const el = $('#chartLegend');
  if (!el) return;
  const byTag = {};
  for (const s of shown) byTag[s.tag] = s;
  el.innerHTML = cands.map((t) => {
    const s = byTag[t];
    const col = t === S.optimalTag ? 'var(--muted)' : PALETTE[cands.indexOf(t) % PALETTE.length];
    let tail = '';
    if (s) tail = view === 'dd' ? ` ${(s.mdd * 100).toFixed(1)}%` : ` ${s.d.final.toFixed(2)}×`;
    else if (S.equityErr[t]) tail = ' 无数据';
    return `<button class="lgchip ${s ? 'on' : 'off'}" data-ctag="${esc(t)}"
      title="${esc(S.equityErr[t] || t)}"><i style="background:${col}"></i>${esc(t)}${esc(tail)}</button>`;
  }).join('');
  bindLegend();
}

async function drawChart() {
  const cv = $('#chart');
  if (!cv) return;
  // The canvas is hidden while the 交易记录 tab is up, and a hidden canvas has
  // clientWidth 0 -> every coordinate is NaN.  Bail out instead of drawing garbage.
  if (!cv.clientWidth || !cv.clientHeight) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const css = getComputedStyle(document.body);
  const cMuted = css.getPropertyValue('--muted').trim();
  const cBorder = css.getPropertyValue('--border').trim();
  const cText = css.getPropertyValue('--text-2').trim();

  initChartOff();
  const cands = chartCandidates();
  const view = S.chartView;

  for (const t of cands) {
    if (S.equity[t]) continue;
    if (S.chartOff[t]) continue;   // 折叠起来的曲线等点开再取，别把首屏拖慢
    // `fetch` does NOT reject on 4xx, so without the `r.ok` check an error body
    // ({"error": "run in progress"}) arrives here looking like a data series and
    // the chart dies on `d.t[0]`.  Validate the SHAPE, not just the request.
    try {
      const r = await fetch('/api/equity/' + encodeURIComponent(t));
      const j = r.ok ? await r.json() : null;
      S.equity[t] = shapeOk(j) ? j : null;
      if (!r.ok) S.equityErr[t] = (j && j.error) || ('HTTP ' + r.status);
      else delete S.equityErr[t];
    } catch { S.equity[t] = null; }
  }
  // Drop anything that slipped through in an earlier round / from the cache.
  for (const t in S.equity) if (S.equity[t] && !shapeOk(S.equity[t])) S.equity[t] = null;

  const shown = [];
  for (const t of cands) {
    if (S.chartOff[t]) continue;
    const c = curveValues(t, view);
    if (!c) continue;
    const base = t === S.optimalTag;
    shown.push({
      tag: t, d: c.d, y: c.y, mdd: c.mdd, base,
      c: base ? cMuted : PALETTE[cands.indexOf(t) % PALETTE.length],
      w: base ? 1.4 : 2,
    });
  }

  paintLegend(cands, shown, view);

  const P = { l: 52, r: 12, t: 14, b: 24 };
  if (!shown.length) {
    ctx.fillStyle = cMuted; ctx.font = '12px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('运行一次回测后显示回测曲线', W / 2, H / 2);
    return;
  }

  const t0 = Math.min(...shown.map((s) => s.d.t[0]));
  const t1 = Math.max(...shown.map((s) => s.d.t[s.d.t.length - 1]));
  let vmin = Infinity, vmax = -Infinity;
  for (const s of shown) {
    for (const v of s.y) { const u = fwd(view, v); if (u < vmin) vmin = u; if (u > vmax) vmax = u; }
  }
  if (view === 'dd') vmax = 0;                 // 回撤视图顶部固定 0%
  const pad = (vmax - vmin) * 0.08 || 0.05;
  vmin -= pad;
  if (view !== 'dd') vmax += pad;
  const X = (t) => P.l + (t - t0) / (t1 - t0) * (W - P.l - P.r);
  const Y = (v) => P.t + (vmax - fwd(view, v)) / (vmax - vmin) * (H - P.t - P.b);

  ctx.strokeStyle = cBorder; ctx.lineWidth = 1; ctx.font = '10.5px system-ui';
  ctx.fillStyle = cMuted; ctx.textAlign = 'right';
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const u = vmin + (vmax - vmin) * i / ticks;
    const y = Math.round(P.t + (vmax - u) / (vmax - vmin) * (H - P.t - P.b)) + .5;
    ctx.beginPath(); ctx.moveTo(P.l, y); ctx.lineTo(W - P.r, y); ctx.stroke();
    ctx.fillText(tickLabel(u, view), P.l - 6, y + 3.5);
  }
  // 基准线：净值视图落在 1.0×，回撤视图落在 0%
  ctx.strokeStyle = cMuted; ctx.setLineDash([3, 3]); ctx.beginPath();
  const zeroV = view === 'dd' ? 0 : 1;
  ctx.moveTo(P.l, Y(zeroV)); ctx.lineTo(W - P.r, Y(zeroV)); ctx.stroke(); ctx.setLineDash([]);

  for (const s of shown) {
    const n = s.y.length;
    if (!s.base) {                                  // 当前运行填充，基线不填，避免糊住
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = X(s.d.t[i]), y = Y(s.y[i]);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.lineTo(X(s.d.t[n - 1]), H - P.b); ctx.lineTo(X(s.d.t[0]), H - P.b);
      ctx.closePath();
      ctx.fillStyle = s.c + '18'; ctx.fill();
    }
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const x = X(s.d.t[i]), y = Y(s.y[i]);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = s.c; ctx.lineWidth = s.w; ctx.stroke();
  }

  ctx.fillStyle = cMuted; ctx.textAlign = 'left'; ctx.font = '10.5px system-ui';
  ctx.fillText(new Date(t0).toISOString().slice(0, 10), P.l, H - 7);
  ctx.textAlign = 'right';
  ctx.fillText(new Date(t1).toISOString().slice(0, 10), W - P.r, H - 7);

  const last = shown[shown.length - 1];
  ctx.fillStyle = cText; ctx.textAlign = 'left'; ctx.font = '11.5px system-ui';
  const head = view === 'dd'
    ? `最大回撤 ${(last.mdd * 100).toFixed(2)}%`
    : `末值 ${last.d.final.toFixed(3)}×`;
  ctx.fillText(`${last.tag} · ${head}  (${last.d.start} → ${last.d.end})`, P.l, P.t + 11);
}

/* ============================== 运行与轮询 ============================== */
async function runNow() {
  const btn = $('#runBtn');
  const diffs = diffFromOptimal();
  if (diffs.length && !confirm(
    `当前参数与已验收最优有 ${diffs.length} 处不同：\n\n`
    + diffs.map((d) => `  ${d.k}: ${d.want} → ${d.got}`).join('\n')
    + `\n\n仍然要跑吗？`)) return;

  const overrides = {};
  for (const k in S.ov) if (!eq(S.ov[k], S.lib[k])) overrides[k] = S.ov[k];
  const cli = {};
  for (const k of ['bar', 'rebalance_days', 'asset_class', 'start', 'end', 'capital']) cli[k] = S.cli[k];
  const sub = (S.ov['factors.subset'] || []).join('+') || '无因子';
  const label = `${sub} · ${S.cli.bar} · ${S.cli.rebalance_days}d · cap${S.ov['portfolio.max_weight_per_instrument']} · tvol${S.ov['execution.max_daily_turnover']}`;

  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const res = await (await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ overrides, cli, stages: S.stages, preset: S.preset, label }),
    })).json();
    if (res.error) { alert(res.error); return; }
    S.selected = res.id; S.logOffset = 0;
    await pollRuns();
    await selectRun(res.id);
  } finally {
    btn.disabled = false; btn.textContent = '▶ 运行回测';
  }
}

/* 第二道：正在敲字的时候，把轮询那一轮的重建跳过去。
 *
 * 它**不是**第一道的备份，别指望它单独兜住 —— 变异测试证伪过这个想法：
 * 拆掉 preserveUi 的回填之后，哪怕这条防线在位，值照样丢。
 * 因为「焦点此刻在输入框上」这个前提本身有缝：焦点还没建立、或刚 blur 的那几百
 * 毫秒里，轮询想重建还是会重建（实测这种间隙里能插进 1 次 renderContent）。
 *
 * 所以两道的分工是这样的：
 *   第一道 preserveUi() 的快照回填 = **真正的修补**，任何重建路径都覆盖；
 *   第二道这条分支            = 少做几次无意义的全页重建（打字时页面不抖、省开销）。
 * 只认「真的在打字」的控件——checkbox / radio / button 点了就完事，
 * 把它们算进来会让游标停在某个按钮上时 UI 一直不刷新。 */
const NON_TYPING_INPUT = new Set(
  ['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'range', 'color', 'date']);
function isTyping(el) {
  if (!el) return false;
  const t = String(el.tagName || '').toUpperCase();
  if (t === 'TEXTAREA') return true;
  if (t !== 'INPUT') return false;
  return !NON_TYPING_INPUT.has(String(el.type || 'text').toLowerCase());
}

async function pollRuns() {
  try {
    const d = await (await fetch('/api/runs')).json();
    S.runs = d.runs || []; S.active = d.active; S.queued = d.queued || [];
  } catch { return; }
  const q = $('#queuePill');
  if (S.active || S.queued.length) {
    q.style.display = '';
    q.className = 'pill accent';
    q.textContent = S.active ? `运行中 ${1 + S.queued.length} 项` : `队列 ${S.queued.length} 项`;
  } else q.style.display = 'none';
  if (S.detail) {
    const cur = S.runs.find((r) => r.id === S.selected);
    if (cur) S.detail.status = cur.status;
    startLog(S.detail);
  }
  if (isTyping(document.activeElement)) return;   /* 用户正在输入：别动 DOM */
  renderContent();
}

async function selectRun(id) {
  S.selected = id; S.logOffset = 0;
  const d = await (await fetch('/api/runs/' + encodeURIComponent(id))).json();
  if (d.error) return;
  d._log = '';
  S.detail = d;
  // 刚选中的运行默认要出现在曲线上（用户之前手动关掉过也要重新显示）
  if (d.tag) delete S.chartOff[d.tag];
  // Keep the 交易记录 tab pointing at the run that was just picked.
  if (S.tab === 'trades' && d.tag) {
    S.trTag = d.tag;
    renderContent();
    ensureTrades(true);
    return;
  }
  renderContent();
  const lp = await (await fetch(`/api/runs/${encodeURIComponent(id)}/log?offset=0`)).json();
  S.logOffset = lp.offset || 0;
  d._log = lp.text || '';
  renderContent();
}

function startLog(d) {
  clearInterval(S.logTimer);
  const live = () => d.status === 'running' || d.status === 'queued';
  if (!live()) { stopLog(); return; }
  S.logTimer = setInterval(async () => {
    try {
      const lp = await (await fetch(`/api/runs/${encodeURIComponent(S.selected)}/log?offset=${S.logOffset}`)).json();
      if (lp.text) {
        S.logOffset = lp.offset;
        const pre = $('#log');
        if (pre) { pre.insertAdjacentHTML('beforeend', logHtml(lp.text)); pre.scrollTop = pre.scrollHeight; }
      }
    } catch { /* ignore */ }
  }, 1100);
}
function stopLog() { clearInterval(S.logTimer); S.logTimer = null; }

function logHtml(txt) {
  return esc(txt)
    .replace(/^(!! .*)$/gm, '<span class="err">$1</span>')
    .replace(/^(\s*\[[a-z]+\].*)$/gm, '<span class="stage">$1</span>');
}

/* ============================== 主题 ============================== */
$('#themeBtn').onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : (cur === 'light' ? 'dark' : 'dark');
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ls-theme', next);
  setTimeout(drawChart, 30);
};
const savedTheme = localStorage.getItem('ls-theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);

$('#runBtn').onclick = runNow;

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* =========================================================================
   交易台
   -------------------------------------------------------------------------
   三条铁律（都是本项目已经用真实数据换来的）：
   1) 信号不能另写一套：目标书由 run_backtest 产出，和报告同源。
   2) 目标书 ≠ 实际持仓：v3 的实际毛敞口均值 0.472，目标 1.032，
      因为 20%/期的换手预算永远追不上。实盘必须复刻预算，否则敞口差 2.2 倍。
   3) 页面上的数字必须和服务端逐位一致：所有金额都由后端算好、前端只负责显示。
   ========================================================================= */
const LIVE_LABEL = { paper: '本地纸面', demo: 'OKX 模拟盘', live: 'OKX 实盘' };
const LIVE_LIMIT_FIELDS = [
  ['max_gross_notional', '总名义额上限 (USD)', '绝对上限，不随本金缩放——唯一能挡住「连错账户」的闸门'],
  ['max_order_notional', '单笔上限 (USD)', '任何一笔订单超过即整批拒绝'],
  ['max_orders', '单次订单笔数上限', '超出通常意味着 target 或持仓读数有误'],
  ['max_turnover_frac', '单次换手上限 (× 净值)', '策略每期只换 20% 毛敞口'],
  ['max_adv_participation', '单笔 ADV 参与率上限', '冲击成本会显著偏离模型'],
  ['min_nav_usd', '净值下限 (USD)', '低于此值下单粒度噪声大于信号'],
  ['max_nav_usd', '净值上限 (USD，可空)', '连到不打算交易的账户时挡住自己'],
  ['max_leverage', '账户杠杆上限 (×)', '请求杠杆超过此值即拒绝下单'],
];

function liveSkeleton() {
  const L = S.live;
  const note = L.statusErr
    ? `<div class="banner danger"><div class="ico">!</div><div><b>交易台无法初始化</b><p>${esc(L.statusErr)}</p></div></div>`
    : '';
  return `${note}
  <h2 class="sec">交易台 <small>信号与回测同源 · 下单前必过风控闸门</small></h2>
  <div class="panel"><div class="head"><h3>模式</h3><span class="spacer"></span>
    <span class="muted" style="font-size:11.5px">模式决定密钥槽与是否真实成交</span></div>
    <div class="body" id="liveMode">${L.meta ? '' : '<div class="empty">正在读取…</div>'}</div></div>

  <div class="grid2" style="margin-top:14px">
    <div class="panel"><div class="head"><h3>账户与连接</h3><span class="spacer"></span>
      <span class="muted" style="font-size:11.5px" id="liveConnPill"></span></div>
      <div class="body" id="liveAcct"><div class="empty">正在读取…</div></div></div>
    <div class="panel"><div class="head"><h3>信号</h3><span class="spacer"></span>
      <span class="muted" style="font-size:11.5px">与报告同一套引擎</span></div>
      <div class="body" id="liveSignal"><div class="empty">正在读取…</div></div></div>
  </div>

  <div class="panel" style="margin-top:14px"><div class="head"><h3>风控闸门</h3>
    <span class="spacer"></span><span class="muted" style="font-size:11.5px">任何一条「拦截」都会让整批订单不发出</span></div>
    <div class="body" id="liveGates"><div class="empty">尚未生成计划</div></div></div>

  <div class="panel" style="margin-top:14px"><div class="head"><h3>下单计划</h3>
    <span class="spacer"></span><span class="muted" style="font-size:11.5px" id="livePlanMeta"></span></div>
    <div class="body" id="livePlan"><div class="empty">点「生成下单计划」开始</div></div></div>

  <div class="panel" style="margin-top:14px"><div class="head"><h3>执行日志</h3>
    <span class="spacer"></span><span class="muted mono" id="liveJobMeta"></span></div>
    <div class="body" id="liveJob"><div class="empty">还没有执行记录</div></div></div>

  <div class="panel" style="margin-top:14px"><div class="head">
    <div class="seg sm" id="liveSeg">
      <button class="segbtn ${L.sub === 'orders' ? 'on' : ''}" data-lsub="orders">订单</button>
      <button class="segbtn ${L.sub === 'fills' ? 'on' : ''}" data-lsub="fills">成交</button>
      <button class="segbtn ${L.sub === 'runs' ? 'on' : ''}" data-lsub="runs">运行记录</button>
      <button class="segbtn ${L.sub === 'limits' ? 'on' : ''}" data-lsub="limits">风控限额</button>
    </div><span class="spacer"></span>
    <button class="btn sm ghost" id="liveExport">导出 CSV</button></div>
    <div class="body" id="liveHist"><div class="empty">正在读取…</div></div></div>`;
}

async function liveApi(path, opts) {
  const r = await fetch(path, opts);
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  // `fetch` 不因 4xx/5xx reject，所以形状必须自己校验，不能只看 r.ok。
  if (d == null || typeof d !== 'object') {
    throw new Error(`${path} 返回的不是 JSON（HTTP ${r.status}）`);
  }
  if (!r.ok && !d.error) throw new Error(`${path} HTTP ${r.status}`);
  return d;
}

async function ensureLive() {
  const L = S.live;
  if (L.meta) { livePaintAll(); return; }
  if (L.statusErr) return;
  try {
    L.meta = await liveApi('/api/live/meta');
    if (L.meta.limits_default) L.limits = Object.assign({}, L.meta.limits_default);
    if (L.limits && L.meta.default_signal) {
      const sg = L.meta.default_signal;
      L.signal = L.signal || sg;
    }
    await liveLoadStatus();
    livePaintAll();
  } catch (e) {
    L.statusErr = String(e && e.message || e);
    S.live.statusErr = L.statusErr;
    renderContent();
  }
}

async function liveLoadStatus() {
  const L = S.live;
  try {
    const st = await liveApi('/api/live/status?mode=' + encodeURIComponent(L.mode));
    L.status = st;
    L.statusErr = null;
    if (st.limits && !L.limitsTouched) L.limits = Object.assign({}, st.limits);
  } catch (e) {
    L.statusErr = String(e && e.message || e);
  }
}

/* ---- 渲染 ---- */
/* ---- 重建守卫：焦点 + 未提交的输入内容 ----

 * `innerHTML =` 会把用户正在敲的内容连同 DOM 一起丢掉。本项目被这一个机制坑了三次：
 *   ① 交易表格被每 2.5s 的 pollRuns 打回「正在读取…」占位；
 *   ② 交易台被同一次轮询打回骨架；
 *   ③ API Key 输入框——焦点还在、字没了（最直接的表现就是「一输入就清空」）。
 * ①②③ 是同一个根因的三种表现，逐个补就好比逐个堵漏，下次新加 input 的人还是会忘。
 *
 * 而且这些值**只在 change（失焦）时才写回 state**：正在输入的那一刻，它们只存在于
 * DOM 里，模板拿到的是服务端旧值。所以恢复时**用户手里那个值优先**——用模板的旧值
 * 去覆盖用户刚敲进去的字，是最糟的结果。
 *
 * 这里一次性解决：重建前快照所有 input/textarea/select，重建后回填。
 */
function _uiKey(el, i) {
  if (el.id) return `id:${el.id}`;
  const ds = el.dataset || {};
  if (ds.limit) return `limit:${ds.limit}`;
  if (ds.lord) return `lord:${ds.lord}`;
  return `n${i}`;
}

function snapshotInputs() {
  const snap = new Map();
  const root = document.getElementById('content');
  if (!root || typeof root.querySelectorAll !== 'function') return snap;
  const els = root.querySelectorAll('input, textarea, select');
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    snap.set(_uiKey(el, i), { v: el.value, c: !!el.checked, tag: el.tagName, t: el.type });
  }
  return snap;
}

function restoreInputs(snap) {
  if (!snap || !snap.size) return;
  const root = document.getElementById('content');
  if (!root || typeof root.querySelectorAll !== 'function') return;
  const els = root.querySelectorAll('input, textarea, select');
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    const old = snap.get(_uiKey(el, i));
    if (!old || old.tag !== el.tagName || old.t !== el.type) continue;
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = old.c;
    else if (el.value !== old.v) el.value = old.v;
  }
}

function preserveUi(fn) {
  const snap = snapshotInputs();
  const el = document.activeElement;
  const id = el && el.id ? el.id : null;
  const pos = (el && typeof el.selectionStart === 'number') ? el.selectionStart : null;
  fn();
  restoreInputs(snap);
  if (!id) return;
  const again = document.getElementById(id);
  if (!again) return;
  try {
    again.focus();
    if (pos != null && again.setSelectionRange) again.setSelectionRange(pos, pos);
  } catch (e) { /* 只关心输入焦点，失败不影响功能 */ }
}

function livePaintAll() {
  preserveUi(() => {
    const L = S.live;
    const put = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
    put('liveMode', liveModeHtml());
    put('liveAcct', liveAcctHtml());
    put('liveSignal', liveSignalHtml());
    put('liveGates', liveGatesHtml());
    put('livePlan', livePlanHtml());
    put('liveJob', liveJobHtml());
    put('liveHist', liveHistHtml());
    const pm = document.getElementById('livePlanMeta');
    if (pm) pm.textContent = L.plan && L.plan.plan
      ? `${L.plan.plan.n_orders} 笔 · ${fmt.dol(L.plan.plan.order_notional)}`
      : '';
    const jm = document.getElementById('liveJobMeta');
    if (jm) jm.textContent = L.job ? `${L.job.kind} · ${L.job.status}${L.job.elapsed ? ' · ' + L.job.elapsed + 's' : ''}` : '';
    const cp = document.getElementById('liveConnPill');
    if (cp) {
      const st = L.status;
      cp.innerHTML = !st ? ''
        : (st.kill_switch ? '<b style="color:var(--danger)">交易已熔断</b>'
          : (st.connected ? '<b style="color:var(--ok)">已连接</b>' : '未连接'));
    }
    liveBind();
  });
}

/* 网络延迟徽标。交易所慢和「我们太啰嗦」是两种病，治法相反，所以必须分得清：
 * p50 是交易所的往返水平，requests 是我们发了多少次请求。
 * 两个数字一起显示，才能看出「慢」到底该怪网络还是该怪自己（后者已改为批量提交）。 */
function liveNetHtml(net) {
  const n = net || (S.live.status && S.live.status.net) || {};
  if (!n.samples) return '';
  const ms = (v) => (v == null ? '—' : (v >= 1000 ? (v / 1000).toFixed(1) + 's' : v + 'ms'));
  const slow = n.verdict === 'slow';
  const px = n.proxy ? ` · 代理 ${esc(String(n.proxy).replace(/^https?:\/\//, ''))}` : '';
  return `<div class="split" style="margin-top:9px;font-size:11.5px">
    <span class="pill ${slow ? 'warn' : 'ok'}">${slow ? '⚠ 交易所响应偏慢' : '✓ 网络正常'}</span>
    <span class="muted">最近 ${ms(n.last_ms)} · 中位 ${ms(n.p50_ms)} · 峰值 ${ms(n.max_ms)}
      · 请求 ${n.requests} 次${n.slow ? `（其中 ${n.slow} 次超 2s）` : ''}
      ${n.retries ? ` · 重试 ${n.retries}` : ''}${px}</span>
  </div>`;
}

function liveModeHtml() {
  const L = S.live;
  const modes = (L.meta && L.meta.modes) || [
    { id: 'paper', label: '本地纸面', note: '' },
    { id: 'demo', label: 'OKX 模拟盘', note: '' },
    { id: 'live', label: 'OKX 实盘', note: '' },
  ];
  const creds = (L.status && L.status.creds) || (L.meta && L.meta.creds) || {};
  let h = `<div class="modecards">`;
  for (const m of modes) {
    const c = creds[m.id] || {};
    const okc = m.id === 'paper' ? true : !!c.configured;
    h += `<button class="modecard ${L.mode === m.id ? 'on' : ''} ${m.id === 'live' ? 'danger' : ''}" data-lmode="${esc(m.id)}">
      <b><span class="${okc ? 'dotok' : 'dotno'}"></span>${esc(m.label)}
        ${m.id === 'live' ? '<span class="chip" style="border-color:var(--danger);color:var(--danger)">真实资金</span>' : ''}</b>
      <small>${esc(m.note || '')}</small>
      <small>${m.id === 'paper' ? '无需密钥' : (c.configured
      ? `密钥 ${esc(c.key_hint)}${c.env_override ? '（来自环境变量）' : '（本地文件）'}`
      : '未配置密钥')}</small>
    </button>`;
  }
  h += `</div>`;

  const c = creds[L.mode] || {};
  if (L.mode !== 'paper') {
    h += `<div class="split" style="margin-top:11px">
      <button class="btn sm" id="liveCredsToggle">${L.credsOpen ? '收起密钥配置' : (c.configured ? '更换 API Key' : '配置 API Key')}</button>
      ${c.configured ? `<button class="btn sm ghost" id="liveCredsDel">删除本机密钥</button>` : ''}
      ${c.env_override ? `<span class="chip warnchip">环境变量优先，界面里改不会生效</span>` : ''}
    </div>`;
    if (L.credsOpen) {
      h += `<div style="margin-top:11px" class="split">
        <input class="liveinput" id="lvKey" placeholder="API Key" autocomplete="off" style="min-width:220px">
        <input class="liveinput" id="lvSecret" type="password" placeholder="Secret Key" autocomplete="new-password" style="min-width:200px">
        <input class="liveinput" id="lvPass" type="password" placeholder="Passphrase" autocomplete="new-password" style="min-width:150px">
        <button class="btn sm primary" id="liveCredsSave">保存</button>
      </div>
      <p class="desc" style="margin:8px 0 0">${L.mode === 'demo'
        ? '模拟盘必须在 OKX「交易 → 模拟交易 → 个人中心 → 模拟盘 API」单独创建密钥——实盘 Key 在模拟环境会返回 50111。'
        : '实盘 Key 请只勾选「交易」权限，不要开提现。'}</p>`;
    }
  }

  const kill = !!(L.status && L.status.kill_switch);
  h += (L.mode === 'paper' ? '' : liveNetHtml());
  h += `<div class="split" style="margin-top:11px;border-top:1px solid var(--border);padding-top:11px">
    <button class="btn sm ${kill ? '' : 'danger'}" id="liveKill">${kill ? '解除熔断' : '🛑 立即熔断（拒绝一切下单）'}</button>
    <span class="muted" style="font-size:11.5px">熔断是一个磁盘文件，服务重启、浏览器关掉都仍然生效。</span>
  </div>`;
  return h;
}

/* 名义额永远取服务端给的 `notional`（= |张数| × ctVal × 标记价）。
 * 千万别在前端自己乘：漏掉 ctVal 会把 BTC-USDT-SWAP（ctVal 0.01）的
 * $8.47 显示成 $847，整页「毛敞口」虚高 100 倍 —— 这是本项目踩过的坑。
 * 服务端没给（规格缺失）就显示「—」，不猜。 */
const posNotional = (p) => (p && p.notional != null && isFinite(p.notional)
  ? Number(p.notional) : null);

function liveAcctHtml() {
  const L = S.live;
  const st = L.status;
  if (!st) return `<div class="empty">正在读取账户…</div>`;
  if (st.account_error) {
    return `<div class="banner warn"><div class="ico">!</div><div><b>账户不可用</b>
      <p>${esc(st.account_error)}</p></div></div>`;
  }
  const a = st.account || {};
  const store = st.store || {};
  const nav = a.nav;
  const gross = (a.positions || []).reduce((s, p) => s + (posNotional(p) || 0), 0);
  const missing = (a.positions || []).filter((p) => posNotional(p) == null).length;
  const used = store.turnover_used_today;
  const budget = st.turnover_budget;
  let h = `<div class="kv">
    <div class="cell"><div class="k">净值</div><div class="v">${fmt.dol(nav)}</div>
      ${a.unrealised != null ? `<div class="s">未实现 ${fmt.dol(a.unrealised)}</div>` : ''}</div>
    <div class="cell"><div class="k">持仓数</div><div class="v">${(a.positions || []).length}</div>
      <div class="s">${esc(a.pos_mode || '')}</div></div>
    <div class="cell"><div class="k">今日换手预算</div>
      <div class="v">${budget != null ? fmt.pct(budget, 0) : '不限制'}</div>
      <div class="s">已用 ${used != null ? fmt.pct(used, 1) : '—'}</div></div>
    <div class="cell"><div class="k">行情来源</div><div class="v" style="font-size:12.5px">${esc(st.prices_source || '—')}</div>
      <div class="s">持仓名义 ${fmt.dol(gross)}${gross > 0 && nav > 0 ? ` (${fmt.n(gross / nav, 3)}×)` : ''}</div></div>
  </div>`;
  if (missing) {
    h += `<div class="banner warn" style="margin-top:11px"><div class="ico">!</div><div>
      <b>有 ${missing} 个持仓算不出名义额</b>
      <p>合约规格缺失（多为已下架），所以这里不做估算——毛敞口是<b>下界</b>。</p></div></div>`;
  }
  const ds = st.data_staleness;
  if (ds && ds.stale) {
    const nb = (ds.newest_bar || '').slice(0, 16).replace('T', ' ');
    h += `<div class="banner warn" style="margin-top:11px"><div class="ico">!</div><div>
      <b>行情数据已过期</b>
      <p>最新已收盘 bar：<span class="mono">${esc(nb || '未知')}</span>
        ${ds.bar_age_hours != null ? `（落后 <b>${fmt.n1(ds.bar_age_hours)}</b> 小时）` : ''}；
        缓存文件 ${ds.age_hours != null ? fmt.n1(ds.age_hours) + ' 小时' : '—'}未更新。
        信号用的是缓存里最后一根已收盘 K 线。先点「刷新行情数据」（增量补尾，约十几秒），
        否则会按旧价格决策。</p></div></div>`;
  }
  if ((a.positions || []).length) {
    h += `<table class="ordtable" style="margin-top:11px"><thead><tr>
      <th>合约</th><th>方向</th><th>张数</th><th>均价</th><th>标记价</th><th>未实现</th><th>名义额</th></tr></thead><tbody>`;
    for (const p of a.positions) {
      const dir = (p.pos > 0) ? '多' : '空';
      const cls = (p.pos > 0) ? 'long' : 'short';
      const n = posNotional(p);
      h += `<tr><td class="mono">${esc(p.instId)}</td>
        <td class="${cls}">${dir}</td>
        <td class="num">${fmt.n(p.pos, 4)}</td>
        <td class="num">${fmt.n(p.avgPx, 6)}</td>
        <td class="num">${fmt.n(p.markPx, 6)}</td>
        <td class="num">${p.upl >= 0 ? '<span class="long">' : '<span class="short">'}${fmt.dol(p.upl)}</span></td>
        <td class="num">${n == null ? '<span class="muted" title="缺 ctVal，不估算">—</span>' : fmt.dol(n)}</td></tr>`;
    }
    h += `</tbody></table>`;
  } else {
    h += `<p class="desc" style="margin:11px 0 0">当前无持仓。</p>`;
  }
  return h;
}

function liveSignalHtml() {
  const L = S.live;
  const pl = L.plan;
  let h = `<div class="split">
    <button class="btn sm" id="liveRefreshData">↻ 刷新行情数据</button>
    <button class="btn sm ${L.busy ? '' : 'primary'}" id="livePlanBtn" ${L.busy ? 'disabled' : ''}>生成下单计划</button>
    <button class="btn sm ghost" id="livePlanFresh" ${L.busy ? 'disabled' : ''}>强制重算信号</button>
  </div>`;
  if (!pl || !pl.target) {
    h += `<p class="desc" style="margin:11px 0 0">首次生成计划要跑一次完整回测（本机约 40–60 秒），之后 30 分钟内走缓存。信号由 <span class="mono">run_backtest</span> 产出，与报告完全同源。</p>`;
    return h;
  }
  const t = pl.target;
  const dg = t.diagnostics || {};
  h += `<div class="kv" style="margin-top:11px">
    <div class="cell"><div class="k">信号日</div><div class="v" style="font-size:12.5px">${esc((t.decision_ts || '').slice(0, 16).replace('T', ' '))}</div>
      <div class="s">bar ${esc((t.panel_last_ts || '').slice(0, 16).replace('T', ' '))}</div></div>
    <div class="cell"><div class="k">下次调仓</div><div class="v" style="font-size:12.5px">${esc((t.next_decision_ts || '').slice(0, 16).replace('T', ' '))}</div>
      <div class="s">${t.rebalance_due ? '<b style="color:var(--danger)">已到调仓日</b>' : `还需 ${Math.max(0, (t.rebalance_bars || 0) - (t.bars_since_decision || 0))} 根 bar`}</div></div>
    <div class="cell"><div class="k">可选池宽</div><div class="v">${dg.n_universe != null ? dg.n_universe : '—'}</div>
      <div class="s">多 ${dg.n_long ?? '—'} / 空 ${dg.n_short ?? '—'}</div></div>
    <div class="cell"><div class="k">目标毛敞口</div><div class="v">${fmt.n(t.gross, 4)}</div>
      <div class="s">每侧 beta ${fmt.n(dg.beta_u, 3)} / ${fmt.n(dg.beta_v, 3)}</div></div>
  </div>`;
  h += `<p class="desc" style="margin:9px 0 0">缩放分解：regime ${fmt.n(dg.regime_scale, 3)} ·
    BTC 波动 ${fmt.n(dg.btc_vol_scale, 3)} · 回撤 ${fmt.n(dg.dd_scale, 3)} ·
    合计 ${fmt.n(dg.total_scale, 3)}。${dg.forming_bar_dropped ? ' 已剔除正在形成的 bar。' : ''}</p>`;
  return h;
}

function liveGatesHtml() {
  const L = S.live;
  const pl = L.plan;
  if (!pl) return `<div class="empty">尚未生成计划</div>`;
  const vs = pl.violations || [];
  if (!vs.length) {
    return `<div class="gates"><div class="gate ok"><span class="ico2">✓</span>
      <div><b>全部通过</b>可以按计划下单。</div></div></div>`;
  }
  let h = `<div class="gates">`;
  for (const v of vs) {
    h += `<div class="gate ${v.sev === 'block' ? 'block' : 'warn'}">
      <span class="ico2">${v.sev === 'block' ? '⛔' : '⚠'}</span>
      <div><b>${esc(v.title)}</b>${v.body || ''}</div></div>`;
  }
  h += `</div>`;
  return h;
}

function livePlanHtml() {
  const L = S.live;
  const pl = L.plan;
  if (!pl || !pl.plan) return `<div class="empty">点「生成下单计划」开始</div>`;
  const p = pl.plan;
  const blocked = !!pl.blocked || (pl.violations || []).some((v) => v.sev === 'block');
  const res = {};
  for (const r of (((pl.record || {}).results) || [])) res[r.instId] = r;

  let h = `<div class="kv">
    <div class="cell"><div class="k">原始目标毛敞口</div><div class="v">${fmt.n((p.raw_target_gross || 0) / (p.nav || 1), 3)}</div>
      <div class="s">× 净值 = ${fmt.dol(p.raw_target_gross)}</div></div>
    <div class="cell"><div class="k">换手预算后</div><div class="v">${fmt.n((p.target_gross || 0) / (p.nav || 1), 3)}</div>
      <div class="s">只执行目标变动的 ${fmt.pct(p.turnover_scale)}</div></div>
    <div class="cell"><div class="k">订单</div><div class="v">${p.n_orders} 笔</div>
      <div class="s">名义额 ${fmt.dol(p.order_notional)} · 换手 ${fmt.pct(p.turnover_frac)}</div></div>
    <div class="cell"><div class="k">信号覆盖率</div><div class="v">${fmt.pct(p.coverage)}</div>
      <div class="s">权重误差 ${fmt.n(p.weight_err, 5)}</div></div>
  </div>`;

  if (p.turnover_scale < 0.999) {
    h += `<div class="banner info" style="margin-top:11px"><div class="ico">i</div><div>
      <b>本次只执行目标变动的 ${esc(fmt.pct(p.turnover_scale))}</b>
      <p>目标变动需换手 ${esc(fmt.n(p.turnover_wanted, 2))}× 净值，而策略每期预算只有 ${esc(fmt.pct(p.turnover_budget, 0))}。
      这不是 bug：回测里 v3 的<b>实际持仓毛敞口均值 0.472</b>，而原始目标均值 <b>1.032</b>——
      报告的 Sharpe 1.937 来自被预算拖住的账。照原始目标下单会让敞口变成 2.2 倍。</p></div></div>`;
  }
  for (const w of (p.warnings || [])) {
    h += `<div class="gate warn" style="margin-top:7px"><span class="ico2">⚠</span><div>${w}</div></div>`;
  }

  if (p.n_orders) {
    const sel = L.sel || {};
    const allOn = p.orders.every((o) => sel[o.instId] !== false);
    h += `<table class="ordtable" style="margin-top:12px"><thead><tr>
      <th><input type="checkbox" id="lvAll" ${allOn ? 'checked' : ''}></th>
      <th>合约</th><th>方向</th><th>动作</th><th>现有张</th><th>目标张</th>
      <th>下单张</th><th>名义额</th><th>参考价</th><th>最小下单额</th>
      ${Object.keys(res).length ? '<th>结果</th>' : ''}</tr></thead><tbody>`;
    for (const o of p.orders) {
      const on = sel[o.instId] !== false;
      const cls = o.side === 'buy' ? 'long' : 'short';
      const r = res[o.instId];
      h += `<tr class="${on ? '' : 'off'}">
        <td><input type="checkbox" data-lord="${esc(o.instId)}" ${on ? 'checked' : ''}></td>
        <td class="mono">${esc(o.instId)}</td>
        <td class="${cls}">${o.side === 'buy' ? '买' : '卖'}</td>
        <td>${esc(o.action)}</td>
        <td class="num">${fmt.n(o.cur_sz, 5)}</td>
        <td class="num">${fmt.n(o.tgt_sz, 5)}</td>
        <td class="num"><b>${fmt.n(o.sz, 5)}</b></td>
        <td class="num">${fmt.dol(o.delta_notional)}</td>
        <td class="num">${fmt.n(o.price, 6)}</td>
        <td class="num muted">${fmt.n(o.min_notional, 3)}</td>
        ${Object.keys(res).length ? `<td class="num">${!r ? '<span class="muted">—</span>'
          : (r.ok ? '<span style="color:var(--ok)">✓</span>'
            : `<span style="color:var(--danger)" title="${esc(r.error || '')}">✗</span>`)}</td>` : ''}
      </tr>`;
    }
    h += `</tbody></table>`;
  }
  if ((p.skips || []).length) {
    h += `<p class="desc" style="margin:11px 0 0"><b>被跳过的目标腿（${p.skips.length}）</b>：`;
    h += p.skips.slice(0, 12).map((s) =>
      `<span class="chip" title="${esc(s.detail)}">${esc(s.instId)} · ${esc(s.reason)}</span>`).join(' ');
    h += `</p>`;
  }

  /* --- 执行区 --- */
  const mode = L.mode;
  const phrase = pl.confirm_phrase || (L.status && L.status.confirm_phrase);
  h += `<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:12px">`;
  if (mode === 'live') {
    h += `<div class="confirmbox"><b>实盘：这一步会用真实资金下单</b>
      <p style="margin:6px 0 8px">请输入确认短语 <span class="mono">${esc(phrase || '')}</span>：</p>
      <div class="split">
        <input class="liveinput" id="lvConfirm" placeholder="${esc(phrase || '')}" value="${esc(L.confirm || '')}" style="min-width:230px">
        <button class="btn danger" id="liveGo">确认下单</button>
        <button class="btn ghost" id="liveDry">仅预演</button>
      </div></div>`;
  } else {
    h += `<div class="split">
      <button class="btn ${mode === 'demo' ? 'primary' : 'primary'}" id="liveGo" ${blocked || !p.n_orders ? 'disabled' : ''}>
        ${mode === 'demo' ? '向 OKX 模拟盘下单' : '执行调仓（本地模拟成交）'}</button>
      <button class="btn ghost" id="liveDry">仅预演（不发单）</button>
      <span class="muted" style="font-size:11.5px">${mode === 'demo'
        ? '订单会真的送进 OKX 模拟撮合，但用的是模拟资金。' : '只在本地账本记账，不联网下单。'}</span>
    </div>`;
  }
  h += `<div class="split" style="margin-top:9px">
      <label class="split" style="gap:5px;font-size:12.5px">
        <input type="checkbox" id="lvForce" ${L.force ? 'checked' : ''}> 强制（非调仓日也执行；不会放宽任何风控上限）</label>
      <span class="spacer"></span>
      <button class="btn sm ghost" id="liveReconcile">对账</button>
      <button class="btn sm ghost" id="liveFlatDry">清仓预演</button>
      <button class="btn sm ghost" id="liveFlat" style="color:var(--danger);border-color:var(--danger)">清仓</button>
    </div>`;
  h += `</div>`;
  return h;
}

function liveJobHtml() {
  const L = S.live;
  const j = L.job;
  if (!j) return `<div class="empty">还没有执行记录</div>`;
  const run = j.status === 'running' || j.status === 'queued';
  let h = `<div class="split" style="margin-bottom:8px">
    <span class="pill ${j.status === 'done' ? 'ok' : (j.status === 'failed' ? 'danger' : 'accent')}">
      ${run ? '<span class="spin"></span> ' : ''}${esc(j.status)}</span>
    <span class="muted" style="font-size:11.5px">${esc(j.label || j.kind)}${j.elapsed ? ' · ' + j.elapsed + 's' : ''}</span>
    <span class="spacer"></span>
    <button class="btn sm ghost" id="liveJobClear">清空</button></div>`;
  h += `<div class="joblog" id="liveJobLog">` + (j.log || []).map((l) =>
    `<span class="t">${new Date(l.t * 1000).toLocaleTimeString('zh-CN')}</span>${esc(l.msg)}`).join('\n')
    + `</div>`;
  if (j.error) h += `<div class="gate block" style="margin-top:8px"><span class="ico2">⛔</span>
    <div><b>${esc(j.error)}</b>${j.trace ? `<pre class="mono" style="max-height:150px;overflow:auto;margin:5px 0 0">${esc(j.trace)}</pre>` : ''}</div></div>`;
  /* 一个卡住的任务和一个只是在等网络的任务长得一模一样，所以这里必须给出延迟数字 */
  const net = (j.result && j.result.net) || null;
  if (net && net.samples) {
    h += liveNetHtml(net);
    if (run) h += `<div class="empty" style="padding:6px 0 0">仍在等待交易所回报。
      若为 demo/live 且半天没动静，看上面的请求次数与延迟：慢在交易所就只是慢，
      次数多而没进展则说明卡在某个接口上。</div>`;
  }
  return h;
}

function liveHistHtml() {
  const L = S.live;
  if (L.sub === 'limits') return liveLimitsHtml();
  const key = L.sub === 'fills' ? 'fills' : (L.sub === 'runs' ? 'runs' : 'orders');
  const rows = L[key];
  if (rows == null) return `<div class="empty">正在读取…</div>`;
  if (!rows.length) return `<div class="empty">暂无记录</div>`;
  if (key === 'orders') {
    let h = `<table class="ordtable"><thead><tr><th>时间</th><th>合约</th><th>方向</th>
      <th>动作</th><th>张数</th><th>名义额</th><th>状态</th><th>clOrdId</th></tr></thead><tbody>`;
    for (const r of rows) {
      h += `<tr><td class="mono">${r.ts ? new Date(r.ts * 1000).toLocaleString('zh-CN') : '—'}</td>
        <td class="mono">${esc(r.instId)}</td>
        <td class="${r.side === 'buy' ? 'long' : 'short'}">${r.side === 'buy' ? '买' : '卖'}</td>
        <td>${esc(r.action || '')}</td><td class="num">${fmt.n(r.sz, 5)}</td>
        <td class="num">${fmt.dol(r.notional)}</td>
        <td>${esc(r.state || '')}${r.error ? ` <span class="muted" title="${esc(r.error)}">!</span>` : ''}</td>
        <td class="mono muted">${esc(r.clOrdId || '')}</td></tr>`;
    }
    return h + `</tbody></table>`;
  }
  if (key === 'fills') {
    let h = `<table class="ordtable"><thead><tr><th>时间</th><th>合约</th><th>方向</th>
      <th>成交价</th><th>张数</th><th>名义额</th><th>费用</th></tr></thead><tbody>`;
    for (const r of rows) {
      const not = (r.notional != null && isFinite(r.notional)) ? Number(r.notional) : null;
      h += `<tr><td class="mono">${r.ts ? new Date(r.ts * 1000).toLocaleString('zh-CN') : '—'}</td>
        <td class="mono">${esc(r.instId)}</td>
        <td class="${r.side === 'buy' ? 'long' : 'short'}">${r.side === 'buy' ? '买' : '卖'}</td>
        <td class="num">${fmt.n(r.px, 6)}</td><td class="num">${fmt.n(r.sz, 5)}</td>
        <td class="num">${not == null ? '<span class="muted" title="缺 ctVal，不估算">—</span>' : fmt.dol(not)}</td>
        <td class="num">${fmt.n(r.fee, 4)}</td></tr>`;
    }
    return h + `</tbody></table>`;
  }
  let h = `<table class="ordtable"><thead><tr><th>时间</th><th>动作</th><th>净值</th>
    <th>订单</th><th>名义额</th><th>覆盖率</th><th>权重误差</th><th>拦截</th></tr></thead><tbody>`;
  for (const r of rows) {
    h += `<tr><td class="mono">${r.ts ? new Date(r.ts * 1000).toLocaleString('zh-CN') : '—'}</td>
      <td>${esc(r.action || '')}${r.dry_run ? ' <span class="muted">(预演)</span>' : ''}</td>
      <td class="num">${fmt.dol(r.nav)}</td>
      <td class="num">${r.n_ok}/${r.n_orders}</td>
      <td class="num">${fmt.dol(r.order_notional)}</td>
      <td class="num">${fmt.pct(r.coverage)}</td>
      <td class="num">${fmt.n(r.weight_err, 5)}</td>
      <td>${(r.violations || []).filter((v) => v.sev === 'block').length || '—'}</td></tr>`;
  }
  return h + `</tbody></table>`;
}

/* 杠杆单独一块，因为它是最容易被误用的旋钮。
 * 关键区别：**账户杠杆 ≠ 收益倍数**。敞口由目标权重与换手预算决定，与杠杆无关；
 * 只调高杠杆而不动敞口，等于把维持保证金压到 1/L —— 只增加爆仓风险，不增加预期收益。 */
function liveLeverageHtml(lim) {
  const L = S.live;
  const st = L.status || {};
  const cur = st.leverage;                     // 将要请求的杠杆（null = 不改）
  const cap = (lim.max_leverage != null) ? lim.max_leverage : (st.leverage_cap);
  const td = st.td_mode || 'cross';
  let h = `<div class="split" style="margin-top:12px;border-top:1px solid var(--border);padding-top:11px">
      <div><b style="font-size:12.5px">保证金模式</b></div>
      <select id="lvTdMode" class="liveinput" style="width:150px">
        <option value="cross"${td === 'cross' ? ' selected' : ''}>全仓 cross</option>
        <option value="isolated"${td === 'isolated' ? ' selected' : ''}>逐仓 isolated</option>
      </select>
    </div>
    <div class="split" style="margin-top:8px">
      <div><b style="font-size:12.5px">账户杠杆</b>
        <div class="muted" style="font-size:11px">留空 = 不改交易所现有设置</div></div>
      <input class="liveinput" id="lvLever" placeholder="如 5" value="${cur == null ? '' : esc(String(cur))}" style="width:110px;text-align:right">
    </div>
    <p class="desc" style="margin:8px 0 0">
      上限 <b>${esc(cap == null ? '—' : String(cap))}×</b>（可在上表「账户杠杆上限」调整）。
      杠杆按<b>逐个合约</b>设置，且会被交易所对该合约的上限压低——
      每次下单的结果会写进运行记录，设置失败会明确提示，不会静默跳过。</p>
    <div class="banner warn" style="margin-top:9px"><div class="ico">!</div><div>
      <b>提高杠杆不放大收益</b>
      <p>敞口由目标权重与换手预算决定（毛敞口约 0.47× 净值、每期换手 20%），
      <b>与账户杠杆无关</b>。只把杠杆从 1× 调到 5× 而敞口不变，
      收益完全不变，只是维持保证金降到 1/5 —— <b>等于用零预期收益换来更高的爆仓风险</b>。</p>
      <p style="margin-top:5px">真正想放大收益，要调的是<b>毛敞口上限</b>
      （上表「总敞口上限」，当前 ${esc(lim.max_gross_frac == null ? '—' : String(lim.max_gross_frac))}× 净值），
      而回撤会按同一倍数放大：报告 §16 实测 2× → MDD −24.0%，
      <b>4× → −42.9%</b>，10× → −77.2%，且交易成本同步放大（4× 时 8.3%/yr）。</p>
    </div></div>`;
  return h;
}

function liveLimitsHtml() {
  const L = S.live;
  const lim = L.limits || (L.status && L.status.limits) || {};
  let h = `<p class="desc" style="margin:0 0 10px">绝对金额上限是唯一能挡住「连错账户」的闸门——
    比例上限会随本金一起放大。</p>
    <table class="ordtable"><thead><tr><th>限额</th><th>值</th><th>说明</th></tr></thead><tbody>`;
  for (const [k, label, help] of LIVE_LIMIT_FIELDS) {
    const v = lim[k];
    h += `<tr><td>${esc(label)}</td>
      <td><input class="liveinput" data-limit="${esc(k)}" value="${v == null ? '' : esc(String(v))}" style="width:130px;text-align:right"></td>
      <td class="muted" style="text-align:left">${esc(help)}</td></tr>`;
  }
  h += `</tbody></table>
    <div class="split" style="margin-top:11px">
      <button class="btn sm primary" id="liveLimitsSave">保存限额</button>
      <label class="split" style="gap:5px;font-size:12.5px">
        <input type="checkbox" id="lvRequireDue" ${lim.require_rebalance_due ? 'checked' : ''}> 仅调仓日允许下单</label>
    </div>
    ${liveLeverageHtml(lim)}
    <p class="desc" style="margin:9px 0 0">重置会计<b>归档</b>本地账本（<span class="mono">archive/&lt;时间戳&gt;/</span>），
      不删除任何东西——账本是证据。只保留最近 10 份快照，只影响 <span class="mono">paper</span> 模式。</p>
    <div class="split" style="margin-top:7px">
      <input class="liveinput" id="lvPaperNav" placeholder="纸面本金 USD" value="1000" style="width:140px">
      <button class="btn sm ghost" id="liveReset">重置纸面账户</button>
      ${(L.status && (L.status.archives || []).length) ? `<span class="muted" style="font-size:11.5px">已归档 ${L.status.archives.length} 份</span>` : ''}
    </div>`;
  return h;
}

/* ---- 事件绑定 ---- */
function liveBind() {
  const L = S.live;
  $$('[data-lmode]').forEach((b) => b.onclick = async () => {
    if (L.mode === b.dataset.lmode) return;
    L.mode = b.dataset.lmode;
    L.plan = null; L.sel = {}; L.job = null; L.orders = null; L.fills = null; L.runs = null;
    L.limits = null; L.credsOpen = false;
    await liveLoadStatus();
    await liveLoadHist();
    livePaintAll();
    renderContent();
  });
  const t = $('#liveCredsToggle');
  if (t) t.onclick = () => { L.credsOpen = !L.credsOpen; livePaintAll(); };
  const sv = $('#liveCredsSave');
  if (sv) sv.onclick = liveSaveCreds;
  const dl = $('#liveCredsDel');
  if (dl) dl.onclick = liveDelCreds;
  const k = $('#liveKill');
  if (k) k.onclick = () => liveKill(!((L.status || {}).kill_switch));
  const rd = $('#liveRefreshData');
  if (rd) rd.onclick = liveRefreshData;
  const pb = $('#livePlanBtn');
  if (pb) pb.onclick = () => livePlan(false);
  const pf = $('#livePlanFresh');
  if (pf) pf.onclick = () => livePlan(true);
  const go = $('#liveGo');
  if (go) go.onclick = () => liveExecute(false);
  const dry = $('#liveDry');
  if (dry) dry.onclick = () => liveExecute(true);
  const fr = $('#lvForce');
  if (fr) fr.onchange = () => { L.force = fr.checked; };
  const cf = $('#lvConfirm');
  if (cf) cf.oninput = () => { L.confirm = cf.value; };
  const rc = $('#liveReconcile');
  if (rc) rc.onclick = liveReconcile;
  const fd = $('#liveFlatDry');
  if (fd) fd.onclick = () => liveFlatten(true);
  const fl = $('#liveFlat');
  if (fl) fl.onclick = () => liveFlatten(false);
  const jc = $('#liveJobClear');
  if (jc) jc.onclick = () => { L.job = null; livePaintAll(); };
  const all = $('#lvAll');
  if (all) all.onchange = () => {
    const on = all.checked;
    const p = L.plan && L.plan.plan;
    if (p) for (const o of p.orders) L.sel[o.instId] = on;
    livePaintAll();
  };
  $$('[data-lord]').forEach((c) => c.onchange = () => {
    L.sel[c.dataset.lord] = c.checked;
    livePaintAll();
  });
  $$('[data-lsub]').forEach((b) => b.onclick = async () => {
    if (L.sub === b.dataset.lsub) return;
    L.sub = b.dataset.lsub;
    await liveLoadHist();
    livePaintAll();
  });
  const ls = $('#liveLimitsSave');
  if (ls) ls.onclick = liveSaveLimits;
  const lrd = $('#lvRequireDue');
  if (lrd) lrd.onchange = () => liveSaveLimits(lrd.checked);
  const ltm = $('#lvTdMode');
  if (ltm) ltm.onchange = () => {
    S.live.status = Object.assign({}, S.live.status || {}, { td_mode: ltm.value });
    S.live.note = `保证金模式改为 ${ltm.value === 'cross' ? '全仓' : '逐仓'}，下次生成计划/执行时随请求发出`;
    livePaintAll();
  };
  const llv = $('#lvLever');
  if (llv) llv.onchange = () => {
    const v = String(llv.value || '').trim();
    const f = v === '' ? null : Number(v);
    S.live.status = Object.assign({}, S.live.status || {}, { leverage: f });
    const cap = (S.live.limits || (S.live.status && S.live.status.limits) || {}).max_leverage;
    S.live.note = (f == null) ? '已清空杠杆设置：不动交易所现有杠杆'
      : (!isFinite(f) || f < 1) ? '杠杆必须 ≥ 1，已忽略'
      : (cap != null && f > cap) ? `杠杆 ${f}× 超过上限 ${cap}×，下单时会被闸门拦截`
      : `杠杆设为 ${f}×，下次生成计划/执行时随请求发出（注意：这不放大收益，见下方说明）`;
    livePaintAll();
  };
  const rs = $('#liveReset');
  if (rs) rs.onclick = liveReset;
  const ex = $('#liveExport');
  if (ex) ex.onclick = liveExport;
}

async function liveLoadHist() {
  const L = S.live;
  if (L.sub === 'limits') return;
  const kind = L.sub;
  try {
    const d = await liveApi(`/api/live/${kind}?mode=${encodeURIComponent(L.mode)}&limit=300`);
    if (!d || !Array.isArray(d.rows)) throw new Error('返回结构不对');
    L[kind] = d.rows;
  } catch (e) {
    L[kind] = [];
    L.statusErr = String(e && e.message || e);
  }
}

/* ---- 动作 ---- */
async function liveSubmit(url, body, label) {
  const L = S.live;
  if (L.busy) return;
  L.busy = true; L.note = label || '';
  livePaintAll();
  try {
    const job = await liveApi(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (job.error) throw new Error(job.error);
    L.job = job;
    livePaintAll();
    pollLiveJob(job.id);
  } catch (e) {
    L.busy = false;
    L.job = { kind: 'error', status: 'failed', label: label || '',
      error: String(e && e.message || e), log: [], elapsed: 0 };
    livePaintAll();
  }
}

function pollLiveJob(id) {
  const L = S.live;
  if (L.jobTimer) clearInterval(L.jobTimer);
  L.jobTimer = setInterval(async () => {
    let j = null;
    try { j = await liveApi('/api/live/job/' + encodeURIComponent(id)); } catch (e) { return; }
    if (!j || j.error) return;
    L.job = j;
    // 只重画日志区，避免把整个交易台重建掉（会丢输入焦点）
    const el = document.getElementById('liveJob');
    if (el) el.innerHTML = liveJobHtml();
    const jm = document.getElementById('liveJobMeta');
    if (jm) jm.textContent = `${j.kind} · ${j.status}${j.elapsed ? ' · ' + j.elapsed + 's' : ''}`;
    const jc = document.getElementById('liveJobClear');
    if (jc) jc.onclick = () => { L.job = null; livePaintAll(); };
    if (j.status === 'done' || j.status === 'failed') {
      clearInterval(L.jobTimer); L.jobTimer = null;
      L.busy = false;
      if (j.status === 'done' && j.result) {
        if (j.result.plan) L.plan = j.result;
        if (j.result.mode && j.mode === 'paper' && j.kind === 'plan') L.sel = {};
      }
      await liveLoadStatus();
      await liveLoadHist();
      livePaintAll();
      if (j.status === 'failed') L.note = j.error || '失败';
    }
  }, 1200);
}

/* 保证金模式与杠杆必须随每一次下单请求一起发出去，不能只在「保存限额」时发。
 * 否则你在页面上把杠杆设成 5×、点了执行，请求里却根本没带这个字段 ——
 * 引擎按「不改杠杆」下单，页面却显示 5×。这是「UI 说一套、请求发另一套」。 */
function liveTradeSettings() {
  const L = S.live;
  const td = $('#lvTdMode');
  const lv = $('#lvLever');
  const out = {};
  if (td && td.value) out.td_mode = td.value;
  if (lv) {
    const v = String(lv.value || '').trim();
    if (v !== '') { const f = Number(v); if (isFinite(f) && f >= 1) out.leverage = f; }
  } else if (L.status && L.status.leverage != null) {
    out.leverage = L.status.leverage;      // 控件不在 DOM 里时以服务端为准
  }
  return out;
}

function livePlan(fresh) {
  const L = S.live;
  liveSubmit('/api/live/plan',
    Object.assign({ mode: L.mode, fresh: !!fresh, limits: L.limits },
                  liveTradeSettings()),
    fresh ? '强制重算信号' : '生成下单计划');
}

function liveSelectedOnly() {
  const L = S.live;
  const p = L.plan && L.plan.plan;
  if (!p || !p.orders) return null;
  const ids = p.orders.map((o) => o.instId);
  const chosen = ids.filter((i) => L.sel[i] !== false);
  if (chosen.length === ids.length) return null;   // 全选 -> 不传过滤
  return chosen;
}

function liveExecute(dry) {
  const L = S.live;
  if (L.mode === 'live' && !dry) {
    const want = (L.plan && L.plan.confirm_phrase) || (L.status || {}).confirm_phrase;
    if ((L.confirm || '').trim() !== want) {
      L.note = `确认短语不正确，需要输入 ${want}`;
      livePaintAll();
      return;
    }
  }
  liveSubmit('/api/live/execute', Object.assign({
    mode: L.mode, dry_run: !!dry, confirm: L.confirm, force: L.force,
    only: liveSelectedOnly(), limits: L.limits,
  }, liveTradeSettings()), dry ? '预演下单' : '执行下单');
}

function liveFlatten(dry) {
  const L = S.live;
  if (L.mode === 'live' && !dry) {
    const want = (L.status || {}).confirm_phrase;
    if ((L.confirm || '').trim() !== want) {
      L.note = '实盘清仓也需要输入当日确认短语';
      livePaintAll();
      return;
    }
  }
  liveSubmit('/api/live/flatten',
    Object.assign({ mode: L.mode, dry_run: !!dry, confirm: L.confirm, limits: L.limits },
                  liveTradeSettings()),
    dry ? '清仓预演' : '清仓');
}

function liveReconcile() {
  liveSubmit('/api/live/reconcile', { mode: S.live.mode }, '对账');
}

function liveRefreshData() {
  liveSubmit('/api/live/refresh-data', { bar: '1h' }, '刷新行情数据');
}

async function liveSaveCreds() {
  const L = S.live;
  const k = ($('#lvKey') || {}).value || '';
  const s = ($('#lvSecret') || {}).value || '';
  const p = ($('#lvPass') || {}).value || '';
  if (!k || !s || !p) { L.note = '三项都要填'; livePaintAll(); return; }
  try {
    const d = await liveApi('/api/live/creds', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: L.mode, api_key: k, secret_key: s, passphrase: p }),
    });
    if (d.error) throw new Error(d.error);
    L.credsOpen = false;
    L.note = '密钥已保存到本机 config/okx_creds.json';
    await liveLoadStatus();
    livePaintAll();
  } catch (e) {
    L.note = String(e && e.message || e);
    livePaintAll();
  }
}

async function liveDelCreds() {
  const L = S.live;
  try {
    const d = await liveApi('/api/live/creds/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: L.mode }),
    });
    if (d.error) throw new Error(d.error);
    L.note = '本机密钥已删除';
    await liveLoadStatus();
    livePaintAll();
  } catch (e) { L.note = String(e && e.message || e); livePaintAll(); }
}

async function liveKill(on) {
  const L = S.live;
  try {
    const d = await liveApi('/api/live/kill', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: !!on }),
    });
    if (d.error) throw new Error(d.error);
    // 服务端回报的是**实际状态**，不是请求意图：删除锁文件可能被宿主的安全机制拒绝。
    L.note = (d.note) ? d.note
      : (d.kill_switch ? '已熔断：所有下单会被拒绝' : '熔断已解除');
    await liveLoadStatus();
    livePaintAll();
    renderContent();
  } catch (e) { L.note = String(e && e.message || e); livePaintAll(); }
}

async function liveSaveLimits(requireDue) {
  const L = S.live;
  const lim = Object.assign({}, L.limits || {});
  $$('[data-limit]').forEach((el) => {
    const k = el.dataset.limit;
    const v = el.value.trim();
    lim[k] = (v === '') ? null : Number(v);
    if (v !== '' && !isFinite(lim[k])) lim[k] = L.limits ? L.limits[k] : null;
  });
  if (requireDue != null) lim.require_rebalance_due = !!requireDue;
  L.limits = lim; L.limitsTouched = true;
  try {
    const d = await liveApi('/api/live/limits', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: L.mode, limits: lim }),
    });
    if (d.error) throw new Error(d.error);
    L.note = '限额已生效';
    await liveLoadStatus();
  } catch (e) { L.note = String(e && e.message || e); }
  livePaintAll();
}

async function liveReset() {
  const L = S.live;
  const nav = Number((($('#lvPaperNav') || {}).value) || 1000);
  try {
    const d = await liveApi('/api/live/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: L.mode, nav: isFinite(nav) ? nav : 1000 }),
    });
    if (d.error) throw new Error(d.error);
    L.plan = null; L.sel = {}; L.orders = null; L.fills = null; L.runs = null;
    L.note = `已重置 ${L.mode} 模式` + (d.archived
      ? `；旧账本已归档（未删除）：${d.archived}` : '（本来就没有账本文件）');
    await liveLoadStatus();
    await liveLoadHist();
    livePaintAll();
  } catch (e) { L.note = String(e && e.message || e); livePaintAll(); }
}

function liveExport() {
  const L = S.live;
  const rows = L[L.sub === 'limits' ? 'orders' : L.sub] || [];
  if (!rows.length) { L.note = '没有可导出的记录'; livePaintAll(); return; }
  const cols = Array.from(rows.reduce((s, r) => {
    Object.keys(r).forEach((k) => s.add(k));
    return s;
  }, new Set()));
  const esc2 = (v) => {
    const s = (v == null) ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const csv = [cols.join(',')].concat(
    rows.map((r) => cols.map((c) => esc2(r[c])).join(','))).join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `live_${L.mode}_${L.sub}.csv`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

boot();
