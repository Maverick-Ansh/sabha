// The Sabha mark: a five-petal lotus. One set of paths (32 x 32 box, petals meet at 16,23) feeds the
// wordmark, the auth gate, the favicon in index.html and the core of the orbit canvas.
import { h } from './dom.js';

export const LOTUS_BASE = [16, 23];
// back to front: outer pair, middle pair, centre petal
export const LOTUS_PETALS = [
  { d: 'M2.6 15.4C8 14.6 13 17.6 16 23C10 24.6 5 21.6 2.6 15.4Z', o: 0.5 },
  { d: 'M29.4 15.4C24 14.6 19 17.6 16 23C22 24.6 27 21.6 29.4 15.4Z', o: 0.5 },
  { d: 'M8.2 8.6C12.6 10.4 15.4 15.2 16 23C10.2 21 7.4 15.4 8.2 8.6Z', o: 0.72 },
  { d: 'M23.8 8.6C19.4 10.4 16.6 15.2 16 23C21.8 21 24.6 15.4 23.8 8.6Z', o: 0.72 },
  { d: 'M16 4C20 9 21.5 15 16 23C10.5 15 12 9 16 4Z', o: 1 },
];
export const LOTUS_WATER = 'M9.5 26.6Q16 28.8 22.5 26.6';

let uid = 0;
export function lotusGlyph(size = 26, cls = '') {
  const id = 'sabhaLotus' + (++uid);
  return h('svg', { class: 'lotus-glyph' + (cls ? ' ' + cls : ''), width: size, height: size, viewBox: '0 0 32 32', 'aria-hidden': 'true' },
    h('defs', null, h('linearGradient', { id, x1: '0.15', y1: '0', x2: '0.85', y2: '1' },
      h('stop', { offset: '0', style: 'stop-color: var(--core-b)' }), h('stop', { offset: '1', style: 'stop-color: var(--core-a)' }))),
    LOTUS_PETALS.map((p, i) => h('path', { class: 'lotus-petal', d: p.d, fill: `url(#${id})`, 'fill-opacity': p.o, style: `--i:${i}` })),
    h('path', { class: 'lotus-water', d: LOTUS_WATER, fill: 'none', stroke: `url(#${id})`, 'stroke-width': 1.3, 'stroke-linecap': 'round', 'stroke-opacity': 0.55 }));
}

// Canvas version: the petals as Path2D, built once.
let paths = null;
export function lotusPaths() {
  if (!paths) paths = LOTUS_PETALS.map((p) => ({ path: new Path2D(p.d), o: p.o }));
  return paths;
}
