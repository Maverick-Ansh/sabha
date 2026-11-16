// Research taste + runs started from the app (PROTOCOL.md sections 8 and 9).
// One card at the top of the Research view: the run strip (Finish now, Stop, Download) and the taste form
// (your taste, papers you love, the question, hours, optional Tavily key). Start builds a mission from it.
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
  const hoursIn = h('input.input', { type: 'number', min: '0.5', max: '12', step: '0.5', value: '6', 'aria-label': 'Hours' });
  const keyIn = h('input.input.mono', { type: 'password', placeholder: 'tvly-…  (optional)', autocomplete: 'off', 'aria-label': 'Tavily key' });
  const keyNote = h('span.micro.dim');
  const startBtn = h('button.btn.brand', { onclick: () => startRun() }, icon('play', 15), 'Start research');
  const saveNote = h('span.micro.dim');
  [tasteIn, lovesIn, topicIn].forEach((t) => autogrow(t, 260));
  for (const t of [tasteIn, lovesIn, topicIn, hoursIn]) t.addEventListener('change', () => saveTaste());
  keyIn.addEventListener('change', () => saveKey());

  const lab = (k, sub, el) => h('label.field', null, h('span.micro', null, k, sub ? h('span.dim', null, ' · ' + sub) : null), el);
  const form = h('div.t-form', null,
    lab('your research taste', 'every agent reads it: what to choose, how to judge, how to write for you', tasteIn),
    lab('papers you love', 'optional', lovesIn),
    lab('the question for this run', null, topicIn),
    h('div.t-row', null,
      lab('hours', 'a Kaggle session lasts 12 h at most', hoursIn),
      lab('web search key', 'Tavily, free tier at tavily.com; without it the free engines are used', h('div.t-key', null, keyIn, keyNote))),
    h('div.t-foot', null, saveNote, h('span.spacer'), startBtn));
  const toggleBtn = h('button.btn.ghost.sm', { onclick: () => { open = !open; paint(); } });
  const head = h('div.r-card-head', null, h('h2.r-h2', null, 'Your research'), h('span.spacer'), toggleBtn);
  const el = h('section.card.r-card.t-card', null, head, runStrip, form);

  // ---------- data ----------
  async function load() {
    try {
      const [t, s] = await Promise.all([get('/api/taste'), get('/api/settings')]);
      tasteIn.value = t.taste || ''; topicIn.value = t.topic || ''; lovesIn.value = (t.loves || []).join('\n');
      hoursIn.value = String(t.hours || 6);
      paintKey(s);
      [tasteIn, lovesIn, topicIn].forEach((x) => x.dispatchEvent(new Event('input')));
      loaded = true;
    } catch { /* the hub may be older than this page: leave the form empty */ }
  }
  function body() {
    return { taste: tasteIn.value, topic: topicIn.value, loves: lovesIn.value, hours: Number(hoursIn.value) || 6 };
  }
  async function saveTaste() {
    const n = ++saving;
    try {
      const t = await put('/api/taste', body());
      if (n === saving) { lovesIn.value = (t.loves || []).join('\n'); hoursIn.value = String(t.hours); setText(saveNote, 'saved on the box'); }
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
      toast('Research started', { sub: `${r.title || r.mission} · ${r.hours} h${r.note ? ' · ' + r.note : ''}. The agents plan first, then read.`, tone: 'mint', timeout: 6000 });
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
      setText(runSub, running
        ? `started ${fmtAgo(run.started)} · ends in about ${fmtDur(Math.max(run.until - now, 0), true)}${run.git_sync ? ' · git sync on' : ' · results stay on the box: download them'}${run.mock ? ' · demo' : ''}`
        : `${run.how || 'ended'} ${run.ended ? fmtAgo(run.ended) : ''} · download the results before the session ends`);
      finishBtn.hidden = stopBtn.hidden = !running;
    }
    paint();
  }
  paint();
  return { el, update, download };
}
