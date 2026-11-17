// Research taste + runs started from the app (PROTOCOL.md sections 8 and 9).
// One card at the top of the Research view: the run strip (Report, Finish now, Stop, Download) and the taste form
// (the question, how long: instant or continuous for N hours in waves, your taste, papers you love, optional
// Tavily key). Start builds a mission from it. On a phone this card is the whole "give the agents a task" screen.
import { h, icon, setText, autogrow } from './dom.js';
import { get, post, api, getToken, enc } from './api.js';
import { fmtAgo, fmtDur } from './format.js';
import { toast } from './notify.js';

const put = (p, b) => api('PUT', p, b);

const TASTE_HINT = 'What you care about, what good work looks like to you, what you distrust or skip.\n'
  + 'Example: I care about methods that release code and test on real hardware. I trust ablations over leaderboard wins. '
  + 'Skip surveys and papers that only report one benchmark.';

export function createTaste({ onChange } = {}) {
  let loaded = false, running = false, lastRun = null, open = true, saving = 0;

  // ---------- run strip ----------
  const runTitle = h('div.t-run-title');
  const runSub = h('div.t-run-sub.dim');
  const finishBtn = h('button.btn', { onclick: () => stopRun('finish'), title: 'Write the final reports and the synthesis now, from what is in hand, then end' }, icon('check', 15), 'Finish now');
  const stopBtn = h('button.btn.danger', { onclick: () => stopRun('now'), title: 'Stop at once (what is written stays)' }, icon('x', 15), 'Stop');
  const dlBtn = h('button.btn', { onclick: () => download(), title: 'Everything this run wrote, as one zip' }, icon('down', 15), 'Download results');
  const runStrip = h('div.t-run', { hidden: true }, h('div.t-run-text', null, runTitle, runSub), h('div.t-run-actions', null, finishBtn, stopBtn, dlBtn));

  // ---------- form ----------
  const tasteIn = h('textarea.textarea', { rows: 4, placeholder: TASTE_HINT, 'aria-label': 'Your research taste' });
  const lovesIn = h('textarea.textarea.mono', { rows: 2, placeholder: 'arXiv ids or links, one per line (read first, as examples of good work)', 'aria-label': 'Papers you love' });
  const topicIn = h('textarea.textarea', { rows: 2, placeholder: 'The question for this run. Example: Which world-model designs actually help a policy plan, and what evidence is solid?', 'aria-label': 'The question for this run' });
  let mode = 'continuous';
  const modeBtns = [['instant', 'Instant', 'one pass, about 20 minutes'], ['continuous', 'Continuous', 'hours of work in waves']].map(([k, label, sub]) =>
    h('button.t-mode', { type: 'button', 'aria-pressed': 'false', 'data-mode': k, onclick: () => { mode = k; paintMode(); saveTaste(); } },
      h('span.t-mode-l', null, label), h('span.t-mode-s', null, sub)));
  const modeSeg = h('div.t-modes', { role: 'radiogroup', 'aria-label': 'How long the agents work' }, modeBtns);
  const HOURS = [[1, '1 h'], [2, '2 h'], [4, '4 h'], [6, '6 h'], [8, '8 h'], [0, 'until the session ends']];
  const hoursIn = h('select.select', { 'aria-label': 'Hours' }, HOURS.map(([v, t]) => h('option', { value: String(v) }, t)));
  const waveIn = h('select.select', { 'aria-label': 'Minutes per wave' },
    [[0, 'automatic'], [20, '20 min'], [30, '30 min'], [45, '45 min'], [60, '60 min'], [90, '90 min']].map(([v, t]) => h('option', { value: String(v) }, t)));
  const contRow = h('div.t-row.t-cont', null,
    h('label.field', null, h('span.micro', null, 'how long', h('span.dim', null, ' · a Kaggle session lasts 12 h at most')), hoursIn),
    h('label.field', null, h('span.micro', null, 'minutes per wave', h('span.dim', null, ' · after each wave: chapter, compact, forget, re-plan')), waveIn));
  const modeNote = h('p.t-mode-note.dim');
  const keyIn = h('input.input.mono', { type: 'password', placeholder: 'tvly-…  (optional)', autocomplete: 'off', 'aria-label': 'Tavily key' });
  const keyNote = h('span.micro.dim');
  const startBtn = h('button.btn.brand', { onclick: () => startRun() }, icon('play', 15), 'Start research');
  const saveNote = h('span.micro.dim');
  [tasteIn, lovesIn, topicIn].forEach((t) => autogrow(t, 260));
  for (const t of [tasteIn, lovesIn, topicIn, hoursIn, waveIn]) t.addEventListener('change', () => saveTaste());
  keyIn.addEventListener('change', () => saveKey());

  const lab = (k, sub, el) => h('label.field', null, h('span.micro', null, k, sub ? h('span.dim', null, ' · ' + sub) : null), el);
  const more = h('details.t-more', null, h('summary.micro', null, 'your taste, papers you love, web search key'),
    lab('your research taste', 'every agent reads it: what to choose, how to judge, how to write for you', tasteIn),
    lab('papers you love', 'optional', lovesIn),
    lab('web search key', 'Tavily, free tier at tavily.com; without it the free engines are used', h('div.t-key', null, keyIn, keyNote)));
  const form = h('div.t-form', null,
    lab('the question for this run', null, topicIn),
    h('div.field', null, h('span.micro', null, 'how the agents work'), modeSeg),
    contRow, modeNote, more,
    h('div.t-foot', null, saveNote, h('span.spacer'), startBtn));
  function paintMode() {
    modeBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    contRow.hidden = mode !== 'continuous';
    const hrs = Number(hoursIn.value);
    setText(modeNote, mode === 'instant'
      ? 'Instant: the agents plan two or three tracks, read what fits in about 20 minutes and write the answer.'
      : `Continuous: the agents work ${hrs ? 'for ' + hrs + ' h' : 'until the session ends'} in waves. After every wave they add a chapter to the report, `
        + 'compact what they know, keep lessons in long-term memory, clear their context and re-plan where the open questions are. '
        + 'You get a message after each wave if alerts are on (Crew, Alerts).');
    setText(startBtn.lastChild, mode === 'instant' ? 'Start instant research' : 'Start continuous research');
  }
  hoursIn.addEventListener('change', paintMode);
  const toggleBtn = h('button.btn.ghost.sm', { onclick: () => { open = !open; paint(); } });
  const head = h('div.r-card-head', null, h('h2.r-h2', null, 'Your research'), h('span.spacer'), toggleBtn);
  const el = h('section.card.r-card.t-card', null, head, runStrip, form);

  // ---------- data ----------
  async function load() {
    try {
      const [t, s] = await Promise.all([get('/api/taste'), get('/api/settings')]);
      tasteIn.value = t.taste || ''; topicIn.value = t.topic || ''; lovesIn.value = (t.loves || []).join('\n');
      hoursIn.value = String(HOURS.some(([v]) => v === Number(t.hours)) ? Number(t.hours) : 4);
      waveIn.value = String([0, 20, 30, 45, 60, 90].includes(Number(t.wave_min)) ? Number(t.wave_min) : 0);
      mode = t.mode === 'instant' ? 'instant' : 'continuous';
      paintMode();
      paintKey(s);
      [tasteIn, lovesIn, topicIn].forEach((x) => x.dispatchEvent(new Event('input')));
      loaded = true;
    } catch { /* the hub may be older than this page: leave the form empty */ }
  }
  function body() {
    return { taste: tasteIn.value, topic: topicIn.value, loves: lovesIn.value, mode, hours: Number(hoursIn.value), wave_min: Number(waveIn.value) };
  }
  async function saveTaste() {
    const n = ++saving;
    try {
      const t = await put('/api/taste', body());
      if (n === saving) { lovesIn.value = (t.loves || []).join('\n'); setText(saveNote, 'saved on the box'); }
    } catch (e) { setText(saveNote, `not saved: ${e.message}`); }
  }
  function paintKey(s) {
    const k = s && s.tavily_key;
    setText(keyNote, k && k.set ? `set ${k.hint || ''}${k.source === 'environment' ? ' (from a secret)' : ''}` : `web search: ${(s && s.search) || 'free engines'}`);
  }
  async function saveKey() {
    try { paintKey(await put('/api/settings', { tavily_key: keyIn.value.trim() })); keyIn.value = ''; toast('Web search key saved on the box', { tone: 'mint', timeout: 3000 }); }
    catch (e) { toast('Key not saved', { sub: e.message, tone: 'coral' }); }
  }
  async function startRun() {
    if (topicIn.value.trim().length < 12) { toast('Write the question for this run first', { tone: 'sun' }); topicIn.focus(); return; }
    startBtn.disabled = true;
    try {
      const r = await post('/api/research/start', body());
      const how = r.mode === 'instant' ? 'instant, about 20 min' : r.wave_min ? `${Number(r.hours).toFixed(1)} h in ${r.wave_min}-minute waves` : `${Number(r.hours).toFixed(1)} h`;
      toast('Research started', { sub: `${r.title || r.mission} · ${how}${r.note ? ' · ' + r.note : ''}. The agents plan first, then read.`, tone: 'mint', timeout: 6000 });
      open = false;
      onChange && onChange();
    } catch (e) { toast('Research did not start', { sub: e.message, tone: 'coral', timeout: 8000 }); }
    finally { startBtn.disabled = running; }
  }
  async function stopRun(mode) {
    const btn = mode === 'finish' ? finishBtn : stopBtn;
    if (!btn.classList.contains('armed')) { btn.classList.add('armed'); setTimeout(() => btn.classList.remove('armed'), 3000); toast(mode === 'finish' ? 'Press Finish now again to confirm' : 'Press Stop again to confirm', { timeout: 2500 }); return; }
    btn.classList.remove('armed');
    try {
      await post('/api/research/stop', { mode });
      toast(mode === 'finish' ? 'Finishing: final reports and synthesis now' : 'Research stopped', { sub: 'Everything written so far stays. Download it before the session ends.', tone: 'mint' });
      onChange && onChange();
    } catch (e) { toast('Did not stop', { sub: e.message, tone: 'coral' }); }
  }
  function download(mid) {
    const id = mid || (lastRun && lastRun.mission);
    if (!id) { toast('No research run yet', { tone: 'sun' }); return; }
    const a = h('a', { href: `/api/research/export/${enc(id)}?token=${enc(getToken() || '')}`, download: `sabha-research-${id}.zip` });
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ---------- paint ----------
  function paint() {
    form.hidden = !open;
    toggleBtn.textContent = open ? 'Hide' : (running ? 'Your taste' : 'New run');
    startBtn.disabled = running;
    startBtn.title = running ? 'One run at a time: finish or stop the current one first' : '';
  }
  function update(d) {
    if (!loaded) load();
    const run = d && d.run;
    lastRun = run && run.mission ? run : lastRun;
    const was = running;
    running = !!(run && run.running);
    if (running !== was) open = !running;
    runStrip.hidden = !(run && run.mission);
    if (run && run.mission) {
      const title = (d.missions || []).find((x) => x.id === run.mission);
      setText(runTitle, (running ? (run.finishing ? 'Finishing · ' : 'Running · ') : 'Last run · ') + ((title && title.title) || run.mission));
      const now = Date.now() / 1000;
      const w = (d.mission && d.mission.waves) || {};
      const waveBit = w.on ? ` · wave ${w.n}${w.closed ? ' (last)' : ''}` : run.mode === 'instant' ? ' · instant' : '';
      setText(runSub, running
        ? `started ${fmtAgo(run.started)}${waveBit} · ends in about ${fmtDur(Math.max(run.until - now, 0), true)}${run.git_sync ? ' · git sync on' : ' · results stay on the box: download them'}${run.mock ? ' · demo' : ''}`
        : `${run.how || 'ended'} ${run.ended ? fmtAgo(run.ended) : ''} · download the results before the session ends`);
      finishBtn.hidden = stopBtn.hidden = !running;
    }
    paint();
  }
  paint();
  paintMode();
  return { el, update, download };
}
