// Crew & models slide-over: edit agents, register endpoints, launch vLLM on the box, read logs.
import { h, icon, setText, clear, autogrow } from './dom.js';
import { get, post, patch, del, enc } from './api.js';
import { subscribe, getState, agents, upsert, remove } from './store.js';
import { HUES, hueOf, truncate } from './format.js';
import { toast } from './notify.js';

const KINDS = ['chat', 'decision', 'embedding', 'custom', 'mock'];
const HARNESS = [['dsh', 'dsh · tools + shell'], ['mock', 'mock · scripted demo'], ['chat', 'chat · no tools']];
let open = null;

export function openCrew(tab = 'crew') {
  if (open) { open.show(tab); return; }
  const tabs = ['crew', 'models'].map((t) => h('button', { 'aria-pressed': 'false', onclick: () => show(t) }, t === 'crew' ? 'Crew' : 'Models'));
  const body = h('div.so-body.scroll');
  const panel = h('aside.slideover', { role: 'dialog', 'aria-label': 'Crew and models' },
    h('header.so-head', null, h('div.display.italic.so-title', null, 'Crew & models'), h('div.seg.seg-sm', null, tabs),
      h('button.btn.ghost.icon', { 'aria-label': 'Close', onclick: () => close() }, icon('x', 18))),
    body);
  const overlay = h('div.overlay.so-overlay', { onclick: () => close() });
  document.body.append(overlay, panel);
  requestAnimationFrame(() => panel.classList.add('in'));
  let cleanup = [];
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  addEventListener('keydown', onKey);

  function show(t) {
    tabs.forEach((b, i) => b.setAttribute('aria-pressed', String(['crew', 'models'][i] === t)));
    cleanup.forEach((f) => f()); cleanup = [];
    clear(body);
    if (t === 'crew') renderCrew(body, cleanup); else renderModels(body, cleanup);
  }
  function close() {
    cleanup.forEach((f) => f());
    removeEventListener('keydown', onKey);
    overlay.remove(); panel.classList.remove('in');
    setTimeout(() => panel.remove(), 260);
    open = null;
  }
  open = { show };
  show(tab);
}

// ---------- crew ----------
function modelOptions(sel, value) {
  clear(sel);
  sel.appendChild(h('option', { value: '' }, 'no model'));
  for (const m of Object.values(getState().models)) sel.appendChild(h('option', { value: m.id, selected: m.id === value }, `${m.name} · ${m.kind}`));
}

function agentCard(a, { onSaved, onCancel } = {}) {
  const isNew = !a.id;
  let hue = hueOf(a), dirty = isNew;
  const name = h('input.input.ac-name', { value: a.name || '', placeholder: 'Name', 'aria-label': 'Name' });
  const role = h('input.input', { value: a.role || '', placeholder: 'Role, e.g. writes and runs tests', 'aria-label': 'Role' });
  const persona = h('textarea.textarea', { rows: 3, placeholder: 'Persona: how this agent thinks and works. Prepended to its first task.', 'aria-label': 'Persona' });
  persona.value = a.persona || '';
  const model = h('select.select', { 'aria-label': 'Model' });
  modelOptions(model, a.model_id);
  const dshOk = !!(getState().system.harnesses || {}).dsh;
  const harness = h('select.select', { 'aria-label': 'Harness' },
    HARNESS.map(([v, l]) => h('option', { value: v, selected: v === (a.harness || (dshOk ? 'dsh' : 'mock')), disabled: v === 'dsh' && !dshOk }, l + (v === 'dsh' && !dshOk ? ' (not installed)' : ''))));
  const hint = !dshOk ? h('div.ac-hint.micro', null, 'dsh is not on this box’s PATH, so the tool harness is off.') : null;
  const swatches = HUES.map((x) => h('button', { class: 'swatch' + (x === hue ? ' on' : ''), 'data-hue': x, title: x, 'aria-label': x, onclick: () => { hue = x; card.dataset.hue = x; swatches.forEach((s) => s.classList.toggle('on', s.dataset.hue === x)); markDirty(); } }));
  const save = h('button.btn.sm.ink', { disabled: !dirty, onclick: doSave }, icon('check', 13), isNew ? 'Seat agent' : 'Save');
  let armed = 0;
  const delBtn = h('button.btn.sm.ghost.danger', { onclick: doDelete }, icon('trash', 13), isNew ? 'Cancel' : 'Remove');
  const card = h('div.agent-card.card', { 'data-hue': hue },
    h('div.ac-top', null, h('span.ac-orb'), h('div.ac-names', null, name, role)),
    h('div.ac-row', null, h('span.micro', null, 'hue'), h('div.swatches', null, swatches)),
    h('div.field', null, h('span.micro', null, 'persona'), persona),
    h('div.ac-grid', null, h('label.field', null, h('span.micro', null, 'model'), model), h('label.field', null, h('span.micro', null, 'harness'), harness)),
    hint,
    h('div.ac-foot', null, delBtn, h('span.spacer'), save));
  autogrow(persona, 200);
  function markDirty() { dirty = true; save.disabled = false; }
  [name, role, persona].forEach((x) => x.addEventListener('input', markDirty));
  [model, harness].forEach((x) => x.addEventListener('change', markDirty));
  async function doSave() {
    const body = { name: name.value.trim() || 'Agent', role: role.value.trim(), persona: persona.value, model_id: model.value, harness: harness.value, hue };
    save.disabled = true;
    try {
      const r = isNew ? await post('/api/agents', body) : await patch(`/api/agents/${enc(a.id)}`, body);
      upsert('agents', r); dirty = false;
      toast(isNew ? `${r.name} takes a seat` : 'Saved', { tone: 'mint', timeout: 2500 });
      onSaved && onSaved(r);
    } catch (e) { save.disabled = false; toast('Could not save', { sub: e.message, tone: 'coral' }); }
  }
  async function doDelete() {
    if (isNew) { card.remove(); onCancel && onCancel(); return; }
    if (Date.now() - armed > 3000) { armed = Date.now(); clear(delBtn).append(icon('trash', 13), 'Really remove?'); setTimeout(() => clear(delBtn).append(icon('trash', 13), 'Remove'), 3000); return; }
    try { await del(`/api/agents/${enc(a.id)}`); remove('agents', a.id); card.remove(); }
    catch (e) { toast('Could not remove', { sub: e.message, tone: 'coral' }); }
  }
  card._isDirty = () => dirty;
  card._refreshModels = () => modelOptions(model, model.value || a.model_id);
  return card;
}

function renderCrew(body, cleanup) {
  const list = h('div.crew-list');
  const addBtn = h('button.btn.add-agent', { onclick: () => { list.appendChild(agentCard({ hue: HUES[agents().length % HUES.length] }, { onSaved: render })); addBtn.scrollIntoView({ block: 'end', behavior: 'smooth' }); } }, icon('plus', 15), 'Seat a new agent');
  body.append(h('p.so-intro.dim', null, 'Who sits on the council, and which model each one thinks with.'), list, addBtn);
  function render() {
    const dirty = Array.from(list.children).some((c) => c._isDirty && c._isDirty());
    if (dirty) { Array.from(list.children).forEach((c) => c._refreshModels && c._refreshModels()); return; }
    clear(list);
    for (const a of agents()) list.appendChild(agentCard(a));
    if (!agents().length) list.appendChild(h('div.empty-note', null, 'No agents yet. Seat the first one below.'));
  }
  render();
  cleanup.push(subscribe('agents', render), subscribe('models', render));
}

// ---------- models ----------
function renderModels(body, cleanup) {
  const list = h('div.model-list');
  const logs = new Map(); // model id -> pre element
  body.append(h('p.so-intro.dim', null, 'Endpoints the agents can think with. Anything OpenAI-compatible works.'), list, addForm(), launchForm());

  function row(m) {
    const logPre = h('pre.model-log.scroll', { hidden: true });
    const logBtn = h('button.btn.ghost.sm', { onclick: async () => {
      logPre.hidden = !logPre.hidden;
      if (!logPre.hidden) {
        logs.set(m.id, logPre);
        setText(logPre, 'loading log…');
        try { const r = await get(`/api/models/${enc(m.id)}/log`); setText(logPre, ((r && r.lines) || []).join('\n') || 'no log lines yet'); logPre.scrollTop = logPre.scrollHeight; }
        catch (e) { setText(logPre, `could not read log: ${e.message}`); }
      } else logs.delete(m.id);
    } }, icon('log', 13), 'Log');
    const call = async (what) => {
      try { const r = await post(`/api/models/${enc(m.id)}/${what}`); upsert('models', r); toast(what === 'probe' ? `${m.name} is ${r.status}` : `Stopping ${m.name}`, { tone: r.status === 'ready' ? 'mint' : 'sun', timeout: 2500 }); }
      catch (e) { toast(`Could not ${what}`, { sub: e.message, tone: 'coral' }); }
    };
    let armed = 0;
    const delBtn = h('button.btn.ghost.sm.icon.danger', { title: 'Remove endpoint', onclick: async () => {
      if (Date.now() - armed > 3000) { armed = Date.now(); delBtn.classList.add('armed'); toast(`Click again to remove ${m.name}`, { timeout: 2800 }); return; }
      try { await del(`/api/models/${enc(m.id)}`); remove('models', m.id); } catch (e) { toast('Could not remove', { sub: e.message, tone: 'coral' }); }
    } }, icon('trash', 13));
    const el = h('div.model-card.card', null,
      h('div.mc-top', null, h('span', { class: 'mstatus ' + (m.status || 'unknown'), title: m.status || 'unknown' }),
        h('span.mc-name', null, m.name), h('span.chip.mono', null, m.kind || 'chat'), h('span.mc-status.micro', null, m.status || 'unknown'),
        h('span.spacer'),
        h('button.btn.ghost.sm', { onclick: () => call('probe') }, icon('zap', 13), 'Probe'),
        m.launch ? h('button.btn.ghost.sm', { onclick: () => call('stop') }, icon('stop', 13), 'Stop') : null,
        logBtn, delBtn),
      h('div.mc-url.mono', null, m.base_url || 'answered by the hub (mock)', m.served_name ? h('span.faint', null, `  ·  ${m.served_name}`) : null, m.context_window ? h('span.faint', null, `  ·  ${m.context_window} ctx`) : null),
      m.launch && m.launch.cmd ? h('div.mc-cmd.mono.faint', { title: m.launch.cmd }, truncate(m.launch.cmd, 180)) : null,
      logPre);
    if (logs.has(m.id)) logBtn.click();
    return el;
  }
  function render() {
    clear(list);
    const ms = Object.values(getState().models);
    if (!ms.length) list.appendChild(h('div.empty-note', null, 'No endpoints yet. Add one, or launch a model on this box.'));
    for (const m of ms) list.appendChild(row(m));
  }
  render();
  cleanup.push(subscribe('models', render));
  cleanup.push(subscribe('modellog', (ev) => {
    const d = ev.data || {};
    const pre = logs.get(d.model_id);
    if (!pre || pre.hidden) return;
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
    pre.append('\n' + (d.line || ''));
    if (pre.textContent.length > 120000) pre.textContent = pre.textContent.slice(-90000);
    if (stick) pre.scrollTop = pre.scrollHeight;
  }));

  function addForm() {
    const f = {
      name: h('input.input', { placeholder: 'Qwen 7B on the box' }), kind: h('select.select', null, KINDS.map((k) => h('option', { value: k }, k))),
      base_url: h('input.input.mono', { placeholder: 'http://127.0.0.1:8000/v1' }), served_name: h('input.input.mono', { placeholder: 'qwen7b' }),
      context_window: h('input.input.mono', { type: 'number', value: '24576' }), api_key_env: h('input.input.mono', { placeholder: 'env var with the key, or blank' }),
    };
    const go = h('button.btn.sm.ink', { onclick: async () => {
      const body = { name: f.name.value.trim() || f.served_name.value.trim() || 'model', kind: f.kind.value, base_url: f.base_url.value.trim(), served_name: f.served_name.value.trim(),
        context_window: parseInt(f.context_window.value, 10) || 24576, api_key_env: f.api_key_env.value.trim() };
      try { const r = await post('/api/models', body); upsert('models', r); toast(`Added ${r.name}`, { tone: 'mint', timeout: 2500 }); f.name.value = f.base_url.value = f.served_name.value = ''; }
      catch (e) { toast('Could not add', { sub: e.message, tone: 'coral' }); }
    } }, icon('plus', 13), 'Add endpoint');
    const lab = (k, el) => h('label.field', null, h('span.micro', null, k), el);
    return h('details.so-form.card', null, h('summary', null, h('span.so-form-title', null, 'Add endpoint'), h('span.micro', null, 'any OpenAI-compatible URL')),
      h('div.form-grid', null, lab('name', f.name), lab('kind', f.kind), lab('base url', f.base_url), lab('served name', f.served_name), lab('context window', f.context_window), lab('api key env', f.api_key_env)),
      h('div.form-foot', null, go));
  }

  function launchForm() {
    const hf = h('input.input.mono', { placeholder: 'Qwen/Qwen2.5-7B-Instruct' });
    const port = h('input.input.mono', { type: 'number', value: '8000' });
    const name = h('input.input.mono', { placeholder: 'qwen7b' });
    const cmd = h('textarea.textarea.mono', { rows: 4 });
    const env = h('textarea.textarea.mono', { rows: 2 });
    env.value = 'CUDA_VISIBLE_DEVICES=0';
    let cmdEdited = false;
    const served = () => name.value.trim() || (hf.value.trim().split('/').pop() || 'model').toLowerCase().replace(/[^a-z0-9._-]+/g, '-');
    const template = () => {
      if (cmdEdited) return;
      cmd.value = `vllm serve ${hf.value.trim() || '<hf_id>'} --port ${port.value || 8000} --served-model-name ${served()} --max-model-len 24576 --gpu-memory-utilization 0.93 --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes`;
    };
    [hf, port, name].forEach((x) => x.addEventListener('input', template));
    cmd.addEventListener('input', () => { cmdEdited = true; });
    template();
    const go = h('button.btn.sm.brand', { onclick: async () => {
      if (!hf.value.trim() && !cmdEdited) { hf.focus(); return; }
      const p = parseInt(port.value, 10) || 8000;
      const envObj = {};
      for (const line of env.value.split('\n')) { const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/); if (m) envObj[m[1]] = m[2]; }
      go.disabled = true;
      try {
        const model = await post('/api/models', { name: served(), kind: 'chat', base_url: `http://127.0.0.1:${p}/v1`, served_name: served(), context_window: 24576, api_key_env: '' });
        upsert('models', model);
        const r = await post(`/api/models/${enc(model.id)}/launch`, { cmd: cmd.value.trim(), env: envObj });
        upsert('models', r);
        logs.set(model.id, null);
        toast(`Launching ${model.name}`, { sub: 'Open its log to watch it warm up.', tone: 'sun', timeout: 5000 });
      } catch (e) { toast('Launch failed', { sub: e.message, tone: 'coral' }); }
      finally { go.disabled = false; }
    } }, icon('play', 13), 'Launch');
    const lab = (k, el) => h('label.field', null, h('span.micro', null, k), el);
    return h('details.so-form.card', null, h('summary', null, h('span.so-form-title', null, 'Launch on this box'), h('span.micro', null, 'vLLM, served next to the hub')),
      h('div.form-grid', null, lab('hugging face id', hf), lab('port', port), lab('served name', name)),
      lab('command', cmd), lab('environment', env),
      h('div.form-foot', null, h('span.micro', null, 'creates the endpoint, then runs the command'), h('span.spacer'), go));
  }
}
