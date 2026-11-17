// Session timeline: x = the cloud session window, one swimlane per agent, events as ticks
// coloured by kind, missions as soft bars. Wheel/pinch zoom, drag to pan, click to replay.
import { h, showTip, hideTip } from './dom.js';
import * as log from './events.js';
import { subscribe, getState, agents, sessionWindow } from './store.js';
import { rgbOf, rgba, hueOf, fmtClock, serverNow, fmtDur, truncate } from './format.js';

const MONO = '"Geist Mono", "Cascadia Mono", Consolas, monospace';
// kind -> [priority, colour token]
const KINDS = { err: [9, 'coral'], final: [8, 'sun'], human: [7, 'rose'], plan: [6, 'sun'], call: [5, 'sky'],
  ok: [4, 'mint'], think: [3, 'lilac'], text: [2, 'ink'], run: [1, 'ink'], status: [1, 'ink'], stderr: [1, 'ink'], raw: [1, 'ink'] };
const KIND_CODE = Object.keys(KINDS);
const STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];

export function createTimeline({ onJump } = {}) {
  const canvas = h('canvas.tl-canvas');
  const hoverLine = h('div.tl-hover', { hidden: true });
  const replayLine = h('div.tl-replay', { hidden: true });
  const hint = h('div.tl-hint.micro', null, 'scroll to zoom · drag to pan · click to replay · double-click for the whole session');
  const el = h('div.timeline', null, canvas, replayLine, hoverLine, hint);
  const ctx = canvas.getContext('2d');
  let INK = rgbOf('ink');
  const colors = {};
  const paintColors = () => {
    INK = rgbOf('ink');
    for (const k of KIND_CODE) colors[k] = KINDS[k][1] === 'ink' ? rgba(INK, k === 'text' ? 0.35 : 0.2) : rgba(rgbOf(KINDS[k][1]), 0.9);
  };
  paintColors();
  addEventListener('sabha-theme', () => { paintColors(); dirty = true; });
  let W = 0, H = 0, dpr = 1, gutter = 92, view = null, dirty = true, visible = false, replayTs = null;
  const AXIS = 18;

  const win = () => {
    const w = sessionWindow();
    const idx = log.entries();
    const first = idx.length ? idx[0].ts : w.start;
    const start = Math.min(w.start, first);
    return { start, end: Math.max(w.end, serverNow() + 60) };
  };
  function full() { const w = win(); const pad = (w.end - w.start) * 0.01; return { v0: w.start - pad, v1: w.end + pad }; }
  // Early in a session the whole window squeezes everything into a sliver: start zoomed on the first hours.
  function initial() {
    const w = win(), el = serverNow() - w.start;
    if (el > 3 * 3600) return full();
    const span = Math.max(2 * 3600, el * 1.6);
    return { v0: w.start - span * 0.01, v1: w.start + span };
  }
  const X = (ts) => gutter + ((ts - view.v0) / (view.v1 - view.v0)) * (W - gutter - 8);
  const T = (x) => view.v0 + ((x - gutter) / (W - gutter - 8)) * (view.v1 - view.v0);
  function lanes() {
    const list = agents();
    const n = Math.max(1, list.length);
    const lh = Math.max(9, Math.min(20, (H - AXIS - 8) / n));
    return { list, lh, y0: AXIS + 4 + Math.max(0, (H - AXIS - 8 - lh * n) / 2) };
  }

  function resize() {
    const b = el.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(10, b.width); H = Math.max(10, b.height);
    gutter = W < 560 ? 8 : 92;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    if (!view) view = initial();
    mark();
  }
  function mark() { dirty = true; if (visible) requestAnimationFrame(render); }

  function render() {
    if (!dirty || !visible || W < 20) return;
    dirty = false;
    if (!view) view = initial();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const w = win(), now = serverNow();
    const { list, lh, y0 } = lanes();
    // elapsed shade
    const xs = Math.max(gutter, X(w.start)), xn = Math.min(W - 8, X(now));
    if (xn > xs) { ctx.fillStyle = rgba(INK, 0.028); ctx.fillRect(xs, AXIS, xn - xs, H - AXIS); }
    // axis
    const span = view.v1 - view.v0;
    const step = STEPS.find((s) => ((W - gutter) / (span / s)) > 70) || 21600;
    ctx.font = `500 9.5px ${MONO}`; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    const xLimit = X(w.end);
    for (let t = Math.ceil(view.v0 / step) * step; t < view.v1; t += step) {
      const x = X(t);
      if (x < gutter || Math.abs(x - xLimit) < 44) continue;
      ctx.fillStyle = rgba(INK, 0.09); ctx.fillRect(Math.round(x), AXIS, 1, H - AXIS);
      ctx.fillStyle = rgba(INK, 0.42); ctx.fillText(fmtClock(t, false), x + 4, AXIS / 2 + 1);
    }
    ctx.fillStyle = rgba(INK, 0.1); ctx.fillRect(gutter, AXIS, W - gutter - 8, 1);
    // lanes + labels
    const laneOf = new Map();
    list.forEach((a, i) => {
      laneOf.set(a.id, i);
      const y = y0 + i * lh;
      ctx.fillStyle = rgba(INK, 0.05); ctx.fillRect(gutter, Math.round(y + lh) - 0.5, W - gutter - 8, 1);
      if (gutter > 20) {
        const rgb = rgbOf(hueOf(a));
        ctx.fillStyle = rgba(rgb, 1); ctx.beginPath(); ctx.arc(12, y + lh / 2, 3, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = rgba(INK, 0.62); ctx.textAlign = 'left'; ctx.font = `500 10px ${MONO}`;
        ctx.fillText(truncate(a.name, 10), 21, y + lh / 2 + 0.5);
      }
    });
    // mission bars
    for (const m of Object.values(getState().missions)) {
      const lane = laneOf.get(m.agent_id);
      if (lane == null || !m.started_ts) continue;
      const a = agents()[lane];
      const x0 = Math.max(gutter, X(m.started_ts)), x1 = Math.min(W - 8, X(m.ended_ts || now));
      if (x1 < gutter || x0 > W) continue;
      const y = y0 + lane * lh + 2;
      const rgb = rgbOf(hueOf(a));
      ctx.fillStyle = rgba(rgb, m.status === 'failed' ? 0.1 : 0.16);
      roundRect(x0, y, Math.max(3, x1 - x0), lh - 4, Math.min(6, (lh - 4) / 2));
      ctx.fill();
    }
    // event ticks, bucketed per pixel column per lane (highest priority kind wins)
    const cols = Math.ceil(W);
    const buckets = list.map(() => new Uint8Array(cols));
    const idx = log.entries();
    let i = log.indexOfTime(view.v0);
    for (; i < idx.length; i++) {
      const e = idx[i];
      if (e.ts > view.v1) break;
      const lane = laneOf.get(e.a);
      if (lane == null || e.k === 'entity') continue;
      const x = Math.round(X(e.ts));
      if (x < gutter || x >= cols) continue;
      const code = KIND_CODE.indexOf(e.k) + 1;
      const b = buckets[lane];
      if (!b[x] || KINDS[KIND_CODE[b[x] - 1]][0] < KINDS[e.k][0]) b[x] = code;
    }
    buckets.forEach((b, lane) => {
      const y = y0 + lane * lh;
      for (let x = gutter; x < cols; x++) {
        if (!b[x]) continue;
        const k = KIND_CODE[b[x] - 1];
        const big = k === 'err' || k === 'final' || k === 'human';
        const th = big ? lh - 3 : lh * 0.5;
        ctx.fillStyle = colors[k];
        ctx.fillRect(x, y + (lh - th) / 2, big ? 2 : 1.4, th);
      }
    });
    // session limit + now
    const xe = X(w.end);
    if (xe > gutter && xe < W) {
      ctx.strokeStyle = rgba(rgbOf('coral'), 0.6); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(xe + 0.5, AXIS); ctx.lineTo(xe + 0.5, H); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = rgba(rgbOf('coral'), 0.8); ctx.textAlign = 'right'; ctx.fillText('limit', xe - 4, AXIS / 2 + 1);
    }
    const xnow = X(now);
    if (xnow > gutter && xnow < W) {
      const sun = rgbOf('core-a');
      ctx.fillStyle = rgba(sun, 0.95); ctx.fillRect(Math.round(xnow), AXIS - 2, 1.5, H - AXIS + 2);
      ctx.beginPath(); ctx.arc(xnow + 0.75, AXIS - 3, 3, 0, Math.PI * 2); ctx.fill();
    }
    placeReplay();
  }
  function roundRect(x, y, w, hh, r) {
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, hh, r); else ctx.rect(x, y, w, hh);
  }
  function placeReplay() {
    if (replayTs == null || !view) { replayLine.hidden = true; return; }
    const x = X(replayTs);
    replayLine.hidden = x < gutter || x > W;
    replayLine.style.transform = `translateX(${x}px)`;
  }

  // ---------- interaction ----------
  function zoomAt(x, factor) {
    const t = T(x), w = win();
    const maxSpan = (w.end - w.start) * 1.6;
    let span = Math.min(maxSpan, Math.max(45, (view.v1 - view.v0) * factor));
    const k = (x - gutter) / (W - gutter - 8);
    view = { v0: t - span * k, v1: t - span * k + span };
    mark();
  }
  el.addEventListener('wheel', (e) => {
    e.preventDefault();
    const b = el.getBoundingClientRect();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { const dt = (e.deltaX / (W - gutter)) * (view.v1 - view.v0); view = { v0: view.v0 + dt, v1: view.v1 + dt }; mark(); return; }
    zoomAt(e.clientX - b.left, Math.exp(e.deltaY * 0.0016));
  }, { passive: false });
  const ptrs = new Map();
  let drag = null, pinch = null;
  el.addEventListener('pointerdown', (e) => {
    el.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size === 1) drag = { x: e.clientX, v: { ...view }, moved: 0 };
    if (ptrs.size === 2) { const [a, b] = Array.from(ptrs.values()); pinch = { d: Math.abs(a.x - b.x) || 1, v: { ...view }, mid: (a.x + b.x) / 2 }; drag = null; }
  });
  el.addEventListener('pointermove', (e) => {
    const b = el.getBoundingClientRect();
    const x = e.clientX - b.left, y = e.clientY - b.top;
    if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && ptrs.size === 2) {
      const [p, q] = Array.from(ptrs.values());
      const d = Math.abs(p.x - q.x) || 1;
      view = { ...pinch.v };
      zoomAt(pinch.mid - b.left, pinch.d / d);
      return;
    }
    if (drag) {
      const dx = e.clientX - drag.x;
      drag.moved = Math.max(drag.moved, Math.abs(dx));
      if (drag.moved > 3) {
        const dt = (dx / (W - gutter - 8)) * (drag.v.v1 - drag.v.v0);
        view = { v0: drag.v.v0 - dt, v1: drag.v.v1 - dt }; mark();
        hideTip(); hoverLine.hidden = true;
        return;
      }
    }
    hover(x, y, e.clientX, e.clientY);
  });
  const end = (e) => {
    const wasClick = drag && drag.moved <= 3 && ptrs.size === 1;
    ptrs.delete(e.pointerId);
    if (ptrs.size < 2) pinch = null;
    if (wasClick) {
      const b = el.getBoundingClientRect();
      const x = e.clientX - b.left;
      if (x >= gutter) {
        const ts = T(x);
        const seq = log.seqAtTime(ts, (en) => en.k !== 'entity');
        if (seq != null) { replayTs = ts; placeReplay(); onJump && onJump(seq, ts); }
      }
    }
    if (!ptrs.size) drag = null;
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', (e) => { ptrs.delete(e.pointerId); drag = null; pinch = null; });
  el.addEventListener('dblclick', () => { view = full(); mark(); });
  el.addEventListener('mouseleave', () => { hoverLine.hidden = true; hideTip(); });

  function hover(x, y, cxp, cyp) {
    if (x < gutter || !view) { hoverLine.hidden = true; hideTip(); return; }
    hoverLine.hidden = false;
    hoverLine.style.transform = `translateX(${x}px)`;
    const ts = T(x);
    const { list, lh, y0 } = lanes();
    const lane = Math.floor((y - y0) / lh);
    const agent = list[lane];
    const tol = (4 / (W - gutter)) * (view.v1 - view.v0);
    let best = null;
    const idx = log.entries();
    for (let i = log.indexOfTime(ts - tol); i < idx.length && idx[i].ts <= ts + tol; i++) {
      const e = idx[i];
      if (e.k === 'entity' || (agent && e.a !== agent.id)) continue;
      if (!best || KINDS[e.k][0] > KINDS[best.k][0]) best = e;
    }
    const sinceStart = ts - win().start;
    showTip(cxp, cyp,
      h('div.tip-title', null, fmtClock(ts), h('span.faint', { style: { fontWeight: 400 } }, sinceStart > 0 ? ` · +${fmtDur(sinceStart, true)}` : '')),
      best ? h('div.tip-line', null, `${(agents().find((a) => a.id === best.a) || {}).name || ''} · ${truncate(best.s || best.type, 120)}`) : h('div.tip-line', null, agent ? agent.name : 'click to replay from here'));
  }

  const ro = new ResizeObserver(resize);
  ro.observe(el);
  subscribe('index', mark);
  subscribe('agents', mark);
  subscribe('missions', mark);
  subscribe('reset', () => { if (!view) mark(); });
  subscribe('telemetry', mark);
  setInterval(mark, 10000);

  return {
    el,
    setVisible(v) { visible = v; if (v) { dirty = true; requestAnimationFrame(render); } },
    clearReplay() { replayTs = null; placeReplay(); },
    setReplay(ts) { replayTs = ts; placeReplay(); },
  };
}
