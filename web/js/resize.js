// Resizable panels in Watch: drag the Everyone stream's left edge to make it wider or narrower, drag the timeline's
// top edge to make it taller or shorter. Double-click a handle (or drag it nearly shut) to fold the panel away; a
// small tab brings the stream back, the timeline folds to a thin bar. Sizes are remembered per browser. Arrow keys
// move a focused handle. Only on a wide screen: a phone keeps its stacked layout.
import { h } from './dom.js';

const KEY = 'sabha.watch.sizes';
const wide = () => matchMedia('(min-width: 1041px)').matches;

export function makeResizable(view, streamPanel, tlPanel) {
  let s = { sw: 420, th: 112, sFold: false, tFold: false };
  try { s = { ...s, ...(JSON.parse(localStorage.getItem(KEY) || '{}') || {}) }; } catch { /* private window */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* fine */ } };

  function apply() {
    if (!wide()) { view.style.removeProperty('--stream-w'); view.style.removeProperty('--timeline-h'); view.classList.remove('stream-folded', 'tl-folded'); return; }
    view.style.setProperty('--stream-w', s.sFold ? '0px' : `${Math.round(s.sw)}px`);
    view.style.setProperty('--timeline-h', s.tFold ? '30px' : `${Math.round(s.th)}px`);
    view.classList.toggle('stream-folded', s.sFold);
    view.classList.toggle('tl-folded', s.tFold);
    sx.setAttribute('aria-valuenow', String(Math.round(s.sw)));
    ty.setAttribute('aria-valuenow', String(Math.round(s.th)));
  }
  const maxW = () => Math.max(300, Math.min(960, view.clientWidth * 0.62));
  const maxH = () => Math.max(120, Math.min(560, view.clientHeight * 0.6));

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

  // the timeline: its top edge
  const ty = h('div.rz.rz-y', { role: 'separator', tabindex: '0', 'aria-orientation': 'horizontal', 'aria-label': 'Resize the timeline',
    'aria-valuemin': '30', title: 'Drag to resize · double-click to fold' }, h('i'));
  drag(ty, (e) => {
    const hh = view.getBoundingClientRect().bottom - e.clientY;
    if (hh < 54) { s.tFold = true; return; }
    s.tFold = false; s.th = Math.min(maxH(), Math.max(70, hh));
  });
  ty.addEventListener('dblclick', () => { s.tFold = !s.tFold; apply(); save(); });
  ty.addEventListener('keydown', (e) => {
    const d = e.key === 'ArrowUp' ? 20 : e.key === 'ArrowDown' ? -20 : 0;
    if (e.key === 'Enter') { s.tFold = !s.tFold; apply(); save(); return; }
    if (!d) return;
    e.preventDefault(); s.tFold = false; s.th = Math.min(maxH(), Math.max(70, s.th + d)); apply(); save();
  });
  tlPanel.prepend(ty);

  addEventListener('resize', apply);
  apply();
  return { apply };
}
