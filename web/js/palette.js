// Command palette (Ctrl/Cmd+K): type a task and Enter dispatches it; also quick actions.
import { h, icon, clear } from './dom.js';
import { agents, missions, agentById, setUI } from './store.js';
import { hueOf, truncate, statusLabel, tone } from './format.js';
import { dispatchMission, preferredAgent, openComposerModal } from './composer.js';
import { isMuted, setMuted, toast } from './notify.js';
import { post, enc } from './api.js';
import { nav } from './nav.js';

let openState = null;

export function openPalette() {
  if (openState) { openState.input.focus(); return; }
  let agentId = (preferredAgent() || {}).id || null;
  let sel = 0, items = [];
  const input = h('input.pal-input', { placeholder: 'Type a task to dispatch, or a command…', 'aria-label': 'Command', autocomplete: 'off', spellcheck: 'false' });
  const agentRow = h('div.pal-agents');
  const list = h('div.pal-list.scroll', { role: 'listbox' });
  const overlay = h('div.overlay', { onclick: close });
  const modal = h('div.modal.palette', { role: 'dialog', 'aria-label': 'Command palette' },
    h('div.pal-top', null, icon('search', 18), input, h('kbd', null, 'esc')),
    agentRow, list,
    h('div.pal-foot.micro', null, h('span', null, '↵ run'), h('span', null, '⇧↵ dispatch with plan first'), h('span', null, 'tab next agent')));
  document.body.append(overlay, modal);
  openState = { input };
  requestAnimationFrame(() => input.focus());

  function renderAgents() {
    clear(agentRow);
    agentRow.appendChild(h('span.micro', null, 'send to'));
    for (const a of agents()) {
      agentRow.appendChild(h('button', { class: 'agent-chip sm' + (a.id === agentId ? ' on' : ''), 'data-hue': hueOf(a), onclick: () => { agentId = a.id; renderAgents(); build(); input.focus(); } }, h('span.dot'), a.name));
    }
  }

  function actions() {
    const out = [
      { label: 'Go to Watch', hint: '1', ic: 'orbit', kw: 'watch orbit stream mode', run: () => nav.go('watch') },
      { label: 'Go to Talk', hint: '2', ic: 'chat', kw: 'talk chat mode', run: () => nav.go('talk') },
      { label: 'Go to Work', hint: '3', ic: 'board', kw: 'work board mode missions', run: () => nav.go('work') },
      { label: 'New mission…', hint: 'composer', ic: 'plus', kw: 'new mission dispatch compose', run: () => openComposerModal() },
      { label: 'Open crew', hint: 'agents', ic: 'crew', kw: 'crew agents persona', run: () => nav.openCrew('crew') },
      { label: 'Open models', hint: 'endpoints', ic: 'zap', kw: 'models endpoints vllm launch gpu', run: () => nav.openCrew('models') },
      { label: isMuted() ? 'Turn chimes on' : 'Turn chimes off', hint: 'sound', ic: isMuted() ? 'bell' : 'bell-off', kw: 'mute sound chime unmute', run: () => setMuted(!isMuted()) },
      { label: 'Watch everyone', hint: 'clear focus', ic: 'orbit', kw: 'focus all everyone clear', run: () => { setUI({ focusAgent: null, focusMission: null }); nav.go('watch'); } },
    ];
    for (const a of agents()) out.push({ label: `Focus ${a.name}`, hint: a.role || 'agent', ic: 'eye', hue: hueOf(a), kw: `focus watch ${a.name} ${a.role || ''}`, run: () => { setUI({ focusAgent: a.id, focusMission: null }); nav.go('watch'); } });
    const ms = missions().slice().sort((x, y) => (tone(x.status) === 'running' ? 0 : 1) - (tone(y.status) === 'running' ? 0 : 1) || (y.created_ts || 0) - (x.created_ts || 0));
    for (const m of ms.slice(0, 12)) {
      const a = agentById(m.agent_id);
      out.push({ label: `Open ${truncate(m.title || m.prompt, 60)}`, hint: statusLabel(m.status), ic: 'layers', hue: hueOf(a), kw: `open mission ${m.title} ${a ? a.name : ''}`, run: () => nav.openMission(m.id) });
      if (['running', 'planning', 'queued', 'awaiting_approval'].includes(m.status)) {
        out.push({ label: `Stop ${truncate(m.title || m.prompt, 60)}`, hint: a ? a.name : '', ic: 'stop', hue: hueOf(a), kw: `stop halt cancel ${m.title}`, run: async () => {
          try { await post(`/api/missions/${enc(m.id)}/stop`); toast('Stopping', { sub: truncate(m.title, 60), tone: 'sun', timeout: 2500 }); } catch (e) { toast('Could not stop', { sub: e.message, tone: 'coral' }); }
        } });
      }
    }
    return out;
  }

  function build() {
    const q = input.value.trim();
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const acts = actions().filter((a) => !words.length || words.every((w) => (a.label + ' ' + a.kw).toLowerCase().includes(w)));
    const a = agentById(agentId);
    const dispatch = q ? { label: `Dispatch “${truncate(q, 80)}”`, hint: `to ${a ? a.name : 'nobody yet'}`, ic: 'send', hue: hueOf(a), dispatch: true, run: (plan) => go(plan) } : null;
    const strong = q.length <= 14 && acts.some((x) => x.label.toLowerCase().split(/\s+/).some((w) => w.startsWith(q.toLowerCase())));
    items = dispatch ? (strong ? [...acts, dispatch] : [dispatch, ...acts]) : acts;
    sel = Math.min(sel, Math.max(0, items.length - 1));
    if (q && !strong) sel = 0;
    clear(list);
    items.slice(0, 40).forEach((it, i) => {
      list.appendChild(h('button', { class: 'pal-item' + (i === sel ? ' on' : '') + (it.dispatch ? ' dispatch' : ''), role: 'option', 'data-hue': it.hue || null,
        onmouseenter: () => { sel = i; paintSel(); }, onclick: () => runItem(it, false) },
      h('span.pal-ic', null, icon(it.ic, 15)), h('span.pal-label.ellipsis', null, it.label), h('span.pal-hint.micro', null, it.hint || '')));
    });
    if (!items.length) list.appendChild(h('div.empty-note', null, 'Nothing matches. Type a full sentence to dispatch it as a mission.'));
  }
  function paintSel() { Array.from(list.children).forEach((c, i) => c.classList.toggle('on', i === sel)); const c = list.children[sel]; if (c && c.scrollIntoView) c.scrollIntoView({ block: 'nearest' }); }

  async function go(plan) {
    const prompt = input.value.trim();
    if (!prompt) return;
    if (!agentId) { toast('Seat an agent first', { sub: 'Open Crew to add one.', tone: 'sun' }); return; }
    close();
    try { await dispatchMission({ prompt, agent_id: agentId, require_plan_approval: plan }); }
    catch (e) { toast('Could not dispatch', { sub: e.message, tone: 'coral' }); }
  }
  function runItem(it, shift) {
    if (it.dispatch) { it.run(shift); return; }
    close(); it.run();
  }

  input.addEventListener('input', build);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); paintSel(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); paintSel(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey && input.value.trim()) { go(true); return; }
      const it = items[sel]; if (it) runItem(it, false);
    } else if (e.key === 'Tab') {
      e.preventDefault();
      const list2 = agents(); if (!list2.length) return;
      const i = list2.findIndex((a) => a.id === agentId);
      agentId = list2[(i + (e.shiftKey ? -1 : 1) + list2.length) % list2.length].id;
      renderAgents(); build();
    } else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
  function close() { overlay.remove(); modal.remove(); openState = null; }
  renderAgents(); build();
}
