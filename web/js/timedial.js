// The run-length dial: drag round a ring from 15 minutes to 12 hours (or type "1h 40m"), see when the run ends and
// how it will be cut into waves, down to when the finished report arrives. The ring is logarithmic, so the short
// runs, where five minutes matter, get most of the travel. Keyboard: arrows step, Page Up/Down move an hour.
// Value out: hours as a float (2.75 = 2 h 45 min); 0 = until the box session ends.
import { h, setText } from './dom.js';

const MIN_M = 15, MAX_M = 720;
const A0 = -135, A1 = 135;          // the ring's arc, degrees from 12 o'clock
const R = 92, C = 130;              // ring radius and centre in a 260 viewBox
const TICKS = [[15, '15m'], [30, '30m'], [60, '1h'], [120, '2h'], [240, '4h'], [480, '8h'], [720, '12h']];
const PRESETS = [[30, '30 min'], [60, '1 h'], [90, '1 h 30'], [120, '2 h'], [240, '4 h'], [480, '8 h'], [0, 'Until the session ends']];

const posOf = (min) => Math.log(min / MIN_M) / Math.log(MAX_M / MIN_M);
const minOf = (p) => MIN_M * Math.pow(MAX_M / MIN_M, Math.min(Math.max(p, 0), 1));
const step = (min) => (min < 180 ? 5 : min < 360 ? 15 : 30);
const snap = (min) => { const s = step(min); return Math.min(MAX_M, Math.max(MIN_M, Math.round(min / s) * s)); };
const rad = (deg) => (deg - 90) * Math.PI / 180;
const pt = (deg, r = R) => [C + r * Math.cos(rad(deg)), C + r * Math.sin(rad(deg))];
function arc(d0, d1, r = R) {
  const [x0, y0] = pt(d0, r), [x1, y1] = pt(d1, r);
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${d1 - d0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}
export function fmtLen(min) {
  if (!min) return 'until the session ends';
  const hh = Math.floor(min / 60), mm = Math.round(min % 60);
  return hh ? `${hh} h${mm ? ' ' + mm + ' min' : ''}` : `${mm} min`;
}
const big = (min) => { const hh = Math.floor(min / 60), mm = Math.round(min % 60); return hh ? [String(hh), 'h', mm ? String(mm).padStart(2, '0') : '', mm ? 'm' : ''] : [String(mm), 'min', '', '']; };
// "90" "90m" "1h30" "1h 30m" "1:30" "1.5h" "2.25" -> minutes (a bare number up to 12 is hours)
export function parseLen(s) {
  const t = String(s || '').trim().toLowerCase().replace(/\s+/g, '');
  if (!t) return null;
  let m;
  if ((m = t.match(/^(\d{1,2}):(\d{1,2})$/))) return +m[1] * 60 + +m[2];
  if ((m = t.match(/^(?:(\d+(?:\.\d+)?)h(?:ours?|rs?)?)?(?:(\d+)m(?:in(?:utes?)?)?)?$/)) && (m[1] || m[2])) return (m[1] ? +m[1] * 60 : 0) + (m[2] ? +m[2] : 0);
  if ((m = t.match(/^(\d+(?:\.\d+)?)$/))) return +m[1] <= 12 ? +m[1] * 60 : +m[1];
  return null;
}
// waves.auto_wave_minutes and research_run.run_shape, mirrored: minutes per wave for a run (0 = one pass)
export function waveMinutes(min, chosen) {
  const hrs = min / 60;
  let w = chosen || (hrs <= 0.75 ? 0 : hrs <= 1.5 ? 20 : hrs <= 3 ? 30 : hrs <= 6 ? 45 : 60);
  if (w && w * 60 > min * 60 * 0.6) w = 0;
  return w;
}
// run.margins()["reports"]: the final write-up starts this long before the end
const finalMin = (min) => Math.min(60, 0.30 * Math.max(min, 10));
const clock = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// opts: { hours, wave: () => chosen minutes per wave (0 = automatic), onChange(hours) }
export function createTimeDial(opts = {}) {
  let min = opts.hours > 0 ? snap(opts.hours * 60) : 0;
  let last = min || 120;          // the dial's position while "until the session ends" is on
  let editing = false;

  const ticks = TICKS.map(([m, label]) => {
    const deg = A0 + (A1 - A0) * posOf(m);
    const [x0, y0] = pt(deg, R + 9), [x1, y1] = pt(deg, R + 14), [lx, ly] = pt(deg, R + 27);
    return h('g', null, h('line', { x1: x0, y1: y0, x2: x1, y2: y1, class: 'td-tick' }),
      h('text', { x: lx, y: ly + 3.5, class: 'td-tlabel', 'text-anchor': 'middle' }, label));
  });
  const track = h('path', { class: 'td-track', d: arc(A0, A1) });
  const fill = h('path', { class: 'td-fill' });
  const glow = h('path', { class: 'td-glow' });
  const knob = h('circle', { class: 'td-knob', r: 11 });
  const knobDot = h('circle', { class: 'td-knob-dot', r: 3.5 });
  const notches = h('g', { class: 'td-notches' });
  const gid = 'tdg' + Math.random().toString(36).slice(2, 8);
  const svg = h('svg', { viewBox: '0 0 260 260', class: 'td-svg', 'aria-hidden': 'true' },
    h('defs', null, h('linearGradient', { id: gid, x1: '0', y1: '1', x2: '1', y2: '0' },
      h('stop', { offset: '0', 'stop-color': 'var(--sky)' }), h('stop', { offset: '.55', 'stop-color': 'var(--lilac)' }), h('stop', { offset: '1', 'stop-color': 'var(--rose)' }))),
    track, glow, fill, notches, ticks, knob, knobDot);
  fill.setAttribute('stroke', `url(#${gid})`);
  glow.setAttribute('stroke', `url(#${gid})`);

  const vNum = h('span.td-n'), vUnit = h('span.td-u'), vNum2 = h('span.td-n'), vUnit2 = h('span.td-u');
  const valueBtn = h('button.td-value', { type: 'button', title: 'Type a length, for example 1h 40m, 95m or 2:15', onclick: () => edit() }, vNum, vUnit, vNum2, vUnit2);
  const input = h('input.td-input', { type: 'text', inputmode: 'text', 'aria-label': 'Run length, for example 1h 40m', hidden: true, autocomplete: 'off' });
  const ends = h('div.td-ends');
  const hint = h('div.td-hint', null, 'drag the ring · tap the time to type');
  const face = h('div.td-face', null, h('div.micro.td-cap', null, 'run for'), valueBtn, input, ends, hint);
  const dial = h('div.td-dial', { role: 'slider', tabindex: '0', 'aria-label': 'How long the agents work', 'aria-valuemin': String(MIN_M), 'aria-valuemax': String(MAX_M) }, svg, face);

  const chips = PRESETS.map(([m, label]) => h('button.td-chip', { type: 'button', 'data-m': String(m), onclick: () => set(m, true) }, label));
  const strip = h('div.td-strip');
  const stripNote = h('div.td-strip-note');
  const el = h('div.td', null, dial, h('div.td-side', null, h('div.td-chips', null, chips), h('div.td-plan', null, h('div.micro', null, 'how the time is spent'), strip, stripNote)));

  function set(m, commit, exact) {
    if (m) { min = exact ? Math.min(MAX_M, Math.max(MIN_M, Math.round(m))) : snap(m); last = min; } else min = 0;
    paint();
    if (commit && opts.onChange) opts.onChange(min / 60);
  }
  function paint() {
    const on = min > 0;
    const v = on ? min : last;
    const deg = A0 + (A1 - A0) * posOf(v);
    el.classList.toggle('session', !on);
    fill.setAttribute('d', on ? arc(A0, Math.max(deg, A0 + 0.5)) : arc(A0, A1));
    glow.setAttribute('d', on ? arc(A0, Math.max(deg, A0 + 0.5)) : '');
    const [kx, ky] = pt(on ? deg : A1);
    knob.setAttribute('cx', kx); knob.setAttribute('cy', ky); knobDot.setAttribute('cx', kx); knobDot.setAttribute('cy', ky);
    knob.style.display = knobDot.style.display = on ? '' : 'none';
    const [a, b, c, d] = on ? big(min) : ['∞', '', '', ''];
    setText(vNum, a); setText(vUnit, b); setText(vNum2, c); setText(vUnit2, d);
    setText(ends, on ? `ends about ${clock(Date.now() + min * 60000)}` : 'until the box session ends');
    dial.setAttribute('aria-valuenow', String(on ? min : MAX_M));
    dial.setAttribute('aria-valuetext', fmtLen(min));
    chips.forEach((ch) => ch.setAttribute('aria-pressed', String(+ch.dataset.m === min)));
    paintPlan();
  }
  function paintPlan() {
    const total = min || 0;
    const wv = total ? waveMinutes(total, opts.wave ? Number(opts.wave()) || 0 : 0) : 0;
    clear(notches);
    stripNote.textContent = '';
    while (strip.firstChild) strip.removeChild(strip.firstChild);
    if (!total) {
      strip.append(h('span.td-seg.work', { style: { flex: '1' } }, h('b', null, 'waves until the session ends')), h('span.td-seg.fin', { style: { flex: '.25' } }, h('b', null, 'report')));
      stripNote.textContent = 'The agents work until the box session is nearly over, then write the finished report.';
      return;
    }
    const fin = finalMin(total);
    const work = Math.max(total - fin, 1);
    const n = wv ? Math.max(1, Math.round(work / wv)) : 1;
    const segs = [];
    for (let i = 0; i < n; i++) segs.push(h('span.td-seg.work', { style: { flex: String(work / n) }, title: wv ? `wave ${i + 1}: read, then write a chapter` : 'read and take notes' },
      h('b', null, wv ? (n <= 8 ? `wave ${i + 1}` : i === 0 ? `${n} waves` : '') : 'read · notebook')));
    segs.push(h('span.td-seg.fin', { style: { flex: String(fin) }, title: 'final reports, synthesis and the refined report' }, h('b', null, 'report')));
    strip.append(...segs);
    const start = Date.now();
    stripNote.append(
      h('span', null, wv ? `${n} ${n === 1 ? 'wave' : 'waves'} of about ${Math.round(work / n)} min: each closes with a chapter, then the agents forget and re-plan.` : 'One pass: read and keep the notebook, no waves.'),
      h('span', null, ` The finished report starts about ${clock(start + work * 60000)}, ready about ${clock(start + total * 60000)}.`));
    if (wv) {
      for (let i = 1; i < n; i++) {
        const deg = A0 + (A1 - A0) * posOf(Math.max(MIN_M, (work / n) * i));
        const [x0, y0] = pt(deg, R - 6), [x1, y1] = pt(deg, R + 6);
        notches.appendChild(h('line', { x1: x0, y1: y0, x2: x1, y2: y1 }));
      }
    }
  }
  const clear = (g) => { while (g.firstChild) g.removeChild(g.firstChild); };

  // ---------- drag ----------
  function fromPointer(e) {
    const r = svg.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width * 260 - C, y = (e.clientY - r.top) / r.height * 260 - C;
    let deg = Math.atan2(y, x) * 180 / Math.PI + 90;
    if (deg > 180) deg -= 360;
    if (deg < A0 || deg > A1) deg = deg < 0 ? (deg < -157.5 && min > 300 ? A1 : A0) : (deg > 157.5 && min < 60 ? A0 : A1);
    return minOf((deg - A0) / (A1 - A0));
  }
  let dragging = false;
  svg.addEventListener('pointerdown', (e) => {
    if (editing) return;
    dragging = true; svg.setPointerCapture(e.pointerId); el.classList.add('drag');
    set(fromPointer(e), false);
    e.preventDefault();
  });
  svg.addEventListener('pointermove', (e) => { if (dragging) set(fromPointer(e), false); });
  const up = () => { if (!dragging) return; dragging = false; el.classList.remove('drag'); if (opts.onChange) opts.onChange(min / 60); };
  svg.addEventListener('pointerup', up); svg.addEventListener('pointercancel', up);
  dial.addEventListener('keydown', (e) => {
    if (editing || e.target !== dial) return;
    const v = min || last;
    const k = e.key;
    let nv = null;
    if (k === 'ArrowUp' || k === 'ArrowRight') nv = v + step(v);
    else if (k === 'ArrowDown' || k === 'ArrowLeft') nv = v - step(v - 1);
    else if (k === 'PageUp') nv = v + 60;
    else if (k === 'PageDown') nv = v - 60;
    else if (k === 'Home') nv = MIN_M;
    else if (k === 'End') nv = MAX_M;
    else if (k === 'Enter') { edit(); e.preventDefault(); return; }
    if (nv == null) return;
    e.preventDefault();
    set(Math.min(MAX_M, Math.max(MIN_M, nv)), true);
  });

  // ---------- typing ----------
  function edit() {
    editing = true;
    input.value = min ? fmtLen(min).replace(' min', 'm').replace(' h', 'h') : '';
    input.placeholder = '1h 40m';
    valueBtn.hidden = true; input.hidden = false;
    input.focus(); input.select();
  }
  function done(commit) {
    if (!editing) return;
    editing = false;
    const v = parseLen(input.value);
    input.hidden = true; valueBtn.hidden = false;
    input.classList.remove('bad');
    if (commit && v != null) set(v, true, true);
    dial.focus();
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { if (parseLen(input.value) == null) { input.classList.add('bad'); return; } done(true); e.preventDefault(); }
    else if (e.key === 'Escape') { done(false); e.preventDefault(); e.stopPropagation(); }
  });
  input.addEventListener('input', () => input.classList.toggle('bad', !!input.value.trim() && parseLen(input.value) == null));
  input.addEventListener('blur', () => done(true));

  paint();
  return {
    el,
    get hours() { return min / 60; },
    set hours(v) { if (!dragging && !editing) { min = v > 0 ? Math.min(MAX_M, Math.max(MIN_M, Math.round(v * 60))) : 0; if (min) last = min; paint(); } },
    refresh: paintPlan,
  };
}
