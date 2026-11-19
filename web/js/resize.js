// The resizable Everyone stream in Watch: drag its left edge to make it wider or narrower; double-click the handle
// (or drag it nearly shut) to fold it away, and a small tab brings it back. The width is remembered per browser.
// Arrow keys move a focused handle. Only on a wide screen: a phone keeps its stacked layout. (The timeline keeps its
// fixed height at the bottom.)
import { h } from './dom.js';

const KEY = 'sabha.watch.sizes';
const wide = () => matchMedia('(min-width: 1041px)').matches;

export function makeResizable(view, streamPanel) {
  let s = { sw: 420, sFold: false };
  try { s = { ...s, ...(JSON.parse(localStorage.getItem(KEY) || '{}') || {}) }; } catch { /* private window */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* fine */ } };

  function apply() {
    if (!wide()) { view.style.removeProperty('--stream-w'); view.style.removeProperty('--timeline-h'); view.classList.remove('stream-folded'); return; }
    view.style.setProperty('--stream-w', s.sFold ? '0px' : `${Math.round(s.sw)}px`);
    view.classList.toggle('stream-folded', s.sFold);
    sx.setAttribute('aria-valuenow', String(Math.round(s.sw)));
  }
  const maxW = () => Math.max(300, Math.min(960, view.clientWidth * 0.62));

  function drag(handle, onMove) {
    handle.addEventListener('pointerdown', (e) => {
      if (!wide() || e.button !== 0) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      view.classList.add('resizing');
      const move = (ev) => { onMove(ev); apply(); };
      const up = () => {
        view.classList.remove('resizing');
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        save();
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
  }

  // the stream: its left edge
  const sx = h('div.rz.rz-x', { role: 'separator', tabindex: '0', 'aria-orientation': 'vertical', 'aria-label': 'Resize the Everyone panel',
    'aria-valuemin': '280', title: 'Drag to resize · double-click to fold' }, h('i'));
  drag(sx, (e) => {
    const w = view.getBoundingClientRect().right - e.clientX;
    if (w < 200) { s.sFold = true; return; }
    s.sFold = false; s.sw = Math.min(maxW(), Math.max(280, w));
  });
  sx.addEventListener('dblclick', () => { s.sFold = !s.sFold; apply(); save(); });
  sx.addEventListener('keydown', (e) => {
    const d = e.key === 'ArrowLeft' ? 24 : e.key === 'ArrowRight' ? -24 : 0;
    if (e.key === 'Enter') { s.sFold = !s.sFold; apply(); save(); return; }
    if (!d) return;
    e.preventDefault(); s.sFold = false; s.sw = Math.min(maxW(), Math.max(280, s.sw + d)); apply(); save();
  });
  const reopen = h('button.rz-tab', { title: 'Show the Everyone panel', 'aria-label': 'Show the Everyone panel', onclick: () => { s.sFold = false; apply(); save(); } },
    h('span', null, 'Everyone'));
  streamPanel.prepend(sx);
  view.appendChild(reopen);

  addEventListener('resize', apply);
  apply();
  return { apply };
}
