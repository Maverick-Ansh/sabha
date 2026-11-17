// Light / dark. Follows the system until the owner picks one with the top bar toggle; the choice is kept per
// browser. index.html sets data-theme before the first paint (no flash); this module keeps it in sync and tells
// the canvas views (orbit, timeline) to re-read their colours with a 'sabha-theme' event.
import { clearColorCache } from './format.js';

const KEY = 'sabha.theme';
const dark = matchMedia('(prefers-color-scheme: dark)');

export function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t === 'dark' || t === 'light' ? t : (dark.matches ? 'dark' : 'light');
}

function sync() {
  const t = currentTheme();
  document.documentElement.dataset.theme = t;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', t === 'dark' ? '#13111F' : '#F6F4FC');
  clearColorCache();
  dispatchEvent(new CustomEvent('sabha-theme', { detail: t }));
}

export function initTheme() {
  sync();
  dark.addEventListener('change', () => {
    let chosen = null;
    try { chosen = localStorage.getItem(KEY); } catch { /* ignore */ }
    if (!chosen) { delete document.documentElement.dataset.theme; sync(); }
  });
}

export function setTheme(t) {
  try { localStorage.setItem(KEY, t); } catch { /* private window: the choice lasts this page */ }
  document.documentElement.dataset.theme = t;
  sync();
}

export function toggleTheme() { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }
