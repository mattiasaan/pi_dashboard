/* Pi Dashboard - frontend senza dipendenze oltre a Chart.js (servito in locale). */
'use strict';

// ------------------------------------------------------------------ utilità
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = (d = 0) => new Intl.NumberFormat('it-IT', { minimumFractionDigits: d, maximumFractionDigits: d });
const n0 = nf(0), n1 = nf(1), n2 = nf(2);

const store = {
  get(k, d) { try { const v = localStorage.getItem('pidash.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('pidash.' + k, JSON.stringify(v)); } catch { /* ignora */ } },
};

function bytes(b, d = 1) {
  if (b == null) return 'n/d';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let v = Math.abs(b);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${nf(i === 0 ? 0 : d).format(Math.sign(b) * v)} ${u[i]}`;
}
const rate = (b) => `${bytes(b)}/s`;
function dur(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} g ${h} h` : h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}
const fmtTime = (ts, withSec = false) => new Date(ts * 1000).toLocaleString('it-IT',
  { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', ...(withSec ? { second: '2-digit' } : {}) });
const hhmm = (ts, sec = false) => new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', ...(sec ? { second: '2-digit' } : {}) });
const ago = (ts) => (ts ? `${dur(Date.now() / 1000 - ts)} fa` : 'mai');
function parseISO(s) { const t = Date.parse(s || ''); return Number.isNaN(t) || t < 946684800000 ? null : t / 1000; }
const isPhone = () => window.matchMedia('(max-width: 699px)').matches;

const ICON = {
  good: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="currentColor"/></svg>',
  warn: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11.5 11H.5z" fill="currentColor"/></svg>',
  crit: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .5 11.5 6 6 11.5.5 6z" fill="currentColor"/></svg>',
  off: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
};
const status = (kind, text) => `<span class="st ${kind}">${ICON[kind] || ''}${esc(text)}</span>`;

function toast(msg, err = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'pi-dashboard' },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401 && !path.includes('/login')) { location.href = '/login'; throw new Error('Sessione scaduta'); }
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === 'string' ? j.detail : m; } catch { /* ignora */ }
    throw new Error(m);
  }
  return r.json();
}

function confirmDialog({ title, text, danger = false, okLabel = 'Conferma', password = false }) {
  return new Promise((resolve) => {
    const dlg = $('#dlg-confirm');
    $('#cf-title').textContent = title; $('#cf-text').textContent = text;
    const ok = $('#cf-ok'); ok.textContent = okLabel;
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    $('#cf-pw-wrap').hidden = !password; $('#cf-pw').value = '';
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok' ? (password ? $('#cf-pw').value : true) : null), { once: true });
    dlg.returnValue = 'cancel';
    dlg.showModal();
    (password ? $('#cf-pw') : $('#dlg-confirm button[value=cancel]')).focus();
  });
}

// ------------------------------------------------------------------ stato globale
const S = {
  live: [], last: null, thresholds: { temp_warn: 75, temp_crit: 82, cpu_high: 90, mem_high: 90, disk_high: 90 },
  info: null, failures: 0, tab: null, top: [],
};
const WINDOW = 300; // secondi mostrati nei grafici live e nelle sparkline
const MAX_LIVE = 400;
const TABS = ['panoramica', 'storico', 'processi', 'container', 'monitor', 'sistema', 'eventi', 'schermo'];
const MORE_TABS = ['monitor', 'sistema', 'eventi', 'schermo'];

// ------------------------------------------------------------------ grafici
const C = { s1: '#3987e5', s2: '#d95926', s3: '#199e70', s4: '#c98500', s7: '#9085e9', grid: '#2c2c2a', axis: '#383835', muted: '#94938a' };

const crosshair = {
  id: 'crosshair',
  afterDatasetsDraw(chart) {
    const a = chart.tooltip?.getActiveElements?.();
    if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = C.axis; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};

function makeChart(canvas, o) {
  return new Chart(canvas, {
    type: 'line',
    plugins: [crosshair],
    data: {
      datasets: o.series.map((s) => ({
        label: s.label, data: [], borderColor: s.color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5,
        pointHitRadius: 14, pointHoverBackgroundColor: s.color, pointHoverBorderColor: '#1a1a19', pointHoverBorderWidth: 2,
        tension: 0.25, spanGaps: false, fill: !!s.fill, backgroundColor: s.fill ? s.color + '22' : 'transparent',
      })),
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { right: 6 } },
      scales: {
        x: { type: 'linear', grid: { display: false }, border: { color: C.axis }, ticks: { color: C.muted, maxTicksLimit: isPhone() ? 4 : 6, maxRotation: 0, callback: (v) => o.xFmt(v) } },
        y: { beginAtZero: o.yMin === undefined, min: o.yMin, suggestedMax: o.yMax, grid: { color: C.grid }, border: { display: false },
          ticks: { color: C.muted, maxTicksLimit: 5, callback: (v) => o.yFmt(v) } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#222221', borderColor: 'rgba(255,255,255,.12)', borderWidth: 1, padding: 10, boxPadding: 4,
          titleColor: C.muted, bodyColor: '#f0efec', usePointStyle: true,
          callbacks: {
            title: (it) => (it.length ? o.tipTitle(it[0].parsed.x) : ''),
            label: (c) => ` ${c.dataset.label}: ${o.yFmt(c.parsed.y, true)}`,
            labelColor: (c) => ({ borderColor: c.dataset.borderColor, backgroundColor: c.dataset.borderColor, borderRadius: 5 }),
          },
        },
      },
    },
  });
}

function legend(el, series) {
  el.innerHTML = series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('');
}

// ------------------------------------------------------------------ router e aggiornamenti periodici
const tasks = [];
const enter = {};
function every(ms, tabs, fn) { tasks.push({ ms, tabs: tabs === '*' ? null : [].concat(tabs), fn, last: 0, busy: false }); }
function loop() {
  const now = Date.now();
  for (const t of tasks) {
    if (document.hidden || now - t.last < t.ms || t.busy) continue;
    if (t.tabs && !t.tabs.includes(S.tab)) continue;
    t.last = now; t.busy = true;
    Promise.resolve().then(t.fn).catch(() => {}).finally(() => { t.busy = false; });
  }
}

function route() {
  const t = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'panoramica';
  const was = S.tab;
  S.tab = t;
  TABS.forEach((n) => { $('#tab-' + n).hidden = n !== t; });
  $$('[data-tab]').forEach((a) => (a.dataset.tab === t ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  $('#tb-more').classList.toggle('on', MORE_TABS.includes(t));
  if ($('#dlg-more').open) $('#dlg-more').close();
  document.body.classList.toggle('kiosk', t === 'schermo');
  if (t === 'schermo' && was !== 'schermo') kioskStart(); else if (was === 'schermo' && t !== 'schermo') kioskStop();
  tasks.forEach((x) => { if (!x.tabs || x.tabs.includes(t)) x.last = 0; });
  enter[t]?.();
  if (S.last) renderLive(S.last);
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------------ banner
function renderBanner() {
  const items = [];
  if (S.failures >= 3) items.push(`<div class="alert crit" role="alert">${status('crit', 'Dashboard non raggiungibile')}<span class="muted">Sto riprovando… Il Raspberry potrebbe essere in riavvio.</span></div>`);
  for (const a of (S.last?.alerts || [])) {
    items.push(`<div class="alert ${a.level}" role="alert">${status(a.level, a.level === 'crit' ? 'Critico' : 'Avviso')}<span>${esc(a.message)}</span></div>`);
  }
  $('#banner').innerHTML = items.join('');
}

// ------------------------------------------------------------------ tessere, core, riepiloghi live
function sparkSVG(points, lo, hi, color) {
  if (points.length < 2) return '<div class="spark"></div>';
  const W = 300, H = 38, t1 = points.at(-1).ts, t0 = t1 - WINDOW;
  const pts = points.map((p) => [(p.ts - t0) / WINDOW * W, H - 3 - (Math.min(Math.max(p.v ?? lo, lo), hi) - lo) / (hi - lo) * (H - 6)]);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('');
  const area = `${line}L${pts.at(-1)[0].toFixed(1)} ${H}L${pts[0][0].toFixed(1)} ${H}Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="${area}" fill="${color}" opacity=".13"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.75" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

function level(v, warn, crit) { return v == null ? 'off' : crit != null && v >= crit ? 'crit' : v >= warn ? 'warn' : null; }

function renderTiles(root, d) {
  const T = S.thresholds, hist = (k) => S.live.filter((s) => s[k] != null).map((s) => ({ ts: s.ts, v: s[k] }));
  const tiles = [
    { k: 'cpu', lab: 'CPU', val: n0.format(d.cpu), unit: '%', sub: `${d.cores.length} core${d.freq ? ', ' + n0.format(d.freq) + ' MHz' : ''}`,
      lv: level(d.cpu, T.cpu_high), lvt: 'Alta', spark: [0, 100, C.s1] },
    { k: 'mem', lab: 'Memoria', val: n0.format(d.mem), unit: '%', sub: `${bytes(d.mem_used)} di ${bytes(d.mem_total)}`,
      lv: level(d.mem, T.mem_high), lvt: 'Alta', spark: [0, 100, C.s3] },
    { k: 'temp', lab: 'Temperatura', val: d.temp == null ? 'n/d' : n1.format(d.temp), unit: d.temp == null ? '' : '°C',
      sub: d.temp == null ? 'Sensore non disponibile' : `Avviso da ${T.temp_warn} °C`, lv: d.temp == null ? null : level(d.temp, T.temp_warn, T.temp_crit),
      lvt: d.temp >= T.temp_crit ? 'Critica' : 'Alta', spark: [30, 90, C.s2] },
    { k: 'disk', lab: 'Disco', val: n0.format(d.disk), unit: '%', sub: d.swap_total ? `Swap ${n0.format(d.swap)}%` : 'Swap non attivo',
      lv: level(d.disk, T.disk_high), lvt: 'Quasi pieno' },
  ];
  if (!root.children.length) root.innerHTML = tiles.map((t) => `<div class="panel tile" data-k="${t.k}"></div>`).join('');
  for (const t of tiles) {
    const bad = t.lv && t.lv !== 'off';
    $(`[data-k=${t.k}]`, root).innerHTML = `
      <div class="lab"><span>${t.lab}</span>${bad ? status(t.lv, t.lvt) : ''}</div>
      <div class="val">${t.val}<small>${t.unit}</small></div>
      <div class="sub">${esc(t.sub)}</div>
      ${t.spark ? sparkSVG(hist(t.k), t.spark[0], t.spark[1], t.spark[2]) : `<div class="bar ${d.disk >= T.disk_high ? 'crit' : d.disk >= T.disk_high - 10 ? 'hot' : ''}" style="margin-top:20px"><span style="width:${d.disk}%"></span></div>`}`;
  }
}

function renderCores(root, d) {
  if (root.children.length !== d.cores.length) {
    root.innerHTML = d.cores.map((_, i) => `<div class="core"><div class="track"><div class="fill"></div></div><div class="pct"></div><div class="idx">Core ${i}</div></div>`).join('');
  }
  d.cores.forEach((v, i) => {
    const c = root.children[i];
    $('.fill', c).style.height = Math.max(v, 1.5) + '%';
    $('.pct', c).textContent = n0.format(v) + '%';
  });
}

function renderQuick(d) {
  $('#quick').innerHTML = `
    <dt>Carico medio</dt><dd>${n2.format(d.load1)}, ${n2.format(d.load5)}, ${n2.format(d.load15)}</dd>
    <dt>Rete in ingresso</dt><dd>${rate(d.net_rx)}</dd>
    <dt>Rete in uscita</dt><dd>${rate(d.net_tx)}</dd>
    <dt>Lettura disco</dt><dd>${rate(d.disk_r)}</dd>
    <dt>Scrittura disco</dt><dd>${rate(d.disk_w)}</dd>
    <dt>Swap</dt><dd>${d.swap_total ? `${bytes(d.swap_used)} di ${bytes(d.swap_total)}` : 'Non attivo'}</dd>
    <dt>Acceso da</dt><dd>${dur(d.uptime)}</dd>`;
  $('#uptime').textContent = `Acceso da ${dur(d.uptime)}`;
}

let liveCpu, liveNet;
function initLiveCharts() {
  const xFmt = (v) => hhmm(v, true), tipTitle = (v) => hhmm(v, true);
  liveCpu = makeChart($('#c-live-cpu'), { series: [{ label: 'CPU', color: C.s1, fill: true }], yMax: 100, yFmt: (v, t) => `${t ? n1.format(v) : n0.format(v)}%`, xFmt, tipTitle });
  const net = [{ label: 'In ingresso', color: C.s1 }, { label: 'In uscita', color: C.s2 }];
  liveNet = makeChart($('#c-live-net'), { series: net, yFmt: (v) => rate(v), xFmt, tipTitle });
  legend($('#lg-live-net'), net);
}

function updateLiveCharts() {
  const pts = (k) => S.live.map((s) => ({ x: s.ts, y: s[k] }));
  liveCpu.data.datasets[0].data = pts('cpu');
  liveNet.data.datasets[0].data = pts('net_rx');
  liveNet.data.datasets[1].data = pts('net_tx');
  const t1 = S.live.at(-1)?.ts;
  for (const ch of [liveCpu, liveNet]) { if (t1) { ch.options.scales.x.min = t1 - WINDOW; ch.options.scales.x.max = t1; } ch.update('none'); }
}

// ---- riepiloghi di container, processi e controlli (panoramica e modalità schermo)
const ctrCounts = () => { const l = CT.data; return { total: l.length, run: l.filter((c) => c.status === 'running').length }; };

function renderCtrSummary(root, max) {
  const l = CT.data;
  if (!CT.loaded) { root.innerHTML = '<div class="muted">Caricamento…</div>'; return; }
  if (CT.error) { root.innerHTML = `<div class="muted">Docker non disponibile</div>`; return; }
  if (!l.length) { root.innerHTML = '<div class="muted">Nessun container.</div>'; return; }
  const { total, run } = ctrCounts();
  const stopped = l.filter((c) => c.status !== 'running');
  const head = `<div class="row-item"><span>${run} di ${total} in esecuzione</span>${stopped.length ? status('warn', `${stopped.length} fermi`) : status('good', 'Tutti attivi')}</div>`;
  const rows = stopped.slice(0, max).map((c) => `<div class="row-item"><span class="nm">${esc(c.name)}</span>
    ${c.is_self ? '' : `<button class="btn sm" data-ov-start="${esc(c.name)}" ${CT.busy.has(c.name) ? 'disabled' : ''}>Avvia</button>`}</div>`).join('');
  root.innerHTML = head + rows + (stopped.length > max ? `<div class="muted">e altri ${stopped.length - max}…</div>` : '');
}

function renderTopProcs(root, n) {
  root.innerHTML = S.top.slice(0, n).map((p) => `<div class="row-item"><span class="nm">${esc(p.name)} <span class="sub">${p.pid}</span></span><span>${n1.format(p.cpu)}% <span class="sub">${bytes(p.rss, 0)}</span></span></div>`).join('') || '<div class="muted">Caricamento…</div>';
}

function chkStatus(c) {
  if (!c.enabled) return status('off', 'In pausa');
  if (c.ok === null || c.ok === undefined) return status('off', 'In attesa');
  if (c.ok) return status('good', 'Raggiungibile');
  return c.down ? status('crit', 'Non raggiungibile') : status('warn', 'Nessuna risposta');
}
function renderMonSummary(root, max) {
  const l = CHK.data;
  if (!CHK.loaded) { root.innerHTML = '<div class="muted">Caricamento…</div>'; return; }
  if (!l.length) { root.innerHTML = '<div class="muted">Nessun controllo. <a class="link" href="#monitor" style="text-decoration:underline">Aggiungine uno</a>.</div>'; return; }
  root.innerHTML = l.slice(0, max).map((c) => `<div class="row-item"><span class="nm">${esc(c.name)}</span><span>${chkStatus(c)}${c.ok && c.ms != null ? ` <span class="sub">${n0.format(c.ms)} ms</span>` : ''}</span></div>`).join('')
    + (l.length > max ? `<div class="muted">e altri ${l.length - max}…</div>` : '');
}

function renderLive(d) {
  if (S.tab === 'panoramica') {
    renderTiles($('#tiles'), d); renderCores($('#cores'), d); renderQuick(d); updateLiveCharts();
    $('#cpu-sub').textContent = d.freq ? `${n0.format(d.freq)} MHz` : '';
    renderCtrSummary($('#ov-ctr'), 4); renderTopProcs($('#ov-top'), 5); renderMonSummary($('#ov-mon'), 5);
  } else if (S.tab === 'schermo') {
    renderTiles($('#k-tiles'), d); renderCores($('#k-cores'), d);
    $('#k-net').innerHTML = `<div><b>${rate(d.net_rx)}</b><span>In ingresso</span></div><div><b>${rate(d.net_tx)}</b><span>In uscita</span></div>
      <div><b>${rate(d.disk_r)}</b><span>Lettura disco</span></div><div><b>${rate(d.disk_w)}</b><span>Scrittura disco</span></div>`;
    renderCtrSummary($('#k-ctr'), 5); renderTopProcs($('#k-top'), 5); renderMonSummary($('#k-mon'), 6);
  }
  if (S.tab !== 'panoramica') $('#uptime').textContent = `Acceso da ${dur(d.uptime)}`;
}

function trimLive() {
  const t1 = S.live.at(-1).ts;
  while (S.live.length > MAX_LIVE || (S.live.length > 1 && S.live[0].ts < t1 - WINDOW)) S.live.shift();
}

// all'avvio riempie il buffer con gli ultimi minuti già salvati, così i grafici non partono vuoti
async function seedLive() {
  try {
    const h = (await api('/api/history?range=15m')).series;
    S.live = h.t.map((t, i) => ({ ts: t, cpu: h.cpu[i], mem: h.mem[i], temp: h.temp[i], net_rx: h.net_rx[i], net_tx: h.net_tx[i] }));
    if (S.live.length) trimLive();
  } catch { /* la dashboard parte comunque */ }
}

async function pollLive() {
  try {
    const d = await api('/api/live');
    S.failures = 0; S.last = d;
    if (!S.live.length || S.live.at(-1).ts !== d.ts) { S.live.push(d); trimLive(); }
    renderLive(d);
  } catch (e) { S.failures++; }
  renderBanner();
}

async function loadTop() {
  const d = await api('/api/processes?sort=cpu&limit=6');
  S.top = d.processes;
  if (S.last) renderLive(S.last);
}

// ------------------------------------------------------------------ modalità schermo
let clockTimer = null, wake = null;
function kioskTick() {
  const n = new Date();
  $('#k-time').textContent = n.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  $('#k-date').textContent = n.toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long' });
}
async function requestWake() {
  try { if ('wakeLock' in navigator && !wake) { wake = await navigator.wakeLock.request('screen'); wake.addEventListener('release', () => { wake = null; }); } } catch { /* non supportato */ }
}
function kioskStart() {
  kioskTick(); clockTimer = setInterval(kioskTick, 1000); requestWake();
  $('#k-host').textContent = S.info ? `${S.info.hostname}` : '';
  $('#k-full').hidden = !document.documentElement.requestFullscreen;
}
function kioskStop() {
  clearInterval(clockTimer); clockTimer = null;
  try { wake?.release(); } catch { /* ignora */ }
  wake = null;
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
}
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.().catch(() => {});
}

// ------------------------------------------------------------------ storico
const HIST = [
  { id: 'cpu', title: 'CPU', series: [{ k: 'cpu', label: 'Utilizzo medio', color: C.s1, fill: true }], yMax: 100, fmt: 'pct' },
  { id: 'mem', title: 'Memoria', series: [{ k: 'mem', label: 'RAM', color: C.s3 }, { k: 'swap', label: 'Swap', color: C.s4 }], yMax: 100, fmt: 'pct' },
  { id: 'temp', title: 'Temperatura', series: [{ k: 'temp', label: 'CPU', color: C.s2 }], yMin: 30, fmt: 'temp' },
  { id: 'net', title: 'Rete', series: [{ k: 'net_rx', label: 'In ingresso', color: C.s1 }, { k: 'net_tx', label: 'In uscita', color: C.s2 }], fmt: 'rate' },
  { id: 'dio', title: 'Disco, lettura e scrittura', series: [{ k: 'disk_r', label: 'Lettura', color: C.s1 }, { k: 'disk_w', label: 'Scrittura', color: C.s2 }], fmt: 'rate' },
  { id: 'load', title: 'Carico medio (1 minuto)', series: [{ k: 'load1', label: 'Carico', color: C.s7 }], fmt: 'num' },
  { id: 'freq', title: 'Frequenza CPU', series: [{ k: 'freq', label: 'Frequenza', color: C.s3 }], fmt: 'mhz' },
  { id: 'disk', title: 'Spazio disco usato', series: [{ k: 'disk', label: 'Disco', color: C.s4 }], yMax: 100, fmt: 'pct' },
];
const FMT = {
  pct: (v, t) => `${t ? n1.format(v) : n0.format(v)}%`, temp: (v) => `${n1.format(v)} °C`,
  rate: (v) => rate(v), num: (v) => n2.format(v), mhz: (v) => `${n0.format(v)} MHz`,
};
const HCH = {}; let histRange = '1h', histData = null;
const RANGE_S = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 };

function initHistory() {
  if (Object.keys(HCH).length) return;
  $('#hist-grid').innerHTML = HIST.map((h) => `
    <div class="panel"><div class="panel-head"><h2>${h.title}</h2><div class="legend" id="lg-${h.id}"></div></div>
    <div class="muted" id="sum-${h.id}" style="font-size:12.5px;margin:-6px 0 8px;min-height:19px"></div>
    <div class="chart-box tall"><canvas id="h-${h.id}" role="img" aria-label="${h.title}"></canvas></div></div>`).join('');
  for (const h of HIST) {
    HCH[h.id] = makeChart($('#h-' + h.id), { series: h.series, yMax: h.yMax, yMin: h.yMin, yFmt: FMT[h.fmt], xFmt: (v) => xFmtRange(v), tipTitle: (v) => fmtTime(v, true) });
    if (h.series.length > 1) legend($('#lg-' + h.id), h.series);
  }
}
const xFmtRange = (v) => (histRange === '1h' || histRange === '6h' || histRange === '24h' ? hhmm(v) : fmtTime(v).replace(',', ''));

function withGaps(t, y, bucket) {
  const out = [];
  for (let i = 0; i < t.length; i++) {
    if (i && t[i] - t[i - 1] > bucket * 2.5) out.push({ x: t[i - 1] + bucket, y: null });
    out.push({ x: t[i], y: y[i] });
  }
  return out;
}

function summarize(vals, f) {
  const v = vals.filter((x) => x != null);
  if (!v.length) return 'Nessun dato in questo intervallo';
  const avg = v.reduce((a, b) => a + b, 0) / v.length;
  return `Media ${f(avg)}, minimo ${f(Math.min(...v))}, massimo ${f(Math.max(...v))}`;
}

async function loadHistory() {
  initHistory();
  const d = await api('/api/history?range=' + histRange);
  histData = d;
  const s = d.series, now = Date.now() / 1000;
  for (const h of HIST) {
    const ch = HCH[h.id];
    h.series.forEach((sr, i) => { ch.data.datasets[i].data = withGaps(s.t, s[sr.k], d.bucket); });
    ch.options.scales.x.min = now - RANGE_S[histRange]; ch.options.scales.x.max = now;
    ch.update('none');
    $('#sum-' + h.id).textContent = s.t.length ? summarize(s[h.series[0].k], FMT[h.fmt]) : 'Nessun dato in questo intervallo';
  }
}

function downloadCsv() {
  if (!histData) return;
  const s = histData.series, cols = Object.keys(s);
  const rows = [cols.join(',')];
  s.t.forEach((t, i) => rows.push(cols.map((c) => (c === 't' ? new Date(t * 1000).toISOString() : s[c][i] ?? '')).join(',')));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.join('\n')], { type: 'text/csv' }));
  a.download = `raspberry-${histRange}.csv`; a.click(); URL.revokeObjectURL(a.href);
}

// ------------------------------------------------------------------ processi
const P = { sort: 'cpu', q: '', group: store.get('pgroup', false), rows: [] };

function renderProcesses() {
  let rows = P.rows;
  if (P.group) {
    const g = new Map();
    for (const p of rows) {
      const e = g.get(p.name) || { name: p.name, count: 0, cpu: 0, rss: 0, mem: 0, pid: p.pid };
      e.count++; e.cpu += p.cpu; e.rss += p.rss; e.mem += p.mem; g.set(p.name, e);
    }
    const key = { cpu: 'cpu', mem: 'rss', pid: 'count', name: 'name' }[P.sort];
    const list = [...g.values()].sort((a, b) => (key === 'name' ? a.name.localeCompare(b.name) : b[key] - a[key]));
    $('#p-count').textContent = `${list.length} gruppi, ${rows.length} processi`;
    $('#p-body').innerHTML = list.map((e) => `<tr class="click" tabindex="0" data-group="${esc(e.name)}">
      <td class="num hide-sm">${e.count}×</td><td class="name">${esc(e.name)}${e.count > 1 ? ` <span class="muted">(${e.count})</span>` : ''}</td><td class="hide-sm"></td>
      <td class="num">${n1.format(e.cpu)}</td><td class="num">${bytes(e.rss)} <span class="muted">(${n1.format(e.mem)}%)</span></td><td class="hide-sm"></td><td class="hide-md"></td></tr>`).join('')
      || '<tr><td colspan="7" class="empty">Nessun processo corrisponde alla ricerca.</td></tr>';
    return;
  }
  $('#p-count').textContent = `${rows.length} processi`;
  $('#p-body').innerHTML = rows.length ? rows.map((p) => `
    <tr class="click" tabindex="0" data-pid="${p.pid}"><td class="num hide-sm">${p.pid}</td><td class="name">${esc(p.name)}</td><td class="hide-sm">${esc(p.user)}</td>
    <td class="num">${n1.format(p.cpu)}</td><td class="num">${bytes(p.rss)} <span class="muted">(${n1.format(p.mem)}%)</span></td>
    <td class="hide-sm">${esc(p.status)}</td><td class="cmd hide-md" title="${esc(p.cmd)}">${esc(p.cmd)}</td></tr>`).join('')
    : '<tr><td colspan="7" class="empty">Nessun processo corrisponde alla ricerca.</td></tr>';
}

async function loadProcesses() {
  const d = await api(`/api/processes?sort=${P.group && P.sort === 'pid' ? 'cpu' : P.sort}&limit=${P.group ? 300 : 100}&q=${encodeURIComponent(P.q)}`);
  P.rows = d.processes; renderProcesses();
}

async function openProc(pid) {
  const dlg = $('#dlg-proc');
  $('#pd-title').textContent = 'Processo ' + pid; $('#pd-body').innerHTML = '<dt>Caricamento</dt><dd>…</dd>'; $('#pd-actions').innerHTML = '';
  dlg.showModal();
  try {
    const p = await api('/api/processes/' + pid);
    $('#pd-title').textContent = `${p.name || 'Processo'} (${p.pid})`;
    const row = (k, v) => (v == null || v === '' ? '' : `<dt>${k}</dt><dd>${v}</dd>`);
    $('#pd-body').innerHTML = row('Comando', p.cmdline ? `<div class="cmdbox">${esc(p.cmdline)}</div>` : '') + row('Eseguibile', esc(p.exe)) + row('Utente', esc(p.user)) + row('Stato', esc(p.status))
      + row('Avviato', p.started ? `${fmtTime(p.started)} (${ago(p.started)})` : '') + row('Memoria', p.rss != null ? `${bytes(p.rss)} reale, ${bytes(p.vms)} virtuale` : '')
      + row('Thread', p.threads) + row('File aperti', p.fds) + row('Priorità (nice)', p.nice) + row('Cartella', esc(p.cwd))
      + row('Processo padre', p.parent ? `${esc(p.parent.name)} (${p.parent.pid})` : '') + row('Processi figli', p.children || '')
      + row('Porte in ascolto', p.listening.length ? esc(p.listening.join(', ')) : '');
    const stopped = p.status === 'stopped';
    $('#pd-actions').innerHTML = p.protected ? '<span class="muted">Questo processo è protetto e non si può fermare dalla dashboard.</span>'
      : `<button class="btn" data-sig="TERM">Termina</button><button class="btn danger" data-sig="KILL">Forza arresto</button>
         ${stopped ? '<button class="btn" data-sig="CONT">Riprendi</button>' : '<button class="btn" data-sig="STOP">Metti in pausa</button>'}`;
    $('#pd-actions').dataset.pid = p.pid; $('#pd-actions').dataset.name = p.name || '';
  } catch (e) { $('#pd-body').innerHTML = `<dt>Errore</dt><dd>${esc(e.message)}</dd>`; }
}

async function procSignal(pid, name, sig) {
  if (sig === 'TERM' || sig === 'KILL') {
    const ok = await confirmDialog({
      title: sig === 'KILL' ? `Forzare l'arresto di ${name}?` : `Terminare ${name}?`,
      text: sig === 'KILL' ? `Il processo ${pid} verrà chiuso subito, senza salvare nulla.` : `Il processo ${pid} riceverà una richiesta di chiusura ordinata.`,
      danger: true, okLabel: sig === 'KILL' ? 'Forza arresto' : 'Termina',
    });
    if (!ok) return;
  }
  try {
    await api(`/api/processes/${pid}/signal`, { method: 'POST', body: { signal: sig } });
    toast(`${name} (${pid}): ${{ TERM: 'richiesta di chiusura inviata', KILL: 'arrestato', STOP: 'messo in pausa', CONT: 'ripreso' }[sig]}`);
    $('#dlg-proc').close(); loadProcesses();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ container
const CT = { busy: new Set(), data: [], filter: store.get('cfilter', 'all'), q: '', loaded: false, error: null };
const isStopped = (c) => c.status !== 'running';

function ctrStatus(c) {
  if (c.status === 'running') return c.health === 'unhealthy' ? status('crit', 'Non sano') : c.health === 'starting' ? status('warn', 'In avvio') : status('good', 'In esecuzione');
  if (c.status === 'restarting') return status('warn', 'In riavvio');
  if (c.status === 'paused') return status('warn', 'In pausa');
  if (c.status === 'exited' && (c.exit_code || c.oom_killed)) return status('crit', c.oom_killed ? 'Fermato (memoria esaurita)' : `Fermato (codice ${c.exit_code})`);
  return status('off', c.status === 'created' ? 'Creato' : 'Fermo');
}

function ctrCard(c) {
  const run = c.status === 'running', busy = CT.busy.has(c.name), n = esc(c.name);
  const bad = !run && (c.exit_code || c.oom_killed);
  const started = parseISO(c.started_at), finished = parseISO(c.finished_at);
  const meta = [];
  meta.push(run ? (started ? `In esecuzione da ${dur(Date.now() / 1000 - started)}` : 'In esecuzione') : (finished ? `Fermo da ${dur(Date.now() / 1000 - finished)}` : 'Mai avviato'));
  if (c.ports.length) meta.push(`Porte: ${c.ports.map(esc).join(', ')}`);
  meta.push(`Riavvio Docker: ${esc(c.restart_policy)}`);
  const memPct = c.mem_limit && c.mem_used != null ? Math.min(100, c.mem_used / c.mem_limit * 100) : 0;
  const res = run ? `<div class="ctr-res">
      <div><div class="k"><span>CPU</span><b>${c.cpu == null ? '-' : n1.format(c.cpu) + '%'}</b></div><div class="bar"><span style="width:${Math.min(100, c.cpu || 0)}%;background:var(--s1)"></span></div></div>
      <div><div class="k"><span>Memoria</span><b>${c.mem_used == null ? '-' : bytes(c.mem_used, 0)}</b></div><div class="bar"><span style="width:${memPct}%"></span></div></div></div>` : '';
  const acts = c.is_self ? '<span class="muted">Questa dashboard</span><span class="grow"></span>'
    : (run ? `<button class="btn sm" data-ca="restart" data-n="${n}" ${busy ? 'disabled' : ''}>Riavvia</button><button class="btn sm danger" data-ca="stop" data-n="${n}" ${busy ? 'disabled' : ''}>Ferma</button>`
      : `<button class="btn sm primary" data-ca="start" data-n="${n}" ${busy ? 'disabled' : ''}>${busy ? 'Avvio…' : 'Avvia'}</button>`);
  return `<article class="panel ctr ${run ? '' : 'stopped'} ${bad ? 'down-bad' : ''}">
    <div class="ctr-top"><div><div class="ctr-name">${n}</div><div class="ctr-img">${esc(c.image)}</div></div>${ctrStatus(c)}</div>
    <div class="ctr-meta">${meta.map((m) => `<span>${m}</span>`).join('')}</div>${res}
    <div class="ctr-act">${acts}<button class="btn sm" data-ca="logs" data-n="${n}">Log</button><button class="btn sm" data-ca="details" data-n="${n}">Dettagli</button></div>
    ${c.is_self ? '' : `<label class="ctr-auto switch"><span>Riavvia da solo se si ferma</span><input type="checkbox" data-ar="${n}" ${c.autorestart ? 'checked' : ''}><span class="track"></span></label>`}
  </article>`;
}

function renderContainers() {
  const all = CT.data, run = all.filter((c) => !isStopped(c)).length, stopped = all.length - run;
  $('#n-all').textContent = all.length; $('#n-run').textContent = run; $('#n-stop').textContent = stopped;
  $$('#ctr-filter button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.f === CT.filter)));
  const q = CT.q.toLowerCase();
  let list = all.filter((c) => (CT.filter === 'all' || (CT.filter === 'running') === !isStopped(c)) && (!q || `${c.name} ${c.image} ${c.project || ''}`.toLowerCase().includes(q)));
  const startable = all.filter((c) => isStopped(c) && !c.is_self);
  $('#c-startall').hidden = !startable.length; $('#c-startall').textContent = `Avvia tutti i fermi (${startable.length})`;
  const badge = all.length ? `${run}/${all.length}` : '';
  for (const el of [$('#nav-ctr')]) { el.textContent = badge; el.classList.toggle('bad', stopped > 0); }
  $('#tb-ctr').hidden = !stopped;
  if (!list.length) {
    $('#c-list').innerHTML = `<div class="panel empty">${all.length ? 'Nessun container corrisponde ai filtri.' : 'Nessun container trovato su questo Docker.'}</div>`;
    return;
  }
  const groups = new Map();
  for (const c of list) { const k = c.project || ''; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); }
  const keys = [...groups.keys()].sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)));
  const showHeads = keys.length > 1 || keys[0] !== '';
  $('#c-list').innerHTML = keys.map((k) => {
    const items = groups.get(k), gs = items.filter((c) => isStopped(c) && !c.is_self).length, gr = items.filter((c) => !isStopped(c) && !c.is_self).length;
    const head = showHeads ? `<div class="group-head"><h2>${k ? esc(k) : 'Altri container'}</h2><span class="sub">${items.length} container${k ? ', progetto Compose' : ''}</span>
      <div class="gact">${k && gs ? `<button class="btn sm" data-ga="start" data-g="${esc(k)}">Avvia i fermi (${gs})</button>` : ''}
      ${k && gr ? `<button class="btn sm" data-ga="restart" data-g="${esc(k)}">Riavvia tutti</button><button class="btn sm danger" data-ga="stop" data-g="${esc(k)}">Ferma tutti</button>` : ''}</div></div>` : '';
    return `<section class="ctr-group">${head}<div class="ctr-grid">${items.map(ctrCard).join('')}</div></section>`;
  }).join('');
}

async function loadContainers(withStats = true) {
  try {
    const d = await api('/api/containers' + (withStats ? '' : '?stats=0'));
    CT.data = d.containers; CT.error = null; CT.loaded = true;
    $('#ctr-msg').innerHTML = ''; $('#ctr-tools').hidden = false; $('#c-list').hidden = false;
    renderContainers();
  } catch (e) {
    CT.error = e.message; CT.loaded = true;
    $('#ctr-msg').innerHTML = `<div class="alert warn" role="alert">${status('warn', 'Docker non disponibile')}<span>${esc(e.message)}</span></div>`;
    $('#ctr-tools').hidden = true; $('#c-list').hidden = true; $('#nav-ctr').textContent = '';
  }
  if (S.last && (S.tab === 'panoramica' || S.tab === 'schermo')) renderLive(S.last);
}

async function ctrAction(name, act) {
  if (act === 'stop') {
    const ok = await confirmDialog({ title: `Fermare ${name}?`, text: 'Il container verrà arrestato. Potrai riavviarlo da questa pagina.', danger: true, okLabel: 'Ferma' });
    if (!ok) return;
  }
  CT.busy.add(name); renderContainers();
  try {
    await api(`/api/containers/${encodeURIComponent(name)}/${act}`, { method: 'POST' });
    toast(`${name}: ${{ start: 'avviato', stop: 'fermato', restart: 'riavviato' }[act]}`);
  } catch (e) { toast(e.message, true); }
  CT.busy.delete(name); await loadContainers();
}

async function bulk(action, names, title) {
  names.forEach((n) => CT.busy.add(n)); renderContainers();
  try {
    const r = (await api('/api/containers-bulk', { method: 'POST', body: { action, names } })).results;
    const bad = r.filter((x) => !x.ok);
    toast(bad.length ? `${title}: ${r.length - bad.length} riusciti, ${bad.length} no (${bad[0].name}: ${bad[0].error})` : `${title}: ${r.length} container`, bad.length > 0);
  } catch (e) { toast(e.message, true); }
  names.forEach((n) => CT.busy.delete(n)); await loadContainers();
}

async function groupAction(project, act) {
  const items = CT.data.filter((c) => c.project === project && !c.is_self);
  const names = items.filter((c) => (act === 'start' ? isStopped(c) : !isStopped(c))).map((c) => c.name);
  if (!names.length) return;
  if (act !== 'start') {
    const ok = await confirmDialog({ title: `${act === 'stop' ? 'Fermare' : 'Riavviare'} ${names.length} container di «${project}»?`, text: names.join(', '), danger: act === 'stop', okLabel: act === 'stop' ? 'Ferma tutti' : 'Riavvia tutti' });
    if (!ok) return;
  }
  await bulk(act, names, { start: 'Avviati', stop: 'Fermati', restart: 'Riavviati' }[act]);
}

async function startAllStopped() {
  const names = CT.data.filter((c) => isStopped(c) && !c.is_self).map((c) => c.name);
  if (!names.length) return;
  const ok = await confirmDialog({ title: `Avviare ${names.length} container?`, text: names.join(', '), okLabel: 'Avvia tutti' });
  if (ok) await bulk('start', names, 'Avviati');
}

async function setAutoRestart(name, enabled, input) {
  try {
    await api(`/api/containers/${encodeURIComponent(name)}/autorestart`, { method: 'PUT', body: { enabled } });
    const c = CT.data.find((x) => x.name === name); if (c) c.autorestart = enabled;
    toast(enabled ? `${name}: ripartirà da solo se si ferma (fino a 3 volte in 10 minuti)` : `${name}: riavvio automatico disattivato`);
  } catch (e) { toast(e.message, true); input.checked = !enabled; }
}

async function openCtrDetails(name) {
  $('#cd-title').textContent = name; $('#cd-body').innerHTML = '<dt>Caricamento</dt><dd>…</dd>'; $('#dlg-ctr').showModal();
  try {
    const d = await api(`/api/containers/${encodeURIComponent(name)}/details`);
    const row = (k, v) => (v == null || v === '' ? '' : `<dt>${k}</dt><dd>${v}</dd>`);
    const list = (a) => (a.length ? `<ul class="mono-list">${a.join('')}</ul>` : '');
    $('#cd-body').innerHTML = row('Immagine', esc(d.image)) + row('Identificativo', esc(d.id)) + row('Progetto Compose', esc(d.project))
      + row('Stato', esc(d.status) + (d.exit_code ? ` (codice ${d.exit_code})` : '') + (d.oom_killed ? ', memoria esaurita' : ''))
      + row('Creato', parseISO(d.created) ? fmtTime(parseISO(d.created)) : '') + row('Ultimo avvio', parseISO(d.started_at) ? fmtTime(parseISO(d.started_at)) : '')
      + row('Ultimo arresto', parseISO(d.finished_at) ? fmtTime(parseISO(d.finished_at)) : '') + row('Riavvii di Docker', d.restart_count)
      + row('Politica di riavvio', esc(d.restart_policy)) + row('Comando', d.command ? `<div class="cmdbox">${esc(d.command)}</div>` : '') + row('Utente', esc(d.user)) + row('Cartella di lavoro', esc(d.workdir))
      + row('Limite di memoria', d.memory_limit ? bytes(d.memory_limit, 0) : '') + row('Errore', esc(d.error))
      + row('Reti', list(d.networks.map((n) => `<li>${esc(n.name)}${n.ip ? ` — ${esc(n.ip)}` : ''}</li>`)))
      + row('Cartelle collegate', list(d.mounts.map((m) => `<li>${esc(m.source)} → ${esc(m.target)} ${m.rw ? '' : '(sola lettura)'}</li>`)))
      + row('Variabili d\'ambiente', d.env_names.length ? `${d.env_names.map(esc).join(', ')}<div class="muted" style="font-size:12px">Solo i nomi: i valori non vengono mostrati.</div>` : '');
  } catch (e) { $('#cd-body').innerHTML = `<dt>Errore</dt><dd>${esc(e.message)}</dd>`; }
}

let logName = '', logTimer = null;
async function loadLogs() {
  const pre = $('#lg-pre');
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  try {
    const d = await api(`/api/containers/${encodeURIComponent(logName)}/logs?tail=${$('#lg-tail').value}`);
    pre.textContent = d.logs || '(nessun log)'; if (atEnd) pre.scrollTop = pre.scrollHeight;
  } catch (e) { pre.textContent = e.message; }
}
function setLogLive() {
  clearInterval(logTimer); logTimer = null;
  if ($('#lg-live').checked) logTimer = setInterval(() => { if (!document.hidden) loadLogs(); }, 3000);
}
function openLogs(name) {
  logName = name; $('#lg-title').textContent = `Log di ${name}`; $('#lg-pre').textContent = 'Caricamento…';
  $('#lg-live').checked = false; setLogLive(); $('#dlg-logs').showModal(); loadLogs().then(() => { const p = $('#lg-pre'); p.scrollTop = p.scrollHeight; });
}

// ------------------------------------------------------------------ monitor di raggiungibilità
const CHK = { data: [], loaded: false };
const PRESETS = [
  { n: 'Internet', k: 'tcp', t: '1.1.1.1:443' }, { n: 'DNS Google', k: 'tcp', t: '8.8.8.8:53' },
  { n: 'Router', k: 'tcp', t: '192.168.1.1:80' }, { n: 'Sito web', k: 'http', t: 'https://example.com' },
];

function stripHTML(h) {
  const last = h.slice(-40), maxMs = Math.max(50, ...last.filter((r) => r.ms != null).map((r) => r.ms));
  const cells = last.map((r) => (r.ok ? `<i style="height:${Math.max(18, Math.min(100, (r.ms || 0) / maxMs * 100))}%" title="${n0.format(r.ms || 0)} ms, ${fmtTime(r.ts, true)}"></i>`
    : `<i class="bad" title="Nessuna risposta, ${fmtTime(r.ts, true)}"></i>`));
  while (cells.length < 40) cells.unshift('<i class="none"></i>');
  return `<div class="strip" aria-hidden="true">${cells.join('')}</div>`;
}

function renderChecks() {
  const l = CHK.data;
  const bad = l.filter((c) => c.enabled && c.ok === false).length;
  for (const el of [$('#nav-mon'), $('#more-mon')]) { el.textContent = bad ? `${bad} giù` : ''; el.classList.toggle('bad', bad > 0); }
  $('#tb-mon').hidden = !bad;
  if (S.tab !== 'monitor') return;
  if (!l.length) {
    $('#chk-list').innerHTML = `<div class="panel" style="grid-column:1/-1"><h2>Nessun controllo</h2>
      <p class="muted">Aggiungi un controllo per sapere subito se Internet, il router o un tuo servizio smettono di rispondere. Per cominciare:</p>
      <div class="chips">${PRESETS.map((p, i) => `<button class="chip" data-preset="${i}" type="button">${esc(p.n)}</button>`).join('')}</div></div>`;
    return;
  }
  $('#chk-list').innerHTML = l.map((c) => `<article class="panel chk">
    <div class="top"><div><div class="nm">${esc(c.name)}</div><div class="tg">${esc(c.target)}</div><div class="tg2">${c.kind === 'http' ? 'Pagina web' : 'Porta TCP'}, ogni ${c.interval >= 60 ? c.interval / 60 + ' min' : c.interval + ' s'}</div></div>${chkStatus(c)}</div>
    <div class="vals"><div><b>${c.ok && c.ms != null ? n0.format(c.ms) + ' ms' : '-'}</b><span>Risposta</span></div>
      <div><b>${c.uptime24h == null ? '-' : n1.format(c.uptime24h) + '%'}</b><span>Raggiungibile (24 ore)</span></div>
      <div><b>${c.uptime7d == null ? '-' : n1.format(c.uptime7d) + '%'}</b><span>Raggiungibile (7 giorni)</span></div></div>
    ${c.ok === false && c.err ? `<div class="muted">Ultimo errore: ${esc(c.err)}</div>` : ''}
    ${stripHTML(c.history)}
    <div class="muted" style="font-size:12px">Ultimo controllo: ${c.last ? ago(c.last) : 'mai'}</div>
    <div class="acts"><button class="btn sm" data-chk-run="${c.id}">Controlla ora</button>
      <label class="switch"><input type="checkbox" data-chk-en="${c.id}" ${c.enabled ? 'checked' : ''}><span class="track"></span><span>Attivo</span></label>
      <span style="flex:1"></span><button class="btn sm danger" data-chk-del="${c.id}" data-n="${esc(c.name)}">Elimina</button></div>
  </article>`).join('');
}

async function loadChecks() {
  try { CHK.data = (await api('/api/checks')).checks; CHK.loaded = true; renderChecks(); } catch { /* riprova al ciclo dopo */ }
  if (S.last && (S.tab === 'panoramica' || S.tab === 'schermo')) renderLive(S.last);
}

function openCheckForm(preset) {
  $('#chk-form').reset(); $('#chk-err').textContent = '';
  $('#chk-presets').innerHTML = PRESETS.map((p, i) => `<button class="chip" data-preset="${i}" type="button">${esc(p.n)}</button>`).join('');
  if (preset) fillPreset(preset);
  syncKind(); $('#dlg-check').showModal(); $('#chk-name').focus();
}
function fillPreset(p) { $('#chk-name').value = p.n; $('#chk-kind').value = p.k; $('#chk-target').value = p.t; syncKind(); }
function syncKind() {
  const http = $('#chk-kind').value === 'http';
  $('#chk-target-l').textContent = http ? 'Indirizzo della pagina' : 'Indirizzo e porta';
  $('#chk-target').placeholder = http ? 'https://esempio.it' : '1.1.1.1:443';
}
async function submitCheck(e) {
  e.preventDefault(); $('#chk-err').textContent = '';
  try {
    await api('/api/checks', { method: 'POST', body: { name: $('#chk-name').value, kind: $('#chk-kind').value, target: $('#chk-target').value, interval: Number($('#chk-int').value) } });
    $('#dlg-check').close(); toast('Controllo aggiunto'); await loadChecks();
  } catch (err) { $('#chk-err').textContent = err.message; }
}

// ------------------------------------------------------------------ sistema
const ALERT_FIELDS = [
  ['temp_warn', 'Temperatura: avviso (°C)'], ['temp_crit', 'Temperatura: critica (°C)'],
  ['cpu_high', 'CPU alta (%)'], ['cpu_secs', 'CPU alta da almeno (secondi)'],
  ['mem_high', 'Memoria alta (%)'], ['disk_high', 'Disco quasi pieno (%)'],
];
const ALERT_FLAGS = [
  ['power_alerts', 'Avvisa su sotto-tensione e throttling'], ['container_alerts', 'Avvisa se un container si ferma'],
  ['monitor_alerts', 'Avvisa se un controllo del Monitor non risponde'], ['telegram', 'Invia gli avvisi anche su Telegram'],
];

function piRows(pi) {
  if (!pi.available) {
    return `<div class="muted">Stato di alimentazione non disponibile: il kernel non espone <code>get_throttled</code> su questo dispositivo.</div>${freqRow(pi)}`;
  }
  const f = pi.flags, line = (label, st) => `<div class="row-line"><div class="top"><span>${label}</span>${st}</div></div>`;
  const now = f.undervoltage ? status('crit', 'Sotto-tensione') : status('good', 'Regolare');
  const past = f.undervoltage_past ? status('warn', 'Rilevata dall\'avvio') : status('good', 'Mai rilevata');
  const thr = f.throttled || f.freq_capped ? status('warn', 'Attivo') : status('good', 'Assente');
  const thrPast = f.throttled_past || f.freq_capped_past || f.temp_limit_past ? status('warn', 'Rilevato dall\'avvio') : status('good', 'Mai rilevato');
  return line('Tensione ora', now) + line('Tensione dall\'avvio', past) + line('Throttling ora', thr) + line('Throttling dall\'avvio', thrPast) + freqRow(pi);
}
function freqRow(pi) {
  if (!pi.freq_mhz) return '';
  const pct = pi.freq_max_mhz ? pi.freq_mhz / pi.freq_max_mhz * 100 : 0;
  return `<div class="row-line"><div class="top"><span>Frequenza CPU</span><span>${n0.format(pi.freq_mhz)} MHz${pi.freq_max_mhz ? ` <span class="muted">su ${n0.format(pi.freq_max_mhz)}</span>` : ''}</span></div>
    ${pi.freq_max_mhz ? `<div class="bar"><span style="width:${pct}%;background:var(--s1)"></span></div>` : ''}</div>`;
}
const barCls = (p) => (p >= 90 ? 'crit' : p >= 80 ? 'hot' : '');

function forecastText(f) {
  if (!f) return 'Previsione disponibile dopo almeno 2 giorni di dati.';
  if (f.days_to_full == null) return `Lo spazio occupato è stabile (${n1.format(f.current)}%): al ritmo attuale il disco non si riempie.`;
  const when = f.days_to_full > 365 ? 'oltre un anno' : f.days_to_full > 60 ? `circa ${Math.round(f.days_to_full / 30)} mesi` : `${f.days_to_full} giorni`;
  return `Il disco cresce di circa ${n2.format(f.slope_per_day)}% al giorno: al ritmo attuale si riempie tra ${when}.`;
}

async function loadSystem() {
  const [d, st] = await Promise.all([api('/api/system'), api('/api/settings')]);
  S.info = d;
  $('#sys-info').innerHTML = `
    <dt>Modello</dt><dd>${esc(d.model)}</dd><dt>Nome host</dt><dd>${esc(d.hostname)}</dd><dt>Sistema</dt><dd>${esc(d.os)}</dd>
    <dt>Kernel</dt><dd>${esc(d.kernel)} (${esc(d.arch)})</dd><dt>Core</dt><dd>${d.cores}</dd><dt>Memoria</dt><dd>${bytes(d.mem_total)}</dd>
    <dt>Ultimo avvio</dt><dd>${fmtTime(d.boot_time)}</dd>`;
  $('#sys-pi').innerHTML = piRows(d.pi);
  $('#sys-disks').innerHTML = d.disks.map((x) => `<div class="row-line"><div class="top"><span>${esc(x.mount)} <span class="muted">${esc(x.device)}, ${esc(x.fstype)}</span></span><span>${bytes(x.used)} di ${bytes(x.total)}, ${n0.format(x.percent)}%</span></div>
    <div class="bar ${barCls(x.percent)}"><span style="width:${x.percent}%"></span></div></div>`).join('') || '<div class="muted">Nessun disco rilevato.</div>';
  $('#sys-forecast').textContent = forecastText(d.disk_forecast);
  $('#sys-net').innerHTML = d.interfaces.map((i) => `<div class="row-line"><div class="top"><span>${esc(i.name)}</span>${i.up ? status('good', 'Attiva') : status('off', 'Non attiva')}</div>
    <div class="muted">${i.ipv4.map(esc).join(', ') || 'Nessun indirizzo IPv4'}${i.mac ? `, ${esc(i.mac)}` : ''}${i.speed ? `, ${i.speed} Mbit/s` : ''}</div></div>`).join('') || '<div class="muted">Nessuna interfaccia.</div>';
  $('#sys-db').innerHTML = `<dt>Campioni recenti</dt><dd>${n0.format(d.db.metrics_raw)} (ogni 10 s, ultime ore)</dd>
    <dt>Campioni al minuto</dt><dd>${n0.format(d.db.metrics_1m)}</dd><dt>Eventi</dt><dd>${n0.format(d.db.events)}</dd>
    <dt>Dimensione database</dt><dd>${bytes(d.db.db_bytes)}</dd><dt>Conservazione</dt><dd>${d.retention_days} giorni</dd>`;
  $('#ret-days').textContent = d.retention_days;
  $('#btn-reboot').disabled = $('#btn-shutdown').disabled = !d.features.power || !d.features.docker;
  $('#dk-calc').disabled = !d.features.docker;
  $('#host').textContent = d.hostname; $('#model').textContent = d.model;

  const a = st.alerts; S.thresholds = a;
  if (!$('#alert-form').contains(document.activeElement)) {
    $('#alert-fields').innerHTML = ALERT_FIELDS.map(([k, l]) => `<div class="field"><label for="a-${k}">${l}</label><input type="number" inputmode="numeric" id="a-${k}" name="${k}" value="${a[k]}" step="1" required></div>`).join('')
      + `<div style="grid-column:1/-1">${ALERT_FLAGS.map(([k, l]) =>
        `<label class="check switch"><input type="checkbox" name="${k}" ${a[k] ? 'checked' : ''} ${k === 'telegram' && !st.telegram_configured ? 'disabled' : ''}><span class="track"></span><span>${l}</span></label>`).join('')}</div>`;
  }
  $('#tg-note').textContent = st.telegram_configured ? 'Telegram collegato' : 'Telegram non configurato';
  $('#tg-test').disabled = !st.telegram_configured;
}

async function saveAlerts(e) {
  e.preventDefault();
  const body = {};
  $$('#alert-form input').forEach((i) => { if (i.disabled) return; body[i.name] = i.type === 'checkbox' ? i.checked : Number(i.value); });
  try { const d = await api('/api/settings', { method: 'PUT', body }); S.thresholds = d.alerts; toast('Soglie salvate'); }
  catch (err) { toast(err.message, true); }
}

async function loadDockerUsage() {
  const body = $('#dk-body'); $('#dk-calc').disabled = true; body.innerHTML = '<p class="muted" style="margin:0">Calcolo in corso…</p>';
  try {
    const u = await api('/api/docker/usage');
    const line = (k, v, extra = '') => `<div class="row-item"><span>${k}</span><span>${v}${extra ? ` <span class="sub">${extra}</span>` : ''}</span></div>`;
    body.innerHTML = line('Immagini', bytes(u.images.size), `${u.images.count}, inutilizzate ${bytes(u.images.unused_size)}`)
      + line('Container', bytes(u.containers.size), `${u.containers.count}`) + line('Volumi', bytes(u.volumes.size), `${u.volumes.count}`)
      + line('Cache di build', bytes(u.build_cache.size), `recuperabile ${bytes(u.build_cache.reclaimable)}`)
      + `<div class="actions-row" style="margin-top:4px">
        ${u.images.dangling_count ? `<button class="btn sm" data-prune="images">Rimuovi immagini senza nome (${bytes(u.images.dangling_size)})</button>` : ''}
        ${u.build_cache.reclaimable ? `<button class="btn sm" data-prune="build_cache">Svuota cache di build (${bytes(u.build_cache.reclaimable)})</button>` : ''}</div>
        <p class="muted" style="margin:6px 0 0;font-size:12.5px">La pulizia non tocca container né volumi.</p>`;
  } catch (e) { body.innerHTML = `<p class="muted" style="margin:0">${esc(e.message)}</p>`; }
  $('#dk-calc').disabled = false;
}

async function prune(what) {
  const label = what === 'images' ? 'le immagini senza nome' : 'la cache di build';
  const ok = await confirmDialog({ title: `Rimuovere ${label}?`, text: 'Libera spazio sul disco. Non tocca container né volumi.', okLabel: 'Rimuovi' });
  if (!ok) return;
  try { const r = await api('/api/docker/prune', { method: 'POST', body: { what } }); toast(`Liberati ${bytes(r.reclaimed)}`); loadDockerUsage(); }
  catch (e) { toast(e.message, true); }
}

async function power(action) {
  const label = action === 'reboot' ? 'Riavviare' : 'Spegnere';
  const pw = await confirmDialog({ title: `${label} il Raspberry?`, text: action === 'reboot' ? 'La dashboard sarà irraggiungibile per un minuto circa.' : 'Per riaccenderlo servirà staccare e riattaccare l\'alimentazione.', danger: true, okLabel: label, password: true });
  if (pw === null) return;
  try { await api('/api/power', { method: 'POST', body: { action, password: pw } }); toast(action === 'reboot' ? 'Riavvio in corso…' : 'Spegnimento in corso…'); }
  catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ eventi
let evLevel = '';
async function loadEvents() {
  const kind = $('#ev-kind').value;
  const d = await api(`/api/events?limit=300${evLevel ? '&level=' + evLevel : ''}${kind ? '&kind=' + kind : ''}`);
  const L = { crit: ['crit', 'Critico'], warn: ['warn', 'Avviso'], info: ['off', 'Info'] };
  const K = { alert: 'Soglie', container: 'Container', process: 'Processi', power: 'Alimentazione', auth: 'Accessi', system: 'Sistema', monitor: 'Monitor' };
  $('#ev-body').innerHTML = d.events.length ? d.events.map((e) => `<tr><td>${fmtTime(e.ts, true)}</td><td>${status(...(L[e.level] || ['off', e.level]))}</td><td class="hide-sm">${esc(K[e.kind] || e.kind)}</td><td class="wrap">${esc(e.message)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">Nessun evento registrato.</td></tr>';
}

// ------------------------------------------------------------------ collegamenti agli elementi
async function logout() { await api('/api/logout', { method: 'POST' }).catch(() => {}); location.href = '/login'; }

function bind() {
  $('#logout').onclick = logout; $('#logout2').onclick = logout;
  $('#tb-more').onclick = () => $('#dlg-more').showModal(); $('#more-close').onclick = () => $('#dlg-more').close();
  // chiude le finestre toccando lo sfondo
  $$('dialog.sheet').forEach((dlg) => dlg.addEventListener('click', (e) => {
    if (e.target !== dlg || dlg.id === 'dlg-confirm') return;
    const r = dlg.getBoundingClientRect();         // solo i tocchi fuori dal riquadro chiudono
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
  }));
  $('#dlg-more').addEventListener('click', (e) => { if (e.target.closest('a')) $('#dlg-more').close(); });

  $$('#range button').forEach((b) => (b.onclick = () => {
    histRange = b.dataset.r; $$('#range button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadHistory().catch((e) => toast(e.message, true));
  }));
  $('#csv').onclick = downloadCsv;

  $$('th [data-s]').forEach((b) => (b.onclick = () => { P.sort = b.dataset.s; $$('th [data-s]').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadProcesses(); }));
  let qt; $('#p-q').oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { P.q = e.target.value; loadProcesses(); }, 250); };
  $('#p-group').checked = P.group;
  $('#p-group').onchange = (e) => { P.group = e.target.checked; store.set('pgroup', P.group); loadProcesses(); };
  const rowOpen = (e) => {
    const tr = e.target.closest('tr[data-pid], tr[data-group]'); if (!tr) return;
    if (tr.dataset.group) { P.group = false; $('#p-group').checked = false; store.set('pgroup', false); P.q = tr.dataset.group; $('#p-q').value = P.q; loadProcesses(); } else openProc(+tr.dataset.pid);
  };
  $('#p-body').onclick = rowOpen;
  $('#p-body').onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowOpen(e); } };
  $('#pd-close').onclick = () => $('#dlg-proc').close();
  $('#pd-actions').onclick = (e) => { const b = e.target.closest('[data-sig]'); if (b) procSignal(+$('#pd-actions').dataset.pid, $('#pd-actions').dataset.name, b.dataset.sig); };

  $$('#ctr-filter button').forEach((b) => (b.onclick = () => { CT.filter = b.dataset.f; store.set('cfilter', CT.filter); renderContainers(); }));
  $('#c-q').oninput = (e) => { CT.q = e.target.value; renderContainers(); };
  $('#c-startall').onclick = startAllStopped;
  $('#c-list').onclick = (e) => {
    const b = e.target.closest('[data-ca]'), g = e.target.closest('[data-ga]');
    if (b) { const a = b.dataset.ca; a === 'logs' ? openLogs(b.dataset.n) : a === 'details' ? openCtrDetails(b.dataset.n) : ctrAction(b.dataset.n, a); }
    else if (g) groupAction(g.dataset.g, g.dataset.ga);
  };
  $('#c-list').onchange = (e) => { const i = e.target.closest('[data-ar]'); if (i) setAutoRestart(i.dataset.ar, i.checked, i); };
  $('#cd-close').onclick = () => $('#dlg-ctr').close();
  for (const id of ['ov-ctr', 'k-ctr']) $('#' + id).onclick = (e) => { const b = e.target.closest('[data-ov-start]'); if (b) ctrAction(b.dataset.ovStart, 'start'); };
  $('#lg-refresh').onclick = loadLogs; $('#lg-tail').onchange = loadLogs; $('#lg-live').onchange = setLogLive;
  $('#lg-close').onclick = () => $('#dlg-logs').close();
  $('#dlg-logs').addEventListener('close', () => { clearInterval(logTimer); logTimer = null; });

  $('#chk-add').onclick = () => openCheckForm(); $('#chk-form').onsubmit = submitCheck; $('#chk-kind').onchange = syncKind;
  $('#chk-cancel').onclick = () => $('#dlg-check').close();
  document.addEventListener('click', (e) => {
    const p = e.target.closest('[data-preset]'); if (!p) return;
    const preset = PRESETS[+p.dataset.preset];
    if ($('#dlg-check').open) fillPreset(preset); else openCheckForm(preset);
  });
  $('#chk-list').onclick = async (e) => {
    const run = e.target.closest('[data-chk-run]'), del = e.target.closest('[data-chk-del]');
    if (run) { run.disabled = true; await api(`/api/checks/${run.dataset.chkRun}/run`, { method: 'POST' }).catch((x) => toast(x.message, true)); loadChecks(); }
    if (del) {
      const ok = await confirmDialog({ title: `Eliminare «${del.dataset.n}»?`, text: 'Il controllo e il suo storico verranno cancellati.', danger: true, okLabel: 'Elimina' });
      if (ok) { await api(`/api/checks/${del.dataset.chkDel}`, { method: 'DELETE' }).catch((x) => toast(x.message, true)); loadChecks(); }
    }
  };
  $('#chk-list').onchange = async (e) => {
    const i = e.target.closest('[data-chk-en]'); if (!i) return;
    await api(`/api/checks/${i.dataset.chkEn}/enabled`, { method: 'POST', body: { enabled: i.checked } }).catch((x) => toast(x.message, true)); loadChecks();
  };

  $('#alert-form').onsubmit = saveAlerts;
  $('#tg-test').onclick = () => api('/api/settings/test-telegram', { method: 'POST' }).then(() => toast('Messaggio inviato')).catch((e) => toast(e.message, true));
  $('#btn-reboot').onclick = () => power('reboot'); $('#btn-shutdown').onclick = () => power('shutdown');
  $('#dk-calc').onclick = loadDockerUsage;
  $('#dk-body').onclick = (e) => { const b = e.target.closest('[data-prune]'); if (b) prune(b.dataset.prune); };

  $$('#ev-filter button').forEach((b) => (b.onclick = () => { evLevel = b.dataset.l; $$('#ev-filter button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadEvents(); }));
  $('#ev-kind').onchange = loadEvents;
  $('#k-full').onclick = toggleFullscreen;
  document.addEventListener('visibilitychange', () => { if (S.tab === 'schermo' && !document.hidden) requestWake(); });
  window.addEventListener('hashchange', route);
}

async function boot() {
  bind(); initLiveCharts();
  enter.storico = () => loadHistory().catch((e) => toast(e.message, true));
  enter.processi = () => loadProcesses().catch((e) => toast(e.message, true));
  enter.container = () => loadContainers();
  enter.monitor = () => loadChecks();
  enter.sistema = () => loadSystem().catch((e) => toast(e.message, true));
  enter.eventi = () => loadEvents().catch((e) => toast(e.message, true));
  enter.panoramica = () => { updateLiveCharts(); loadTop().catch(() => {}); };
  enter.schermo = () => { loadTop().catch(() => {}); };
  every(2000, '*', pollLive);
  every(60000, 'storico', loadHistory);
  every(3000, 'processi', loadProcesses);
  every(5000, 'container', () => loadContainers());
  every(10000, 'monitor', loadChecks);
  every(15000, 'sistema', loadSystem);
  every(10000, 'eventi', loadEvents);
  every(5000, ['panoramica', 'schermo'], loadTop);
  every(15000, ['panoramica', 'schermo'], () => loadContainers(false));
  every(15000, ['panoramica', 'schermo'], loadChecks);
  every(30000, '*', () => { if (S.tab !== 'container') loadContainers(false); if (S.tab !== 'monitor') loadChecks(); });
  api('/api/settings').then((d) => { S.thresholds = d.alerts; }).catch(() => {});
  api('/api/system').then((d) => { S.info = d; $('#host').textContent = d.hostname; $('#model').textContent = d.model; $('#ret-days').textContent = d.retention_days; $('#k-host').textContent = d.hostname; }).catch(() => {});
  await seedLive();
  route();
  setInterval(loop, 500); loop();
}
boot();
