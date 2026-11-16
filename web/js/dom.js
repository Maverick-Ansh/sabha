// Tiny DOM helpers. Untrusted text only ever goes in via text nodes / textContent.

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set(['svg', 'path', 'circle', 'line', 'g', 'rect', 'polyline', 'polygon',
  'defs', 'linearGradient', 'radialGradient', 'stop', 'text', 'ellipse']);
const PROPS = new Set(['value', 'checked', 'disabled', 'selected', 'hidden', 'readOnly', 'multiple', 'indeterminate']);

// h('div', { class: 'x', onclick: fn, style: { '--hue': 'red' } }, 'text', child, [more])
// A tag like 'div.a.b' adds classes.
export function h(tag, props, ...children) {
  let cls = '';
  const dot = tag.indexOf('.');
  if (dot > 0) { cls = tag.slice(dot + 1).replace(/\./g, ' '); tag = tag.slice(0, dot); }
  const svg = SVG_TAGS.has(tag);
  const el = svg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  if (cls) el.setAttribute('class', cls);
  if (props) {
    for (const k in props) {
      const v = props[k];
      if (v == null || v === false) continue;
      if (k === 'class') {
        el.setAttribute('class', cls ? cls + ' ' + v : v);
      } else if (k === 'style') {
        if (typeof v === 'string') el.setAttribute('style', v);
        else for (const s in v) {
          if (v[s] == null) continue;
          if (s.startsWith('--')) el.style.setProperty(s, v[s]); else el.style[s] = v[s];
        }
      } else if (k === 'dataset') {
        for (const d in v) if (v[d] != null) el.dataset[d] = v[d];
      } else if (k === 'text') {
        el.textContent = v;
      } else if (k === 'ref') {
        v(el);
      } else if (k.startsWith('on') && typeof v === 'function') {
        el.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (k === 'innerHTML' || k === 'outerHTML') {
        throw new Error('h(): raw HTML is not allowed');
      } else if (!svg && PROPS.has(k)) {
        el[k] = v;
      } else {
        el.setAttribute(k, v === true ? '' : String(v));
      }
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children) {
    if (c == null || c === false || c === true) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function replace(el, ...children) { clear(el); return append(el, children); }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function setText(el, s) { const v = s == null ? '' : String(s); if (el.textContent !== v) el.textContent = v; }
export function toggle(el, cls, on) { el.classList.toggle(cls, !!on); }

// Line icons on a 24px grid, stroke = currentColor.
const ICONS = {
  x: 'M6 6l12 12M18 6L6 18',
  plus: 'M12 5v14M5 12h14',
  up: 'M12 19V5M5 12l7-7 7 7',
  down: 'M12 5v14M5 12l7 7 7-7',
  'chev-down': 'M6 9l6 6 6-6',
  'chev-up': 'M6 15l6-6 6 6',
  'chev-right': 'M9 6l6 6-6 6',
  'chev-left': 'M15 6l-6 6 6 6',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  stop: 'M8 8h8v8H8z',
  play: 'M8 5.5l10.5 6.5L8 18.5z',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21',
  crew: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20a6.5 6.5 0 0 1 13 0M16 4.3a3.5 3.5 0 0 1 0 6.4M21.5 20a6.5 6.5 0 0 0-3.8-5.9',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20.5 20.5L16 16',
  bell: 'M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15zM10 21h4',
  'bell-off': 'M6 16v-5a6 6 0 0 1 9.4-4.9M18 11v5l1.5 2h-15M10 21h4M3 3l18 18',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5',
  refresh: 'M20 12a8 8 0 1 1-2.4-5.7M20 4v5h-5',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  braces: 'M8 4c-2 0-2 1.5-2 3.5S5.5 11 4 12c1.5 1 2 2.5 2 4.5S6 20 8 20M16 4c2 0 2 1.5 2 3.5s.5 3.5 2 4.5c-1.5 1-2 2.5-2 4.5S18 20 16 20',
  panel: 'M4 5h16v14H4zM9.5 5v14',
  zap: 'M13 3L4.5 14H11l-1 7 8.5-11H12z',
  pen: 'M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4',
  grip: 'M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01',
  send: 'M5 12h13M13 6l6 6-6 6',
  layers: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5',
  orbit: 'M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0',
  chat: 'M5 5h14v10H10l-5 4z',
  board: 'M4 5h4v14H4zM10 5h4v9h-4zM16 5h4v6h-4z',
  log: 'M5 6h14M5 10h14M5 14h9M5 18h6',
  flask: 'M9 3h6M10 3v6.2L4.8 18a2 2 0 0 0 1.7 3h11a2 2 0 0 0 1.7-3L14 9.2V3M7.6 14.5h8.8',
};

export function icon(name, size = 16, sw = 1.7) {
  const d = ICONS[name] || ICONS.x;
  return h('svg', {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': name === 'grip' ? 3 : sw, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', class: 'ico',
  }, h('path', { d }));
}

// Auto-growing textarea.
export function autogrow(ta, max = 220) {
  const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(max, ta.scrollHeight + 2) + 'px'; };
  ta.addEventListener('input', fit);
  requestAnimationFrame(fit);
  return fit;
}

// Simple hover tooltip (one shared element).
let tipEl = null;
export function showTip(x, y, ...children) {
  if (!tipEl) { tipEl = h('div.tip'); document.body.appendChild(tipEl); }
  replace(tipEl, ...children);
  tipEl.hidden = false;
  const r = tipEl.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tipEl.style.left = Math.max(8, left) + 'px';
  tipEl.style.top = Math.max(8, top) + 'px';
}
export function hideTip() { if (tipEl) tipEl.hidden = true; }

export function copyText(s) {
  if (navigator.clipboard) return navigator.clipboard.writeText(s).catch(() => {});
  return Promise.resolve();
}
