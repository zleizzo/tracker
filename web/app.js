'use strict';
/* Tracker dashboard. Vanilla JS, no dependencies. Talks to the local API in tracker/server.py. */

// ---------------------------------------------------------------------------
// utilities
// ---------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'style') n.style.cssText = v;
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, '');
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    n.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return n;
}
const SVG_NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}, ...children) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'text') n.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c) n.appendChild(c);
  return n;
}

const pad = n => String(n).padStart(2, '0');
function fmtDur(s, opts = {}) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60 && !opts.minutesOnly) return `${s}s`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${pad(m)}m`;
}
function fmtHours(s) {
  if (s >= 3600) { const h = s / 3600; return (h >= 10 ? h.toFixed(0) : h.toFixed(1).replace(/\.0$/, '')) + 'h'; }
  return `${Math.round(s / 60)}m`;
}
const fmtClock = ts => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const isoDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayIso = () => isoDate(new Date());
function shiftIso(iso, days) { const d = new Date(iso + 'T12:00:00'); d.setDate(d.getDate() + days); return isoDate(d); }
function fmtDay(iso, long = false) {
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString(undefined, long ? { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' } : { weekday: 'short', month: 'short', day: 'numeric' });
}
const WD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function toLocalInput(ts) { const d = new Date(ts * 1000); return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function fromLocalInput(v) { const t = new Date(v).getTime(); return isNaN(t) ? null : Math.round(t / 1000); }
const pct = (a, b) => (b > 0 ? Math.round(100 * a / b) : 0);

function store(key, val) {
  try {
    if (val === undefined) { const v = localStorage.getItem('tracker.' + key); return v == null ? undefined : JSON.parse(v); }
    localStorage.setItem('tracker.' + key, JSON.stringify(val));
  } catch (e) { return undefined; }
}

function isDark() {
  const t = document.documentElement.dataset.theme;
  if (t) return t === 'dark';
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

// ---------------------------------------------------------------------------
// api
// ---------------------------------------------------------------------------
async function request(method, path, body) {
  const res = await fetch(path, {
    method, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const msg = (data && data.error) || `${res.status} ${res.statusText}`;
    toast(msg, true);
    throw new Error(msg);
  }
  return data;
}
const api = {
  get: p => request('GET', p),
  post: (p, b) => request('POST', p, b || {}),
  put: (p, b) => request('PUT', p, b || {}),
  del: p => request('DELETE', p),
};

let toastTimer = null;
function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.style.background = isError ? 'var(--bad)' : '';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 5000 : 2500);
}

// ---------------------------------------------------------------------------
// categories, colors
// ---------------------------------------------------------------------------
const CATS = {
  productive: { label: 'Productive', color: 'var(--cat-productive)' },
  neutral: { label: 'Neutral', color: 'var(--cat-neutral)' },
  distracting: { label: 'Distracting', color: 'var(--cat-distracting)' },
  switching: { label: 'Context switching', color: 'var(--cat-switching)' },
};
const CAT_ORDER = ['productive', 'neutral', 'distracting', 'switching'];
const catColor = c => (CATS[c] || CATS.neutral).color;
const catLabel = c => (CATS[c] || CATS.neutral).label;
function projectColor(p) {
  if (!p || p.id == null) return 'var(--other)';
  if (p.id === 'cs') return 'var(--muted)';
  return `var(--s${(Number(p.color) % 8) + 1})`;
}
const HEAT_LIGHT = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'];
const HEAT_DARK = ['#104281', '#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4', '#cde2fb'];

// ---------------------------------------------------------------------------
// state, filters, routing
// ---------------------------------------------------------------------------
const state = Object.assign({
  range: 'today', from: todayIso(), to: todayIso(), project: '', category: '', h0: 0, h1: 24,
  weekdays: [0, 1, 2, 3, 4, 5, 6], date: todayIso(), showShort: false,
}, store('filters') || {});
let projectsCache = [];
let statusCache = null;
// ?range=7&project=3&category=productive&hours=9-17&weekdays=0,1,2,3,4&date=2026-10-02 override saved filters (linkable views)
(function applyQueryOverrides() {
  const q = new URLSearchParams(location.search);
  if (!q.toString()) return;
  if (q.get('range')) state.range = q.get('range');
  if (q.get('from')) { state.from = q.get('from'); state.range = 'custom'; }
  if (q.get('to')) { state.to = q.get('to'); state.range = 'custom'; }
  if (q.has('project')) state.project = q.get('project');
  if (q.has('category')) state.category = q.get('category');
  if (q.get('hours')) { const [a, b] = q.get('hours').split('-').map(Number); if (!isNaN(a) && !isNaN(b)) { state.h0 = a; state.h1 = b; } }
  if (q.get('weekdays')) state.weekdays = q.get('weekdays').split(',').map(Number).filter(n => n >= 0 && n < 7);
  if (q.get('date')) state.date = q.get('date');
  if (q.get('theme')) document.documentElement.dataset.theme = q.get('theme');
})();

function rangeDates() {
  const today = new Date();
  const back = n => { const d = new Date(today); d.setDate(d.getDate() - n); return isoDate(d); };
  switch (state.range) {
    case 'today': return [todayIso(), todayIso()];
    case 'yesterday': return [back(1), back(1)];
    case 'week': { const dow = (today.getDay() + 6) % 7; return [back(dow), todayIso()]; }
    case '7': return [back(6), todayIso()];
    case '30': return [back(29), todayIso()];
    case '90': return [back(89), todayIso()];
    case 'month': return [isoDate(new Date(today.getFullYear(), today.getMonth(), 1)), todayIso()];
    default: return [state.from || todayIso(), state.to || state.from || todayIso()];
  }
}
function filterQuery(includeDims = true) {
  const [from, to] = rangeDates();
  const q = new URLSearchParams({ from, to });
  if (includeDims) {
    if (state.project) q.set('project', state.project);
    if (state.category) q.set('category', state.category);
  }
  if (!(state.h0 === 0 && state.h1 === 24)) q.set('hours', `${state.h0}-${state.h1}`);
  if (state.weekdays.length < 7) q.set('weekdays', state.weekdays.join(','));
  return q.toString();
}
function saveFilters() {
  const { range, from, to, project, category, h0, h1, weekdays, date, showShort } = state;
  store('filters', { range, from, to, project, category, h0, h1, weekdays, date, showShort });
}

function syncFilterControls() {
  $('#f-range').value = state.range;
  $('#f-custom').hidden = state.range !== 'custom';
  $('#f-from').value = state.from; $('#f-to').value = state.to;
  $('#f-category').value = state.category;
  $('#f-h0').value = state.h0; $('#f-h1').value = state.h1;
  const wd = $('#f-weekdays');
  wd.innerHTML = '';
  WD.forEach((name, i) => wd.appendChild(el('button', {
    class: state.weekdays.includes(i) ? 'on' : '', text: name.slice(0, 2), title: name,
    onclick: () => {
      const set = new Set(state.weekdays);
      if (set.has(i)) set.delete(i); else set.add(i);
      if (set.size === 0) WD.forEach((_, j) => set.add(j));
      state.weekdays = [...set].sort();
      saveFilters(); render();
    },
  })));
  fillProjectSelect($('#f-project'), state.project, { all: true });
}
function fillProjectSelect(sel, value, opts = {}) {
  sel.innerHTML = '';
  if (opts.all) sel.appendChild(el('option', { value: '', text: 'All projects' }));
  if (opts.none) sel.appendChild(el('option', { value: '', text: opts.none }));
  for (const p of projectsCache.filter(p => (!p.archived && !p.done) || String(p.id) === String(value))) {
    sel.appendChild(el('option', { value: p.id, text: p.title }));
  }
  if (opts.all) {
    sel.appendChild(el('option', { value: 'unassigned', text: 'Unassigned' }));
    sel.appendChild(el('option', { value: 'cs', text: 'Context switching' }));
  }
  sel.value = value == null ? '' : String(value);
  if (sel.value !== String(value == null ? '' : value)) sel.value = '';
}

function bindFilters() {
  $('#f-range').addEventListener('change', e => { state.range = e.target.value; $('#f-custom').hidden = state.range !== 'custom'; saveFilters(); if (state.range !== 'custom') render(); });
  $('#f-from').addEventListener('change', e => { state.from = e.target.value; if (state.to < state.from) state.to = state.from; saveFilters(); render(); });
  $('#f-to').addEventListener('change', e => { state.to = e.target.value; if (state.to < state.from) state.from = state.to; saveFilters(); render(); });
  $('#f-project').addEventListener('change', e => { state.project = e.target.value; saveFilters(); render(); });
  $('#f-category').addEventListener('change', e => { state.category = e.target.value; saveFilters(); render(); });
  const hours = () => {
    let a = Math.max(0, Math.min(23, Number($('#f-h0').value) || 0));
    let b = Math.max(1, Math.min(24, Number($('#f-h1').value) || 24));
    if (b <= a) b = a + 1;
    state.h0 = a; state.h1 = b; saveFilters(); render();
  };
  $('#f-h0').addEventListener('change', hours); $('#f-h1').addEventListener('change', hours);
  $('#f-reset').addEventListener('click', () => {
    Object.assign(state, { range: 'today', project: '', category: '', h0: 0, h1: 24, weekdays: [0, 1, 2, 3, 4, 5, 6] });
    saveFilters(); syncFilterControls(); render();
  });
}

function currentView() {
  const h = location.hash.replace(/^#/, '') || 'overview';
  const [view, ...rest] = h.split('/');
  return { view: ['overview', 'timeline', 'plan', 'projects', 'sort', 'trends', 'settings'].includes(view) ? view : 'overview', arg: rest.join('/') };
}

// ---------------------------------------------------------------------------
// tooltip
// ---------------------------------------------------------------------------
const tip = $('#tooltip');
function showTip(e, build) {
  tip.innerHTML = '';
  const content = build();
  if (!content) { hideTip(); return; }
  tip.appendChild(content);
  tip.hidden = false;
  moveTip(e);
}
function moveTip(e) {
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
function hideTip() { tip.hidden = true; }
function tipRows(title, rows, foot) {
  const box = el('div');
  if (title) box.appendChild(el('div', { class: 'tt-title', text: title }));
  for (const r of rows) {
    if (!r) continue;
    box.appendChild(el('div', { class: 'tt-row' },
      r.color ? el('span', { class: 'key', style: `background:${r.color}` }) : null,
      el('span', { class: r.dim ? 'dim' : '', text: r.label }), el('b', { text: r.value })));
  }
  if (foot) box.appendChild(el('div', { class: 'dim', text: foot, style: 'margin-top:4px' }));
  return box;
}
function hoverable(node, build) {
  node.addEventListener('pointerenter', e => showTip(e, build));
  node.addEventListener('pointermove', moveTip);
  node.addEventListener('pointerleave', hideTip);
  node.addEventListener('focus', e => showTip({ clientX: node.getBoundingClientRect().left, clientY: node.getBoundingClientRect().top }, build));
  node.addEventListener('blur', hideTip);
  return node;
}

// ---------------------------------------------------------------------------
// chart building blocks
// ---------------------------------------------------------------------------
function card({ title, sub, span2, chart, table, cls }) {
  const body = el('div', { class: 'card-body' });
  const c = el('div', { class: `card ${span2 ? 'span2' : ''} ${cls || ''}` });
  const tools = el('div', { class: 'tools' });
  const head = el('div', { class: 'card-head' }, el('h2', { text: title }), sub ? el('span', { class: 'sub', text: sub }) : null, tools);
  c.append(head, body);
  let showingTable = false;
  const draw = () => { body.innerHTML = ''; const n = showingTable ? table() : chart(); body.appendChild(n || el('div', { class: 'empty', text: 'No data in this range' })); };
  if (table) {
    const b = el('button', { class: 'btn ghost small', text: 'Table', onclick: () => { showingTable = !showingTable; b.textContent = showingTable ? 'Chart' : 'Table'; draw(); } });
    tools.appendChild(b);
  }
  draw();
  return c;
}
function legend(items) {
  return el('div', { class: 'legend' }, items.map(i => el('span', {}, el('i', { class: 'swatch', style: `background:${i.color}` }), i.label)));
}
function dataTable(cols, rows) {
  if (!rows.length) return el('div', { class: 'empty', text: 'No data in this range' });
  return el('table', { class: 'data' },
    el('thead', {}, el('tr', {}, cols.map(c => el('th', { class: c.num ? 'num' : '', text: c.h })))),
    el('tbody', {}, rows.map(r => el('tr', {}, cols.map(c => {
      const v = typeof c.k === 'function' ? c.k(r) : r[c.k];
      return el('td', { class: `${c.num ? 'num' : ''} ${c.trunc ? 'trunc' : ''}`, title: c.trunc && typeof v === 'string' ? v : null }, v instanceof Node ? v : (v == null ? '' : String(v)));
    })))));
}
function catPill(c) { return el('span', { class: 'pill cat' }, el('i', { class: 'swatch', style: `background:${catColor(c)}` }), catLabel(c)); }
function projPill(p) { return p ? el('span', { class: 'pill' }, el('i', { class: 'swatch', style: `background:${projectColor(p)}` }), p.title) : el('span', { class: 'muted', text: '—' }); }
function mixBar(parts, total, colorOf) {
  const m = el('span', { class: 'mix' });
  for (const [k, v] of Object.entries(parts).sort((a, b) => b[1] - a[1])) {
    if (v / total < 0.01) continue;
    m.appendChild(el('i', { style: `width:${(100 * v / total).toFixed(1)}%;background:${colorOf(k)}` }));
  }
  return m;
}

/** Horizontal bars. rows: [{label, value, color, segments?: [{value,color,label}], swatch?, tipRows?}] */
function hbars(rows, opts = {}) {
  if (!rows.length) return null;
  const max = Math.max(...rows.map(r => r.value), 1);
  const wrap = el('div', { class: 'hbars' });
  for (const r of rows) {
    const track = el('div', { class: 'track' });
    if (r.segments && r.segments.length) {
      let acc = 0;
      const segs = r.segments.filter(s => s.value > 0).sort((a, b) => b.value - a.value);
      segs.forEach((s, i) => {
        const w = 100 * s.value / max, left = 100 * acc / max;
        const gap = i < segs.length - 1 ? 2 : 0;
        track.appendChild(el('div', { class: 'fill', style: `left:${left}%;width:calc(${w}% - ${gap}px);background:${s.color};border-radius:${i === segs.length - 1 ? '0 4px 4px 0' : '0'}` }));
        acc += s.value;
      });
    } else {
      track.appendChild(el('div', { class: 'fill', style: `width:${100 * r.value / max}%;background:${r.color}` }));
    }
    const row = el('div', { class: 'hbar', tabindex: '0' },
      el('div', { class: 'lbl', title: r.label }, el('i', { class: 'swatch', style: `background:${r.swatch || r.color}` }), r.label),
      track, el('div', { class: 'val', text: fmtDur(r.value) }));
    hoverable(row, () => tipRows(r.label, r.tipRows || (r.segments ? r.segments.filter(s => s.value > 0).sort((a, b) => b.value - a.value).map(s => ({ color: s.color, label: s.label, value: fmtDur(s.value) })) : [{ label: 'Time', value: fmtDur(r.value) }]), r.foot));
    wrap.appendChild(row);
  }
  return wrap;
}

function niceTicks(maxSeconds, target = 4) {
  const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 3 * 3600, 4 * 3600, 6 * 3600, 8 * 3600, 12 * 3600, 24 * 3600, 48 * 3600, 96 * 3600, 240 * 3600];
  let step = steps.find(s => maxSeconds / s <= target) || steps[steps.length - 1];
  const top = Math.max(step, Math.ceil(maxSeconds / step) * step);
  const ticks = [];
  for (let v = 0; v <= top + 1e-6; v += step) ticks.push(v);
  return { step, top, ticks };
}
function roundedTop(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
}
/** Stacked columns. items: [{label, values:{key:seconds}, tipTitle?}]; series: [{key,label,color}] bottom→top. */
function stackedColumns(items, series, opts = {}) {
  if (!items.length) return null;
  const W = opts.width || 760, H = opts.height || 220, m = { t: 12, r: 8, b: 26, l: 44 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const totals = items.map(it => series.reduce((a, s) => a + (it.values[s.key] || 0), 0));
  const isCount = !!opts.count;
  const { top, ticks } = isCount ? countTicks(Math.max(...totals, 1)) : niceTicks(Math.max(...totals, 1));
  const y = v => m.t + ih - (v / top) * ih;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
  for (const t of ticks) {
    root.appendChild(svg('line', { class: t === 0 ? 'axis-line' : 'grid-line', x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
    root.appendChild(svg('text', { x: m.l - 6, y: y(t) + 3.5, 'text-anchor': 'end', text: isCount ? String(t) : (t === 0 ? '0' : fmtHours(t)) }));
  }
  const n = items.length, slot = iw / n, bw = Math.min(24, Math.max(3, slot * 0.72));
  const labelEvery = Math.ceil(n / Math.max(1, Math.floor(iw / 52)));
  items.forEach((it, i) => {
    const x = m.l + i * slot + (slot - bw) / 2;
    const g = svg('g', { class: 'col' });
    let acc = 0;
    const present = series.filter(s => (it.values[s.key] || 0) > 0);
    present.forEach((s, idx) => {
      const v = it.values[s.key];
      const h = (v / top) * ih;
      const yTop = y(acc + v);
      const isTop = idx === present.length - 1;
      const gap = isTop ? 0 : 2;
      const hh = Math.max(0, h - gap);
      if (hh > 0) {
        if (isTop && hh >= 4) g.appendChild(svg('path', { class: 'seg', d: roundedTop(x, yTop, bw, hh, 4), fill: s.color }));
        else g.appendChild(svg('rect', { class: 'seg', x, y: yTop, width: bw, height: hh, fill: s.color }));
      }
      acc += v;
    });
    const hit = svg('rect', { class: 'hit', x: m.l + i * slot, y: m.t, width: slot, height: ih, tabindex: '0' });
    hoverable(hit, () => tipRows(it.tipTitle || it.label,
      [...present].reverse().map(s => ({ color: s.color, label: s.label, value: isCount ? String(it.values[s.key]) : fmtDur(it.values[s.key]) })),
      it.tipFoot || (present.length > 1 ? `Total ${isCount ? totals[i] : fmtDur(totals[i])}` : null)));
    g.appendChild(hit);
    root.appendChild(g);
    if (i % labelEvery === 0 || n <= 12) root.appendChild(svg('text', { x: x + bw / 2, y: H - 8, 'text-anchor': 'middle', text: it.label }));
  });
  const wrap = el('div', { class: 'chart' }, root);
  if (series.length > 1 && !opts.noLegend) wrap.appendChild(legend(series.filter(s => items.some(it => (it.values[s.key] || 0) > 0))));
  return wrap;
}
function countTicks(max) {
  const raw = max / 4, mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const step = [1, 2, 5, 10].map(f => f * mag).find(s => max / s <= 4) || mag * 10;
  const top = Math.max(step, Math.ceil(max / step) * step);
  const ticks = []; for (let v = 0; v <= top; v += step) ticks.push(v);
  return { top, ticks };
}

/** 7 × 24 heatmap of seconds. */
function heatmap(grid, opts = {}) {
  const max = Math.max(...grid.flat(), 1);
  if (max <= 1) return null;
  const cw = 28, ch = 20, l = 34, t = 16, W = l + 24 * cw + 4, H = t + 7 * ch + 4;
  const ramp = isDark() ? HEAT_DARK : HEAT_LIGHT;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
  for (let h = 0; h < 24; h += 3) root.appendChild(svg('text', { x: l + h * cw + 2, y: 11, text: `${pad(h)}` }));
  grid.forEach((row, wd) => {
    root.appendChild(svg('text', { x: 0, y: t + wd * ch + 14, text: WD[wd] }));
    row.forEach((v, h) => {
      const idx = v <= 0 ? -1 : Math.min(ramp.length - 1, Math.floor((v / max) * ramp.length));
      const rect = svg('rect', { x: l + h * cw + 1, y: t + wd * ch + 1, width: cw - 2, height: ch - 2, rx: 3, fill: idx < 0 ? 'var(--surface-2)' : ramp[idx], tabindex: '0', class: 'cell' });
      hoverable(rect, () => tipRows(`${WD[wd]} ${pad(h)}:00–${pad(h + 1)}:00`, [{ label: opts.label || 'Time', value: fmtDur(v) }], opts.days ? `${fmtDur(v / opts.days)} per day on average` : null));
      root.appendChild(rect);
    });
  });
  const scale = el('div', { class: 'scale' }, 'less', el('span', { class: 'ramp' }, ramp.map(c => el('i', { style: `background:${c}` }))), `more (max ${fmtDur(max)})`);
  return el('div', { class: 'chart heat' }, root, scale);
}
function sparkline(values, color = 'var(--accent)', w = 120, h = 28) {
  const max = Math.max(...values, 1), n = values.length;
  if (n < 2) return null;
  const pts = values.map((v, i) => `${(i / (n - 1)) * (w - 4) + 2},${h - 2 - (v / max) * (h - 6)}`).join(' ');
  return svg('svg', { class: 'spark', viewBox: `0 0 ${w} ${h}`, width: w, height: h },
    svg('polyline', { points: pts, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }),
    svg('circle', { cx: pts.split(' ').pop().split(',')[0], cy: pts.split(' ').pop().split(',')[1], r: 3, fill: color }));
}
function tile({ label, value, delta, deltaClass, hero, spark }) {
  return el('div', { class: `tile ${hero ? 'hero' : ''}` }, el('div', { class: 'label', text: label }), el('div', { class: 'value', text: value }),
    delta ? el('div', { class: `delta ${deltaClass || ''}`, text: delta }) : null, spark || null);
}

// ---------------------------------------------------------------------------
// modal
// ---------------------------------------------------------------------------
function openModal(title, buildBody) {
  const root = $('#modal-root');
  root.innerHTML = '';
  const box = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' }, el('h2', { text: title }));
  const bg = el('div', { class: 'modal-bg', onclick: e => { if (e.target === bg) close(); } }, box);
  const close = () => { root.innerHTML = ''; document.removeEventListener('keydown', esc); };
  const esc = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc);
  box.appendChild(buildBody(close));
  root.appendChild(bg);
  const first = box.querySelector('input, select, textarea');
  if (first) first.focus();
  return close;
}
function field(label, input) { return el('label', {}, label, input); }
function select(options, value, attrs = {}) {
  const s = el('select', { class: 'input', ...attrs });
  for (const o of options) s.appendChild(el('option', { value: o.value, text: o.label }));
  if (value != null) s.value = String(value);
  return s;
}
function projectTaskFields(projectId, taskId, allProjects, tasks) {
  const pSel = select([{ value: '', label: '— no project —' }, ...allProjects.filter(p => (!p.archived && !p.done) || p.id === projectId).map(p => ({ value: p.id, label: p.title }))], projectId == null ? '' : projectId);
  const tSel = select([{ value: '', label: '— whole project —' }], '');
  const fillTasks = () => {
    const pid = Number(pSel.value);
    tSel.innerHTML = '';
    tSel.appendChild(el('option', { value: '', text: pid ? '— whole project —' : '—' }));
    for (const t of tasks.filter(t => t.project_id === pid && !t.done)) tSel.appendChild(el('option', { value: t.id, text: t.title }));
    tSel.disabled = !pid;
    if (taskId && tasks.some(t => t.id === taskId && t.project_id === pid)) tSel.value = String(taskId);
  };
  pSel.addEventListener('change', fillTasks);
  fillTasks();
  return { pSel, tSel };
}
const categorySelect = (value, auto = 'Auto (from project / rules)') => select([{ value: '', label: auto }, ...CAT_ORDER.slice(0, 3).map(c => ({ value: c, label: catLabel(c) }))], value || '');

// ---------------------------------------------------------------------------
// status pill
// ---------------------------------------------------------------------------
async function refreshStatus() {
  try { statusCache = await api.get('/api/status'); } catch (e) { statusCache = null; }
  const box = $('#status');
  box.innerHTML = '';
  if (!statusCache) { box.append(el('span', { class: 'dot bad' }), 'Server unreachable'); return; }
  const s = statusCache;
  const fresh = s.heartbeat_age != null && s.heartbeat_age < 30;
  let cls = 'ok', text = 'Tracking';
  if (s.paused) { cls = 'paused'; text = 'Paused'; }
  else if (!s.daemon_running) { cls = 'bad'; text = 'Daemon not running'; }
  else if (!fresh) { cls = 'paused'; text = `Last heartbeat ${fmtDur(s.heartbeat_age || 0)} ago`; }
  else if (s.last_segment) {
    const l = s.last_segment;
    text = l.kind === 'active' ? `Tracking · ${l.app || '?'}${l.domain ? ' · ' + l.domain : ''}` : (l.kind === 'locked' ? 'Locked' : 'Idle');
  }
  box.append(el('span', { class: `dot ${cls}` }), text);
  const c = s.counts || {};
  if (s.daemon_running && c.active_recent > 0 && c.titles_recent === 0) {
    box.appendChild(el('a', { href: '#settings', class: 'pill', text: '⚠ no window titles — grant Accessibility', style: 'color:var(--bad)' }));
  } else if (s.daemon_running && c.browser_recent > 5 && c.urls_recent === 0) {
    box.appendChild(el('a', { href: '#settings', class: 'pill', text: '⚠ no browser URLs — allow Automation', style: 'color:var(--bad)' }));
  }
}

// ---------------------------------------------------------------------------
// views
// ---------------------------------------------------------------------------
const main = $('#main');
let renderToken = 0;

async function render() {
  const { view, arg } = currentView();
  const token = ++renderToken;
  $$('#tabs a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  $('#filters').hidden = !['overview', 'trends', 'projects', 'sort'].includes(view);
  main.classList.add('loading');
  try {
    if (!projectsCache.length || view === 'projects' || view === 'sort') projectsCache = (await api.get('/api/projects')).projects;
    syncFilterControls();
    const node = await ({ overview: renderOverview, timeline: renderTimeline, plan: renderPlan, projects: renderProjects, sort: renderSort, trends: renderTrends, settings: renderSettings })[view](arg);
    if (token !== renderToken) return;
    main.innerHTML = '';
    main.appendChild(node);
  } catch (e) {
    if (token !== renderToken) return;
    main.innerHTML = '';
    main.appendChild(el('div', { class: 'card' }, el('p', { class: 'err', text: `Could not load: ${e.message}` })));
  } finally {
    if (token === renderToken) main.classList.remove('loading');
  }
}

// ---- overview ---------------------------------------------------------------
function projectSeriesFrom(byProject, limit = 7) {
  const real = byProject.filter(p => p.id != null && p.id !== 'cs');
  const top = real.slice(0, limit);
  const other = real.slice(limit);
  const series = top.map(p => ({ key: String(p.id), label: p.title, color: projectColor(p) }));
  if (other.length) series.push({ key: '__other', label: `Other (${other.length})`, color: 'var(--other)', members: other.map(p => String(p.id)) });
  if (byProject.some(p => p.id == null)) series.push({ key: 'none', label: 'Unassigned', color: 'var(--other)' });
  if (byProject.some(p => p.id === 'cs')) series.push({ key: 'cs', label: 'Context switching', color: 'var(--muted)' });
  return series;
}
function dayValuesByProject(day, series) {
  const vals = {};
  for (const s of series) {
    if (s.members) vals[s.key] = s.members.reduce((a, k) => a + (day.by_project[k] || 0), 0);
    else vals[s.key] = day.by_project[s.key] || 0;
  }
  return vals;
}
const catSeries = CAT_ORDER.map(c => ({ key: c, label: catLabel(c), color: catColor(c) }));
const dayLabel = d => { const dt = new Date(d.day + 'T12:00:00'); return `${WD[d.weekday].slice(0, 2)} ${dt.getDate()}`; };

async function renderOverview() {
  const s = await api.get('/api/summary?' + filterQuery());
  const t = s.totals, [from, to] = rangeDates();
  const multiDay = s.per_day.length > 1;
  const activeDays = s.per_day.filter(d => d.productive + d.neutral + d.distracting + d.switching > 0).length || 1;
  const wrap = el('div');
  const sparkVals = multiDay ? s.per_day.map(d => d.productive + d.neutral + d.distracting + d.switching) : null;
  wrap.appendChild(el('div', { class: 'tiles' },
    tile({ label: multiDay ? `Tracked · ${fmtDur(t.active / activeDays)} per active day` : 'Tracked', value: fmtDur(t.active), hero: true, spark: sparkVals ? sparkline(sparkVals) : null }),
    tile({ label: 'Productive', value: t.pulse == null ? '—' : `${t.pulse}%`, delta: `${fmtDur(t.productive)} productive · ${fmtDur(t.neutral)} neutral` }),
    tile({ label: 'Distracting', value: fmtDur(t.distracting), delta: t.active ? `${pct(t.distracting, t.active)}% of tracked time` : '' }),
    tile({ label: 'Switches', value: String(t.switches.total), delta: `${t.switches.app} apps · ${t.switches.window} windows · ${t.switches.tab} tabs` }),
    tile({ label: 'Context switching', value: fmtDur(t.switching), delta: t.active ? `${pct(t.switching, t.active)}% of tracked time` : '' }),
    tile({ label: 'Breaks', value: fmtDur(t.idle), delta: t.manual ? `${fmtDur(t.manual)} logged manually` : 'idle time between first and last activity' }),
    s.plan && s.plan.planned > 0 ? tile({ label: 'On plan', value: s.plan.adherence == null ? '—' : `${Math.round(100 * s.plan.adherence)}%`, delta: `${fmtDur(s.plan.on_plan)} of ${fmtDur(s.plan.elapsed)} planned · ${s.plan.days} day${s.plan.days === 1 ? '' : 's'}` }) : null,
  ));
  const grid = el('div', { class: 'grid' });
  wrap.appendChild(grid);
  if (multiDay) {
    grid.appendChild(card({ title: 'Time per day', sub: 'by category', span2: true,
      chart: () => stackedColumns(s.per_day.map(d => ({ label: dayLabel(d), tipTitle: fmtDay(d.day), values: d })), catSeries),
      table: () => dataTable([{ h: 'Day', k: d => fmtDay(d.day) }, ...CAT_ORDER.map(c => ({ h: catLabel(c), k: d => fmtDur(d[c]), num: true })), { h: 'Switches', k: 'switches', num: true }, { h: 'Idle', k: d => fmtDur(d.idle), num: true }], s.per_day) }));
  } else {
    grid.appendChild(card({ title: 'Time by hour', sub: 'by category', span2: true,
      chart: () => stackedColumns(s.per_hour.map(h => ({ label: pad(h.hour), tipTitle: `${pad(h.hour)}:00–${pad(h.hour + 1)}:00`, values: h })), catSeries),
      table: () => dataTable([{ h: 'Hour', k: h => `${pad(h.hour)}:00` }, ...CAT_ORDER.map(c => ({ h: catLabel(c), k: h => fmtDur(h[c]), num: true })), { h: 'Switches', k: 'switches', num: true }], s.per_hour.filter(h => h.productive + h.neutral + h.distracting + h.switching > 0)) }));
  }
  grid.appendChild(card({ title: 'Projects', sub: 'time attributed by rules and assignments',
    chart: () => hbars(s.by_project.map(p => ({ label: p.title, value: p.seconds, color: projectColor(p), foot: p.id == null ? 'Sort these on the Sort tab' : null }))),
    table: () => dataTable([{ h: 'Project', k: p => projPill(p) }, { h: 'Category', k: p => catPill(p.category) }, { h: 'Time', k: p => fmtDur(p.seconds), num: true }, { h: 'Share', k: p => `${pct(p.seconds, t.active)}%`, num: true }], s.by_project) }));
  grid.appendChild(card({ title: 'Apps', sub: 'colour shows category mix',
    chart: () => hbars(s.by_app.slice(0, 12).map(a => ({ label: a.app, value: a.seconds, color: catColor(a.category), swatch: catColor(a.category), segments: Object.entries(a.categories).map(([c, v]) => ({ value: v, color: catColor(c), label: catLabel(c) })) }))),
    table: () => dataTable([{ h: 'App', k: 'app' }, { h: 'Mostly', k: a => catPill(a.category) }, { h: 'Time', k: a => fmtDur(a.seconds), num: true }], s.by_app) }));
  grid.appendChild(card({ title: 'Sites', sub: 'browser time by domain',
    chart: () => hbars(s.by_domain.slice(0, 12).map(a => ({ label: a.domain, value: a.seconds, color: catColor(a.category), swatch: catColor(a.category), segments: Object.entries(a.categories).map(([c, v]) => ({ value: v, color: catColor(c), label: catLabel(c) })) }))),
    table: () => dataTable([{ h: 'Domain', k: 'domain' }, { h: 'Mostly', k: a => catPill(a.category) }, { h: 'Time', k: a => fmtDur(a.seconds), num: true }], s.by_domain) }));
  if (s.by_task.length) {
    grid.appendChild(card({ title: 'Tasks', sub: 'sub-tasks with time',
      chart: () => hbars(s.by_task.map(tk => ({ label: `${tk.project} › ${tk.title}`, value: tk.seconds, color: projectColor(projectsCache.find(p => p.id === tk.project_id)) }))),
      table: () => dataTable([{ h: 'Project', k: 'project' }, { h: 'Task', k: 'title' }, { h: 'Time', k: tk => fmtDur(tk.seconds), num: true }], s.by_task) }));
  }
  if (s.by_workspace.length) {
    grid.appendChild(card({ title: 'Editor workspaces', sub: 'repos / folders open in your editor',
      chart: () => hbars(s.by_workspace.slice(0, 12).map(w => ({ label: w.workspace, value: w.seconds, color: 'var(--s1)' }))),
      table: () => dataTable([{ h: 'Workspace', k: 'workspace' }, { h: 'Editor', k: 'app' }, { h: 'Time', k: w => fmtDur(w.seconds), num: true }], s.by_workspace) }));
  }
  const pById = Object.fromEntries(projectsCache.map(p => [String(p.id), p]));
  const projCell = key => key === 'cs' ? el('span', { class: 'pill', text: 'Context switching' }) : (key === 'none' ? el('span', { class: 'muted', text: 'Unassigned' }) : projPill(pById[key]));
  grid.appendChild(card({ title: 'Top activities', sub: 'what you actually had in front of you', span2: true,
    chart: () => dataTable([{ h: 'App', k: 'app' }, { h: 'Window / page', k: 'title', trunc: true }, { h: 'Context', k: 'context' }, { h: 'Project', k: a => projCell(a.project) }, { h: 'Category', k: a => catPill(a.category) }, { h: 'Time', k: a => fmtDur(a.seconds), num: true }], s.top_activities.slice(0, 25)) }));
  grid.appendChild(card({ title: 'Longest sessions', sub: 'uninterrupted stretches in one app', span2: true,
    chart: () => dataTable([{ h: 'When', k: ss => `${fmtDay(isoDate(new Date(ss.start * 1000)))} ${fmtClock(ss.start)}–${fmtClock(ss.end)}` }, { h: 'App', k: 'app' }, { h: 'Context', k: ss => ss.context || ss.title || '', trunc: true }, { h: 'Projects', k: ss => mixBar(ss.projects, ss.seconds, k => k === 'cs' ? 'var(--muted)' : projectColor(pById[k])) }, { h: 'Focused', k: ss => fmtDur(ss.seconds), num: true }], s.longest_sessions) }));
  return wrap;
}

// ---- trends -----------------------------------------------------------------
async function renderTrends() {
  const [s, ph] = await Promise.all([api.get('/api/summary?' + filterQuery()), api.get('/api/plan/history?' + filterQuery(false))]);
  const t = s.totals, days = s.per_day, n = days.length;
  const activeDays = days.filter(d => d.productive + d.neutral + d.distracting + d.switching > 0);
  const avg = k => activeDays.length ? activeDays.reduce((a, d) => a + d[k], 0) / activeDays.length : 0;
  const best = [...activeDays].sort((a, b) => b.productive - a.productive)[0];
  const busiestHour = s.per_hour.reduce((a, h) => (h.productive + h.neutral + h.distracting > (a.productive + a.neutral + a.distracting) ? h : a), s.per_hour[0]);
  const wrap = el('div');
  wrap.appendChild(el('div', { class: 'tiles' },
    tile({ label: 'Active days', value: String(activeDays.length), delta: `of ${n} in range`, hero: true }),
    tile({ label: 'Productive per active day', value: fmtDur(avg('productive')), delta: `${fmtDur(avg('neutral'))} neutral · ${fmtDur(avg('distracting'))} distracting` }),
    tile({ label: 'Switches per active day', value: String(Math.round(avg('switches'))), delta: `${fmtDur(avg('switching'))} context switching` }),
    tile({ label: 'Best day', value: best ? fmtDay(best.day) : '—', delta: best ? `${fmtDur(best.productive)} productive` : '' }),
    tile({ label: 'Peak hour', value: busiestHour ? `${pad(busiestHour.hour)}:00` : '—', delta: busiestHour ? `${fmtDur(busiestHour.productive + busiestHour.neutral + busiestHour.distracting)} across range` : '' }),
  ));
  const grid = el('div', { class: 'grid' });
  wrap.appendChild(grid);
  const pSeries = projectSeriesFrom(s.by_project);
  grid.appendChild(card({ title: 'Projects per day', sub: 'top 7 projects, rest folded into Other', span2: true,
    chart: () => stackedColumns(days.map(d => ({ label: dayLabel(d), tipTitle: fmtDay(d.day), values: dayValuesByProject(d, pSeries) })), pSeries),
    table: () => dataTable([{ h: 'Day', k: d => fmtDay(d.day) }, ...pSeries.map(sr => ({ h: sr.label, k: d => fmtDur(dayValuesByProject(d, pSeries)[sr.key]), num: true }))], days) }));
  grid.appendChild(card({ title: 'Categories per day', span2: true,
    chart: () => stackedColumns(days.map(d => ({ label: dayLabel(d), tipTitle: fmtDay(d.day), values: d })), catSeries),
    table: () => dataTable([{ h: 'Day', k: d => fmtDay(d.day) }, ...CAT_ORDER.map(c => ({ h: catLabel(c), k: d => fmtDur(d[c]), num: true }))], days) }));
  grid.appendChild(card({ title: 'When you work', sub: 'weekday × hour, total time in range', span2: true,
    chart: () => heatmap(s.heatmap, { days: Math.max(1, Math.round(n / 7)) }),
    table: () => dataTable([{ h: 'Day', k: r => WD[r.wd] }, ...[6, 9, 12, 15, 18, 21].map(h => ({ h: `${pad(h)}–${pad(h + 3)}`, k: r => fmtDur(r.row.slice(h, h + 3).reduce((a, b) => a + b, 0)), num: true })), { h: 'Total', k: r => fmtDur(r.row.reduce((a, b) => a + b, 0)), num: true }], s.heatmap.map((row, wd) => ({ wd, row }))) }));
  grid.appendChild(card({ title: 'By hour of day',
    chart: () => stackedColumns(s.per_hour.map(h => ({ label: pad(h.hour), tipTitle: `${pad(h.hour)}:00–${pad(h.hour + 1)}:00`, values: h })), catSeries, { noLegend: true }),
    table: () => dataTable([{ h: 'Hour', k: h => `${pad(h.hour)}:00` }, ...CAT_ORDER.map(c => ({ h: catLabel(c), k: h => fmtDur(h[c]), num: true }))], s.per_hour) }));
  grid.appendChild(card({ title: 'By day of week',
    chart: () => stackedColumns(s.per_weekday.map(w => ({ label: WD[w.weekday], values: w })), catSeries, { noLegend: true }),
    table: () => dataTable([{ h: 'Day', k: w => WD[w.weekday] }, ...CAT_ORDER.map(c => ({ h: catLabel(c), k: w => fmtDur(w[c]), num: true })), { h: 'Switches', k: 'switches', num: true }], s.per_weekday) }));
  if (ph.days.length) {
    const planSeries = [{ key: 'on_plan', label: 'On plan', color: 'var(--s1)' }, { key: 'missed', label: 'Planned but not followed', color: 'var(--other)' }, { key: 'outside', label: 'Work outside the plan', color: 'var(--s3)' }];
    grid.appendChild(card({ title: 'Plan adherence per day', sub: 'planned time you actually spent on the planned project', span2: true,
      chart: () => stackedColumns(ph.days.map(d => ({ label: `${WD[d.weekday].slice(0, 2)} ${new Date(d.day + 'T12:00:00').getDate()}`, tipTitle: fmtDay(d.day), values: d, tipFoot: d.adherence == null ? null : `${Math.round(100 * d.adherence)}% of ${fmtDur(d.elapsed)} planned` })), planSeries),
      table: () => dataTable([{ h: 'Day', k: d => fmtDay(d.day) }, { h: 'Blocks', k: 'blocks', num: true }, { h: 'Planned', k: d => fmtDur(d.planned), num: true }, { h: 'On plan', k: d => fmtDur(d.on_plan), num: true }, { h: 'Adherence', k: d => d.adherence == null ? '—' : `${Math.round(100 * d.adherence)}%`, num: true }, { h: 'Outside plan', k: d => fmtDur(d.outside), num: true }], ph.days) }));
  }
  const swSeries = [{ key: 'switches', label: 'Switches', color: 'var(--s1)' }];
  grid.appendChild(card({ title: 'Switches per day', sub: 'app + window + tab changes',
    chart: () => stackedColumns(days.map(d => ({ label: dayLabel(d), tipTitle: fmtDay(d.day), values: { switches: d.switches } })), swSeries, { count: true }),
    table: () => dataTable([{ h: 'Day', k: d => fmtDay(d.day) }, { h: 'Switches', k: 'switches', num: true }, { h: 'Context switching', k: d => fmtDur(d.switching), num: true }], days) }));
  grid.appendChild(card({ title: 'Switches per hour',
    chart: () => stackedColumns(s.per_hour.map(h => ({ label: pad(h.hour), tipTitle: `${pad(h.hour)}:00`, values: { switches: h.switches } })), swSeries, { count: true }),
    table: () => dataTable([{ h: 'Hour', k: h => `${pad(h.hour)}:00` }, { h: 'Switches', k: 'switches', num: true }], s.per_hour) }));
  return wrap;
}

// ---- lanes shared by Timeline and Plan ----------------------------------------
function projectRuns(segments, pById) {
  const runs = [];
  let run = null;
  const flush = () => { if (run) runs.push(run); run = null; };
  for (const sg of segments) {
    if (sg.kind === 'idle' || sg.kind === 'locked') { flush(); continue; }
    const key = sg.cs ? 'cs' : String(sg.project_id == null ? 'none' : sg.project_id);
    if (run && run.key === key && sg.start - run.end < 60) run.end = sg.end;
    else {
      flush();
      run = { key, start: sg.start, end: sg.end, color: key === 'cs' ? 'var(--muted)' : (key === 'none' ? 'var(--other)' : projectColor(pById[key])),
        label: key === 'cs' ? 'Context switching' : (key === 'none' ? 'Unassigned' : (pById[key] || {}).title || 'Project') };
    }
  }
  flush();
  return runs;
}
function planRuns(blocks, pById) {
  return blocks.map(b => {
    const p = pById[String(b.project_id)];
    return { key: String(b.project_id), start: b.start, end: b.end, color: projectColor(p), labelInside: true,
      label: (p ? p.title : 'Project') + (b.task_title ? ' › ' + b.task_title : ''), foot: b.note || null };
  });
}
function laneEl(runs, t0, span, cls = '') {
  const x = ts => `${(100 * (ts - t0) / span).toFixed(3)}%`;
  const w = (a, b) => `${Math.max(0.05, 100 * (b - a) / span).toFixed(3)}%`;
  const lane = el('div', { class: `tl-lane ${cls}` });
  for (const r of runs) {
    lane.appendChild(hoverable(el('div', { class: 'blk', style: `left:${x(r.start)};width:${w(r.start, r.end)};background:${r.color}` }, r.labelInside ? el('span', { text: r.label }) : null),
      () => tipRows(r.label, [{ label: `${fmtClock(r.start)}–${fmtClock(r.end)}`, value: fmtDur(r.end - r.start) }], r.foot)));
  }
  return lane;
}
function hourAxis() {
  const hours = el('div', { class: 'tl-hours' });
  for (let h = 0; h <= 24; h += 2) hours.appendChild(el('span', { style: `left:${(100 * h / 24).toFixed(2)}%`, text: pad(h % 24) }));
  return hours;
}

// ---- timeline ---------------------------------------------------------------
async function renderTimeline(arg) {
  let openLog = false;
  if (arg) {
    for (const part of arg.split('/')) {
      if (/^\d{4}-\d{2}-\d{2}$/.test(part)) state.date = part;
      if (part === 'log') openLog = true;
    }
    history.replaceState(null, '', '#timeline');
  }
  if (!state.date) state.date = todayIso();
  saveFilters();
  const data = await api.get(`/api/timeline?date=${state.date}`);
  const { t0, t1 } = data;
  const span = t1 - t0;
  const pById = Object.fromEntries(data.projects.map(p => [String(p.id), p]));
  const pName = id => (id == null ? null : (pById[String(id)] || {}).title);
  const wrap = el('div');

  const head = el('div', { class: 'tl-head' },
    el('button', { class: 'btn small', text: '‹', onclick: () => { state.date = shiftIso(state.date, -1); saveFilters(); render(); } }),
    el('button', { class: 'btn small', text: '›', disabled: state.date >= todayIso(), onclick: () => { state.date = shiftIso(state.date, 1); saveFilters(); render(); } }),
    el('h2', { text: fmtDay(state.date, true) }),
    el('input', { type: 'date', class: 'input', value: state.date, max: todayIso(), onchange: e => { if (e.target.value) { state.date = e.target.value; saveFilters(); render(); } } }),
    state.date !== todayIso() ? el('button', { class: 'btn ghost small', text: 'Today', onclick: () => { state.date = todayIso(); saveFilters(); render(); } }) : null,
    el('span', { style: 'margin-left:auto' }),
    el('a', { class: 'btn small', href: `#plan/${state.date}`, text: data.plan_blocks && data.plan_blocks.length ? 'Plan vs actual' : 'Plan this day' }),
    el('button', { class: 'btn small', text: 'Assign a time range…', onclick: () => openAssign({ start: t0 + 9 * 3600, end: t0 + 10 * 3600 }) }),
    el('button', { class: 'btn primary small', text: 'Log time away…', onclick: () => openLogAway({}) }),
  );
  wrap.appendChild(head);

  // totals for the day
  const totals = { productive: 0, neutral: 0, distracting: 0, switching: 0, idle: 0, manual: 0 };
  let firstActive = null, lastActive = null;
  for (const sg of data.segments) {
    if (sg.kind === 'idle' || sg.kind === 'locked') continue;
    firstActive = firstActive == null ? sg.start : Math.min(firstActive, sg.start);
    lastActive = lastActive == null ? sg.end : Math.max(lastActive, sg.end);
  }
  for (const sg of data.segments) {
    const d = sg.end - sg.start;
    if (sg.kind === 'idle' || sg.kind === 'locked') {
      if (firstActive != null) { const o = Math.min(sg.end, lastActive) - Math.max(sg.start, firstActive); if (o > 0) totals.idle += o; }
    } else { totals[sg.category || 'neutral'] += d; if (sg.kind === 'manual') totals.manual += d; }
  }
  const dayGaps = data.gaps.filter(g => firstActive != null && g.end > firstActive && g.start < lastActive);
  const active = totals.productive + totals.neutral + totals.distracting + totals.switching;
  wrap.appendChild(el('div', { class: 'tiles' },
    tile({ label: 'Tracked', value: fmtDur(active), hero: true, delta: totals.manual ? `incl. ${fmtDur(totals.manual)} logged manually` : '' }),
    tile({ label: 'Productive', value: fmtDur(totals.productive), delta: active ? `${pct(totals.productive, totals.productive + totals.neutral + totals.distracting)}% of attributable time` : '' }),
    tile({ label: 'Distracting', value: fmtDur(totals.distracting) }),
    tile({ label: 'Context switching', value: fmtDur(totals.switching), delta: `${data.segments.filter(sg => sg.kind === 'active' && ['app', 'window', 'tab'].includes(sg.switch_kind)).length} switches` }),
    tile({ label: 'Breaks', value: fmtDur(totals.idle), delta: `${dayGaps.length} gap${dayGaps.length === 1 ? '' : 's'} ≥ ${statusCache ? statusCache.settings.min_idle_gap_minutes : 5} min between first and last activity` }),
  ));

  // the track
  const x = ts => `${(100 * (ts - t0) / span).toFixed(3)}%`;
  const w = (a, b) => `${Math.max(0.05, 100 * (b - a) / span).toFixed(3)}%`;
  const hours = hourAxis();
  const track = el('div', { class: 'tl-track' });
  const sessionFor = sg => data.sessions.find(ss => ss.bundle === sg.bundle && ss.start <= sg.start && ss.end >= sg.end);
  for (const sg of data.segments) {
    const idle = sg.kind === 'idle' || sg.kind === 'locked';
    const blk = el('div', {
      class: `blk ${idle ? 'idle' : ''} ${sg.kind === 'manual' ? 'manual' : ''} ${sg.cs ? 'cs' : ''}`,
      style: `left:${x(sg.start)};width:${w(sg.start, sg.end)};${idle ? '' : `background-color:${catColor(sg.category)}`}`,
      tabindex: '0',
    });
    hoverable(blk, () => tipRows(idle ? (sg.kind === 'locked' ? 'Locked / asleep' : 'Idle') : (sg.kind === 'manual' ? `Logged: ${sg.title}` : `${sg.app}${sg.domain ? ' · ' + sg.domain : (sg.workspace ? ' · ' + sg.workspace : '')}`), [
      { label: `${fmtClock(sg.start)}–${fmtClock(sg.end)}`, value: fmtDur(sg.end - sg.start) },
      !idle && sg.title && sg.kind !== 'manual' ? { label: sg.title.length > 60 ? sg.title.slice(0, 60) + '…' : sg.title, value: '', dim: true } : null,
      !idle ? { color: catColor(sg.category), label: sg.cs ? 'Context switching' : catLabel(sg.category), value: '' } : null,
      !idle && !sg.cs ? { label: pName(sg.project_id) || 'Unassigned', value: sg.source === 'assignment' ? 'manual assignment' : (sg.source === 'rule' ? 'rule' : ''), dim: !sg.project_id } : null,
    ], idle ? 'Click to log what you did while away' : (sg.kind === 'manual' ? 'Click to edit' : 'Click to assign this session')));
    blk.addEventListener('click', () => {
      if (idle) openLogAway({ start: sg.start, end: sg.end });
      else if (sg.kind === 'manual') openLogAway({ entry: data.manual.find(m => m.id === sg.manual_id) });
      else { const ss = sessionFor(sg); openAssign(ss ? { start: ss.start, end: ss.end, bundle: ss.bundle, app: ss.app, sessions: [ss] } : { start: sg.start, end: sg.end, bundle: sg.bundle, app: sg.app }); }
    });
    track.appendChild(blk);
  }
  const now = Date.now() / 1000;
  if (now > t0 && now < t1) track.appendChild(el('div', { class: 'now', style: `left:${x(now)}` }));
  const lanes = [el('div', { class: 'lane-label', text: 'Project' }), laneEl(projectRuns(data.segments, pById), t0, span)];
  if (data.plan_blocks && data.plan_blocks.length) lanes.push(el('div', { class: 'lane-label', text: 'Plan' }), laneEl(planRuns(data.plan_blocks, pById), t0, span, 'plan'));
  wrap.appendChild(el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h2', { text: 'Day' }), el('span', { class: 'sub', text: 'top: category (hatched = idle, striped = context switching or logged manually) · below: project, then the plan if there is one' })),
    el('div', { class: 'tl-wrap' }, hours, track, ...lanes),
    legend([...CAT_ORDER.map(c => ({ label: catLabel(c), color: catColor(c) })), { label: 'Idle / away', color: 'var(--cat-idle)' }])));

  // sessions
  const grid = el('div', { class: 'grid', style: 'margin-top:16px' });
  wrap.appendChild(grid);
  const sessCard = el('div', { class: 'card span2 sessions' });
  const shown = data.sessions.filter(ss => state.showShort || ss.seconds >= 60);
  sessCard.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Sessions' }), el('span', { class: 'sub', text: `${shown.length} shown · click Assign to attribute a stretch to a project` }),
    el('div', { class: 'tools' }, el('label', { class: 'f small' }, el('input', { type: 'checkbox', checked: state.showShort, onchange: e => { state.showShort = e.target.checked; saveFilters(); render(); } }), 'show < 1 min'))));
  const selected = new Map();
  const rowBoxes = [];
  const countEl = el('span', { class: 'count' });
  const selbar = el('div', { class: 'selbar', hidden: true }, countEl,
    el('button', { class: 'btn primary small', text: 'Assign selected…', onclick: () => openAssign({ sessions: [...selected.values()] }) }),
    el('button', { class: 'btn ghost small', text: 'Select all unassigned', onclick: () => { for (const r of rowBoxes) if (r.unassigned && !r.cb.checked) { r.cb.checked = true; r.cb.dispatchEvent(new Event('change')); } } }),
    el('button', { class: 'btn ghost small', text: 'Clear', onclick: () => { for (const r of rowBoxes) if (r.cb.checked) { r.cb.checked = false; r.cb.dispatchEvent(new Event('change')); } } }));
  const updateBar = () => {
    const n = selected.size;
    selbar.hidden = n === 0;
    if (n) countEl.textContent = `${n} session${n === 1 ? '' : 's'} selected · ${fmtDur([...selected.values()].reduce((a, x) => a + x.seconds, 0))}`;
  };
  sessCard.appendChild(el('div', { class: 'sess head' }, el('span'), el('span', { text: 'Time' }), el('span', { text: 'App' }), el('span', { text: 'Context / window' }), el('span', { text: 'Focused', style: 'text-align:right' }), el('span')));
  const manualSessions = data.manual.map(m => ({ manual: m, start: m.start, end: m.end, seconds: m.end - m.start }));
  const all = [...shown.map(ss => ({ sess: ss, start: ss.start, end: ss.end })), ...manualSessions].sort((a, b) => a.start - b.start);
  if (!all.length) sessCard.appendChild(el('div', { class: 'empty', text: 'Nothing tracked on this day' }));
  for (const item of all) {
    if (item.manual) {
      const m = item.manual;
      sessCard.appendChild(el('div', { class: 'sess manual' }, el('span'),
        el('span', { class: 'time', text: `${fmtClock(m.start)}–${fmtClock(m.end)}` }),
        el('span', { class: 'app' }, el('i', { class: 'swatch', style: `background:${catColor(m.category || (pById[m.project_id] || {}).category || 'neutral')}` }), `Logged: ${m.title}`),
        el('span', { class: 'ctx' }, pName(m.project_id) || 'No project', m.note ? ` · ${m.note}` : ''),
        el('span', { class: 'dur', text: fmtDur(m.end - m.start) }),
        el('span', {}, el('button', { class: 'btn ghost small', text: 'Edit', onclick: () => openLogAway({ entry: m }) }))));
      continue;
    }
    const ss = item.sess;
    const projKeys = Object.entries(ss.projects).sort((a, b) => b[1] - a[1]);
    const mainKey = projKeys[0] ? projKeys[0][0] : 'none';
    const label = mainKey === 'cs' ? 'Context switching' : (mainKey === 'none' ? 'Unassigned' : (pById[mainKey] || {}).title);
    const domCat = Object.entries(ss.categories).sort((a, b) => b[1] - a[1])[0];
    const cb = el('input', { type: 'checkbox', title: 'Select for bulk assign' });
    const row = el('div', { class: 'sess' }, cb,
      el('span', { class: 'time', text: `${fmtClock(ss.start)}–${fmtClock(ss.end)}` }),
      el('span', { class: 'app', title: ss.app }, el('i', { class: 'swatch', style: `background:${catColor(domCat ? domCat[0] : 'neutral')}` }), ss.app, projKeys.length > 1 ? mixBar(ss.projects, ss.seconds, k => k === 'cs' ? 'var(--muted)' : projectColor(pById[k])) : null),
      el('span', { class: 'ctx', title: ss.title }, ss.context ? el('b', { text: ss.context }) : null, ss.context && ss.title ? ' · ' : '', ss.title || '', ' ', el('span', { class: 'pill', text: label })),
      el('span', { class: 'dur', text: fmtDur(ss.seconds) }),
      el('span', {}, el('button', { class: 'btn ghost small', text: 'Assign', onclick: () => openAssign({ start: ss.start, end: ss.end, bundle: ss.bundle, app: ss.app, sessions: [ss] }) })));
    cb.addEventListener('change', () => { if (cb.checked) selected.set(ss.id, ss); else selected.delete(ss.id); row.classList.toggle('selected', cb.checked); updateBar(); });
    rowBoxes.push({ cb, unassigned: mainKey === 'none' });
    sessCard.appendChild(row);
  }
  if (shown.length) sessCard.appendChild(el('p', { class: 'muted small', style: 'margin-top:8px', text: 'Tick several sessions to assign them all at once.' }));
  sessCard.appendChild(selbar);
  grid.appendChild(sessCard);

  if (data.assignments.length) {
    grid.appendChild(card({ title: 'Manual assignments on this day', sub: 'override rules for a time range', span2: true,
      chart: () => dataTable([{ h: 'Range', k: a => `${fmtClock(a.start)}–${fmtClock(a.end)}` }, { h: 'Scope', k: a => a.bundle ? (data.sessions.find(ss => ss.bundle === a.bundle) || {}).app || a.bundle : 'All apps' }, { h: 'Project', k: a => pName(a.project_id) || '—' }, { h: 'Task', k: a => (data.tasks.find(t => t.id === a.task_id) || {}).title || '' }, { h: 'Category', k: a => a.category ? catPill(a.category) : el('span', { class: 'muted', text: 'auto' }) }, { h: 'Note', k: 'note', trunc: true }, { h: '', k: a => el('button', { class: 'btn ghost small danger', text: 'Remove', onclick: async () => { await api.del(`/api/assignments/${a.id}`); toast('Assignment removed'); render(); } }) }], data.assignments) }));
  }

  // modals
  function openAssign({ start, end, bundle, app, sessions }) {
    const list = sessions && sessions.length ? sessions : [];
    const bulk = list.length > 1;
    const totalSecs = list.reduce((a, x) => a + x.seconds, 0);
    const title = bulk ? `Assign ${list.length} sessions · ${fmtDur(totalSecs)}` : (bundle ? `Assign ${app} · ${fmtClock(start)}–${fmtClock(end)}` : 'Assign a time range');
    // windows / pages / workspaces seen in the selected sessions, merged across sessions
    const idents = {};
    for (const x of list) for (const it of (x.identities || [])) {
      const k = `${it.field}:${it.pattern.toLowerCase()}`;
      const e = idents[k] || (idents[k] = { field: it.field, pattern: it.pattern, label: it.label, seconds: 0 });
      e.seconds += it.seconds;
    }
    const identList = Object.values(idents).sort((a, b) => b.seconds - a.seconds);
    openModal(title, close => {
      const { pSel, tSel } = projectTaskFields(null, null, data.projects, data.tasks);
      const cSel = categorySelect('');
      const sIn = el('input', { type: 'datetime-local', class: 'input', value: toLocalInput(bulk ? list[0].start : start) });
      const eIn = el('input', { type: 'datetime-local', class: 'input', value: toLocalInput(bulk ? list[0].end : end) });
      const scope = (bulk || bundle)
        ? select([{ value: 'app', label: bulk ? "Only each session's app in its range" : `Only ${app} in this range` }, { value: 'all', label: bulk ? 'Everything in each range' : 'Everything in this range' }], 'app')
        : select([{ value: 'all', label: 'Everything in this range' }], 'all');
      const note = el('input', { type: 'text', class: 'input', placeholder: 'optional note' });
      const err = el('div', { class: 'err' });
      const boxes = identList.map(it => ({ it, cb: el('input', { type: 'checkbox', checked: identList.length === 1 || it.seconds >= Math.max(300, 0.25 * totalSecs) }) }));
      const allCb = el('input', { type: 'checkbox', checked: boxes.some(b => b.cb.checked), onchange: e => boxes.forEach(b => { b.cb.checked = e.target.checked; }) });
      const remember = identList.length ? el('div', {},
        el('label', { class: 'f', style: 'display:flex;align-items:center;gap:8px;margin-bottom:4px;color:var(--ink);font-size:13px' }, allCb, el('b', { text: 'Remember for next time' })),
        el('p', { class: 'muted small', style: 'margin-bottom:6px', text: 'Also assign these windows / pages / workspaces whenever they show up again (past and future). Each one becomes a rule you can edit on the Sort tab.' }),
        el('div', { class: 'remember' }, boxes.map(b => el('label', {}, b.cb, el('span', { class: 'field-tag', text: b.it.field }), el('span', { class: 'lbl', title: `${b.it.field}: ${b.it.pattern}`, text: b.it.label }), el('span', { class: 'secs', text: fmtDur(b.it.seconds) }))))) : null;
      const summary = bulk ? el('p', { class: 'muted small', text: list.slice(0, 6).map(x => `${fmtClock(x.start)}–${fmtClock(x.end)} ${x.app}`).join(' · ') + (list.length > 6 ? ` · and ${list.length - 6} more` : '') }) : null;
      const form = el('form', { class: 'form', onsubmit: async e => {
        e.preventDefault();
        if (!pSel.value && !tSel.value && !cSel.value) { err.textContent = 'Pick a project, a task or a category'; return; }
        const common = { project_id: pSel.value || null, task_id: tSel.value || null, category: cSel.value || null, note: note.value };
        if (bulk) {
          await api.post('/api/assignments/bulk', { items: list.map(x => ({ start: x.start, end: x.end, bundle: scope.value === 'app' ? x.bundle : null })), ...common });
        } else {
          const s0 = fromLocalInput(sIn.value), e0 = fromLocalInput(eIn.value);
          if (!s0 || !e0 || e0 <= s0) { err.textContent = 'End must be after start'; return; }
          await api.post('/api/assignments', { start: s0, end: e0, bundle: scope.value === 'app' ? (list[0] ? list[0].bundle : bundle) : null, ...common });
        }
        const chosen = boxes.filter(b => b.cb.checked).map(b => ({ field: b.it.field, pattern: b.it.pattern }));
        let ruleMsg = '';
        if (chosen.length) {
          const r = await api.post('/api/rules/auto', { rules: chosen, ...common });
          const n = r.created + r.updated;
          ruleMsg = ` · ${n} window${n === 1 ? '' : 's'} remembered`;
        }
        close(); toast((bulk ? `Assigned ${list.length} sessions` : 'Assigned') + ruleMsg); render();
      } },
        summary,
        bulk ? null : el('div', { class: 'row' }, field('From', sIn), field('To', eIn)),
        el('div', { class: 'row' }, field('Project', pSel), field('Task', tSel)),
        el('div', { class: 'row' }, field('Category', cSel), field('Scope', scope)),
        field('Note', note),
        remember, err,
        el('div', { class: 'actions' },
          el('a', { class: 'left small muted', href: '#sort', text: 'Rules live on the Sort tab →' }),
          el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: close }), el('button', { type: 'submit', class: 'btn primary', text: bulk ? `Assign ${list.length} sessions` : 'Assign' })));
      return form;
    });
  }
  function defaultLogRange() {
    const g = dayGaps[dayGaps.length - 1];
    if (g) return [Math.max(g.start, firstActive), Math.min(g.end, lastActive, now)];
    if (state.date === todayIso()) { const s0 = Math.floor(now / 1800) * 1800 - 3600; return [s0, s0 + 3600]; }
    return [t0 + 12 * 3600, t0 + 13 * 3600];
  }
  function openLogAway({ start, end, entry }) {
    const isEdit = !!entry;
    const [ds, de] = (start == null && !entry) ? defaultLogRange() : [start, end];
    const s = entry ? entry.start : ds;
    const e2 = entry ? entry.end : (de || s + 3600);
    openModal(isEdit ? 'Edit logged time' : 'Log time away from the laptop', close => {
      const titleIn = el('input', { type: 'text', class: 'input', required: true, placeholder: 'e.g. Reading paper, Meeting with advisor, Whiteboarding', value: entry ? entry.title : '', list: 'manual-titles' });
      const dl = el('datalist', { id: 'manual-titles' }, [...new Set(data.manual.map(m => m.title))].map(t => el('option', { value: t })));
      const { pSel, tSel } = projectTaskFields(entry ? entry.project_id : null, entry ? entry.task_id : null, data.projects, data.tasks);
      const cSel = categorySelect(entry ? entry.category : '', 'Auto (project default, else neutral)');
      const sIn = el('input', { type: 'datetime-local', class: 'input', value: toLocalInput(s) });
      const eIn = el('input', { type: 'datetime-local', class: 'input', value: toLocalInput(e2) });
      const note = el('input', { type: 'text', class: 'input', placeholder: 'optional note', value: entry ? entry.note : '' });
      const err = el('div', { class: 'err' });
      const form = el('form', { class: 'form', onsubmit: async ev => {
        ev.preventDefault();
        const s0 = fromLocalInput(sIn.value), e0 = fromLocalInput(eIn.value);
        if (!s0 || !e0 || e0 <= s0) { err.textContent = 'End must be after start'; return; }
        const body = { start: s0, end: e0, title: titleIn.value.trim(), project_id: pSel.value || null, task_id: tSel.value || null, category: cSel.value || null, note: note.value };
        if (isEdit) await api.put(`/api/manual/${entry.id}`, body); else await api.post('/api/manual', body);
        close(); toast(isEdit ? 'Updated' : 'Logged'); render();
      } },
        el('p', { class: 'muted small', text: 'Time you log here counts as tracked time and replaces anything recorded in the same range.' }),
        field('What were you doing?', titleIn), dl,
        el('div', { class: 'row' }, field('From', sIn), field('To', eIn)),
        el('div', { class: 'row' }, field('Project', pSel), field('Task', tSel)),
        field('Category', cSel), field('Note', note), err,
        el('div', { class: 'actions' },
          isEdit ? el('button', { type: 'button', class: 'btn danger left', text: 'Delete', onclick: async () => { await api.del(`/api/manual/${entry.id}`); close(); toast('Deleted'); render(); } }) : null,
          el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: close }), el('button', { type: 'submit', class: 'btn primary', text: isEdit ? 'Save' : 'Log it' })));
      return form;
    });
  }
  if (openLog) setTimeout(() => openLogAway({}), 50);
  return wrap;
}

// ---- projects ---------------------------------------------------------------
const ratioClass = r => (r == null ? '' : (r > 1.1 ? 'over' : (r < 0.9 ? 'under' : '')));
const ratioText = r => (r == null ? '—' : `${r >= 1 ? '+' : '−'}${Math.round(Math.abs(r - 1) * 100)}%`);
const fmtEst = h => (h == null ? '' : (Number.isInteger(h) ? `${h}h` : `${h}h`));

function estimateLine(stats, done, completedAt) {
  const est = stats.estimate_seconds;
  const line = el('div', { class: 'est-line' });
  if (done) {
    const when = completedAt ? `Completed ${fmtDay(isoDate(new Date(completedAt * 1000)))}` : 'Completed';
    if (est) line.append(`${when} · estimated ${fmtDur(est)}, took ${fmtDur(stats.seconds)} (`, el('span', { class: `delta ${ratioClass(stats.ratio)}`, text: ratioText(stats.ratio) }), `) over ${stats.active_days} active day${stats.active_days === 1 ? '' : 's'}`);
    else line.append(`${when} · ${fmtDur(stats.seconds)} over ${stats.active_days} active day${stats.active_days === 1 ? '' : 's'} (no estimate was set)`);
    return [line];
  }
  if (!est) {
    line.append(`${fmtDur(stats.seconds)} all time`, stats.active_days ? ` · ${stats.active_days} active day${stats.active_days === 1 ? '' : 's'} since ${fmtDay(stats.first_day)}` : '', ' · no estimate yet (Edit)');
    return [line];
  }
  const ratio = stats.ratio || 0;
  const meter = el('div', { class: 'meter', title: `${Math.round(ratio * 100)}% of estimate` }, el('i', { class: ratio > 1 ? 'over' : '', style: `width:${Math.min(100, ratio * 100).toFixed(1)}%` }));
  const pace = stats.pace ? `${fmtDur(stats.pace)}/active day` : '';
  const proj = stats.projected_days != null ? ` · ~${Math.max(1, Math.round(stats.projected_days))} more active day${Math.round(stats.projected_days) === 1 ? '' : 's'} at recent pace (${pace})` : (ratio >= 1 ? ' · over the estimate' : '');
  line.append(`${fmtDur(stats.seconds)} of ${fmtDur(est)} estimated (${Math.round(ratio * 100)}%)`, stats.active_days ? ` · ${stats.active_days} active day${stats.active_days === 1 ? '' : 's'} since ${fmtDay(stats.first_day)}` : '', proj);
  return [el('div', { style: 'margin-left:20px' }, meter), line];
}

async function renderProjects() {
  const [ps, s] = await Promise.all([api.get('/api/projects/stats'), api.get('/api/summary?' + filterQuery(false))]);
  projectsCache = ps.projects;
  const byProj = Object.fromEntries(s.by_project.map(p => [String(p.id), p.seconds]));
  const [from, to] = rangeDates();
  const rangeLabel = from === to ? fmtDay(from) : `${fmtDay(from)} – ${fmtDay(to)}`;
  const wrap = el('div');

  // calibration: how good are your estimates?
  const cal = ps.calibration_summary;
  if (cal) {
    const c = ps.calibration;
    wrap.appendChild(card({ title: 'Estimation accuracy', sub: `${cal.count} completed project${cal.count === 1 ? '' : 's'} and tasks with an estimate`, cls: 'calib',
      chart: () => el('div', {},
        el('p', { style: 'margin-bottom:10px' }, 'On average you take ', el('b', { text: `${cal.mean_ratio.toFixed(2)}×` }), ' your estimate (median ', el('b', { text: `${cal.median_ratio.toFixed(2)}×` }), `). ${cal.under} ran over by more than 10%, ${cal.over} came in under by more than 10%. In total: ${fmtDur(cal.total_estimated)} estimated, ${fmtDur(cal.total_actual)} actual.`),
        dataTable([{ h: 'Item', k: r => el('span', {}, r.kind === 'task' ? el('span', { class: 'muted', text: `${r.project} › ` }) : null, r.title) }, { h: 'Type', k: r => el('span', { class: 'pill', text: r.kind }) }, { h: 'Estimated', k: r => fmtDur(r.estimate_seconds), num: true }, { h: 'Actual', k: r => fmtDur(r.actual_seconds), num: true }, { h: 'Ratio', k: r => el('span', { class: `ratio ${ratioClass(r.ratio)}`, text: `${r.ratio.toFixed(2)}× (${ratioText(r.ratio)})` }), num: true }, { h: 'Completed', k: r => r.completed_at ? fmtDay(isoDate(new Date(r.completed_at * 1000))) : '' }], c)),
    }));
    wrap.lastChild.style.marginBottom = '16px';
  }

  // new project form
  const title = el('input', { type: 'text', class: 'input', placeholder: 'New project title', required: true, style: 'min-width:200px' });
  const desc = el('input', { type: 'text', class: 'input', placeholder: 'Brief description', style: 'flex:1;min-width:220px' });
  const cat = select(CAT_ORDER.slice(0, 3).map(c => ({ value: c, label: `${catLabel(c)} by default` })), 'productive');
  const estIn = el('input', { type: 'number', class: 'input', placeholder: 'est. hours', min: 0, step: 0.5, style: 'width:100px', title: 'Estimated total hours to completion' });
  wrap.appendChild(el('div', { class: 'card', style: 'margin-bottom:16px' },
    el('form', { class: 'inline-form', onsubmit: async e => { e.preventDefault(); await api.post('/api/projects', { title: title.value, description: desc.value, category: cat.value, estimate_hours: estIn.value || null }); toast('Project created'); render(); } },
      title, desc, cat, estIn, el('button', { type: 'submit', class: 'btn primary', text: 'Add project' }))));

  const list = el('div', { class: 'grid' });
  wrap.appendChild(list);
  const open = projectsCache.filter(p => !p.archived && !p.done), done = projectsCache.filter(p => p.done && !p.archived), archived = projectsCache.filter(p => p.archived);
  if (!projectsCache.length) list.appendChild(el('div', { class: 'card span2 empty', text: 'No projects yet. Add one above, then sort apps and sites into it on the Sort tab.' }));

  const renderTask = (p, t) => {
    const st = t.stats || { seconds: 0 };
    const est = el('input', { type: 'number', class: 'input est', min: 0, step: 0.5, placeholder: 'est h', value: t.estimate_hours ?? '', title: 'Estimated hours', onchange: async e => { await api.put(`/api/tasks/${t.id}`, { estimate_hours: e.target.value || null }); toast('Estimate saved'); render(); } });
    let note = null;
    if (t.done && st.estimate_seconds) note = el('span', { class: `est-note delta ${ratioClass(st.ratio)}`, text: `${fmtDur(st.estimate_seconds)} est → ${fmtDur(st.seconds)} (${ratioText(st.ratio)})` });
    else if (st.estimate_seconds) note = el('span', { class: 'est-note', text: `${Math.round((st.ratio || 0) * 100)}% of ${fmtDur(st.estimate_seconds)}` });
    return el('div', { class: `task ${t.done ? 'done' : ''}` },
      el('input', { type: 'checkbox', checked: !!t.done, title: t.done ? 'Reopen' : 'Mark complete', onchange: async e => { await api.put(`/api/tasks/${t.id}`, { done: e.target.checked }); render(); } }),
      el('span', { class: 't', text: t.title, title: t.description || '' }),
      el('span', { class: 'dur', text: st.seconds ? fmtDur(st.seconds) : '' }),
      note || (t.done ? null : est),
      el('button', { class: 'x', text: '✕', title: 'Delete task', onclick: async () => { if (confirm(`Delete task "${t.title}"?`)) { await api.del(`/api/tasks/${t.id}`); render(); } } }));
  };
  const renderProject = p => {
    const st = p.stats || { seconds: 0, active_days: 0 };
    const c = el('div', { class: `card project ${p.archived ? 'archived' : ''} ${p.done ? 'done' : ''}` });
    c.appendChild(el('div', { class: 'ph' },
      el('h2', {}, el('i', { class: 'swatch', style: `background:${projectColor(p)}` }), p.title),
      catPill(p.category),
      p.done ? el('span', { class: 'pill state', text: '✓ completed' }) : null,
      el('span', { class: 'tot', text: fmtDur(byProj[String(p.id)] || 0), title: `time in ${rangeLabel}` }),
      el('button', { class: 'btn ghost small', text: 'Edit', onclick: () => editProject(p) })));
    if (p.description) c.appendChild(el('p', { class: 'desc', text: p.description }));
    c.append(...estimateLine(st, p.done, p.completed_at));
    const tasks = el('div', { class: 'tasks' });
    for (const t of p.tasks) tasks.appendChild(renderTask(p, t));
    if (!p.done) {
      const tIn = el('input', { type: 'text', class: 'input', placeholder: 'Add a sub-task…', style: 'flex:1' });
      const tEst = el('input', { type: 'number', class: 'input est', min: 0, step: 0.5, placeholder: 'est h', title: 'Estimated hours' });
      tasks.appendChild(el('form', { class: 'task', onsubmit: async e => { e.preventDefault(); if (!tIn.value.trim()) return; await api.post('/api/tasks', { project_id: p.id, title: tIn.value.trim(), estimate_hours: tEst.value || null }); render(); } }, tIn, tEst, el('button', { type: 'submit', class: 'btn ghost small', text: 'Add' })));
    }
    c.appendChild(tasks);
    c.appendChild(el('div', { style: 'display:flex;gap:8px;margin-top:10px;margin-left:20px' },
      p.done
        ? el('button', { class: 'btn ghost small', text: 'Reopen', onclick: async () => { await api.put(`/api/projects/${p.id}`, { done: false }); toast('Reopened'); render(); } })
        : el('button', { class: 'btn small', text: 'Mark complete', onclick: async () => {
            const open = p.tasks.filter(t => !t.done).length;
            if (confirm(`Mark "${p.title}" as complete?${open ? ` ${open} open task${open === 1 ? '' : 's'} will stay open.` : ''} The estimate vs actual comparison is recorded.`)) { await api.put(`/api/projects/${p.id}`, { done: true }); toast('Completed 🎉'); render(); }
          } })));
    return c;
  };
  open.forEach(p => list.appendChild(renderProject(p)));
  if (done.length) {
    list.appendChild(el('h3', { class: 'span2', text: `Completed (${done.length})`, style: 'margin-top:8px' }));
    done.forEach(p => list.appendChild(renderProject(p)));
  }
  if (archived.length) {
    list.appendChild(el('h3', { class: 'span2', text: `Archived (${archived.length})`, style: 'margin-top:8px' }));
    archived.forEach(p => list.appendChild(renderProject(p)));
  }
  function editProject(p) {
    openModal(`Edit ${p.title}`, close => {
      const t = el('input', { type: 'text', class: 'input', value: p.title, required: true });
      const d = el('textarea', { class: 'input', text: p.description || '' });
      const c = select(CAT_ORDER.slice(0, 3).map(x => ({ value: x, label: catLabel(x) })), p.category);
      const col = select([0, 1, 2, 3, 4, 5, 6, 7].map(i => ({ value: i, label: `Colour ${i + 1}` })), p.color);
      const est = el('input', { type: 'number', class: 'input', min: 0, step: 0.5, value: p.estimate_hours ?? '', placeholder: 'hours to completion' });
      const doneCb = el('input', { type: 'checkbox', checked: !!p.done });
      const arch = el('input', { type: 'checkbox', checked: !!p.archived });
      return el('form', { class: 'form', onsubmit: async e => { e.preventDefault(); const body = { title: t.value, description: d.value, category: c.value, color: Number(col.value), archived: arch.checked, estimate_hours: est.value || null }; if (doneCb.checked !== !!p.done) body.done = doneCb.checked; await api.put(`/api/projects/${p.id}`, body); close(); render(); } },
        field('Title', t), field('Description', d),
        el('div', { class: 'row' }, field('Default category', c), field('Colour', col)),
        field('Estimated total time to completion (hours)', est),
        el('label', { class: 'f' }, doneCb, ' Completed (records the estimate vs actual comparison)'),
        el('label', { class: 'f' }, arch, ' Archived (hidden from pickers, data kept)'),
        el('div', { class: 'actions' },
          el('button', { type: 'button', class: 'btn danger left', text: 'Delete project', onclick: async () => { if (confirm(`Delete "${p.title}" and its tasks, rules, plan blocks and assignments? Tracked time is kept.`)) { await api.del(`/api/projects/${p.id}`); close(); render(); } } }),
          el('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: close }), el('button', { type: 'submit', class: 'btn primary', text: 'Save' })));
    });
  }
  return wrap;
}

// ---- plan (time blocks for a day, and how it went) -----------------------------
async function renderPlan(arg) {
  if (arg) {
    for (const part of arg.split('/')) if (/^\d{4}-\d{2}-\d{2}$/.test(part)) state.date = part;
    history.replaceState(null, '', '#plan');
  }
  if (!state.date) state.date = todayIso();
  saveFilters();
  const [plan, tl] = await Promise.all([api.get(`/api/plan?date=${state.date}`), api.get(`/api/timeline?date=${state.date}`)]);
  const { t0, t1 } = plan, span = t1 - t0, T = plan.totals, blocks = plan.blocks;
  const pById = Object.fromEntries(tl.projects.map(p => [String(p.id), p]));
  const soFar = plan.in_progress ? ' so far' : '';
  const wrap = el('div');
  const nav = d => { state.date = shiftIso(state.date, d); saveFilters(); render(); };
  const copyFrom = async fromDay => {
    if (blocks.length && !confirm('This day already has a plan. Replace it?')) return;
    await api.post('/api/plan/copy', { from_day: fromDay, to_day: state.date, replace: true });
    toast('Plan copied'); render();
  };
  const lastSameWeekday = shiftIso(state.date, -7);
  wrap.appendChild(el('div', { class: 'tl-head' },
    el('button', { class: 'btn small', text: '‹', onclick: () => nav(-1) }),
    el('button', { class: 'btn small', text: '›', onclick: () => nav(1) }),
    el('h2', { text: fmtDay(state.date, true) }),
    el('input', { type: 'date', class: 'input', value: state.date, onchange: e => { if (e.target.value) { state.date = e.target.value; saveFilters(); render(); } } }),
    state.date !== todayIso() ? el('button', { class: 'btn ghost small', text: 'Today', onclick: () => { state.date = todayIso(); saveFilters(); render(); } }) : null,
    el('span', { style: 'margin-left:auto' }),
    el('a', { class: 'btn small', href: `#timeline/${state.date}`, text: 'Timeline' }),
    el('button', { class: 'btn small', text: 'Copy previous day', onclick: () => copyFrom(shiftIso(state.date, -1)) }),
    el('button', { class: 'btn small', text: `Copy last ${WD[(new Date(state.date + 'T12:00:00').getDay() + 6) % 7]}`, onclick: () => copyFrom(lastSameWeekday) }),
    blocks.length ? el('button', { class: 'btn ghost small danger', text: 'Clear', onclick: async () => { if (confirm('Remove all blocks on this day?')) { await api.del(`/api/plan?date=${state.date}`); render(); } } }) : null,
  ));

  if (blocks.length) {
    wrap.appendChild(el('div', { class: 'tiles' },
      tile({ label: 'Planned', value: fmtDur(T.planned), hero: true, delta: `${blocks.length} block${blocks.length === 1 ? '' : 's'}${plan.in_progress ? ` · ${fmtDur(T.elapsed)} elapsed` : ''}` }),
      tile({ label: `On plan${soFar}`, value: T.adherence == null ? '—' : `${Math.round(100 * T.adherence)}%`, delta: `${fmtDur(T.on_plan)} of ${fmtDur(T.elapsed)} planned time on the planned project` }),
      tile({ label: 'Off plan during blocks', value: fmtDur(T.off_plan), delta: 'work on something else' }),
      tile({ label: 'Idle during blocks', value: fmtDur(T.idle_in_blocks), delta: 'away from the laptop' }),
      tile({ label: 'Outside the plan', value: fmtDur(T.outside), delta: `work in unplanned time · ${T.coverage == null ? '—' : Math.round(100 * T.coverage) + '%'} of all work was on plan` }),
    ));
  }

  // visual comparison
  const legendItems = [];
  const seen = new Set();
  for (const r of [...planRuns(blocks, pById), ...projectRuns(tl.segments, pById)]) { if (!seen.has(r.label)) { seen.add(r.label); legendItems.push({ label: r.label, color: r.color }); } }
  wrap.appendChild(el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h2', { text: 'Plan vs actual' }), el('span', { class: 'sub', text: 'top: planned blocks · bottom: project you actually worked on' })),
    el('div', { class: 'tl-wrap' }, hourAxis(),
      el('div', { class: 'lane-label', text: 'Plan' }), blocks.length ? laneEl(planRuns(blocks, pById), t0, span, 'plan') : el('div', { class: 'muted small', text: 'No blocks yet — add some below, or copy another day.' }),
      el('div', { class: 'lane-label', text: 'Actual' }), laneEl(projectRuns(tl.segments, pById), t0, span)),
    legendItems.length ? legend(legendItems) : null));

  // editor
  const editor = el('div', { class: 'card plan-editor', style: 'margin-top:16px' });
  editor.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Blocks' }), el('span', { class: 'sub', text: 'changes save immediately' })));
  editor.appendChild(el('div', { class: 'prow head' }, el('span', { text: 'From' }), el('span', { text: 'To' }), el('span', { text: 'Project' }), el('span', { text: 'Task' }), el('span', { text: 'Note' }), el('span')));
  const clock = ts => (ts >= t1 ? '23:59' : fmtClock(ts));
  for (const b of blocks) {
    const sIn = el('input', { type: 'time', class: 'input', value: clock(b.start), step: 300 });
    const eIn = el('input', { type: 'time', class: 'input', value: clock(b.end), step: 300 });
    const { pSel, tSel } = projectTaskFields(b.project_id, b.task_id, tl.projects, tl.tasks);
    pSel.querySelector('option[value=""]').textContent = '— choose —';
    const note = el('input', { type: 'text', class: 'input', value: b.note || '', placeholder: 'note' });
    const save = async () => {
      if (!pSel.value) { toast('Choose a project for the block', true); return; }
      await api.put(`/api/plan/blocks/${b.id}`, { start: sIn.value, end: eIn.value === '23:59' ? '24:00' : eIn.value, project_id: pSel.value, task_id: tSel.value || null, note: note.value });
      render();
    };
    for (const n of [sIn, eIn, pSel, tSel, note]) n.addEventListener('change', save);
    editor.appendChild(el('div', { class: 'prow' }, sIn, eIn, pSel, tSel, note, el('button', { class: 'x', text: '✕', title: 'Remove block', onclick: async () => { await api.del(`/api/plan/blocks/${b.id}`); render(); } })));
  }
  const last = blocks[blocks.length - 1];
  const defStart = last ? Math.min(last.end, t1 - 3600) : t0 + 9 * 3600;
  const aS = el('input', { type: 'time', class: 'input', value: clock(defStart), step: 300 });
  const aE = el('input', { type: 'time', class: 'input', value: clock(Math.min(defStart + 3600, t1 - 60)), step: 300 });
  const { pSel: aP, tSel: aT } = projectTaskFields(null, null, tl.projects, tl.tasks);
  aP.querySelector('option[value=""]').textContent = '— choose a project —';
  const aN = el('input', { type: 'text', class: 'input', placeholder: 'note (optional)' });
  const addForm = el('form', { class: 'prow add', onsubmit: async e => {
    e.preventDefault();
    if (!aP.value) { toast('Choose a project for the block', true); return; }
    await api.post('/api/plan/blocks', { day: state.date, start: aS.value, end: aE.value === '23:59' ? '24:00' : aE.value, project_id: aP.value, task_id: aT.value || null, note: aN.value });
    toast('Block added'); render();
  } }, aS, aE, aP, aT, aN, el('button', { type: 'submit', class: 'btn primary small', text: 'Add' }));
  editor.appendChild(addForm);
  wrap.appendChild(editor);

  // review tables
  if (blocks.length) {
    const grid = el('div', { class: 'grid', style: 'margin-top:16px' });
    const adhere = (v, total) => el('span', { class: 'adhere' }, el('span', { class: 'bar' }, el('i', { style: `width:${total > 0 ? Math.min(100, 100 * v / total).toFixed(1) : 0}%` })), el('span', { class: 'tabular', text: total > 0 ? `${Math.round(100 * v / total)}%` : '—' }));
    grid.appendChild(card({ title: `How each block went${soFar}`, span2: true,
      chart: () => dataTable([
        { h: 'Block', k: b => `${fmtClock(b.start)}–${clock(b.end)}` },
        { h: 'Planned', k: b => el('span', {}, projPill(pById[String(b.project_id)]), b.task_title ? el('span', { class: 'muted', text: ` › ${b.task_title}` }) : null, b.note ? el('div', { class: 'muted small', text: b.note }) : null) },
        { h: 'On plan', k: b => el('span', {}, adhere(b.on_plan, b.elapsed), el('span', { class: 'muted small', text: ` ${fmtDur(b.on_plan)} of ${fmtDur(b.elapsed)}${b.task_id && b.on_task ? ` (${fmtDur(b.on_task)} on the task)` : ''}` })) },
        { h: 'Actually did', k: b => el('div', { class: 'actual-list' }, b.actual.map(a => el('span', {}, el('i', { class: 'swatch', style: `background:${a.color}` }), `${a.title} ${fmtDur(a.seconds)}`)), b.idle > 60 ? el('span', { class: 'muted' }, `idle ${fmtDur(b.idle)}`) : null, !b.actual.length && b.idle <= 60 ? el('span', { class: 'muted', text: b.elapsed > 0 ? 'nothing tracked' : 'not started yet' }) : null) },
      ], blocks) }));
    grid.appendChild(card({ title: `Per project${soFar}`, sub: 'planned vs actual across the whole day', span2: true,
      chart: () => dataTable([
        { h: 'Project', k: p => p.id === null && p.key === 'none' ? el('span', { class: 'muted', text: 'Unassigned' }) : projPill(pById[p.key] || { title: p.title, color: p.color }) },
        { h: 'Planned', k: p => fmtDur(p.planned), num: true },
        { h: 'Actual (whole day)', k: p => fmtDur(p.actual), num: true },
        { h: 'Within its blocks', k: p => fmtDur(p.on_plan), num: true },
        { h: 'Δ actual − planned', k: p => el('span', { class: `delta ${p.actual - p.planned > 600 ? 'over' : (p.planned - p.actual > 600 ? 'under' : '')}`, style: p.actual - p.planned > 600 ? 'color:var(--ink)' : (p.planned - p.actual > 600 ? 'color:var(--bad)' : '') , text: `${p.actual >= p.planned ? '+' : '−'}${fmtDur(Math.abs(p.actual - p.planned))}` }), num: true },
      ], plan.projects) }));
    wrap.appendChild(grid);
  }
  return wrap;
}

// ---- sort (unsorted inbox + rules) -------------------------------------------
async function renderSort() {
  const [uns, rulesRes] = await Promise.all([api.get('/api/unsorted?' + filterQuery(false) + '&limit=60'), api.get('/api/rules')]);
  const tasks = projectsCache.flatMap(p => p.tasks || []);
  const wrap = el('div', { class: 'grid' });
  const [from, to] = rangeDates();

  // inbox
  const inbox = el('div', { class: 'card span2 unsorted' });
  inbox.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Needs sorting' }), el('span', { class: 'sub', text: `${uns.items.length} apps, sites and workspaces without a rule · ${from === to ? fmtDay(from) : fmtDay(from) + ' – ' + fmtDay(to)}` })));
  if (!uns.items.length) inbox.appendChild(el('div', { class: 'empty', text: 'Everything in this range is covered by a rule. 🎉' }));
  inbox.appendChild(el('div', { class: 'row head small muted' }, el('span', { text: 'Match' }), el('span', { text: 'Value' }), el('span', { text: 'Time' }), el('span', { text: 'Project / task' }), el('span', { text: 'Category' }), el('span')));
  for (const it of uns.items) {
    const { pSel, tSel } = projectTaskFields(null, null, projectsCache, tasks);
    const cSel = categorySelect('', 'Category: auto');
    const save = async () => {
      if (!pSel.value && !tSel.value && !cSel.value) { toast('Choose a project or a category first', true); return; }
      await api.post('/api/rules', { field: it.field, pattern: it.pattern, project_id: pSel.value || null, task_id: tSel.value || null, category: cSel.value || null });
      toast(`Rule added for ${it.pattern}`); render();
    };
    inbox.appendChild(el('div', { class: 'row' },
      el('span', { class: 'field-tag', text: it.field }),
      el('span', {}, el('div', { class: 'pat', title: it.pattern }, it.pattern, it.field !== 'app' && it.app ? el('span', { class: 'muted', text: ` · ${it.app}` }) : null), it.titles.length ? el('div', { class: 'titles', title: it.titles.join('\n'), text: it.titles.join(' · ') }) : null),
      el('span', { class: 'tabular', text: fmtDur(it.seconds) }),
      el('span', { style: 'display:flex;gap:4px' }, pSel, tSel),
      cSel,
      el('button', { class: 'btn small', text: 'Save rule', onclick: save })));
  }
  wrap.appendChild(inbox);

  // add rule
  const addCard = el('div', { class: 'card span2' });
  addCard.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Add a rule' }), el('span', { class: 'sub', text: 'rules match case-insensitively; higher priority wins, then the more specific field (url › title › workspace › domain › bundle › app)' })));
  const fSel = select(['app', 'bundle', 'domain', 'url', 'title', 'workspace'].map(f => ({ value: f, label: f })), 'domain');
  const pat = el('input', { type: 'text', class: 'input', placeholder: 'pattern (substring, or regex if ticked)', required: true, style: 'min-width:240px;flex:1' });
  const rx = el('input', { type: 'checkbox' });
  const { pSel, tSel } = projectTaskFields(null, null, projectsCache, tasks);
  const cSel = categorySelect('', 'Category: auto');
  const prio = el('input', { type: 'number', class: 'input', value: 0, style: 'width:70px', title: 'priority' });
  addCard.appendChild(el('form', { class: 'inline-form', onsubmit: async e => { e.preventDefault(); await api.post('/api/rules', { field: fSel.value, pattern: pat.value, is_regex: rx.checked, project_id: pSel.value || null, task_id: tSel.value || null, category: cSel.value || null, priority: Number(prio.value) || 0 }); toast('Rule added'); render(); } },
    fSel, pat, el('label', { class: 'f small' }, rx, 'regex'), pSel, tSel, cSel, el('label', { class: 'f small' }, 'priority', prio), el('button', { type: 'submit', class: 'btn primary', text: 'Add rule' })));
  wrap.appendChild(addCard);

  // rules
  const rules = el('div', { class: 'card span2 rules' });
  rules.appendChild(el('div', { class: 'card-head' }, el('h2', { text: `Rules (${rulesRes.rules.length})` }), el('span', { class: 'sub', text: 'evaluated top to bottom; first match wins' })));
  rules.appendChild(el('div', { class: 'rule head' }, el('span', { text: 'Field' }), el('span', { text: 'Pattern' }), el('span', { text: 'Project › task' }), el('span', { text: 'Category' }), el('span', { text: 'Prio' }), el('span')));
  if (!rulesRes.rules.length) rules.appendChild(el('div', { class: 'empty', text: 'No rules yet. Use the inbox above to create some.' }));
  for (const r of rulesRes.rules) {
    const proj = projectsCache.find(p => p.id === r.project_id);
    const prioIn = el('input', { type: 'number', class: 'input', value: r.priority, style: 'width:60px', onchange: async e => { await api.put(`/api/rules/${r.id}`, { priority: Number(e.target.value) || 0 }); toast('Priority updated'); render(); } });
    rules.appendChild(el('div', { class: 'rule' },
      el('span', { class: 'field-tag' }, r.field, r.origin === 'auto' ? el('span', { class: 'pill', text: 'auto', title: 'remembered from an assignment', style: 'margin-left:4px;text-transform:none;letter-spacing:0' }) : null),
      el('span', { class: 'pat', title: r.pattern }, r.is_regex ? el('code', { text: r.pattern }) : r.pattern),
      el('span', {}, proj ? projPill(proj) : el('span', { class: 'muted', text: '—' }), r.task_title ? el('span', { class: 'muted', text: ` › ${r.task_title}` }) : null),
      r.category ? catPill(r.category) : el('span', { class: 'muted small', text: proj ? `auto (${catLabel(proj.category)})` : 'auto' }),
      prioIn,
      el('button', { class: 'x', text: '✕', title: 'Delete rule', onclick: async () => { await api.del(`/api/rules/${r.id}`); toast('Rule deleted'); render(); } })));
  }
  wrap.appendChild(rules);
  return wrap;
}

// ---- settings ---------------------------------------------------------------
async function renderSettings() {
  const st = await api.get('/api/status');
  statusCache = st;
  const s = st.settings;
  const wrap = el('div', { class: 'grid' });
  const c = st.counts || {};
  const axOk = !(c.active_recent > 0 && c.titles_recent === 0);
  const urlOk = !(c.browser_recent > 5 && c.urls_recent === 0);

  const status = el('div', { class: 'card' });
  status.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Daemon' })));
  status.appendChild(el('dl', { class: 'kv' },
    el('dt', { text: 'Status' }), el('dd', {}, el('span', { class: `dot ${st.daemon_running ? 'ok' : 'bad'}`, style: 'display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--' + (st.daemon_running ? 'good' : 'bad') + ')' }), st.daemon_running ? (st.paused ? 'running, paused' : 'running') : 'not running — run `tracker start`'),
    el('dt', { text: 'Last heartbeat' }), el('dd', { text: st.heartbeat_age == null ? '—' : `${fmtDur(st.heartbeat_age)} ago` }),
    el('dt', { text: 'Window titles' }), el('dd', {}, axOk ? 'OK' : el('span', { class: 'err', text: 'none seen recently — grant Accessibility (System Settings › Privacy & Security › Accessibility › TrackerDaemon)' })),
    el('dt', { text: 'Browser URLs' }), el('dd', {}, urlOk ? 'OK' : el('span', { class: 'err', text: 'none seen recently — approve the Automation prompt for your browser, or grant Accessibility for the fallback' })),
    el('dt', { text: 'Database' }), el('dd', { text: `${st.db_path} · ${(st.db_bytes / 1e6).toFixed(1)} MB · ${c.segment_count || 0} segments${c.first_start ? ' since ' + fmtDay(isoDate(new Date(c.first_start * 1000))) : ''}` }),
    el('dt', { text: 'Backup' }), el('dd', { text: s.backup_dir ? `${s.backup_dir}${s.last_backup ? ' · last ' + new Date(Number(s.last_backup) * 1000).toLocaleString() : ' · not yet run'}` : 'not configured' })));
  status.appendChild(el('div', { style: 'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap' },
    el('button', { class: 'btn', text: st.paused ? 'Resume tracking' : 'Pause tracking', onclick: async () => { await api.post('/api/pause', { paused: !st.paused }); toast(st.paused ? 'Resumed' : 'Paused'); render(); refreshStatus(); } }),
    el('button', { class: 'btn', text: 'Back up now', disabled: !s.backup_dir, onclick: async () => { const r = await api.post('/api/backup', {}); toast(`Backup written (${(r.bytes / 1e6).toFixed(1)} MB)`); render(); } })));
  wrap.appendChild(status);

  const exp = el('div', { class: 'card' });
  const [from, to] = rangeDates();
  const fIn = el('input', { type: 'date', class: 'input', value: from }), tIn = el('input', { type: 'date', class: 'input', value: to });
  exp.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Export' }), el('span', { class: 'sub', text: 'CSV of every labelled segment' })));
  exp.appendChild(el('div', { class: 'inline-form' }, fIn, el('span', { class: 'dash', text: '–' }), tIn, el('button', { class: 'btn primary', text: 'Download CSV', onclick: () => { window.location = `/api/export.csv?from=${fIn.value}&to=${tIn.value}`; } })));
  exp.appendChild(el('p', { class: 'muted small', style: 'margin-top:10px' }, 'Columns: start, end, seconds, kind, app, bundle, title, url, domain, workspace, project, task, category, source, context_switching, switch_kind. The same data is available from the terminal with ', el('code', { text: 'tracker export --from YYYY-MM-DD --to YYYY-MM-DD --out file.csv' }), '.'));
  wrap.appendChild(exp);

  const form = el('div', { class: 'card span2 settings' });
  form.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'Settings' }), el('span', { class: 'sub', text: 'the daemon picks changes up within 10 seconds; analytics apply immediately' })));
  const rows = [
    ['idle_threshold_seconds', 'Idle after (seconds)', 'No keyboard or mouse input for this long marks you as away. The active segment is trimmed back to the last input.'],
    ['cs_max_seconds', 'Context switch: max seconds per window', 'A stretch counts as context switching when consecutive windows each get at most this many seconds…'],
    ['cs_min_count', 'Context switch: min windows in a run', '…for at least this many windows in a row…'],
    ['cs_min_distinct', 'Context switch: min distinct windows', '…across at least this many different windows/tabs. Such runs are labelled "context switching" and never attributed to a project.'],
    ['session_gap_seconds', 'Session merge gap (seconds)', 'Two stretches in the same app separated by a shorter glance elsewhere are shown as one session.'],
    ['min_idle_gap_minutes', 'Offer to log gaps longer than (minutes)', 'Idle gaps at least this long are listed on the Timeline for "log time away".'],
    ['backup_dir', 'Backup folder', 'A consistent copy of the database is written here hourly (e.g. ~/Library/Mobile Documents/com~apple~CloudDocs/Tracker for iCloud Drive, or a Google Drive folder). Leave empty to disable.'],
    ['dashboard_port', 'Dashboard port', 'Takes effect after `tracker restart`.'],
  ];
  const inputs = {};
  for (const [key, label, help] of rows) {
    inputs[key] = el('input', { type: key === 'backup_dir' ? 'text' : 'number', class: 'input', value: s[key] ?? '', style: key === 'backup_dir' ? 'width:100%' : 'width:120px' });
    form.appendChild(el('div', { class: 'row' }, el('div', {}, el('div', { text: label }), el('div', { class: 'help', text: help })), inputs[key]));
  }
  form.appendChild(el('div', { class: 'actions', style: 'display:flex;justify-content:flex-end;margin-top:12px' }, el('button', { class: 'btn primary', text: 'Save settings', onclick: async () => {
    const body = {}; for (const k of Object.keys(inputs)) body[k] = inputs[k].value.trim();
    await api.put('/api/settings', body); toast('Settings saved'); render();
  } })));
  wrap.appendChild(form);

  const help = el('div', { class: 'card span2' });
  help.appendChild(el('div', { class: 'card-head' }, el('h2', { text: 'How attribution works' })));
  help.appendChild(el('ol', { style: 'margin:0;padding-left:18px;color:var(--ink-2);display:grid;gap:4px' },
    el('li', {}, el('b', { text: 'Logged time ' }), '(Timeline › Log time away) replaces whatever was recorded in that range.'),
    el('li', {}, el('b', { text: 'Manual assignments ' }), '(Timeline › Assign) override rules for one time range, optionally for one app only — e.g. a 10am VS Code session to project A and the 11am one to project B.'),
    el('li', {}, el('b', { text: 'Rules ' }), '(Sort tab) match the browser domain / URL, window title, editor workspace (VS Code shows the open folder in its title), or app. Highest priority wins, then the most specific field.'),
    el('li', {}, el('b', { text: 'Project default category ' }), 'applies when a rule sets a project but no category. Everything else is neutral.'),
    el('li', {}, el('b', { text: 'Context switching ' }), 'is detected automatically from rapid runs of short segments and is never attributed to a project.')));
  wrap.appendChild(help);
  return wrap;
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
function initTheme() {
  const saved = store('theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    store('theme', next);
    render();
  });
}
initTheme();
bindFilters();
window.addEventListener('hashchange', render);
render();
refreshStatus();
setInterval(refreshStatus, 30000);
setInterval(() => { if (currentView().view === 'timeline' && state.date === todayIso() && !$('#modal-root').children.length) render(); }, 60000);
