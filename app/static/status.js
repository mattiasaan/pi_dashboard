const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v) => (v == null ? '-' : v.toLocaleString('it-IT', { maximumFractionDigits: 2 }) + '%');
// stato sempre con icona e testo, mai solo colore
const ICON = {
  good: '<svg class="ic" viewBox="0 0 24 24" style="color:var(--good)"><circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 9.5"/></svg>',
  crit: '<svg class="ic" viewBox="0 0 24 24" style="color:var(--crit)"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/></svg>',
  off: '<svg class="ic" viewBox="0 0 24 24" style="color:var(--muted)"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
};
function strip(h) {
  const cells = h.map((r) => `<i class="${r.ok ? '' : 'bad'}" title="${new Date(r.ts * 1000).toLocaleString('it-IT')}"></i>`);
  while (cells.length < 40) cells.unshift('<i class="none"></i>');
  return `<div class="strip" aria-hidden="true">${cells.join('')}</div>`;
}
async function load() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) throw new Error();
    const d = await r.json();
    document.title = d.title; $('title').textContent = d.title;
    document.getElementById('title').textContent = d.title;
    const bad = d.checks.filter((c) => c.ok === false).length;
    document.getElementById('sbn').innerHTML = !d.checks.length ? ICON.off + 'Nessun servizio da mostrare'
      : bad ? ICON.crit + (bad === 1 ? 'Un servizio non risponde' : `${bad} servizi non rispondono`)
      : ICON.good + 'Tutti i servizi funzionano';
    document.getElementById('list').innerHTML = d.checks.map((c) => `<section class="panel svc">
      <div class="row"><span class="nm">${esc(c.name)}</span><span>${c.ok ? ICON.good + ' Funziona' : c.ok === false ? ICON.crit + ' Non risponde' : ICON.off + ' In attesa'}</span></div>
      ${strip(c.history)}
      <div class="meta">Disponibilità: ${pct(c.uptime24h)} (24 ore), ${pct(c.uptime7d)} (7 giorni)${c.ok && c.ms != null ? `, risposta ${Math.round(c.ms)} ms` : ''}</div></section>`).join('');
    document.getElementById('foot').textContent = 'Aggiornato alle ' + new Date(d.now * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  } catch {
    document.getElementById('sbn').innerHTML = ICON.off + 'Impossibile leggere lo stato in questo momento';
  }
}
function $(s) { return document.querySelector(s); }
load(); setInterval(load, 30000);
