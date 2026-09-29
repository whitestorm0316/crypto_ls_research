



  ;

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ?    : s).replace(  g, (c) =>
  ({   :   ,   :   ,   :   ,   :   ,   :    }[c]));
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));


const OPT = {
  cli: { bar:   , rebalance_days: 3, asset_class:    },
  ov: {
      : [  ,   ],
      : 0.20,
      : 0.20,
  },
};

const S = {
  spec: null, presets: [], bundles: [], lib: {}, baseline: null,
  cli: {}, ov: {}, stages: [  ,   ], bundleId:   , preset:   ,
  warnings: [], runs: [], active: null, queued: [],
  selected: null, detail: null, logOffset: 0, logTimer: null, chartTimer: null,
  equity: {}, equityErr: {},
  
  tab:   , tr: null, trErr: null, trTag: null,
  fills: null, fillsFilt: {}, fillsPage: 0, blotter: null, blotterPage: 0,
  attrSort:   ,
};


const fmt = {
  n: (v, d = 3) => (v == null || !isFinite(v)) ?    : Number(v).toFixed(d),
  n1: (v) => (v == null || !isFinite(v)) ?    : Number(v).toFixed(1),
  pct: (v, d = 2) => (v == null || !isFinite(v)) ?    : (Number(v) * 100).toFixed(d) +   ,
  sci: (v) => (v == null || !isFinite(v)) ?    : Number(v).toExponential(2),
  pp: (v, d = 2) => (v == null || !isFinite(v)) ?    : (v >= 0 ?    :   ) + (Number(v) * 100).toFixed(d) +   ,
  sgn: (v, d = 3) => (v == null || !isFinite(v)) ?    : (v >= 0 ?    :   ) + Number(v).toFixed(d),
  usd: (v) => {
    if (v == null || !isFinite(v)) return   ;
    const a = Math.abs(v);
    if (a >= 1e9) return    + (v / 1e9).toFixed(2) +   ;
    if (a >= 1e6) return    + (v / 1e6).toFixed(2) +   ;
    if (a >= 1e3) return    + (v / 1e3).toFixed(1) +   ;
    if (a >= 1) return    + v.toFixed(0);
    return    + v.toFixed(2);
  },
  dol: (v) => (v == null || !isFinite(v)) ?    :    + Math.round(v).toLocaleString(  ),
  int: (v) => (v == null || !isFinite(v)) ?    : Math.round(v).toLocaleString(  ),
  val(v, k) {
    if (v == null) return   ;
    if (k ===   ) return fmt.pct(v);
    if (k ===   ) return fmt.n1(v);
    if (k ===   ) return fmt.sci(v);
    return fmt.n(v);
  },
};


async function boot() {
  try {
    const r = await fetch(  );
    const d = await r.json();
    S.spec = d.spec; S.presets = d.presets; S.bundles = d.bundles;
    S.lib = d.lib_defaults || {}; S.baseline = d.baseline; S.optimalTag = d.optimal_tag;
  } catch (e) {
    $(  ).innerHTML =  
 ;
    return;
  }
  applyPreset(  );
  
  const wantTrades = location.hash ===   ;
  if (wantTrades) S.tab =   ;
  try {
    renderSide();
    renderContent();
    refreshWarnings();
    await pollRuns();
    setInterval(pollRuns, 2500);
    window.addEventListener(  , debounce(() => drawChart(), 180));
    if (wantTrades) {
      ensureTrades();
    } else {
      
      const last = S.runs.find((r) => r.status ===    || r.status ===   );
      if (last) await selectRun(last.id);
    }
  } catch (e) {
    fatal(   + ((e && e.stack) || e));
  }
}


function defaults() {
  const cli = {}, ov = {};
  for (const it of (S.spec.global || [])) {
    if (it.key.startsWith(  )) cli[it.key.slice(5)] = it.default;
  }
  for (const g of S.spec.groups) for (const it of g.items) ov[it.key] = clone(it.default);
  return { cli, ov };
}
const clone = (v) => (Array.isArray(v) ? v.slice() : (v && typeof v ===    ? JSON.parse(JSON.stringify(v)) : v));

function applyPreset(id) {
  const p = S.presets.find((x) => x.id === id);
  const d = defaults();
  S.cli = Object.assign({}, d.cli, p ? (p.cli || {}) : {});
  S.ov = Object.assign({}, d.ov, p ? (p.overrides || {}) : {});
  S.preset = id;
}


function renderSide() {
  const el = $(  );
  let h =   ;

  
  h +=  

 ;
  for (const p of S.presets) {
    h +=  
  <span class=  >${esc(p.badge)}<  b>
        <small>${esc(p.desc)}<  button> 

 < 
    <p class=   style=  >选中预设只会改参数，不会自动开跑 —— 先看告警，再点右上角运行。< 
    <  div> 


 <div class=  ><div class=  ><h3>运行范围<  div><div class=  >
      <div class=   style=  > 

 <button class=   data-bundle=  >
        <b>${esc(b.label)} <span class=  >${esc(b.eta)}<  b>
        <small>${esc(b.desc)}<br><span class=  >${esc(b.stages.join(  ))}<  small>< 
  }
  h +=   ;

  
  h +=   ;
  h += fieldRows((S.spec.global || []).filter((i) => i.level ===   ),   );
  h +=   ;
  h += fieldRows((S.spec.global || []).filter((i) => i.level ===   ),   );
  h +=   ;

  
  for (const g of S.spec.groups) {
    const core = g.items.filter((i) => i.level ===   );
    const adv = g.items.filter((i) => i.level ===   );
    const isFac = g.id ===   ;
    h +=  

 ;
    h +=   ;
    if (isFac) h += factorPicker(g);
    h += fieldRows(core.filter((i) => i.type !==   ),   );
    h +=   ;
    if (adv.length) {
      h +=   
        + fieldRows(adv,   ) +   ;
    }
    h +=   ;
  }

  el.innerHTML = h;
  bindSide();
  updateOptPill();
}

function factorPicker(g) {
  const item = g.items.find((i) => i.key ===   );
  const sel = S.ov[  ] || [];
  let h =   ;
  for (const f of item.options) {
    const on = sel.includes(f);
    const banned = f ===   ;
    h +=  
 ;
  }
  h +=  
 <br>当前子集：<b>${esc(sel.join(  ))}<  b> 





 cli 
  

 cli 



 changed    trap   


 select 

 selected     

 bool 

 selected   
 selected   

 step=   
  
  
 pct  number 
 text  text  number 
  



 subset 







 [data-preset] 


 [data-bundle] 




 [data-factor] 

 factors.subset 
 factors.subset 
 custom 


 [data-key] 
 SELECT  change  input 



 pct  number 
 number 
 SELECT  true  false  true 
 cli  rebalance_days  capital 
 cli 
 custom 








 .field[data-k] 

 #side [data-key] 

 cli 
 cli 
 .field  changed 









 /api/check 
 POST  Content-Type  application/json 






 #side [data-key] 

 cli  _cli. 
 .field  trap 








 --  _  - 




 #optPill 



 pill ok 
 已验收最优 v3 ✓ 
 Sharpe 1.937 / CAGR 25.09%  
  } else {
    el.className =   ;
    el.textContent =   ;
    el.title =   ;
  }
}


function fatal(msg) {
  const c = $(  );
  if (!c) return;
  c.insertAdjacentHTML(  ,
    banner(  ,   ,   , pre_escape(msg)));
  const p = $(  );
  if (p) { p.className =   ; p.textContent =   ; }
}
const pre_escape = (s) =>   ;
window.addEventListener(  , (e) => fatal((e.message ||   ) +    + (e.filename ||   ) +    + (e.lineno || 0)));
window.addEventListener(  , (e) => fatal(   + (e.reason && e.reason.message || e.reason)));


function renderContent() {
  const el = $(  );
  let h =   ;

  const diffs = diffFromOptimal();
  if (!diffs.length) {
    h += banner(  ,   ,   ,
       

 );
  } else {
    h += banner(  ,   ,   ,
         +
      diffs.map((d) =>   ).join(  )
      +   );
  }

  for (const w of S.warnings) {
    h += banner(w.sev ===    ?    :   ,   , w.title, w.body);
  }

  const d = S.detail;
  if (d && (d.stage_errors || []).length) {
    h += banner(  ,   ,   ,
        
      +   );
  }
  h +=   ;

  h += tabBar();
  h +=   ;

  
  const r = d && d.result;
  const b = S.baseline;
  const KPI = [
    [  , r && r.sharpe, b && b.sharpe,   , d && d.delta && d.delta.sharpe],
    [  , r && r.cagr, b && b.cagr,   , d && d.delta && d.delta.cagr],
    [  , r && r.mdd, b && b.mdd,   , d && d.delta && d.delta.mdd],
  ];
  const rest = r ? r.metrics.filter((m) => [  ,   ,   ,   ,   ,   ,   ,   ].includes(m.label)) : [];
  h +=   ;
  h +=   ;
  for (const [k, v, bv, f, dl] of KPI) {
    h += kpiCard(k, fmt.val(v, f), dl, f ===    ?    : f);
  }
  for (const m of rest) h += kpiCard(m.label, fmt.val(m.value, m.fmt), null,   );
  h +=   ;

  
  h +=  





 ;
  h +=   ;                       

  h +=   ;

  
  h +=   ;
  h +=   ;
  if (!S.runs.length) h +=   ;
  for (const run of S.runs) {
    const su = run.summary || {};
    h +=  

  · <span style=  >${run.n_stage_errors} 个 stage 报错<  small>< 
      <div class=  >${su.sharpe != null ?    + fmt.n(su.sharpe) :   }< 
      <div class=  >${su.cagr != null ?    + fmt.pct(su.cagr) :   }< 
      <div class=  ><span class=  ><  div>
    < 
  }
  h +=   ;

  
  if (d) {
    h +=  


 ;
  }

  
  if (d && d.argv) {
    h +=  

 ;
  }

  el.innerHTML = h;
  
  
  el.classList.toggle(  , S.tab ===   );

  const bo = $(  );
  if (bo) bo.onclick = () => { applyPreset(  ); renderSide(); renderContent(); refreshWarnings(); };
  $$(  ).forEach((row) => row.onclick = () => selectRun(row.dataset.run));
  $$(  ).forEach((t) => t.onclick = () => {
    if (S.tab === t.dataset.tab) return;
    S.tab = t.dataset.tab;
    try { history.replaceState(null,   , S.tab ===    ?    :   ); } catch { }
    renderContent();
    if (S.tab ===   ) ensureTrades();
  });

  const note = $(  );
  if (note && r) note.textContent =   ;
  if (S.tab !==   ) drawChart();
  if (d) { const lg = $(  ); if (lg) lg.innerHTML = logHtml(d._log ||   ); startLog(d); }
  if (S.tab ===   ) bindTrBody();
}

function cmdText(d) {
  const q = (s) => (  .test(String(s)) ?    : String(s));
  return    + (d.argv || []).map(q).join(  );
}

function kpiCard(k, v, delta, unit) {
  let dl =   ;
  if (delta != null && isFinite(delta)) {
    const cls = Math.abs(delta) < 1e-9 ?    : (delta > 0 ?    :   );
    const t = unit ===    ? fmt.pp(delta) : fmt.sgn(delta);
    dl =   ;
  }
  return   ;
}

function yearTable(d) {
  const r = d && d.result;
  const by = (S.baseline && S.baseline.yearly && S.baseline.yearly.years) || [];
  if (!by.length) return   ;
  const cur = {};
  for (const y of ((r && r.yearly && r.yearly.years) || [])) cur[y.year] = y;
  const full = r && r.yearly && r.yearly.full;
  let h =  

 ;
  for (const y of by) {
    const c = cur[y.year];
    const cs = c ? c.sharpe : null;
    const dl = (cs != null && y.sharpe != null) ? cs - y.sharpe : null;
    const cls = dl == null ?    : (dl > 0 ?    : (dl < 0 ?    :   ));
    const isLocked = y.year >= 2026;
    const cagr = (c && c.cagr != null) ? c.cagr : y.cagr;
    h +=  



 ${fmt.n(cs)} <span class=   style=  >(${dl == null ?    : fmt.sgn(dl)})<  td>
      <td class=  >${cagr != null ? fmt.pct(cagr) :   }<  tr> 

 <  table> 


 <p class=   style=  >口径自检：由日频收益重算的全样本 Sharpe =
      <b>${fmt.n(full.sharpe)}<  b>
      ${ok ?    :   }。< 
  }
  h +=  

 ;
  return h;
}

const statusText = (s) => ({
  queued:   , running:   , done:   , partial:   ,
  failed:   , cancelled:   , interrupted:   ,
}[s] || s);

function banner(cls, ico, title, body) {
  return  
 ;
}


function tabBar() {
  const n = S.tr && S.tr.summary ? S.tr.summary.n_fills : null;
  return  


 <span class=  >${fmt.int(n)} 笔<  button>
    <span class=  >< 
    <span class=   style=  >全部数据来自已保存的 baseline.pkl< 
  < 
}

function tradeTagChoices() {
  const seen = new Set();
  const out = [];
  const push = (t, label) => { if (t && !seen.has(t)) { seen.add(t); out.push({ t, label }); } };
  push(S.optimalTag, S.optimalTag +   );
  for (const r of S.runs) {
    if (r.status ===    && !(r.stage_errors || []).length) push(r.tag, r.label || r.tag);
  }
  for (const t of (S.tags || [])) push(t, t);
  return out;
}

function tradesHtml() {
  return  
 ;
}

const trLoading = () =>   ;

function trErrHtml(msg) {
  return  
 ;
}

function paintTr() {
  const el = $(  );
  if (!el) return;
  el.innerHTML = trBodyHtml();
  bindTrBody();
}

function trBodyHtml() {
  if (S.trErr) return trErrHtml(S.trErr);
  if (!S.tr) return trLoading();
  const s = S.tr.summary || {};
  if (!s.n_fills) {
    return  


 ;
  }
  const choices = tradeTagChoices();
  let h =   ;
  h +=  

 <option value=   ${c.t === S.trTag ?    :   }>${esc(c.label)} · ${esc(c.t)}<  select>
    <span class=  >< 
    <button class=   data-exp=  >⭳ 成交明细 CSV< 
    <button class=   data-exp=  >⭳ 逐名归因 CSV< 
    <button class=   data-exp=  >⭳ 调仓流水 CSV< 
  < 

  h +=  




 ;

  
  const K = [
    [  , fmt.int(s.n_rebalances),   ],
    [  , fmt.int(s.n_fills),   ],
    [  , fmt.int(s.n_instruments),   ],
    [  , fmt.usd(s.total_notional),   ],
    [  , fmt.dol(s.total_cost),   ],
    [  , fmt.int(s.days) +   ,   ],
  ];
  h +=   ;
  for (const [k, v, sub] of K) h +=   ;
  h +=   ;

  
  const rc = s.reconcile || {};
  const same = Math.abs((rc.turnover_from_book_delta || 0) - (rc.turnover_scheduled || 0)) < 1e-6
    && Math.abs((rc.turnover_scheduled || 0) - (rc.turnover_bars_total || 0)) < 1e-6;
  h +=  








 三套口径<b style=  >逐位一致< 
      :   }< 
    <  div> 




 <div class=   style=  ><div class=  >⚠< 
      <b>${fmt.int(du.n)} 笔成交（${fmt.n(du.share_of_fills * 100, 1)}%）金额低于 ${fmt.dol(du.threshold_usd)}< 
      <p>合计 <b>${fmt.dol(du.notional)}< 
      <b>${fmt.n(du.share_of_notional * 100, 4)}%< 
      <b>不是真实下单<  b>（含 <span class=  >dust< 
      上面的口径对账也用的是全量数据。勾选「含碎单」可原样查看。<  div>< 
  }

  
  h +=  






 <option value=   ${S.attrSort === v ?    :   }>${t}< 
      <  div>
      <div class=   style=  >${attrTableHtml()}<  div> 


 <h2 class=  >调仓流水 <small>每一次调仓选了什么、换手多少、这一段赚了多少<  h2>
    <div class=  ><div class=  >
      <h3>调仓决策<  span>
      <label class=  >年份< 
      <select id=  >${[  ].concat((S.tr.options || {}).years || [])
      .map((y) =>   ).join(  )}< 
      < 
      <div class=   style=  >${blotterTableHtml()}<  div> 



 <h2 class=  >成交明细 <small>每一笔实际下单 —— 时间、合约、方向、从多少到多少、金额、成本<  h2>
    <div class=  ><div class=   style=  >
      <h3>Fills<  span>
      <div class=  >
        <label>年份< 
      .map((y) =>   ).join(  )}< 
        <label>动作< 
      .map((a) =>   ).join(  )}< 
        <label>方向< 
      .map((a) =>   ).join(  )}< 
        <input id=   placeholder=   value=   style=  >
        <label class=  ><input type=   id=   ${S.fillsFilt.include_dust ?    :   }> 含碎单< 
        <button class=   id=  >筛选< 
        <button class=   id=  >重置< 
      <  div>
      <div class=   style=  >${fillsTableHtml()}<  div> 





 <div class=   style=  ><label>运行< 
    <select id=  >${choices.map((c) =>   ).join(  )}< 
    <span class=  ><  span>< 
}

function attrTableHtml() {
  const rows = (S.tr.attribution || []).slice();
  if (!rows.length) return   ;
  const key = { net: (r) => -r.net, net_worst: (r) => r.net, gross: (r) => -r.gross, cost: (r) => -r.cost, trades: (r) => -r.n_trades, name: (r) => r.inst }[S.attrSort] || ((r) => -r.net);
  rows.sort(key);
  const hasF = rows.some((r) => r.funding != null);
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.net))) || 1;
  let h =  



 ;
  for (const r of rows) {
    const cls = r.net > 0 ?    : (r.net < 0 ?    :   );
    const barW = Math.max(1, Math.round(Math.abs(r.net) / maxAbs * 52));
    const bar = r.net >= 0 ?    :   ;
    h +=  



 <td class=  >${fmt.n(r.funding * 100, 3)}%< 
      <td class=  >${fmt.int(r.n_trades)}< 
      <td class=  >${fmt.int(r.n_periods_held)}< 
      <td class=  >${fmt.n(r.avg_abs_w, 4)}< 
      <td class=  >${r.long_share == null ?    : fmt.n(r.long_share * 100, 0) +   }< 
      <td class=  >${r.win_rate == null ?    : fmt.n(r.win_rate * 100, 0) +   }< 
      <td style=  ><div class=  ><i style=  ><  div><  tr> 

 <  table>< 
  if (!hasF) {
    h +=  

 ;
  }
  h +=  

 ;
  return h;
}

function blotterTableHtml() {
  const d = S.blotter;
  if (!d) return   ;
  if (!d.rows.length) return   ;
  let h =  




 ;
  for (const r of d.rows) {
    h +=  










 ;
  }
  h +=   ;
  h +=  

 ;
  return h + pagerHtml(d,   );
}

function fillsTableHtml() {
  const d = S.fills;
  if (!d) return   ;
  let h =   ;
  if (d.dust && d.dust.n) {
    h +=  





 ;
  }
  if (!d.rows.length) return h +   ;
  h +=  



 ;
  for (const f of d.rows) {
    const up = f.dw > 0;
    h +=  











 ;
  }
  h +=   ;
  return h + pagerHtml(d,   , [60, 100, 200, 500]);
}

function pagerHtml(d, kind) {
  if (!d || !d.total) return   ;
  const from = d.page * d.per_page + 1;
  const to = Math.min(d.total, (d.page + 1) * d.per_page);
  return  






 ;
}


function ensureTrades(force) {
  const tag = S.trTag || (S.selected && (S.runs.find((r) => r.id === S.selected) || {}).tag) || S.optimalTag;
  if (S.tr && S.trTag === tag && !force) { paintTr(); loadFills(0, true); loadBlotter(0, true); return; }
  S.tr = null; S.trErr = null; S.trTag = tag; S.fills = null; S.blotter = null;
  S.fillsPage = 0; S.blotterPage = 0;
  paintTr();
  fetch(   + encodeURIComponent(tag))
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || (   + r.status));
      S.tr = j;
      paintTr();
      loadFills(0, true);
      loadBlotter(0, true);
    })
    .catch((e) => { S.trErr = String((e && e.message) || e); paintTr(); });
}

function fillsQuery(page, perPage) {
  const f = S.fillsFilt;
  const p = new URLSearchParams();
  p.set(  , String(page));
  p.set(  , String(perPage || 60));
  for (const k of [  ,   ,   ,   ]) if (f[k]) p.set(k, f[k]);
  if (f.include_dust) p.set(  ,   );
  return p.toString();
}

function loadFills(page, silent) {
  if (!S.trTag) return;
  S.fillsPage = Math.max(0, page);
  if (!silent) { S.fills = null; paintTr(); }
  const per = (S.fills && S.fills.per_page) || 60;
  fetch(  )
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || (   + r.status));
      S.fills = j;
    })
    .catch((e) => { S.fillsErr = String((e && e.message) || e); })
    .finally(() => paintTr());
}

function loadBlotter(page, silent) {
  if (!S.trTag) return;
  S.blotterPage = Math.max(0, page);
  if (!silent) { S.blotter = null; paintTr(); }
  const p = new URLSearchParams({ page: String(S.blotterPage), per_page:    });
  if (S.blYear) p.set(  , String(S.blYear));
  if (S.blInst) p.set(  , S.blInst);
  fetch(  )
    .then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || (   + r.status));
      S.blotter = j;
    })
    .catch((e) => { S.blotterErr = String((e && e.message) || e); })
    .finally(() => paintTr());
}

function bindTrBody() {
  const tg = $(  );
  if (tg) tg.onchange = () => { S.trTag = tg.value; ensureTrades(true); };
  const as = $(  );
  if (as) as.onchange = () => { S.attrSort = as.value; paintTr(); };
  const by = $(  );
  if (by) by.onchange = () => { S.blYear = by.value ||   ; loadBlotter(0); };
  const fy = $(  );
  if (fy) fy.onchange = () => { S.fillsFilt.year = fy.value ||   ; loadFills(0); };
  const fa = $(  );
  if (fa) fa.onchange = () => { S.fillsFilt.action = fa.value ||   ; loadFills(0); };
  const fs = $(  );
  if (fs) fs.onchange = () => { S.fillsFilt.side = fs.value ||   ; loadFills(0); };
  const fd = $(  );
  if (fd) fd.onchange = () => { S.fillsFilt.include_dust = fd.checked; loadFills(0); };
  const fq = $(  );
  if (fq) fq.onkeydown = (e) => { if (e.key ===   ) { S.fillsFilt.q = fq.value.trim(); loadFills(0); } };
  const ap = $(  );
  if (ap) ap.onclick = () => { const q = $(  ); if (q) S.fillsFilt.q = q.value.trim(); loadFills(0); };
  const rs = $(  );
  if (rs) rs.onclick = () => { S.fillsFilt = {}; loadFills(0); };
  for (const b of $$(  )) {
    b.onclick = () => {
      const [kind, pg] = b.dataset.pg.split(  );
      (kind ===    ? loadFills : loadBlotter)(parseInt(pg, 10));
    };
  }
  for (const s of $$(  )) {
    s.onchange = () => {
      const n = parseInt(s.value, 10);
      if (s.dataset.per ===   ) { S.fillsPer = n; loadFills(0); }
      else { S.blPer = n; loadBlotter(0); }
    };
  }
  for (const g of $$(  )) {
    g.onkeydown = (e) => {
      if (e.key !==   ) return;
      const d = g.dataset.goto ===    ? S.fills : S.blotter;
      const n = parseInt(g.value, 10);
      if (!d || !isFinite(n)) return;
      const pg = Math.min(Math.max(1, n), d.n_pages) - 1;
      (g.dataset.goto ===    ? loadFills : loadBlotter)(pg);
    };
  }
  for (const b of $$(  )) {
    b.onclick = () => {
      const kind = b.dataset.exp;
      const p = new URLSearchParams();
      if (kind ===   ) {
        const f = S.fillsFilt;
        for (const k of [  ,   ,   ,   ]) if (f[k]) p.set(k, f[k]);
      }
      const qs = p.toString();
      window.open(  ,   );
    };
  }
}


async function drawChart() {
  const cv = $(  );
  if (!cv) return;
  
  
  if (!cv.clientWidth || !cv.clientHeight) return;
  const tag = S.detail && S.detail.tag;
  const ctx = cv.getContext(  );
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const css = getComputedStyle(document.body);
  const cMuted = css.getPropertyValue(  ).trim();
  const cAcc = css.getPropertyValue(  ).trim();
  const cBorder = css.getPropertyValue(  ).trim();
  const cText = css.getPropertyValue(  ).trim();

  const series = [];
  const need = [];
  if (tag && !S.equity[tag]) need.push(tag);
  if (S.optimalTag && !S.equity[S.optimalTag]) need.push(S.optimalTag);
  for (const t of need) {
    
    
    
    try {
      const r = await fetch(   + encodeURIComponent(t));
      const j = r.ok ? await r.json() : null;
      S.equity[t] = shapeOk(j) ? j : null;
      if (!r.ok) {
        S.equityErr[t] = (await r.clone().json().catch(() => ({}))).error ||   ;
      }
    } catch { S.equity[t] = null; }
  }
  
  for (const t in S.equity) if (S.equity[t] && !shapeOk(S.equity[t])) S.equity[t] = null;
  if (S.optimalTag && S.equity[S.optimalTag]) series.push({ d: S.equity[S.optimalTag], c: cMuted, w: 1.4, base: true });
  if (tag && S.equity[tag]) series.push({ d: S.equity[tag], c: cAcc, w: 2, base: false });

  const P = { l: 50, r: 12, t: 12, b: 24 };
  if (!series.length) {
    ctx.fillStyle = cMuted; ctx.font =   ; ctx.textAlign =   ;
    ctx.fillText(  , W / 2, H / 2);
    return;
  }
  const t0 = Math.min(...series.map((s) => s.d.t[0]));
  const t1 = Math.max(...series.map((s) => s.d.t[s.d.t.length - 1]));
  let vmin = Infinity, vmax = -Infinity;
  for (const s of series) for (const v of s.d.v) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
  const pad = (vmax - vmin) * 0.08 || 0.05;
  vmin -= pad; vmax += pad;
  const X = (t) => P.l + (t - t0) / (t1 - t0) * (W - P.l - P.r);
  const Y = (v) => P.t + (vmax - v) / (vmax - vmin) * (H - P.t - P.b);

  ctx.strokeStyle = cBorder; ctx.lineWidth = 1; ctx.font =   ;
  ctx.fillStyle = cMuted; ctx.textAlign =   ;
  for (let i = 0; i <= 4; i++) {
    const v = vmin + (vmax - vmin) * i / 4, y = Math.round(Y(v)) + .5;
    ctx.beginPath(); ctx.moveTo(P.l, y); ctx.lineTo(W - P.r, y); ctx.stroke();
    ctx.fillText(v.toFixed(2) +   , P.l - 6, y + 3.5);
  }
  ctx.strokeStyle = cMuted; ctx.setLineDash([3, 3]); ctx.beginPath();
  ctx.moveTo(P.l, Y(1)); ctx.lineTo(W - P.r, Y(1)); ctx.stroke(); ctx.setLineDash([]);

  for (const s of series) {
    if (!s.base) {
      ctx.beginPath();
      s.d.v.forEach((v, i) => i ? ctx.lineTo(X(s.d.t[i]), Y(v)) : ctx.moveTo(X(s.d.t[i]), Y(v)));
      ctx.lineTo(X(s.d.t[s.d.t.length - 1]), Y(vmin)); ctx.lineTo(X(s.d.t[0]), Y(vmin));
      ctx.closePath();
      ctx.fillStyle = cAcc +   ; ctx.fill();
    }
    ctx.beginPath();
    s.d.v.forEach((v, i) => i ? ctx.lineTo(X(s.d.t[i]), Y(v)) : ctx.moveTo(X(s.d.t[i]), Y(v)));
    ctx.strokeStyle = s.c; ctx.lineWidth = s.w; ctx.stroke();
  }
  ctx.fillStyle = cMuted; ctx.textAlign =   ; ctx.font =   ;
  ctx.fillText(new Date(t0).toISOString().slice(0, 10), P.l, H - 7);
  ctx.textAlign =   ;
  ctx.fillText(new Date(t1).toISOString().slice(0, 10), W - P.r, H - 7);
  const last = series[series.length - 1];
  ctx.fillStyle = cText; ctx.textAlign =   ; ctx.font =   ;
  ctx.fillText(   + last.d.final.toFixed(3) +    + last.d.start +    + last.d.end +   , P.l, P.t + 11);
}


async function runNow() {
  const btn = $(  );
  const diffs = diffFromOptimal();
  if (diffs.length && !confirm(
      
    + diffs.map((d) =>   ).join(  )
    +   )) return;

  const overrides = {};
  for (const k in S.ov) if (!eq(S.ov[k], S.lib[k])) overrides[k] = S.ov[k];
  const cli = {};
  for (const k of [  ,   ,   ,   ,   ,   ]) cli[k] = S.cli[k];
  const sub = (S.ov[  ] || []).join(  ) ||   ;
  const label =   ;

  btn.disabled = true; btn.textContent =   ;
  try {
    const res = await (await fetch(  , {
      method:   , headers: {   :    },
      body: JSON.stringify({ overrides, cli, stages: S.stages, preset: S.preset, label }),
    })).json();
    if (res.error) { alert(res.error); return; }
    S.selected = res.id; S.logOffset = 0;
    await pollRuns();
    await selectRun(res.id);
  } finally {
    btn.disabled = false; btn.textContent =   ;
  }
}

async function pollRuns() {
  try {
    const d = await (await fetch(  )).json();
    S.runs = d.runs || []; S.active = d.active; S.queued = d.queued || [];
  } catch { return; }
  const q = $(  );
  if (S.active || S.queued.length) {
    q.style.display =   ;
    q.className =   ;
    q.textContent = S.active ?    :   ;
  } else q.style.display =   ;
  if (S.detail) {
    const cur = S.runs.find((r) => r.id === S.selected);
    if (cur) S.detail.status = cur.status;
    startLog(S.detail);
  }
  renderContent();
}

async function selectRun(id) {
  S.selected = id; S.logOffset = 0;
  const d = await (await fetch(   + encodeURIComponent(id))).json();
  if (d.error) return;
  d._log =   ;
  S.detail = d;
  
  if (S.tab ===    && d.tag) {
    S.trTag = d.tag;
    renderContent();
    ensureTrades(true);
    return;
  }
  renderContent();
  const lp = await (await fetch(  )).json();
  S.logOffset = lp.offset || 0;
  d._log = lp.text ||   ;
  renderContent();
}

function startLog(d) {
  clearInterval(S.logTimer);
  const live = () => d.status ===    || d.status ===   ;
  if (!live()) { stopLog(); return; }
  S.logTimer = setInterval(async () => {
    try {
      const lp = await (await fetch(  )).json();
      if (lp.text) {
        S.logOffset = lp.offset;
        const pre = $(  );
        if (pre) { pre.insertAdjacentHTML(  , logHtml(lp.text)); pre.scrollTop = pre.scrollHeight; }
      }
    } catch {  }
  }, 1100);
}
function stopLog() { clearInterval(S.logTimer); S.logTimer = null; }

function logHtml(txt) {
  return esc(txt)
    .replace(  gm,   )
    .replace(  gm,   );
}


$(  ).onclick = () => {
  const cur = document.documentElement.getAttribute(  );
  const next = cur ===    ?    : (cur ===    ?    :   );
  document.documentElement.setAttribute(  , next);
  localStorage.setItem(  , next);
  setTimeout(drawChart, 30);
};
const savedTheme = localStorage.getItem(  );
if (savedTheme) document.documentElement.setAttribute(  , savedTheme);

$(  ).onclick = runNow;

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

boot();
