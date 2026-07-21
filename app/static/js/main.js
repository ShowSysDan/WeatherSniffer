/* Shared helpers for WeatherSniffer pages. */

function wsCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : '';
}

async function wsPostJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': wsCsrfToken() },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  return { status: resp.status, data: data || { ok: false, error: 'HTTP ' + resp.status } };
}

/* Stamp the CSRF token into every POST form so templates don't have to. */
document.addEventListener('DOMContentLoaded', () => {
  const token = wsCsrfToken();
  if (!token) return;
  document.querySelectorAll('form[method=post], form[method=POST]').forEach(form => {
    if (form.querySelector('input[name=_csrf]')) return;
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = '_csrf';
    input.value = token;
    form.appendChild(input);
  });
});

function wsShowOutput(el, text) {
  el.textContent = text;
  el.style.display = 'block';
}
