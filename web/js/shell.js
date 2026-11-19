// Top bar: wordmark + lotus mark, mode switch, session clock, GPU chips, mute, crew, connection dot.
import { h, icon, setText, clear } from './dom.js';
import { subscribe, getState, sessionWindow } from './store.js';
import { fmtDur, serverNow } from './format.js';
import { isMuted, setMuted } from './notify.js';
import { lotusGlyph } from './lotus.js';
import { currentTheme, toggleTheme } from './theme.js';

export const MODES = ['research', 'watch', 'talk', 'work', 'council', 'nirmana'];
const LABEL = { research: 'Research', watch: 'Watch', talk: 'Talk', work: 'Work', council: 'Council', nirmana: 'Nirmana' };
const TAB_ICON = { research: 'flask', watch: 'orbit', talk: 'chat', work: 'board', council: 'crew', nirmana: 'loom' };

function shortGpu(name) {
  const s = String(name || 'GPU').replace(/NVIDIA|Tesla|GeForce|Corporation/gi, '').trim();
  const m = s.match(/(A100|H100|H200|L4|T4|V100|P100|A10G?|L40S?|RTX\s?\w+|\w+)/i);
  return m ? m[1].replace(/\s+/g, '') : s.slice(0, 8);
}

export function createTopbar({ onMode, onCrew, onPalette }) {
  // mode switch with sliding highlight
  const hl = h('span.seg-hl');
  const btns = MODES.map((m, i) => h('button', { 'aria-pressed': 'false', title: `${LABEL[m]} (${i + 1})`, onclick: () => onMode(m) }, LABEL[m]));
  const seg = h('div.seg.mode-switch', { role: 'tablist', 'aria-label': 'Mode' }, hl, btns);
  // the same switch as a bottom tab bar: CSS shows it on phones and hides the segmented control there
  const tabs = MODES.map((m) => h('button.tab', { 'aria-pressed': 'false', 'aria-label': LABEL[m], onclick: () => onMode(m) }, icon(TAB_ICON[m], 21, 1.6), h('span', null, LABEL[m])));
  const tabbar = h('nav.tabbar', { 'aria-label': 'Mode' }, tabs);

  // session clock
  const C = 2 * Math.PI * 11;
  const arc = h('circle', { cx: 14, cy: 14, r: 11, class: 'clock-arc', 'stroke-dasharray': `${C} ${C}`, 'stroke-dashoffset': '0', transform: 'rotate(-90 14 14)' });
  const clockLabel = h('span.clock-label');
  const clock = h('div.session-clock', { title: 'Cloud session time left' },
    h('svg', { width: 28, height: 28, viewBox: '0 0 28 28', 'aria-hidden': 'true' },
      h('circle', { cx: 14, cy: 14, r: 11, class: 'clock-track' }), arc,
      h('circle', { cx: 14, cy: 14, r: 2, class: 'clock-pin' })),
    clockLabel);

  const gpus = h('div.gpus');
  const muteBtn = h('button.btn.ghost.icon.mute-btn', { onclick: () => setMuted(!isMuted()) });
  const themeBtn = h('button.btn.ghost.icon.theme-btn', { onclick: () => toggleTheme() });
  const paintTheme = () => {
    const d = currentTheme() === 'dark';
    themeBtn.replaceChildren(icon(d ? 'sun' : 'moon', 17));
    themeBtn.title = d ? 'Light mode' : 'Dark mode';
    themeBtn.setAttribute('aria-label', themeBtn.title);
  };
  paintTheme();
  addEventListener('sabha-theme', paintTheme);
  const dot = h('span.conn-dot', { role: 'status' });
  const demo = h('span.chip.mono.demo-chip', { hidden: true }, 'demo');

  const el = h('header.topbar', null,
    h('div.tb-left', null,
      h('a.wordmark', { href: '#/watch', 'aria-label': 'Sabha' }, lotusGlyph(28), h('span.wm-text', null, 'Sabha')),
      demo),
    h('div.tb-center', null, seg),
    h('div.tb-right', null,
      clock, gpus,
      h('button.btn.ghost.icon.palette-btn', { title: 'Command palette (Ctrl+K)', onclick: onPalette }, icon('search', 16)),
      themeBtn, muteBtn,
      h('button.btn.crew-btn', { onclick: onCrew }, icon('crew', 15), h('span', null, 'Crew')),
      dot));

  function setMode(mode) {
    btns.forEach((b, i) => b.setAttribute('aria-pressed', String(MODES[i] === mode)));
    tabs.forEach((b, i) => b.setAttribute('aria-pressed', String(MODES[i] === mode)));
    requestAnimationFrame(() => {
      const b = btns[MODES.indexOf(mode)];
      if (!b) return;
      hl.style.left = b.offsetLeft + 'px';
      hl.style.width = b.offsetWidth + 'px';
    });
  }

  function renderClock() {
    const st = getState();
    const tel = st.telemetry;
    let left, limit;
    if (tel && Number.isFinite(tel.uptime_s) && tel.session_limit_s) {
      limit = tel.session_limit_s;
      left = limit - tel.uptime_s - (Date.now() / 1000 - st.telemetryAt);
    } else {
      const w = sessionWindow();
      limit = w.limit; left = w.end - serverNow();
    }
    left = Math.max(0, left);
    const frac = limit ? Math.max(0, Math.min(1, left / limit)) : 0;
    arc.setAttribute('stroke-dashoffset', String(C * (1 - frac)));
    clock.classList.toggle('low', left < 3600 && left >= 900);
    clock.classList.toggle('crit', left < 900);
    setText(clockLabel, left > 0 ? `${fmtDur(left)} left` : 'session over');
    clock.title = `Cloud session: ${fmtDur(limit - left)} used of ${fmtDur(limit)}`;
  }

  function renderGpus(tel) {
    clear(gpus);
    const list = (tel && tel.gpus) || [];
    for (const g of list.slice(0, 4)) {
      const memFrac = g.mem_total_mb ? g.mem_used_mb / g.mem_total_mb : 0;
      const util = Math.round(g.util || 0);
      gpus.appendChild(h('div', { class: 'gpu-chip' + (util > 5 ? ' busy' : ''), title: `${g.name} · ${util}% util · ${Math.round(g.mem_used_mb || 0)} / ${Math.round(g.mem_total_mb || 0)} MB${g.temp_c != null ? ' · ' + g.temp_c + '°C' : ''}` },
        h('span.gpu-name', null, shortGpu(g.name)),
        h('span.gpu-util', null, `${util}%`),
        h('span.gpu-mem', null, h('span', { style: { width: Math.round(memFrac * 100) + '%' } }))));
    }
    if (!list.length && tel) gpus.appendChild(h('div.gpu-chip.none', { title: 'No GPU reported by the box' }, h('span.gpu-name', null, 'cpu'), h('span.gpu-util', null, `${Math.round(tel.cpu_pct || 0)}%`)));
  }

  function renderMute() {
    clear(muteBtn);
    muteBtn.appendChild(icon(isMuted() ? 'bell-off' : 'bell', 16));
    muteBtn.title = isMuted() ? 'Chimes off' : 'Chimes on';
    muteBtn.setAttribute('aria-pressed', String(!isMuted()));
  }

  function renderConn(c) {
    dot.className = 'conn-dot ' + c;
    const t = { live: 'Live', connecting: 'Connecting', reconnecting: 'Reconnecting', auth: 'Needs a token', offline: 'Offline' }[c] || c;
    dot.title = t; dot.setAttribute('aria-label', t);
  }

  subscribe('telemetry', (tel) => { renderGpus(tel); renderClock(); });
  subscribe('reset', (st) => { demo.hidden = !(st.app && st.app.demo); renderClock(); });
  subscribe('conn', renderConn);
  subscribe('mute', renderMute);
  setInterval(renderClock, 15000);
  addEventListener('resize', () => setMode(getState().ui.mode));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => setMode(getState().ui.mode));
  renderMute(); renderConn(getState().conn); renderClock();
  return { el, tabbar, setMode };
}
