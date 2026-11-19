// REST + WebSocket client. Same origin. Token per PROTOCOL section 1.

const TOKEN_KEY = 'sabha.token';
let token = '';
let onUnauthorized = () => {};

export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }
export function getToken() { return token; }

function lsGet(k) { try { return localStorage.getItem(k) || ''; } catch { return ''; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

// Read #token=... (possibly mixed with a route like #/watch&token=...), store it, strip it from the URL.
export function initToken() {
  const hash = location.hash || '';
  const m = hash.match(/[#&?]token=([^&]+)/);
  if (m) {
    token = decodeURIComponent(m[1]);
    lsSet(TOKEN_KEY, token);
    let rest = hash.replace(/[#&?]?token=[^&]+&?/, '');
    if (rest && !rest.startsWith('#')) rest = '#' + rest;
    if (rest === '#') rest = '';
    history.replaceState(null, '', location.pathname + location.search + rest);
  } else {
    token = lsGet(TOKEN_KEY);
  }
  return token;
}

// Accept a full hub link or a bare token.
export function setTokenFrom(input) {
  const s = String(input || '').trim();
  const m = s.match(/token=([^&#\s]+)/);
  token = m ? decodeURIComponent(m[1]) : s;
  lsSet(TOKEN_KEY, token);
  return token;
}

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(method, path, body) {
  const headers = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new ApiError(0, 'The hub is unreachable');
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 200) }; } }
  if (res.status === 401) { onUnauthorized(); throw new ApiError(401, 'unauthorized'); }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}
export const get = (p) => api('GET', p);
export const post = (p, b = {}) => api('POST', p, b);
export const patch = (p, b) => api('PATCH', p, b);
export const put = (p, b) => api('PUT', p, b);
export const del = (p) => api('DELETE', p);
export const enc = encodeURIComponent;

// WebSocket with reconnect + backoff. handlers: { onHello, onEvent, onPong, onStatus, getSince }
export function connect(handlers) {
  let ws = null, tries = 0, timer = 0, pingTimer = 0, closed = false, opened = false, lastPing = 0;

  const status = (s) => handlers.onStatus && handlers.onStatus(s);

  function open() {
    if (closed) return;
    const since = handlers.getSince ? handlers.getSince() : 0;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const qs = new URLSearchParams();
    if (token) qs.set('token', token);
    if (since > 0) qs.set('since', String(since));
    opened = false;
    status(tries ? 'reconnecting' : 'connecting');
    try { ws = new WebSocket(`${proto}//${location.host}/ws?${qs}`); } catch { schedule(); return; }

    ws.onopen = () => { opened = true; };
    ws.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch { return; }
      if (ev.type === 'hello') {
        tries = 0;
        status('live');
        handlers.onHello && handlers.onHello(ev);
        startPing();
      } else if (ev.type === 'pong') {
        const now = Date.now();
        handlers.onPong && handlers.onPong(ev, (lastPing + now) / 2000);
      } else {
        handlers.onEvent && handlers.onEvent(ev);
      }
    };
    ws.onclose = async () => {
      stopPing();
      ws = null;
      if (closed) return;
      // A handshake that never opened may be an auth failure: ask REST, which can see 401.
      if (!opened) {
        try { await get('/api/state'); } catch (e) { if (e.status === 401) { status('auth'); return; } }
      }
      schedule();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  function schedule() {
    tries++;
    status('reconnecting');
    const delay = Math.min(15000, 600 * Math.pow(1.7, tries - 1)) * (0.75 + Math.random() * 0.5);
    clearTimeout(timer);
    timer = setTimeout(open, delay);
  }
  function startPing() {
    stopPing();
    const ping = () => {
      if (ws && ws.readyState === 1) { lastPing = Date.now(); ws.send(JSON.stringify({ type: 'ping' })); }
    };
    ping();
    pingTimer = setInterval(ping, 25000);
  }
  function stopPing() { clearInterval(pingTimer); }

  // Wake up immediately when the tab returns or the network comes back.
  const kick = () => { if (!ws && !closed) { clearTimeout(timer); open(); } };
  addEventListener('online', kick);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) kick(); });

  open();
  return {
    close() { closed = true; clearTimeout(timer); stopPing(); if (ws) ws.close(); },
    reconnect() { closed = false; tries = 0; if (ws) ws.close(); else open(); },
  };
}
