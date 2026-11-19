// Your profile (PROTOCOL.md section 18): who you are, and the keys that unlock the box's tools (GitHub for the
// library and research sync, web search engines, Hugging Face). A key is sent to the box once, over the app's link,
// and never comes back: the panel only ever sees "set" and its last four characters. The box keeps keys in one file
// only the hub can read, puts them in its own environment, and never writes them into reports, exports or GitHub.
import { h, icon, clear, setText } from './dom.js';
import { get, api } from './api.js';
import { toast } from './notify.js';

const put = (p, b) => api('PUT', p, b);
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const KEYS = [
  { field: 'git_token', name: 'GitHub', secret: 'SABHA_GIT_TOKEN', hint: 'github_pat_…  (fine-grained, one repository)',
    what: 'Keeps every finished report in your repository, so a new box still has your library and your default models. Research runs sync there too.' },
  { field: 'tavily_key', name: 'Tavily', secret: 'TAVILY_API_KEY', hint: 'tvly-…', what: 'Better web search for the agents (free tier at tavily.com). Without a key they use free engines.' },
  { field: 'hf_token', name: 'Hugging Face', secret: 'HF_TOKEN', hint: 'hf_…  (read access is enough)', what: 'Lets Nirmana load gated or private models.' },
  { field: 'brave_key', name: 'Brave Search', secret: 'BRAVE_API_KEY', hint: 'BSA…', what: 'Another web search engine for the agents, used when no Tavily key is set.' },
  { field: 'serper_key', name: 'Serper (Google)', secret: 'SERPER_API_KEY', hint: 'your Serper key', what: 'Google results for the agents, used when neither Tavily nor Brave is set.' },
];
export const initials = (name) => (String(name || '').trim().split(/\s+/).map((w) => w[0]).join('').slice(0, 2) || '').toUpperCase();

let cur = null;
export function openProfile(onChange) {
  if (cur) return;
  let s = null;
  const close = () => {
    if (!cur) return;
    removeEventListener('keydown', onKey);
    panel.classList.remove('open'); scrim.classList.remove('open');
    setTimeout(() => { panel.remove(); scrim.remove(); }, reduced() ? 0 : 320);
    cur = null;
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const avatar = h('div.pf-avatar');
  const nameIn = h('input.input.pf-name', { placeholder: 'Your name', maxlength: 60, 'aria-label': 'Your name', autocomplete: 'name' });
  const aboutIn = h('input.input', { placeholder: 'What you work on (optional)', maxlength: 240, 'aria-label': 'About you' });
  const saveProfile = async () => {
    try { s = await put('/api/settings', { name: nameIn.value, about: aboutIn.value }); paintHead(); onChange && onChange(s); }
    catch (e) { toast('Not saved', { sub: e.message, tone: 'coral' }); }
  };
  nameIn.addEventListener('change', saveProfile);
  aboutIn.addEventListener('change', saveProfile);
  nameIn.addEventListener('input', () => setText(avatar, initials(nameIn.value) || '?'));
  const keysEl = h('div.pf-keys');
  const checkEl = h('div.pf-check', { hidden: true });
  const body = h('div.pf-body.scroll', null,
    h('section.pf-me', null, avatar, h('div.pf-me-fields', null, nameIn, aboutIn)),
    h('section.pf-sec', null, h('h3', null, 'Keys'), h('p.pf-lede', null, 'Give a key once and the box takes care of the rest: the agents, the library and Nirmana use it on their own.'), keysEl, checkEl),
    h('section.pf-sec.pf-safe', null, h('h3', null, icon('check', 15), 'How your keys are kept'),
      h('ul', null,
        h('li', null, 'A key goes to the box once, over this private link, and never comes back to any browser. This page only sees ', h('b', null, 'set'), ' and the last four characters.'),
        h('li', null, 'On the box it lives in one file only the hub can read (settings.json, mode 600) and in the hub\'s own environment.'),
        h('li', null, 'It is never written into reports, notebooks, exports, the library or GitHub. GitHub errors in logs are masked.'),
        h('li', null, 'Anyone with this app\'s link can use the box (not read the keys). Keep the link private; a new link is one run of the notebook cell.'),
        h('li', null, 'The box is wiped when its session ends. For every new box, put the keys in the notebook\'s secrets (names below): they never appear in the notebook itself.')),
      h('div.pf-secrets', null, KEYS.map((k) => h('code', null, k.secret)), h('code', null, 'SABHA_GIT_REPO')),
      h('button.btn.danger.sm.pf-forget', { onclick: (e) => forget(e.currentTarget) }, icon('trash', 14), 'Forget every key on this box')));
  const panel = h('aside.pf', { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Your profile' },
    h('header.pf-head', null, h('h2', null, 'Your profile'), h('button.btn.ghost.icon', { 'aria-label': 'Close', title: 'Close (Esc)', onclick: close }, icon('x', 18))),
    body);
  const scrim = h('div.pf-scrim', { onclick: close });
  document.body.append(scrim, panel);
  addEventListener('keydown', onKey);
  void panel.offsetWidth;
  panel.classList.add('open'); scrim.classList.add('open');
  cur = { close };

  function paintHead() {
    const p = (s && s.profile) || {};
    if (document.activeElement !== nameIn) nameIn.value = p.name || '';
    if (document.activeElement !== aboutIn) aboutIn.value = p.about || '';
    setText(avatar, initials(p.name) || '?');
  }
  function row(k) {
    const st = (s && s[k.field]) || {};
    const keyIn = h('input.input.mono', { type: 'password', placeholder: k.hint, autocomplete: 'off', spellcheck: 'false', 'aria-label': `${k.name} key` });
    const repoIn = k.field === 'git_token' ? h('input.input.mono', { placeholder: 'owner/repository (private)', value: (s && s.git_repo) || '', 'aria-label': 'GitHub repository', autocomplete: 'off' }) : null;
    const save = h('button.btn.brand.sm', { onclick: async () => {
      const b = {};
      if (keyIn.value.trim()) b[k.field] = keyIn.value.trim();
      if (repoIn && repoIn.value.trim() !== ((s && s.git_repo) || '')) b.git_repo = repoIn.value.trim();
      if (!Object.keys(b).length) { toast('Nothing to save', { tone: 'sun' }); return; }
      save.disabled = true;
      try {
        s = await put('/api/settings', b);
        keyIn.value = '';
        toast(`${k.name} saved on the box`, { sub: 'The key is not shown again: only its last four characters.', tone: 'mint' });
        paintKeys();
        if (k.field === 'git_token') check();
        onChange && onChange(s);
      } catch (e) { toast('Not saved', { sub: e.message, tone: 'coral', timeout: 8000 }); save.disabled = false; }
    } }, 'Save');
    const remove = st.source === 'settings' ? h('button.btn.ghost.sm', { title: `Remove the ${k.name} key from this box`, onclick: async () => {
      try { s = await put('/api/settings', { [k.field]: '' }); paintKeys(); toast(`${k.name} key removed`, { tone: 'mint' }); onChange && onChange(s); }
      catch (e) { toast('Not removed', { sub: e.message, tone: 'coral' }); }
    } }, 'Remove') : null;
    const status = st.set ? h('span.pf-pill.on', null, icon('check', 11), `set ${st.hint || ''}`.trim(), st.source === 'environment' ? ' · from a secret' : '')
      : h('span.pf-pill', null, 'not set');
    return h('div.pf-key', null,
      h('div.pf-key-top', null, h('b', null, k.name), status, h('span.spacer'),
        k.field === 'git_token' && st.set ? h('button.btn.ghost.sm', { onclick: () => check() }, 'Check') : null, remove),
      h('p.pf-key-what', null, k.what),
      h('div.pf-key-row', null, repoIn, keyIn, save));
  }
  function paintKeys() { clear(keysEl).append(...KEYS.map(row)); }
  async function check() {
    checkEl.hidden = false;
    clear(checkEl).append(h('div.pf-check-h', null, 'Checking the GitHub token…'));
    try {
      const r = await api('POST', '/api/settings/check-github');
      clear(checkEl).append(
        h('div.pf-check-h' + (r.ok && !r.warnings.length ? '.good' : r.ok ? '.warn' : '.bad'), null,
          r.ok ? `${r.repo} · ${r.private ? 'private' : 'PUBLIC'} · ${r.can_write ? 'can write' : 'read only'} · ${r.kind} token` : r.error),
        r.warnings && r.warnings.length ? h('ul', null, r.warnings.map((w) => h('li', null, w))) : (r.ok ? h('p', null, 'All good: private repository, writable, and the token reaches only what it needs.') : null));
    } catch (e) { clear(checkEl).append(h('div.pf-check-h.bad', null, e.message)); }
  }
  async function forget(btn) {
    if (!btn.classList.contains('armed')) { btn.classList.add('armed'); setText(btn.lastChild, 'Press again to forget every key'); setTimeout(() => { btn.classList.remove('armed'); setText(btn.lastChild, 'Forget every key on this box'); }, 3500); return; }
    try { s = await api('POST', '/api/settings/forget'); paintKeys(); checkEl.hidden = true; toast('Every key on this box is forgotten', { sub: 'Keys in the notebook secrets come back the next time the cell runs.', tone: 'mint', timeout: 6000 }); onChange && onChange(s); }
    catch (e) { toast('Not done', { sub: e.message, tone: 'coral' }); }
  }
  (async () => {
    try { s = await get('/api/settings'); } catch (e) { toast('Settings did not load', { sub: e.message, tone: 'coral' }); }
    paintHead(); paintKeys();
    if (s && s.git_token && s.git_token.set && s.git_repo) check();
    if (!(s && s.profile && s.profile.name)) setTimeout(() => nameIn.focus(), 300);
  })();
}
