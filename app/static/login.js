document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('err'); err.textContent = '';
  try {
    const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: document.getElementById('pw').value }) });
    if (r.ok) { location.href = '/'; return; }
    const j = await r.json().catch(() => ({}));
    err.textContent = j.detail || 'Accesso non riuscito';
  } catch { err.textContent = 'Dashboard non raggiungibile'; }
});
