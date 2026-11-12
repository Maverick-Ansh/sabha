// Mission drawer: Plan / Stream / Files / Report tabs, steer box and lifecycle buttons.
import { h, icon, setText, clear, autogrow } from './dom.js';
import { get, post, del, enc } from './api.js';
import { subscribe, getState, agentById, upsert, remove } from './store.js';
import { hueOf, fmtDur, fmtDate, elapsed, statusLabel, tone, fmtNum, fmtBytes, truncate } from './format.js';
import { md } from './md.js';
import { createStream } from './stream.js';
import { toast } from './notify.js';

const TABS = ['plan', 'stream', 'files', 'report'];
let current = null;

export function closeDrawer() { if (current) current.close(); }

export function openDrawer(id, tab) {
  if (current) current.close(true);
  const m0 = getState().missions[id];
  if (!m0) { toast('That mission is gone', { tone: 'coral' }); return; }
  tab = tab || (m0.status === 'awaiting_approval' ? 'plan' : m0.status === 'done' || m0.status === 'failed' ? 'report' : 'stream');
  const M = () => getState().missions[id];

  // ---------- header ----------
  const titleEl = h('h2.dr-title.display.italic');
  const statusEl = h('span.status');
  const agentChip = h('span.chip.hue');
  const metaEl = h('span.mono.faint.dr-meta');
  const tabBtns = TABS.map((t) => h('button', { class: 'dr-tab', 'aria-selected': 'false', onclick: () => show(t) }, t[0].toUpperCase() + t.slice(1)));
  const body = h('div.dr-body');

  // ---------- footer ----------
  const notesEl = h('div.dr-notes');
  const steer = h('textarea.textarea.dr-steer', { rows: 1, placeholder: 'Whisper a note to the agent…' });
  const fitSteer = autogrow(steer, 140);
  const interrupt = h('input', { type: 'checkbox' });
  const noteBtn = h('button.btn.sm', { onclick: sendNote }, icon('send', 13), 'Send note');
  const stopBtn = h('button.btn.sm', { onclick: () => act('stop') }, icon('stop', 13), 'Stop');
  const resumeBtn = h('button.btn.sm', { onclick: resume }, icon('play', 13), 'Resume');
  let delArmed = 0;
  const delBtn = h('button.btn.sm.danger', { onclick: delMission }, icon('trash', 13), 'Delete');
  steer.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendNote(); } });

  const drawer = h('aside.drawer', { role: 'dialog', 'aria-label': 'Mission' },
    h('header.dr-head', null,
      h('div.dr-headtext', null, h('div.micro', null, `mission · ${id}`), titleEl, h('div.dr-chips', null, statusEl, agentChip, metaEl)),
      h('button.btn.ghost.icon', { 'aria-label': 'Close', onclick: () => close() }, icon('x', 18))),
    h('nav.dr-tabs', null, tabBtns),
    body,
    h('footer.dr-foot', null, notesEl,
      h('div.dr-steerrow', null, steer, h('div.dr-steeropts', null, h('label.check', null, interrupt, 'interrupt now'), noteBtn)),
      h('div.dr-actions', null, stopBtn, resumeBtn, h('span.spacer'), delBtn)));
  const overlay = h('div.overlay.dr-overlay', { onclick: () => close() });
  document.body.append(overlay, drawer);
  requestAnimationFrame(() => drawer.classList.add('in'));

  let stream = null, planDirty = false, draft = null, active = null;

  function paintHead() {
    const m = M(); if (!m) return;
    const a = agentById(m.agent_id);
    drawer.dataset.hue = hueOf(a);
    setText(titleEl, m.title || truncate(m.prompt, 80));
    statusEl.className = 'status ' + tone(m.status); setText(statusEl, statusLabel(m.status));
    clear(agentChip).append(h('span.dot'), a ? a.name : 'agent');
    const e = elapsed(m);
    setText(metaEl, [e ? fmtDur(e) : '', m.runs > 1 ? `${m.runs} runs` : '', m.created_ts ? fmtDate(m.created_ts) : ''].filter(Boolean).join(' · '));
    const t = tone(m.status);
    stopBtn.hidden = !(t === 'running' || m.status === 'queued' || m.status === 'awaiting_approval');
    resumeBtn.hidden = !(m.status === 'done' || m.status === 'failed' || m.status === 'stopped');
    const pending = (m.notes || []).filter((n) => !n.delivered);
    clear(notesEl);
    for (const n of pending) notesEl.appendChild(h('div.dr-note', null, h('span.micro', null, 'queued note'), h('span', null, truncate(n.text, 160))));
  }

  function show(t) {
    active = t;
    tabBtns.forEach((b, i) => b.setAttribute('aria-selected', String(TABS[i] === t)));
    if (stream && t !== 'stream') { stream.destroy(); stream = null; }
    clear(body);
    if (t === 'plan') renderPlan();
    else if (t === 'stream') { stream = createStream({ mission: id, compact: true, title: 'This mission' }); body.appendChild(stream.el); }
    else if (t === 'files') renderFiles();
    else renderReport();
  }

  // ---------- plan ----------
  function renderPlan() {
    const m = M(); if (!m) return;
    clear(body);
    const wrap = h('div.dr-pane.scroll');
    wrap.appendChild(h('div.dr-task', null, h('div.micro', null, 'task'), h('div.dr-prompt', null, m.prompt || '')));
    if (m.status === 'awaiting_approval') {
      if (!planDirty || !draft) draft = (m.plan || []).map((s) => ({ id: s.id, text: s.text || '' }));
      const list = h('ol.plan-edit');
      const renderRows = () => {
        clear(list);
        draft.forEach((s, i) => {
          const input = h('input.input.plan-input', { value: s.text, 'aria-label': `Step ${i + 1}`, oninput: () => { s.text = input.value; planDirty = true; } });
          const li = h('li.plan-row', { draggable: 'true' },
            h('span.plan-grip', { title: 'Drag to reorder' }, icon('grip', 14)), h('span.plan-num.mono', null, String(i + 1)), input,
            h('button.btn.ghost.sm.icon', { title: 'Move up', disabled: i === 0, onclick: () => move(i, -1) }, icon('chev-up', 14)),
            h('button.btn.ghost.sm.icon', { title: 'Move down', disabled: i === draft.length - 1, onclick: () => move(i, 1) }, icon('chev-down', 14)),
            h('button.btn.ghost.sm.icon', { title: 'Remove step', onclick: () => { draft.splice(i, 1); planDirty = true; renderRows(); } }, icon('x', 14)));
          li.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(i)); li.classList.add('dragging'); });
          li.addEventListener('dragend', () => li.classList.remove('dragging'));
          li.addEventListener('dragover', (e) => { e.preventDefault(); li.classList.add('over'); });
          li.addEventListener('dragleave', () => li.classList.remove('over'));
          li.addEventListener('drop', (e) => {
            e.preventDefault(); li.classList.remove('over');
            const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
            if (Number.isInteger(from) && from !== i) { const [x] = draft.splice(from, 1); draft.splice(i, 0, x); planDirty = true; renderRows(); }
          });
          list.appendChild(li);
        });
      };
      const move = (i, d) => { const j = i + d; if (j < 0 || j >= draft.length) return; [draft[i], draft[j]] = [draft[j], draft[i]]; planDirty = true; renderRows(); };
      renderRows();
      const approve = h('button.btn.brand', { onclick: async () => {
        const plan = draft.filter((s) => s.text.trim()).map((s, i) => ({ id: s.id || `p${i + 1}`, text: s.text.trim(), status: 'todo' }));
        approve.disabled = true;
        try { const r = await post(`/api/missions/${enc(id)}/approve`, { plan }); upsert('missions', r); planDirty = false; toast('Plan approved', { sub: 'The run is starting.', tone: 'mint', timeout: 3000 }); }
        catch (e) { toast('Could not approve', { sub: e.message, tone: 'coral' }); }
        finally { approve.disabled = false; }
      } }, icon('check', 15), 'Approve and start');
      wrap.append(h('div.plan-banner', null, h('span.display.italic', null, 'A plan, waiting for your nod.'), h('span.dim', null, 'Edit, reorder, add or remove steps, then approve.')),
        list,
        h('div.plan-tools', null,
          h('button.btn.sm', { onclick: () => { draft.push({ id: '', text: '' }); planDirty = true; renderRows(); list.lastChild && list.lastChild.querySelector('input').focus(); } }, icon('plus', 13), 'Add step'),
          h('button.btn.ghost.sm', { onclick: () => { planDirty = false; renderPlan(); } }, 'Reset'),
          h('span.spacer'), approve));
    } else {
      const plan = m.plan || [];
      if (!plan.length) wrap.appendChild(h('div.empty-note', null, 'No plan yet. If the agent keeps a todo list, it shows up here.'));
      else wrap.appendChild(h('ol.plan-view', null, plan.map((s) => h('li', { class: 'plan-step ' + (s.status || 'todo') },
        h('span.plan-state', null, s.status === 'done' ? icon('check', 12, 2.4) : s.status === 'skipped' ? icon('x', 11, 2) : null),
        h('span.plan-text', null, s.text || ''), h('span.micro', null, s.status || 'todo')))));
    }
    body.appendChild(wrap);
  }

  // ---------- files ----------
  function renderFiles() {
    const listEl = h('div.files-list.scroll');
    const viewEl = h('div.files-view.scroll', null, h('div.empty-note', null, 'Pick a file to read it here.'));
    const countEl = h('span.micro');
    const pane = h('div.files', null,
      h('div.files-side', null, h('div.files-head', null, h('span.micro', null, 'workspace'), countEl, h('span.spacer'),
        h('button.btn.ghost.sm.icon', { title: 'Refresh', onclick: load }, icon('refresh', 14))), listEl),
      viewEl);
    body.appendChild(pane);
    let selected = null;
    async function load() {
      clear(listEl).appendChild(h('div.empty-note', null, 'Reading the workspace…'));
      try {
        const r = await get(`/api/missions/${enc(id)}/files`);
        const files = ((r && r.files) || []).slice().sort((a, b) => a.path.localeCompare(b.path));
        clear(listEl);
        setText(countEl, files.length ? `${files.length}` : '');
        if (!files.length) listEl.appendChild(h('div.empty-note', null, 'The workspace is empty so far.'));
        for (const f of files) {
          const depth = f.path.split('/').length - 1;
          const btn = h('button', { class: 'file-item' + (f.path === selected ? ' on' : ''), style: { paddingLeft: 10 + depth * 12 + 'px' }, title: f.path, onclick: () => openFile(f.path, btn) },
            icon('file', 13), h('span.ellipsis', null, f.path.split('/').pop()), h('span.file-size.mono', null, fmtBytes(f.size)));
          listEl.appendChild(btn);
        }
      } catch (e) { clear(listEl).appendChild(h('div.empty-note', null, `Could not list files: ${e.message}`)); }
    }
    async function openFile(path, btn) {
      selected = path;
      listEl.querySelectorAll('.file-item.on').forEach((x) => x.classList.remove('on'));
      btn.classList.add('on');
      clear(viewEl).appendChild(h('div.empty-note', null, 'Opening…'));
      try {
        const f = await get(`/api/missions/${enc(id)}/file?path=${enc(path)}`);
        clear(viewEl);
        viewEl.appendChild(h('div.file-head', null, h('span.mono.ellipsis', null, path), h('span.micro', null, f.size != null ? fmtBytes(f.size) : ''), f.truncated ? h('span.chip.mono', null, 'truncated') : null));
        if (f.binary) viewEl.appendChild(h('div.empty-note', null, 'Binary file. Nothing to show as text.'));
        else if (/\.(md|markdown)$/i.test(path)) viewEl.appendChild(md(f.text || '', 'file-md'));
        else viewEl.appendChild(h('pre.file-text', null, f.text || ''));
      } catch (e) { clear(viewEl).appendChild(h('div.empty-note', null, `Could not open: ${e.message}`)); }
    }
    load();
  }

  // ---------- report ----------
  function renderReport() {
    const m = M(); if (!m) return;
    clear(body);
    const s = m.stats || {};
    const wrap = h('div.dr-pane.scroll');
    if (m.result_text) wrap.appendChild(h('div.report-card', null, h('div.micro', null, 'result'), md(m.result_text)));
    if (m.error) wrap.appendChild(h('div.report-err', null, h('div.micro', null, 'error'), h('div', null, m.error)));
    if (!m.result_text && !m.error) wrap.appendChild(h('div.empty-note', null, tone(m.status) === 'running' ? 'Still working. The report lands here when the run ends.' : 'No report yet.'));
    const cell = (k, v) => h('div.stat-cell', null, h('div.display.italic.stat-n', null, v), h('div.micro', null, k));
    wrap.appendChild(h('div.stat-grid', null,
      cell('turns', fmtNum(s.turns)), cell('steps', fmtNum(s.steps)), cell('tool calls', fmtNum(s.tool_calls)), cell('tool errors', fmtNum(s.tool_errors)),
      cell('prompt tok', fmtNum(s.prompt_tokens)), cell('output tok', fmtNum(s.completion_tokens)), cell('runs', String(m.runs || 0)), cell('elapsed', fmtDur(elapsed(m), true))));
    wrap.appendChild(h('div.dr-times.mono.faint', null,
      `created ${fmtDate(m.created_ts)}`, m.started_ts ? ` · started ${fmtDate(m.started_ts)}` : '', m.ended_ts ? ` · ended ${fmtDate(m.ended_ts)}` : '',
      m.session_id ? ` · session ${m.session_id}` : ''));
    body.appendChild(wrap);
  }

  // ---------- actions ----------
  async function act(what, payload = {}) {
    try { const r = await post(`/api/missions/${enc(id)}/${what}`, payload); if (r && r.id) upsert('missions', r); return r; }
    catch (e) { toast(`Could not ${what}`, { sub: e.message, tone: 'coral' }); return null; }
  }
  async function sendNote() {
    const text = steer.value.trim();
    if (!text) { steer.focus(); return; }
    const r = await act('note', { text, interrupt: interrupt.checked });
    if (r) {
      toast(interrupt.checked ? 'Interrupting with your note' : 'Note queued', { sub: interrupt.checked ? 'The run restarts with it now.' : 'It joins the next run on this session.', tone: 'sun', timeout: 3500 });
      steer.value = ''; interrupt.checked = false; fitSteer();
    }
  }
  async function resume() {
    const text = steer.value.trim();
    const r = await act('resume', text ? { text } : {});
    if (r) { steer.value = ''; fitSteer(); toast('Back on the floor', { tone: 'mint', timeout: 3000 }); }
  }
  async function delMission() {
    if (Date.now() - delArmed > 3000) {
      delArmed = Date.now(); clear(delBtn).append(icon('trash', 13), 'Really delete?'); delBtn.classList.add('armed');
      setTimeout(() => { if (Date.now() - delArmed >= 3000) { clear(delBtn).append(icon('trash', 13), 'Delete'); delBtn.classList.remove('armed'); } }, 3100);
      return;
    }
    try { await del(`/api/missions/${enc(id)}`); remove('missions', id); close(); toast('Mission deleted', { timeout: 2500 }); }
    catch (e) { toast('Could not delete', { sub: e.message, tone: 'coral' }); }
  }

  // ---------- lifecycle ----------
  const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.modal')) close(); };
  addEventListener('keydown', onKey);
  const unsub = subscribe('missions', ({ mission, id: rid, removed }) => {
    if (removed && rid === id) { close(); return; }
    if (!mission || mission.id !== id) return;
    paintHead();
    if (active === 'plan' && !planDirty && !drawer.contains(document.activeElement && document.activeElement.closest('.plan-edit'))) renderPlan();
    if (active === 'report') renderReport();
  });
  function close(instant) {
    if (!current || current.id !== id) return;
    current = null;
    unsub(); removeEventListener('keydown', onKey);
    if (stream) stream.destroy();
    overlay.remove();
    if (instant) { drawer.remove(); return; }
    drawer.classList.remove('in');
    setTimeout(() => drawer.remove(), 260);
  }
  current = { id, close };
  paintHead();
  show(tab);
  return current;
}
