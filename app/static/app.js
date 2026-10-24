/* Pi Dashboard - frontend senza dipendenze oltre a Chart.js (servito in locale). */
'use strict';

// ------------------------------------------------------------------ utilità
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = (d = 0) => new Intl.NumberFormat('it-IT', { minimumFractionDigits: d, maximumFractionDigits: d });
const n0 = nf(0), n1 = nf(1), n2 = nf(2);

function bytes(b, d = 1) {
  if (b == null) return 'n/d';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let v = Math.abs(b);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${nf(i === 0 ? 0 : d).format(Math.sign(b) * v)} ${u[i]}`;
}
const rate = (b) => `${bytes(b)}/s`;
function dur(s) {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} g ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}
const fmtTime = (ts, withSec = false) => new Date(ts * 1000).toLocaleString('it-IT',
  { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', ...(withSec ? { second: '2-digit' } : {}) });
const hhmm = (ts, sec = false) => new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', ...(sec ? { second: '2-digit' } : {}) });

const ICON = {
  good: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="currentColor"/></svg>',
  warn: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11.5 11H.5z" fill="currentColor"/></svg>',
  crit: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .5 11.5 6 6 11.5.5 6z" fill="currentColor"/></svg>',
  off: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
};
const status = (kind, text) => `<span class="st ${kind}">${ICON[kind] || ''}${esc(text)}</span>`;

let toastTimer;
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
  info: null, failures: 0, tab: null,
};
const MAX_LIVE = 400;

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
        label: s.label, data: [], borderColor: s.color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
        pointHoverBackgroundColor: s.color, pointHoverBorderColor: '#1a1a19', pointHoverBorderWidth: 2,
        tension: 0.25, spanGaps: false, fill: !!s.fill, backgroundColor: s.fill ? s.color + '22' : 'transparent',
      })),
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { right: 6 } },
      scales: {
        x: { type: 'linear', grid: { display: false }, border: { color: C.axis }, ticks: { color: C.muted, maxTicksLimit: 6, maxRotation: 0, callback: (v) => o.xFmt(v) } },
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

// ------------------------------------------------------------------ router / polling
const tasks = [];
function every(ms, tab, fn) { tasks.push({ ms, tab, fn, last: 0, busy: false }); }
async function loop() {
  const now = Date.now();
  for (const t of tasks) {
    if (document.hidden || now - t.last < t.ms || t.busy) continue;
    if (t.tab !== '*' && t.tab !== S.tab) continue;
    t.last = now; t.busy = true;
    Promise.resolve().then(t.fn).catch(() => {}).finally(() => { t.busy = false; });
  }
}
const TABS = ['panoramica', 'storico', 'processi', 'container', 'sistema', 'eventi'];
const enter = {};
function route() {
  const t = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'panoramica';
  S.tab = t;
  TABS.forEach((n) => { $('#tab-' + n).hidden = n !== t; });
  $$('.nav a').forEach((a) => (a.dataset.tab === t ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  tasks.forEach((x) => { if (x.tab === t) x.last = 0; });
  enter[t]?.();
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

// ------------------------------------------------------------------ panoramica
const WINDOW = 300; // secondi mostrati nei grafici live e nelle sparkline
function sparkSVG(points, lo, hi, color) {
  if (points.length < 2) return '<div class="spark"></div>';
  const W = 300, H = 38, t1 = points.at(-1).ts, t0 = t1 - WINDOW;
  const pts = points.map((p) => [(p.ts - t0) / WINDOW * W, H - 3 - (Math.min(Math.max(p.v ?? lo, lo), hi) - lo) / (hi - lo) * (H - 6)]);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('');
  const area = `${line}L${pts.at(-1)[0].toFixed(1)} ${H}L${pts[0][0].toFixed(1)} ${H}Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="${area}" fill="${color}" opacity=".13"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.75" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

function level(v, warn, crit) { return v == null ? 'off' : crit != null && v >= crit ? 'crit' : v >= warn ? 'warn' : null; }

function renderTiles(d) {
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
  const root = $('#tiles');
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

function renderCores(d) {
  const root = $('#cores');
  if (root.children.length !== d.cores.length) {
    root.innerHTML = d.cores.map((_, i) => `<div class="core"><div class="track"><div class="fill"></div></div><div class="pct"></div><div class="idx">Core ${i}</div></div>`).join('');
  }
  d.cores.forEach((v, i) => {
    const c = root.children[i];
    $('.fill', c).style.height = Math.max(v, 1.5) + '%';
    $('.pct', c).textContent = n0.format(v) + '%';
  });
  $('#cpu-sub').textContent = d.freq ? `${n0.format(d.freq)} MHz` : '';
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
    renderTiles(d); renderCores(d); renderQuick(d); updateLiveCharts();
  } catch (e) { S.failures++; }
  renderBanner();
}

// ------------------------------------------------------------------ storico
const HIST = [
  { id: 'cpu', title: 'CPU', series: [{ k: 'cpu', label: 'Utilizzo medio', color: C.s1, fill: true }], yMax: 100, fmt: 'pct' },
  { id: 'mem', title: 'Memoria', series: [{ k: 'mem', label: 'RAM', color: C.s3 }, { k: 'swap', label: 'Swap', color: C.s4 }], yMax: 100, fmt: 'pct' },
  { id: 'temp', title: 'Temperatura', series: [{ k: 'temp', label: 'CPU', color: C.s2 }], yMin: 30, fmt: 'temp' },
  { id: 'net', title: 'Rete', series: [{ k: 'net_rx', label: 'In ingresso', color: C.s1 }, { k: 'net_tx', label: 'In uscita', color: C.s2 }], fmt: 'rate' },
  { id: 'dio', title: 'Disco, lettura e scrittura', series: [{ k: 'disk_r', label: 'Lettura', color: C.s1 }, { k: 'disk_w', label: 'Scrittura', color: C.s2 }], fmt: 'rate' },
  { id: 'load', title: 'Carico medio (1 minuto)', series: [{ k: 'load1', label: 'Carico', color: C.s7 }], fmt: 'num' },
];
const FMT = {
  pct: (v, t) => `${t ? n1.format(v) : n0.format(v)}%`, temp: (v) => `${n1.format(v)} °C`,
  rate: (v) => rate(v), num: (v) => n2.format(v),
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
const xFmtRange = (v) => (histRange === '1h' || histRange === '6h' ? hhmm(v) : histRange === '24h' ? hhmm(v) : fmtTime(v).replace(',', ''));

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
  const s = d.series;
  for (const h of HIST) {
    const ch = HCH[h.id];
    h.series.forEach((sr, i) => { ch.data.datasets[i].data = withGaps(s.t, s[sr.k], d.bucket); });
    const now = Date.now() / 1000;
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
const P = { sort: 'cpu', q: '' };
async function loadProcesses() {
  const d = await api(`/api/processes?sort=${P.sort}&limit=80&q=${encodeURIComponent(P.q)}`);
  $('#p-count').textContent = `${d.processes.length} processi`;
  $('#p-body').innerHTML = d.processes.length ? d.processes.map((p) => `
    <tr><td class="num">${p.pid}</td><td class="name">${esc(p.name)}</td><td>${esc(p.user)}</td>
    <td class="num">${n1.format(p.cpu)}</td><td class="num">${bytes(p.rss)} <span class="muted">(${n1.format(p.mem)}%)</span></td>
    <td>${esc(p.status)}</td><td class="cmd" title="${esc(p.cmd)}">${esc(p.cmd)}</td>
    <td style="text-align:right"><button class="btn sm" data-sig="TERM" data-pid="${p.pid}" data-name="${esc(p.name)}">Termina</button>
    <button class="btn sm danger" data-sig="KILL" data-pid="${p.pid}" data-name="${esc(p.name)}">Forza</button></td></tr>`).join('')
    : '<tr><td colspan="8" class="empty">Nessun processo corrisponde alla ricerca.</td></tr>';
}

async function procSignal(pid, name, sig) {
  const ok = await confirmDialog({
    title: sig === 'KILL' ? `Forzare l'arresto di ${name}?` : `Terminare ${name}?`,
    text: sig === 'KILL' ? `Il processo ${pid} verrà chiuso subito, senza salvare nulla.` : `Il processo ${pid} riceverà una richiesta di chiusura ordinata.`,
    danger: true, okLabel: sig === 'KILL' ? 'Forza arresto' : 'Termina',
  });
  if (!ok) return;
  try { await api(`/api/processes/${pid}/signal`, { method: 'POST', body: { signal: sig } }); toast(`${name} (${pid}): richiesta inviata`); loadProcesses(); }
  catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ container
const CT = { busy: new Set(), data: [] };
function ctrStatus(c) {
  if (c.status === 'running') return c.health === 'unhealthy' ? status('crit', 'Non sano') : c.health === 'starting' ? status('warn', 'In avvio') : status('good', 'In esecuzione');
  if (c.status === 'restarting') return status('warn', 'In riavvio');
  if (c.status === 'paused') return status('warn', 'In pausa');
  if (c.status === 'exited' && c.exit_code) return status('crit', `Terminato (codice ${c.exit_code})`);
  return status('off', c.status === 'created' ? 'Creato' : 'Fermato');
}

function renderContainers() {
  const list = CT.data;
  $('#c-body').innerHTML = list.length ? list.map((c) => {
    const run = c.status === 'running', busy = CT.busy.has(c.name), n = esc(c.name);
    const memPct = c.mem_limit && c.mem_used != null ? Math.min(100, c.mem_used / c.mem_limit * 100) : 0;
    const acts = c.is_self
      ? `<span class="muted">Questa dashboard</span> <button class="btn sm" data-ca="logs" data-n="${n}">Log</button>`
      : `${run ? `<button class="btn sm" data-ca="restart" data-n="${n}" ${busy ? 'disabled' : ''}>Riavvia</button> <button class="btn sm danger" data-ca="stop" data-n="${n}" ${busy ? 'disabled' : ''}>Ferma</button>`
        : `<button class="btn sm primary" data-ca="start" data-n="${n}" ${busy ? 'disabled' : ''}>Avvia</button>`} <button class="btn sm" data-ca="logs" data-n="${n}">Log</button>`;
    return `<tr><td class="name">${n}<div class="muted" style="font-weight:400;font-size:12px">${esc(c.id)}, riavvio: ${esc(c.restart_policy)}</div></td>
      <td>${ctrStatus(c)}</td><td class="muted img" title="${esc(c.image)}">${esc(c.image)}</td><td>${c.ports.length ? c.ports.map(esc).join('<br>') : '<span class="muted">Nessuna</span>'}</td>
      <td class="num">${c.cpu == null ? '<span class="muted">-</span>' : n1.format(c.cpu) + '%'}</td>
      <td>${c.mem_used == null ? '<span class="muted">-</span>' : `${bytes(c.mem_used)}<div class="bar" style="margin-top:4px;min-width:80px"><span style="width:${memPct}%"></span></div>`}</td>
      <td style="text-align:right;white-space:nowrap">${acts}</td></tr>`;
  }).join('') : '<tr><td colspan="7" class="empty">Nessun container trovato su questo Docker.</td></tr>';
  const run = list.filter((c) => c.status === 'running').length;
  $('#nav-ctr').textContent = list.length ? `${run}/${list.length}` : '';
}

async function loadContainers(withStats = true) {
  try {
    const d = await api('/api/containers' + (withStats ? '' : '?stats=0'));
    CT.data = d.containers; $('#ctr-msg').innerHTML = ''; $('#tab-container .panel').hidden = false;
    renderContainers();
  } catch (e) {
    $('#ctr-msg').innerHTML = `<div class="alert warn" role="alert">${status('warn', 'Docker non disponibile')}<span>${esc(e.message)}</span></div>`;
    $('#tab-container .panel').hidden = true; $('#nav-ctr').textContent = '';
  }
}

async function ctrAction(name, act) {
  if (act === 'stop') {
    const ok = await confirmDialog({ title: `Fermare ${name}?`, text: 'Il container verrà arrestato.', danger: true, okLabel: 'Ferma' });
    if (!ok) return;
  }
  CT.busy.add(name); renderContainers();
  try {
    await api(`/api/containers/${encodeURIComponent(name)}/${act}`, { method: 'POST' });
    toast(`${name}: ${{ start: 'avviato', stop: 'fermato', restart: 'riavviato' }[act]}`);
  } catch (e) { toast(e.message, true); }
  CT.busy.delete(name); await loadContainers();
}

let logName = '';
async function loadLogs() {
  const pre = $('#lg-pre');
  try {
    const d = await api(`/api/containers/${encodeURIComponent(logName)}/logs?tail=${$('#lg-tail').value}`);
    pre.textContent = d.logs || '(nessun log)'; pre.scrollTop = pre.scrollHeight;
  } catch (e) { pre.textContent = e.message; }
}
function openLogs(name) { logName = name; $('#lg-title').textContent = `Log di ${name}`; $('#lg-pre').textContent = 'Caricamento…'; $('#dlg-logs').showModal(); loadLogs(); }

// ------------------------------------------------------------------ sistema
const ALERT_FIELDS = [
  ['temp_warn', 'Temperatura: avviso (°C)'], ['temp_crit', 'Temperatura: critica (°C)'],
  ['cpu_high', 'CPU alta (%)'], ['cpu_secs', 'CPU alta da almeno (secondi)'],
  ['mem_high', 'Memoria alta (%)'], ['disk_high', 'Disco quasi pieno (%)'],
];
const ALERT_FLAGS = [['power_alerts', 'Avvisa su sotto-tensione e throttling'], ['container_alerts', 'Avvisa se un container si ferma'], ['telegram', 'Invia gli avvisi anche su Telegram']];

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
  $('#sys-net').innerHTML = d.interfaces.map((i) => `<div class="row-line"><div class="top"><span>${esc(i.name)}</span>${i.up ? status('good', 'Attiva') : status('off', 'Non attiva')}</div>
    <div class="muted">${i.ipv4.map(esc).join(', ') || 'Nessun indirizzo IPv4'}${i.mac ? `, ${esc(i.mac)}` : ''}${i.speed ? `, ${i.speed} Mbit/s` : ''}</div></div>`).join('') || '<div class="muted">Nessuna interfaccia.</div>';
  $('#sys-db').innerHTML = `<dt>Campioni recenti</dt><dd>${n0.format(d.db.metrics_raw)} (ogni 10 s, ultime ore)</dd>
    <dt>Campioni al minuto</dt><dd>${n0.format(d.db.metrics_1m)}</dd><dt>Eventi</dt><dd>${n0.format(d.db.events)}</dd>
    <dt>Dimensione database</dt><dd>${bytes(d.db.db_bytes)}</dd><dt>Conservazione</dt><dd>${d.retention_days} giorni</dd>`;
  $('#ret-days').textContent = d.retention_days;
  $('#btn-reboot').disabled = $('#btn-shutdown').disabled = !d.features.power || !d.features.docker;
  $('#host').textContent = d.hostname; $('#model').textContent = d.model;

  const a = st.alerts; S.thresholds = a;
  $('#alert-fields').innerHTML = ALERT_FIELDS.map(([k, l]) => `<div class="field"><label for="a-${k}">${l}</label><input type="number" id="a-${k}" name="${k}" value="${a[k]}" step="1" required></div>`).join('');
  $('#alert-fields').insertAdjacentHTML('beforeend', `<div style="grid-column:1/-1">${ALERT_FLAGS.map(([k, l]) =>
    `<label class="check"><input type="checkbox" name="${k}" ${a[k] ? 'checked' : ''} ${k === 'telegram' && !st.telegram_configured ? 'disabled' : ''}> ${l}</label>`).join('')}</div>`);
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
  const d = await api(`/api/events?limit=300${evLevel ? '&level=' + evLevel : ''}`);
  const L = { crit: ['crit', 'Critico'], warn: ['warn', 'Avviso'], info: ['off', 'Info'] };
  const K = { alert: 'Soglie', container: 'Container', process: 'Processi', power: 'Alimentazione', auth: 'Accessi', system: 'Sistema' };
  $('#ev-body').innerHTML = d.events.length ? d.events.map((e) => `<tr><td>${fmtTime(e.ts, true)}</td><td>${status(...L[e.level] || ['off', e.level])}</td><td>${esc(K[e.kind] || e.kind)}</td><td style="white-space:normal">${esc(e.message)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">Nessun evento registrato.</td></tr>';
}

// ------------------------------------------------------------------ avvio
function bind() {
  $('#logout').onclick = async () => { await api('/api/logout', { method: 'POST' }).catch(() => {}); location.href = '/login'; };
  $$('#range button').forEach((b) => (b.onclick = () => {
    histRange = b.dataset.r; $$('#range button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadHistory().catch((e) => toast(e.message, true));
  }));
  $('#csv').onclick = downloadCsv;
  $$('th [data-s]').forEach((b) => (b.onclick = () => { P.sort = b.dataset.s; $$('th [data-s]').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadProcesses(); }));
  let qt; $('#p-q').oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { P.q = e.target.value; loadProcesses(); }, 250); };
  $('#p-body').onclick = (e) => { const b = e.target.closest('[data-sig]'); if (b) procSignal(+b.dataset.pid, b.dataset.name, b.dataset.sig); };
  $('#c-body').onclick = (e) => {
    const b = e.target.closest('[data-ca]'); if (!b) return;
    b.dataset.ca === 'logs' ? openLogs(b.dataset.n) : ctrAction(b.dataset.n, b.dataset.ca);
  };
  $('#lg-refresh').onclick = loadLogs; $('#lg-tail').onchange = loadLogs; $('#lg-close').onclick = () => $('#dlg-logs').close();
  $('#alert-form').onsubmit = saveAlerts;
  $('#tg-test').onclick = () => api('/api/settings/test-telegram', { method: 'POST' }).then(() => toast('Messaggio inviato')).catch((e) => toast(e.message, true));
  $('#btn-reboot').onclick = () => power('reboot'); $('#btn-shutdown').onclick = () => power('shutdown');
  $$('#ev-filter button').forEach((b) => (b.onclick = () => { evLevel = b.dataset.l; $$('#ev-filter button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); loadEvents(); }));
  window.addEventListener('hashchange', route);
}

async function boot() {
  bind(); initLiveCharts();
  enter.storico = () => loadHistory().catch((e) => toast(e.message, true));
  enter.processi = () => loadProcesses().catch((e) => toast(e.message, true));
  enter.container = () => loadContainers();
  enter.sistema = () => loadSystem().catch((e) => toast(e.message, true));
  enter.eventi = () => loadEvents().catch((e) => toast(e.message, true));
  every(2000, '*', pollLive);
  every(60000, 'storico', loadHistory);
  every(3000, 'processi', loadProcesses);
  every(5000, 'container', () => loadContainers());
  every(30000, '*', () => (S.tab === 'container' ? null : loadContainers(false)));
  every(15000, 'sistema', loadSystem);
  every(10000, 'eventi', loadEvents);
  seedLive().then(() => { if (S.live.length) updateLiveCharts(); });
  api('/api/settings').then((d) => { S.thresholds = d.alerts; }).catch(() => {});
  api('/api/system').then((d) => { S.info = d; $('#host').textContent = d.hostname; $('#model').textContent = d.model; $('#ret-days').textContent = d.retention_days; }).catch(() => {});
  route();
  setInterval(loop, 500); loop();
}
boot();
