// Mission composer (Work header, Watch empty state modal) + the shared dispatch call.
import { h, icon, autogrow, clear } from './dom.js';
import { post } from './api.js';
import { subscribe, agents, agentById, defaultAgent, upsert } from './store.js';
import { toast, firstDispatch } from './notify.js';
import { hueOf } from './format.js';

let lastAgent = '';
try { lastAgent = localStorage.getItem('sabha.lastAgent') || ''; } catch { /* ignore */ }

export async function dispatchMission({ prompt, agent_id, require_plan_approval = false, thread_id, title }) {
  const body = { prompt, agent_id, require_plan_approval: !!require_plan_approval };
  if (thread_id) body.thread_id = thread_id;
  if (title) body.title = title;
  const m = await post('/api/missions', body);
  if (m && m.id) upsert('missions', m);
  lastAgent = agent_id;
  try { localStorage.setItem('sabha.lastAgent', agent_id); } catch { /* ignore */ }
  const a = agentById(agent_id);
  toast(`Dispatched to ${a ? a.name : 'the council'}`, { sub: require_plan_approval ? 'It will bring a plan first.' : 'It is on the floor now.', tone: 'mint', timeout: 3800 });
  firstDispatch();
  return m;
}

export function preferredAgent() {
  return (lastAgent && agentById(lastAgent)) || defaultAgent();
}

export function agentSelect(value, onChange) {
  const sel = h('select.select.agent-select', { 'aria-label': 'Agent', onchange: () => { onChange && onChange(sel.value); paint(); } });
  function fill() {
    const cur = sel.value || value || (preferredAgent() || {}).id || '';
    clear(sel);
    for (const a of agents()) sel.appendChild(h('option', { value: a.id, selected: a.id === cur }, `${a.name}${a.role ? ' · ' + a.role : ''}`));
    if (!agents().length) sel.appendChild(h('option', { value: '' }, 'no agents yet'));
    paint();
  }
  function paint() { sel.dataset.hue = hueOf(agentById(sel.value)); }
  const un1 = subscribe('agents', fill), un2 = subscribe('reset', fill);
  fill();
  sel._destroy = () => { un1(); un2(); };
  return sel;
}

export function createComposer({ compact = false, autofocus = false, onDone } = {}) {
  const ta = h('textarea.textarea.composer-input', { rows: compact ? 2 : 3, placeholder: 'Describe the mission. What should be true when it is done?' });
  const fit = autogrow(ta, 260);
  const sel = agentSelect();
  const plan = h('input', { type: 'checkbox' });
  const send = h('button.btn.brand', { onclick: go }, icon('send', 15), 'Dispatch');
  const el = h('div', { class: 'composer card' + (compact ? ' compact' : '') },
    h('div.composer-head', null, h('span.micro', null, 'new mission'), h('span.micro.composer-hint', null, 'ctrl + enter to dispatch')),
    ta,
    h('div.composer-row', null,
      h('div.composer-left', null, h('span.micro', null, 'to'), sel, h('label.check', null, plan, 'plan first')),
      send));
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });
  let sending = false;
  async function go() {
    const prompt = ta.value.trim();
    if (!prompt || sending) { ta.focus(); return; }
    if (!sel.value) { toast('Add an agent first', { sub: 'Open Crew to seat one.', tone: 'sun' }); return; }
    sending = true; send.disabled = true;
    try {
      const m = await dispatchMission({ prompt, agent_id: sel.value, require_plan_approval: plan.checked });
      ta.value = ''; fit();
      onDone && onDone(m);
    } catch (e) {
      toast('Could not dispatch', { sub: e.message, tone: 'coral' });
    } finally { sending = false; send.disabled = false; }
  }
  if (autofocus) requestAnimationFrame(() => ta.focus());
  return { el, focus: () => ta.focus(), destroy: () => sel._destroy() };
}

export function openComposerModal() {
  const close = () => { overlay.remove(); modal.remove(); comp.destroy(); removeEventListener('keydown', onKey, true); };
  const comp = createComposer({ autofocus: true, onDone: close });
  const overlay = h('div.overlay', { onclick: close });
  const modal = h('div.modal.composer-modal', { role: 'dialog', 'aria-label': 'New mission' },
    h('div.modal-head', null, h('div.display.italic.modal-title', null, 'Give the council a task'),
      h('button.btn.ghost.icon', { 'aria-label': 'Close', onclick: close }, icon('x', 16))),
    comp.el);
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  addEventListener('keydown', onKey, true);
  document.body.append(overlay, modal);
}
