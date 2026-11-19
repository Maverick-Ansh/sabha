// The research library (PROTOCOL.md section 17): every research this box ran, and with GitHub connected every
// finished report from earlier boxes too. A shelf of covers; a cover opens the report in the reader (a run on
// this box) or as its saved page (a report kept in GitHub). Finished reports are kept the moment the reader shows
// them, so nothing needs saving by hand.
import { h, icon, clear, setText } from './dom.js';
import { get, api, getToken, enc } from './api.js';
import { toast } from './notify.js';

const put = (p, b) => api('PUT', p, b);
const PAIRS = [['sky', 'lilac'], ['lilac', 'rose'], ['mint', 'sky'], ['sun', 'rose'], ['rose', 'lilac'], ['mint', 'lilac'], ['coral', 'sun'], ['sky', 'mint']];
const hash = (s) => { let x = 7; for (const c of String(s)) x = (x * 31 + c.charCodeAt(0)) >>> 0; return x; };
const day = (s) => { const t = Date.parse(s || ''); return Number.isFinite(t) ? new Date(t).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : ''; };

// opts: { onOpen(entry) } opens a run that lives on this box
export function createLibrary(opts = {}) {
  let data = null, busy = false;
  const count = h('span.lb-count');
  const ghChip = h('span.lb-gh');
  const grid = h('div.lb-grid');
  const connect = h('div.lb-connect', { hidden: true });
  const el = h('section.lb', null,
    h('div.lb-head', null, h('div', null, h('h2.r-h2', null, 'Library'), h('div.lb-sub', null, 'Every research you ran. Finished reports are kept as pages that look exactly like the reader.')),
      h('span.spacer'), ghChip, count),
    grid, connect);

  function cover(e) {
    const [a, b] = PAIRS[hash(e.lid) % PAIRS.length];
    const st = e.live_mission && e.status === 'running' ? 'running' : '';
    const badges = [
      st ? h('span.lb-badge.run', null, h('i'), 'running') : null,
      e.in_github ? h('span.lb-badge.gh', { title: 'Kept in GitHub: it opens on any box' }, icon('check', 11), 'GitHub') : null,
      e.on_box ? h('span.lb-badge', { title: 'On this box' }, 'this box') : null,
      e.on_box && !e.saved_at && !st ? h('span.lb-badge.new', { title: 'Open its report once to keep it' }, 'not kept yet') : null,
    ];
    const when = day(e.started || e.saved_at);
    const stats = [e.papers ? `${e.papers} papers` : null, e.waves ? `${e.waves} ${e.waves === 1 ? 'wave' : 'waves'}` : null, e.hours ? `${Number(e.hours) >= 1 ? Number(e.hours).toFixed(Number(e.hours) % 1 ? 1 : 0) + ' h' : Math.round(Number(e.hours) * 60) + ' min'}` : null].filter(Boolean);
    return h('button.lb-card', { type: 'button', style: { '--ca': `var(--${a})`, '--cb': `var(--${b})` }, onclick: () => open(e), title: e.question || e.title },
      h('div.lb-cover', { 'aria-hidden': 'true' }, h('i.o1'), h('i.o2'), h('span.lb-mono', null, (e.title || '?').trim()[0].toUpperCase())),
      h('div.lb-body', null,
        h('div.lb-when', null, when || 'undated', e.final === false && !st ? ' · no finished report' : ''),
        h('div.lb-title', null, e.title || e.mission || e.lid),
        e.one_line || e.question ? h('div.lb-line', null, e.one_line || e.question) : null,
        h('div.lb-foot', null, stats.length ? h('span.lb-stats', null, stats.join(' · ')) : null, h('span.spacer'), badges)));
  }

  function open(e) {
    if (e.live_mission && opts.onOpen) { opts.onOpen(e); return; }
    window.open(`/api/library/${enc(e.lid)}/html?token=${enc(getToken() || '')}`, '_blank', 'noopener');
  }

  function paintConnect(gh) {
    clear(connect);
    connect.hidden = gh.on;
    if (gh.on) return;
    const repo = h('input.input.mono', { placeholder: 'owner/repo, e.g. you/sabha-research', value: gh.repo || '', 'aria-label': 'GitHub repository', autocomplete: 'off' });
    const tok = h('input.input.mono', { type: 'password', placeholder: 'GitHub token with Contents: read and write', 'aria-label': 'GitHub token', autocomplete: 'off' });
    const go = h('button.btn.brand', { onclick: async () => {
      if (!repo.value.trim() || !tok.value.trim()) { toast('Give the repository and a token', { tone: 'sun' }); return; }
      go.disabled = true;
      try {
        await put('/api/settings', { git_repo: repo.value.trim(), git_token: tok.value.trim() });
        tok.value = '';
        toast('GitHub connected', { sub: 'Finished reports are kept there from now on. Reports already on this box go up the next time you open them.', tone: 'mint', timeout: 6000 });
        load();
      } catch (e) { toast('Not connected', { sub: e.message, tone: 'coral', timeout: 8000 }); }
      finally { go.disabled = false; }
    } }, icon('check', 14), 'Connect');
    connect.append(
      h('div.lb-connect-text', null, h('b', null, 'Keep reports past this box. '),
        'A Kaggle or Colab box is wiped when its session ends. Connect a private GitHub repository and every finished report is kept there; '
        + 'the next box lists them here and opens any of them. A fine-grained token with only this repository and Contents: read and write is enough. '
        + 'You can also set SABHA_GIT_REPO and SABHA_GIT_TOKEN as notebook secrets.'),
      h('div.lb-connect-row', null, repo, tok, go));
  }

  async function load() {
    if (busy) return;
    busy = true;
    try {
      data = await get('/api/library');
      const rows = data.entries || [];
      setText(count, `${rows.length} ${rows.length === 1 ? 'research' : 'researches'}`);
      const gh = data.github || {};
      clear(ghChip).append(gh.on ? h('span.lb-gh-on', { title: gh.error || `Kept in ${gh.repo}` }, h('i', { class: gh.error ? 'warn' : '' }), `GitHub · ${gh.repo}`) : '');
      clear(grid);
      rows.forEach((e, i) => { const c = cover(e); c.style.animationDelay = `${Math.min(i, 8) * 40}ms`; grid.appendChild(c); });
      if (!rows.length) grid.appendChild(h('div.lb-empty', null, h('div.lb-empty-mark', null, icon('layers', 22)), h('p', null, 'No research yet. Start one and it lands here, with its report.')));
      paintConnect(gh);
    } catch (e) {
      clear(grid).appendChild(h('div.lb-empty', null, h('p.dim', null, `The library did not load: ${e.message}`)));
    } finally { busy = false; }
  }

  return { el, load, get size() { return data && data.entries ? data.entries.length : 0; } };
}

// The reader calls this when it shows a finished report: keep it in the library (and GitHub). Quiet unless new.
export async function keepReport(mission, html) {
  try {
    const r = await api('POST', '/api/library/save', { mission, html });
    return r;
  } catch { return null; }
}
