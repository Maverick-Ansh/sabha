// Formatting, hue lookups and event summaries. Pure functions, no DOM writes.

export const HUES = ['sun', 'coral', 'mint', 'sky', 'lilac', 'rose'];

let clockOffset = 0; // server_ts - client_ts, seconds
export function setClockOffset(s) { if (Number.isFinite(s)) clockOffset = s; }
export function serverNow() { return Date.now() / 1000 + clockOffset; }

const rgbCache = new Map();
export function clearColorCache() { rgbCache.clear(); }  // the theme changed: the *-rgb tokens have new values
export function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
// [r,g,b] from the --<name>-rgb token
export function rgbOf(name) {
  let v = rgbCache.get(name);
  if (!v) {
    v = cssVar(`--${name}-rgb`).split(',').map((x) => parseFloat(x) || 0);
    if (v.length !== 3) v = [128, 128, 128];
    rgbCache.set(name, v);
  }
  return v;
}
export const rgba = (rgb, a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;
export function hueOf(agent) { return agent && HUES.includes(agent.hue) ? agent.hue : 'sky'; }

export function fmtDur(s, short = false) {
  s = Math.max(0, Math.floor(s || 0));
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return short ? `${hr}h${m ? ' ' + m + 'm' : ''}` : `${hr}h ${m}m`;
  if (m) return short || m >= 10 ? `${m}m` : `${m}m ${sec}s`;
  return `${sec}s`;
}
const pad = (n) => String(n).padStart(2, '0');
export function fmtClock(ts, seconds = true) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? ':' + pad(d.getSeconds()) : ''}`;
}
export function fmtDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + fmtClock(ts, false);
}
export function fmtAgo(ts) {
  if (!ts) return '';
  const s = serverNow() - ts;
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return fmtDate(ts);
}
export function fmtNum(n) {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}
export function fmtBytes(b) {
  b = Number(b) || 0;
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(b < 10240 ? 1 : 0)} KB`;
  return `${(b / 1048576).toFixed(1)} MB`;
}
export function oneLine(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
export function truncate(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
export function asText(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

// Tool name -> orbit station
export const STATIONS = ['think', 'shell', 'files', 'web', 'plan', 'talk', 'tool'];
export function stationFor(tool) {
  const t = String(tool || '').toLowerCase();
  if (/bash|pwsh|powershell|shell|terminal|exec|command|run_?cmd/.test(t)) return 'shell';
  if (/web|search|fetch|http|browse|url/.test(t)) return 'web';
  if (/todo|plan|goal|task/.test(t)) return 'plan';
  if (/read|write|edit|fs|file|glob|grep|ls|dir|patch|apply/.test(t)) return 'files';
  if (/talk|chat|ask|message|reply/.test(t)) return 'talk';
  return 'tool';
}

// One-line summary of a tool input.
export function inputSummary(tool, input) {
  if (input == null) return '';
  if (typeof input === 'string') return oneLine(input);
  if (typeof input !== 'object') return String(input);
  const pick = ['command', 'cmd', 'script', 'path', 'file_path', 'filePath', 'file', 'url', 'query', 'q', 'pattern', 'glob', 'name', 'goal', 'text', 'content'];
  for (const k of pick) {
    const v = input[k];
    if (typeof v === 'string' && v.trim()) {
      const extra = k.includes('path') && typeof input.content === 'string' ? ` (${fmtBytes(input.content.length)})` : '';
      return oneLine(v) + extra;
    }
  }
  for (const k of ['todos', 'items', 'steps', 'plan']) {
    if (Array.isArray(input[k])) return `${input[k].length} item${input[k].length === 1 ? '' : 's'}`;
  }
  try { return oneLine(JSON.stringify(input)); } catch { return ''; }
}

// Short kind code used by the stream filters and the timeline colours.
export function kindOf(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case 'run.thinking': return 'think';
    case 'run.text': return 'text';
    case 'run.tool_call': return 'call';
    case 'run.tool_result': return d.status === 'error' || d.error ? 'err' : 'ok';
    case 'run.final': return 'final';
    case 'run.error': return 'err';
    case 'run.ended': return d.ok === false ? 'err' : 'run';
    case 'run.started': return 'run';
    case 'run.status': case 'run.waiting': return 'status';
    case 'run.stderr': return 'stderr';
    case 'run.raw': return 'raw';
    case 'plan.proposed': return 'plan';
    case 'human.note': case 'human.approved': case 'human.stopped': return 'human';
    default: return 'entity';
  }
}

// One-line human summary of any event (roster "now", timeline hover, palette).
export function eventSummary(ev, toolName) {
  const d = ev.data || {};
  switch (ev.type) {
    case 'run.thinking': return 'thinking · ' + oneLine(d.text);
    case 'run.text': return oneLine(d.text);
    case 'run.tool_call': return `${d.tool || 'tool'} · ${inputSummary(d.tool, d.input)}`;
    case 'run.tool_result': {
      const head = `${toolName || 'result'} ${d.status === 'error' || d.error ? '✕' : '✓'}`;
      return head + ' · ' + oneLine(d.error || asText(d.result)).slice(0, 120);
    }
    case 'run.final': return 'final · ' + oneLine(d.text);
    case 'run.error': return 'error · ' + oneLine(d.message);
    case 'run.started': return `run ${d.run || ''} started${d.resumed ? ' (resumed)' : ''}`;
    case 'run.ended': return `run ${d.run || ''} ended · exit ${d.exit_code ?? '?'}`;
    case 'run.status': return statusLine(d);
    case 'run.waiting': return `waiting · ${d.name || 'model'} is still loading`;
    case 'run.stderr': return oneLine(d.line);
    case 'run.raw': return 'raw · ' + oneLine(asText(d.raw));
    case 'plan.proposed': return `plan proposed · ${(d.steps || []).length} steps`;
    case 'human.note': return 'note · ' + oneLine(d.note && d.note.text);
    case 'human.approved': return `plan approved · ${(d.steps || []).length} steps`;
    case 'human.stopped': return 'stopped by you';
    default: return ev.type;
  }
}

export function usageTokens(u) {
  if (!u || typeof u !== 'object') return 0;
  const i = u.input ?? u.prompt_tokens ?? u.inputTokens ?? 0;
  const o = u.output ?? u.completion_tokens ?? u.outputTokens ?? 0;
  return (Number(i) || 0) + (Number(o) || 0) || Number(u.total_tokens || u.totalTokens || 0);
}
export function statusLine(d) {
  const parts = [];
  if (d.turn != null) parts.push(`turn ${d.turn}`);
  if (d.step != null) parts.push(`step ${d.step}`);
  const tok = usageTokens(d.usage);
  if (tok) parts.push(`${fmtNum(tok)} tok`);
  if (d.reason) parts.push(typeof d.reason === 'string' ? d.reason : (d.reason.kind || 'ended'));
  if (!parts.length) parts.push(String(d.phase || 'status').replace('_', ' '));
  return parts.join(' · ');
}

// Mission status -> tone + label
export function tone(status) {
  switch (status) {
    case 'running': case 'planning': return 'running';
    case 'awaiting_approval': return 'needs';
    case 'failed': return 'failed';
    case 'done': case 'stopped': return 'done';
    default: return 'queued';
  }
}
export function statusLabel(status) {
  return ({ awaiting_approval: 'needs you', planning: 'planning', running: 'running', queued: 'queued',
    done: 'done', failed: 'failed', stopped: 'stopped' })[status] || status || 'unknown';
}
export const isActive = (m) => m && (m.status === 'running' || m.status === 'planning');
export function planProgress(plan) {
  const p = Array.isArray(plan) ? plan : [];
  const done = p.filter((s) => s.status === 'done' || s.status === 'skipped').length;
  const doing = p.some((s) => s.status === 'doing');
  return { done, total: p.length, frac: p.length ? (done + (doing ? 0.5 : 0)) / p.length : 0 };
}
export function elapsed(m) {
  if (!m) return 0;
  const start = m.started_ts || m.created_ts;
  if (!start) return 0;
  return (m.ended_ts || serverNow()) - start;
}

export function greeting(d = new Date()) {
  const hr = d.getHours();
  if (hr < 5) return ['Burning the midnight oil.', 'What are we building?'];
  if (hr < 12) return ['Good morning.', 'What are we building?'];
  if (hr < 17) return ['Good afternoon.', 'What should the council take on?'];
  if (hr < 22) return ['Good evening.', 'What are we building tonight?'];
  return ['Late night.', 'What are we building?'];
}

export function prettyJSON(v) { try { return JSON.stringify(v, null, 2); } catch { return String(v); } }
