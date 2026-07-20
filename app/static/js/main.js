/* Shared helpers for WeatherSniffer pages. */

async function wsPostJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  return { status: resp.status, data: data || { ok: false, error: 'HTTP ' + resp.status } };
}

function wsShowOutput(el, text) {
  el.textContent = text;
  el.style.display = 'block';
}
