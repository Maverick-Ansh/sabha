// The orbit: a lotus seen from above. Each outer petal ends at a tool station, agents circle the
// core as glossy orbs, and the mark sits on a glass disc in the middle. Drawn on <canvas>.
// Light theme: glows use multiply / soft alpha so nothing washes out on paper.
import { subscribe, getState, agents, agentById } from './store.js';
import { rgbOf, rgba, hueOf, stationFor, STATIONS, truncate, statusLabel } from './format.js';
import { h, showTip, hideTip } from './dom.js';
import { lotusPaths } from './lotus.js';

const TAU = Math.PI * 2;
const MONO = '"Geist Mono", "Cascadia Mono", Consolas, monospace';
const SANS = '-apple-system, "SF Pro Text", Inter, "Segoe UI", system-ui, sans-serif';

export function createOrbit(canvas, { onFocus, onResearch } = {}) {
  const ctx = canvas.getContext('2d');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  let INK = rgbOf('ink'), PAPER = rgbOf('paper'), DISC = rgbOf('disc');
  const SUN = rgbOf('core-a'), CORAL = rgbOf('core-b');
  // glows multiply on the light page (nothing washes out) and add light on the dark one (multiply would hide them)
  const blendNow = () => (document.documentElement.dataset.theme === 'dark' ? 'screen' : 'multiply');
  let BLEND = blendNow();
  addEventListener('sabha-theme', () => { INK = rgbOf('ink'); PAPER = rgbOf('paper'); DISC = rgbOf('disc'); BLEND = blendNow(); });
  let W = 0, H = 0, dpr = 1, cx = 0, cy = 0, R = 0, sunR = 30;
  let active = false, raf = 0, last = 0, clock = 0, sunFlare = -9, bloomAt = -1;
  const orbs = new Map();         // agent_id -> orb state ('r:<track>' for a research agent)
  const research = new Map();     // 'r:<track>' -> { name, working, now, station } from GET /api/research (watch.js)
  const isR = (id) => typeof id === 'string' && id.startsWith('r:');
  const particles = [], ripples = [];
  const pulses = {};              // station -> { t, rgb }
  const callStation = new Map();  // call_id -> station
  let hoverId = null, mouse = null;

  // ---------- geometry ----------
  function stationPos(name) {
    const i = Math.max(0, STATIONS.indexOf(name));
    const a = -Math.PI / 2 + (i / STATIONS.length) * TAU;
    return { x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R, a };
  }
  function layoutOrbs() {
    const list = agents();
    const seen = new Set();
    const r0 = sunR * 2.1, r1 = R * 0.8;
    list.forEach((a, i) => {
      seen.add(a.id);
      let o = orbs.get(a.id);
      if (!o) { o = { id: a.id, angle: (i * 2.399) % TAU, energy: 0, kick: 0, lastThink: 0, lastTalk: 0, x: cx, y: cy, r: 6 }; orbs.set(a.id, o); }
      o.radius = list.length === 1 ? (r0 + r1) / 2 : r0 + (r1 - r0) * (i / (list.length - 1));
      o.speed = (0.06 + 0.018 * (i % 3)) * (i % 2 ? 0.85 : 1);
      o.rgb = rgbOf(hueOf(a));
    });
    for (const id of Array.from(orbs.keys())) if (!seen.has(id) && !isR(id)) orbs.delete(id);
  }
  // Research agents ride an outer band of the orbit, so they read apart from the crew.
  function setResearch(list) {
    const seen = new Set();
    list.forEach((x, i) => {
      seen.add(x.id);
      research.set(x.id, x);
      let o = orbs.get(x.id);
      if (!o) { o = { id: x.id, angle: (i * 2.399 + 1.1) % TAU, energy: 0, kick: 0, lastThink: 0, lastTalk: 0, lastSpark: -9, x: cx, y: cy, r: 6 }; orbs.set(x.id, o); }
      o.band = 0.9 - 0.05 * (i % 3);  // a share of R, applied each frame (R changes on resize)
      o.speed = 0.045 + 0.012 * (i % 3);
      o.rgb = rgbOf(x.hue || 'sky');
    });
    for (const id of Array.from(orbs.keys())) if (isR(id) && !seen.has(id)) { orbs.delete(id); research.delete(id); }
    if (!active) draw();
  }

  function resize() {
    const box = canvas.parentElement.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(10, box.width); H = Math.max(10, box.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    const foot = H > 420 ? 98 : 72; // room for the stats cards under the orbit
    cx = W / 2; cy = (H - foot) / 2 + 4;
    R = Math.max(60, Math.min((W - 112) / 2, (H - foot - 64) / 2)); // side room for station labels
    sunR = Math.max(22, Math.min(40, R * 0.15));
    layoutOrbs();
    if (!active) draw();
  }

  // ---------- events -> motion ----------
  const orbPos = (id) => { const o = orbs.get(id); return o ? { x: o.x, y: o.y } : { x: cx, y: cy }; };
  function spark(from, to, rgb, opts = {}) {
    if (reduce.matches) { if (opts.arrive) opts.arrive(); return; }
    if (particles.length > 80) particles.shift();
    particles.push({ from, to, rgb, t: 0, dur: opts.dur || 0.95, size: opts.size || 1, arrive: opts.arrive, bend: (Math.random() < 0.5 ? -1 : 1) * (0.18 + Math.random() * 0.12) });
  }
  const pulse = (station, rgb) => { pulses[station] = { t: clock, rgb }; };

  function onEvent(ev) {
    const o = orbs.get(ev.agent_id);
    if (!o) return;
    const d = ev.data || {};
    o.kick = 1; o.lastEv = clock;
    const rgb = o.rgb;
    switch (ev.type) {
      case 'run.tool_call': {
        const st = stationFor(d.tool);
        if (d.call_id) callStation.set(d.call_id, st);
        if (callStation.size > 400) callStation.delete(callStation.keys().next().value);
        spark(() => orbPos(o.id), () => stationPos(st), rgb, { arrive: () => pulse(st, rgb) });
        break;
      }
      case 'run.tool_result': {
        const st = callStation.get(d.call_id) || 'tool';
        const bad = d.status === 'error' || d.error;
        spark(() => stationPos(st), () => orbPos(o.id), bad ? CORAL : rgb, { arrive: () => { o.kick = 1; if (bad) ripples.push({ id: o.id, t: clock, rgb: CORAL }); } });
        break;
      }
      case 'run.thinking':
        if (clock - o.lastThink > 1.2) { o.lastThink = clock; ripples.push({ id: o.id, t: clock, rgb }); }
        if (clock - (o.lastThinkSpark || -9) > 3) { o.lastThinkSpark = clock; spark(() => orbPos(o.id), () => stationPos('think'), rgb, { size: 0.7, arrive: () => pulse('think', rgb) }); }
        break;
      case 'run.text':
        if (clock - o.lastTalk > 2) { o.lastTalk = clock; spark(() => orbPos(o.id), () => stationPos('talk'), rgb, { size: 0.8, arrive: () => pulse('talk', rgb) }); }
        break;
      case 'run.final':
        spark(() => orbPos(o.id), () => ({ x: cx, y: cy }), SUN, { size: 1.8, dur: 1.2, arrive: () => { sunFlare = clock; } });
        break;
      case 'run.error':
        ripples.push({ id: o.id, t: clock, rgb: CORAL });
        break;
      case 'plan.proposed': case 'human.note': case 'human.approved':
        pulse('plan', SUN);
        break;
      default: break;
    }
    if (ripples.length > 40) ripples.shift();
    if (!active) draw();
  }

  // ---------- drawing ----------
  function working(agentId) {
    if (isR(agentId)) return !!(research.get(agentId) || {}).working;
    const ms = getState().missions;
    for (const id in ms) { const m = ms[id]; if (m.agent_id === agentId && (m.status === 'running' || m.status === 'planning')) return true; }
    return false;
  }
  function needs(agentId) {
    if (isR(agentId)) return null;
    const ms = getState().missions;
    for (const id in ms) { const m = ms[id]; if (m.agent_id === agentId && m.status === 'awaiting_approval') return 'needs'; }
    return null;
  }
  const bez = (p0, p1, bend, t) => {
    const mx = (p0.x + p1.x) / 2, my = (p0.y + p1.y) / 2;
    const nx = -(p1.y - p0.y) * bend, ny = (p1.x - p0.x) * bend;
    const qx = mx + nx + (cx - mx) * 0.25, qy = my + ny + (cy - my) * 0.25;
    const u = 1 - t;
    return { x: u * u * p0.x + 2 * u * t * qx + t * t * p1.x, y: u * u * p0.y + 2 * u * t * qy + t * t * p1.y };
  };
  function glow(x, y, r, rgb, a) {
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, rgba(rgb, a)); g.addColorStop(1, rgba(rgb, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill();
  }

  function draw() {
    const t = clock;
    const focus = getState().ui.focusAgent;
    const running = Object.values(getState().missions).filter((m) => m.status === 'running' || m.status === 'planning').length;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // soft light, then the lotus itself
    ctx.globalCompositeOperation = 'source-over';
    glow(cx, cy, R * 1.12, SUN, 0.09 + Math.min(0.08, running * 0.02));
    glow(cx, cy - R * 0.2, R * 0.8, CORAL, 0.05);
    const open = reduce.matches ? 1 : bloomAt < 0 ? 0 : Math.min(1, (t - bloomAt) / 1.9);
    const N = STATIONS.length, step = TAU / N;
    const drift = reduce.matches ? 0 : Math.sin(t * 0.11) * 0.05;
    for (let i = 0; i < N; i++) {
      const pl = pulses[STATIONS[i]], age = pl ? t - pl.t : 9;
      petal(-Math.PI / 2 + i * step, R * 0.99, R * 0.47, 0.115, 0.045, open, i, t, age < 1.6 ? { rgb: pl.rgb, a: 0.26 * (1 - age / 1.6) } : null);
    }
    for (let i = 0; i < N; i++) petal(-Math.PI / 2 + (i + 0.5) * step + drift, R * 0.6, R * 0.3, 0.12, 0.06, open, i + 3, t);

    // the ring, with one slow glint running round it
    ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(SUN, 0.2 * open); ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.stroke();
    if (ctx.createConicGradient && open === 1) {
      const sweep = reduce.matches ? -0.6 : t * 0.22;
      const g = ctx.createConicGradient(sweep, cx, cy);
      g.addColorStop(0, rgba(SUN, 0)); g.addColorStop(0.09, rgba(SUN, 0.55)); g.addColorStop(0.1, rgba(SUN, 0)); g.addColorStop(1, rgba(SUN, 0));
      ctx.strokeStyle = g; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.stroke(); ctx.lineWidth = 1;
    }

    // agent orbit guides + trails
    for (const o of orbs.values()) {
      const dim = focus && focus !== o.id;
      ctx.strokeStyle = rgba(o.rgb, dim ? 0.04 : focus === o.id ? 0.42 : 0.07 + o.energy * 0.14);
      ctx.lineWidth = focus === o.id ? 1.3 : 1;
      ctx.beginPath(); ctx.arc(cx, cy, o.radius, 0, TAU); ctx.stroke();
      if (o.energy > 0.05 && !dim) {
        ctx.lineCap = 'round';
        for (let i = 0; i < 10; i++) {
          const a0 = o.angle - (i + 1) * 0.07 * o.dir, a1 = o.angle - i * 0.07 * o.dir;
          ctx.strokeStyle = rgba(o.rgb, (1 - i / 10) * 0.5 * o.energy); ctx.lineWidth = 2.6 * (1 - i / 12);
          ctx.beginPath(); ctx.arc(cx, cy, o.radius, Math.min(a0, a1), Math.max(a0, a1)); ctx.stroke();
        }
        ctx.lineCap = 'butt';
      }
    }

    // stations
    ctx.textBaseline = 'middle';
    for (const name of STATIONS) {
      const p = stationPos(name), pl = pulses[name];
      const age = pl ? t - pl.t : 9;
      if (age < 1.4) {
        const k = age / 1.4;
        ctx.strokeStyle = rgba(pl.rgb, 0.55 * (1 - k)); ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(p.x, p.y, 6 + k * 18, 0, TAU); ctx.stroke();
        glow(p.x, p.y, 20, pl.rgb, 0.35 * (1 - k));
      }
      ctx.globalAlpha = open;
      ctx.fillStyle = age < 1.4 ? rgba(pl.rgb, 1) : rgba(DISC, 1);
      ctx.strokeStyle = age < 1.4 ? rgba(pl.rgb, 1) : rgba(SUN, 0.55); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, TAU); ctx.fill(); ctx.stroke();
      const lx = cx + Math.cos(p.a) * (R + 18), ly = cy + Math.sin(p.a) * (R + 18);
      ctx.font = `600 10px ${SANS}`;
      ctx.textAlign = Math.abs(Math.cos(p.a)) < 0.3 ? 'center' : Math.cos(p.a) > 0 ? 'left' : 'right';
      ctx.fillStyle = rgba(INK, age < 1.4 ? 0.85 : 0.55);
      spaced(name.toUpperCase(), lx, ly);
      ctx.globalAlpha = 1;
    }

    // the core: the mark on a glass disc
    const flare = Math.max(0, 1 - (t - sunFlare) / 1.6);
    const breath = reduce.matches ? 0 : Math.sin(t * 1.3) * 0.5 + 0.5;
    const dR = sunR * (1.12 + breath * 0.02);
    ctx.globalCompositeOperation = BLEND;
    glow(cx, cy, sunR * (2.7 + breath * 0.35 + flare * 1.6), SUN, 0.22 + flare * 0.3);
    glow(cx, cy, sunR * 1.8, CORAL, 0.12 + flare * 0.15);
    ctx.globalCompositeOperation = 'source-over';
    for (let i = 0; i < 2; i++) { // two halos breathing out
      const k = reduce.matches ? 0.5 : ((t * 0.22 + i * 0.5) % 1);
      ctx.strokeStyle = rgba(SUN, 0.22 * (1 - k)); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(cx, cy, dR + 4 + k * sunR * 1.1, 0, TAU); ctx.stroke();
    }
    ctx.save();
    ctx.shadowColor = rgba(SUN, 0.45); ctx.shadowBlur = 22; ctx.shadowOffsetY = 8;
    const disc = ctx.createRadialGradient(cx, cy - dR * 0.4, dR * 0.1, cx, cy, dR);
    disc.addColorStop(0, rgba(DISC, 1)); disc.addColorStop(1, rgba(DISC, 0.96));
    ctx.fillStyle = disc; ctx.beginPath(); ctx.arc(cx, cy, dR, 0, TAU); ctx.fill();
    ctx.restore();
    ctx.strokeStyle = rgba(DISC, 0.95); ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(cx, cy, dR, 0, TAU); ctx.stroke();
    const gs = dR * 1.34 * (1 + flare * 0.12), k32 = gs / 32;
    ctx.save();
    ctx.translate(cx - gs / 2, cy - gs / 2 - dR * 0.03); ctx.scale(k32, k32);
    const lg = ctx.createLinearGradient(6, 4, 26, 26);
    lg.addColorStop(0, rgba(CORAL, 1)); lg.addColorStop(1, rgba(SUN, 1));
    ctx.fillStyle = lg;
    for (const p of lotusPaths()) { ctx.globalAlpha = p.o; ctx.fill(p.path); }
    ctx.restore(); ctx.globalAlpha = 1;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    // darkened lilac on the light page, lightened lilac on the dark one (the dark version vanished: seen on a phone)
    ctx.font = `600 9.5px ${SANS}`;
    ctx.fillStyle = BLEND === 'screen' ? rgba(SUN.map((c) => c + (255 - c) * 0.55), 0.95) : rgba(SUN.map((c) => c * 0.72), 0.95);
    spaced(running ? `${running} IN SESSION` : 'IN SESSION', cx, cy + dR + 17);

    // particles
    for (const p of particles) {
      const p0 = p.from(), p1 = p.to();
      for (let i = 6; i >= 0; i--) {
        const tt = Math.max(0, p.t - i * 0.035);
        const q = bez(p0, p1, p.bend, ease(tt));
        ctx.fillStyle = rgba(p.rgb, i ? 0.32 * (1 - i / 7) : 1);
        ctx.beginPath(); ctx.arc(q.x, q.y, (i ? 2.1 - i * 0.2 : 2.8) * p.size, 0, TAU); ctx.fill();
      }
      const head = bez(p0, p1, p.bend, ease(p.t));
      ctx.globalCompositeOperation = BLEND; glow(head.x, head.y, 12 * p.size, p.rgb, 0.4); ctx.globalCompositeOperation = 'source-over';
    }
    // ripples
    for (const r of ripples) {
      const o = orbs.get(r.id); if (!o) continue;
      const k = (t - r.t) / 1.8;
      if (k > 1) continue;
      ctx.strokeStyle = rgba(r.rgb, 0.5 * (1 - k)); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(o.x, o.y, o.r + 4 + k * 26, 0, TAU); ctx.stroke();
    }

    // orbs
    const acts = getState().act.agents;
    for (const o of orbs.values()) {
      const dim = focus && focus !== o.id;
      const a = dim ? 0.38 : 1;
      const r = o.r;
      ctx.globalAlpha = a;
      ctx.globalCompositeOperation = BLEND;
      glow(o.x, o.y, r * (2.4 + o.energy * 2.6 + o.kick), o.rgb, 0.16 + o.energy * 0.32);
      ctx.globalCompositeOperation = 'source-over';
      const g = ctx.createRadialGradient(o.x - r * 0.36, o.y - r * 0.42, r * 0.06, o.x, o.y, r);
      g.addColorStop(0, 'rgba(255,255,255,.97)');
      g.addColorStop(0.42, rgba(o.rgb, 0.86 + o.energy * 0.14));
      g.addColorStop(1, rgba(o.rgb.map((c) => c * 0.8), 0.96));
      ctx.save();
      ctx.shadowColor = rgba(o.rgb, 0.5); ctx.shadowBlur = 10; ctx.shadowOffsetY = 4;
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(o.x, o.y, r, 0, TAU); ctx.fill();
      ctx.restore();
      const flag = needs(o.id);
      if (flag) {
        ctx.setLineDash([3, 4]); ctx.lineDashOffset = -t * 8; ctx.strokeStyle = rgba(SUN, 0.95); ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(o.x, o.y, r + 5, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
      }
      if (hoverId === o.id || focus === o.id) {
        ctx.strokeStyle = rgba(o.rgb, 0.8); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(o.x, o.y, r + 8, 0, TAU); ctx.stroke();
      }
      // action label
      const act = isR(o.id) ? research.get(o.id) : acts[o.id];
      if ((o.energy > 0.5 || focus === o.id) && act && act.now && !dim) label(o, truncate(act.now, 38));
      ctx.globalAlpha = 1;
    }
  }
  // One petal from the core out to `tip`, `half` wide at its belly. `open` (0..1) unfolds it; `lit` tints it.
  function petal(angle, tip, half, a0, a1, open, i, t, lit) {
    const k = Math.max(0, Math.min(1, open * 1.7 - i * 0.07));
    if (k <= 0) return;
    const e = 1 - Math.pow(1 - k, 3);
    const sway = reduce.matches ? 1 : 1 + Math.sin(t * 0.8 + i * 1.7) * 0.014;
    const r0 = sunR * 0.9, r1 = r0 + (tip - r0) * e * sway, w = half * (0.35 + 0.65 * e), L = r1 - r0;
    ctx.save();
    ctx.translate(cx, cy); ctx.rotate(angle);
    ctx.beginPath();
    ctx.moveTo(r0, 0);
    ctx.bezierCurveTo(r0 + L * 0.3, -w, r0 + L * 0.74, -w * 0.82, r1, 0);
    ctx.bezierCurveTo(r0 + L * 0.74, w * 0.82, r0 + L * 0.3, w, r0, 0);
    const g = ctx.createLinearGradient(r0, 0, r1, 0);
    g.addColorStop(0, rgba(SUN, a0 * e)); g.addColorStop(1, rgba(CORAL, a1 * e));
    ctx.fillStyle = g; ctx.fill();
    if (lit) { ctx.fillStyle = rgba(lit.rgb, lit.a); ctx.fill(); }
    ctx.strokeStyle = `rgba(255,255,255,${0.7 * e})`; ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
  }
  function spaced(text, x, y) {
    if ('letterSpacing' in ctx) { ctx.letterSpacing = '1.4px'; ctx.fillText(text, x, y); ctx.letterSpacing = '0px'; }
    else ctx.fillText(text, x, y);
  }
  function label(o, text) {
    const dx = o.x - cx, dy = o.y - cy, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    ctx.font = `500 10.5px ${MONO}`;
    const hh = 20, ax = o.x + ux * (o.r + 14);
    // prefer the outward side; flip if there is no room; then trim the text to what fits
    let right = ux >= 0;
    if ((right ? W - ax : ax) < 110) right = !right;
    const room = Math.max(60, (right ? W - ax : ax) - 8);
    while (text.length > 6 && ctx.measureText(text).width + 16 > room) text = text.slice(0, -2).trimEnd() + '…';
    const w = ctx.measureText(text).width + 16;
    let x = right ? ax : ax - w, y = o.y + uy * (o.r + 14) - hh / 2;
    x = Math.max(4, Math.min(W - w - 4, x)); y = Math.max(4, Math.min(H - hh - 4, y));
    ctx.fillStyle = rgba(PAPER, 0.88); ctx.strokeStyle = rgba(o.rgb, 0.35); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, hh, 10) : ctx.rect(x, y, w, hh); ctx.fill(); ctx.stroke();
    ctx.fillStyle = rgba(o.rgb, 1); ctx.beginPath(); ctx.arc(x + 8, y + hh / 2, 2.5, 0, TAU); ctx.fill();
    ctx.fillStyle = rgba(INK, 0.72); ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(text, x + 14, y + hh / 2 + 0.5);
  }
  const ease = (x) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);

  // ---------- update loop ----------
  function update(dt) {
    clock += dt;
    for (const o of orbs.values()) {
      const work = working(o.id) || clock - (o.lastEv || -99) < 6;
      if (isR(o.id)) o.radius = R * (o.band || 0.88);
      // (no decorative sparks: every moving dot is a real event from onEvent; a working research orb just glows)
      o.energy += ((work ? 1 : 0) - o.energy) * Math.min(1, dt * 1.6);
      o.kick = Math.max(0, o.kick - dt * 1.8);
      o.dir = 1;
      if (!reduce.matches) o.angle = (o.angle + o.speed * (1 + o.energy * 0.8) * dt) % TAU;
      const breath = reduce.matches ? 0 : (Math.sin(clock * 2.2 + o.radius) * 0.5 + 0.5) * o.energy;
      o.r = 6.5 + o.energy * 3 + breath * 1.4 + o.kick * 1.6;
      o.x = cx + Math.cos(o.angle) * o.radius; o.y = cy + Math.sin(o.angle) * o.radius;
    }
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.t += dt / p.dur;
      if (p.t >= 1) { particles.splice(i, 1); if (p.arrive) p.arrive(); }
    }
    while (ripples.length && clock - ripples[0].t > 2) ripples.shift();
  }
  function busy() { return (bloomAt >= 0 && clock - bloomAt < 2.2) || particles.length || ripples.length || Array.from(orbs.values()).some((o) => o.energy > 0.05); }
  function frame(ts) {
    raf = requestAnimationFrame(frame);
    const minGap = reduce.matches ? 160 : busy() ? 24 : 48; // ~40 fps when busy, ~20 idle
    if (ts - last < minGap) return;
    const dt = last ? Math.min(0.1, (ts - last) / 1000) : 0.016;
    last = ts;
    update(dt); draw();
  }
  function setActive(on) {
    on = on && !document.hidden;
    if (on === active) return;
    active = on;
    cancelAnimationFrame(raf); last = 0;
    if (on) { if (bloomAt < 0) bloomAt = clock; raf = requestAnimationFrame(frame); }
  }

  // ---------- pointer ----------
  function hit(x, y) {
    let best = null, bd = 18;
    for (const o of orbs.values()) { const d = Math.hypot(o.x - x, o.y - y); if (d < bd) { bd = d; best = o; } }
    return best;
  }
  canvas.addEventListener('mousemove', (e) => {
    const b = canvas.getBoundingClientRect();
    mouse = { x: e.clientX - b.left, y: e.clientY - b.top };
    const o = hit(mouse.x, mouse.y);
    hoverId = o ? o.id : null;
    canvas.style.cursor = o || Math.hypot(mouse.x - cx, mouse.y - cy) < sunR ? 'pointer' : 'default';
    if (!o) { hideTip(); return; }
    if (isR(o.id)) {
      const x = research.get(o.id) || {};
      showTip(e.clientX, e.clientY,
        h('div', { class: 'tip-title', 'data-hue': x.hue || 'sky' }, h('span.dot'), x.name || 'research agent', h('span.faint', { style: { fontWeight: 400 } }, ' · research')),
        h('div.tip-line', null, x.now || 'idle'), h('div.tip-line', null, 'click to open Research'));
      return;
    }
    const a = agentById(o.id) || {};
    const act = getState().act.agents[o.id];
    const m = Object.values(getState().missions).find((x) => x.agent_id === o.id && x.status !== 'done' && x.status !== 'stopped' && x.status !== 'failed');
    const model = getState().models[a.model_id];
    showTip(e.clientX, e.clientY,
      h('div', { class: 'tip-title', 'data-hue': hueOf(a) }, h('span.dot'), a.name || 'agent', h('span.faint', { style: { fontWeight: 400 } }, a.role ? ' · ' + a.role : '')),
      h('div.tip-line', null, m ? `${statusLabel(m.status)} · ${truncate(m.title, 60)}` : 'idle'),
      act ? h('div.tip-line', null, truncate(act.now, 140)) : null,
      h('div.tip-line', null, `${model ? model.name : 'no model'} · ${a.harness || ''}`));
  });
  canvas.addEventListener('mouseleave', () => { hoverId = null; hideTip(); });
  canvas.addEventListener('click', (e) => {
    const b = canvas.getBoundingClientRect();
    const x = e.clientX - b.left, y = e.clientY - b.top;
    const o = hit(x, y);
    if (o && isR(o.id)) { if (onResearch) onResearch(o.id.slice(2)); return; }
    if (o) onFocus && onFocus(getState().ui.focusAgent === o.id ? null : o.id);
    else if (Math.hypot(x - cx, y - cy) < sunR * 1.3) onFocus && onFocus(null);
  });

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas.parentElement);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (!active) draw(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) setActive(false); else if (wantActive) setActive(true); });
  let wantActive = false;

  subscribe('reset', () => { layoutOrbs(); if (!active) draw(); });
  subscribe('agents', () => { layoutOrbs(); if (!active) draw(); });
  subscribe('run', ({ ev, live }) => { if (live && active) onEvent(ev); });
  subscribe('ui', () => { if (!active) draw(); });
  resize();

  return {
    setActive(on) { wantActive = on; setActive(on); },
    setResearch,
  };
}
