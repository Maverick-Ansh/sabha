// Nirmana: the workshop. Models on the box as instruments (load, remove, context window, live use, try),
// and flows: stages chained into a pipeline whose every step is shown while it runs (PROTOCOL.md section 15).
import { h, icon, setText, clear, autogrow, append } from './dom.js';
const fill = (el, ...kids) => append(clear(el), kids);  // like el.append, but skips null (native append prints "null")
import { get, post, put, del, api } from './api.js';
import { subscribe, getState } from './store.js';
import { fmtNum, fmtAgo, fmtDur, truncate } from './format.js';
import { renderInto } from './md.js';
import { toast } from './notify.js';
import { nav } from './nav.js';

const HUES = ['lilac', 'sky', 'mint', 'sun', 'coral', 'rose'];
const CTX_STEPS = [2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072];
const KIND = {
  decide: { label: 'Decide', ic: 'gate', tip: 'a model reads the text and picks a branch' },
  think: { label: 'Think', ic: 'spark', tip: 'a model does the work you describe' },
  agent: { label: 'Agent', ic: 'crew', tip: 'a crew agent takes it as a real mission, with tools' },
  search: { label: 'Search', ic: 'search', tip: 'a web search for the text' },
  read: { label: 'Read', ic: 'file', tip: 'reads the first link in the text' },
};
const STATUS = { ready: 'ready', starting: 'warming up', pending: 'waiting its turn', downloading: 'downloading', failed: 'failed', demo: 'demo' };
const gb = (x) => `${Number(x || 0).toFixed(x >= 10 ? 0 : 1)} GB`;
const tok = (n) => (n >= 1024 ? `${Math.round(n / 1024)}k` : String(n));
const snap = (v, max) => { let best = CTX_STEPS[0]; for (const c of CTX_STEPS) if (c <= max && Math.abs(c - v) <= Math.abs(best - v)) best = c; return best; };
const hueOf = (role, i) => ({ deep: 'lilac', reader: 'sky' }[role] || HUES[(i + 2) % HUES.length]);

export function createNirmana() {
  let pane = 'models', active = false, timer = 0, forge = null;
  const el = h('section.view.nirmana');

  // ---------- header ----------
  const segBtns = ['models', 'flows'].map((p) => h('button', { 'aria-pressed': String(p === pane), onclick: () => setPane(p) }, p === 'models' ? 'Models' : 'Flows'));
  const head = h('header.nm-head', null,
    h('div.nm-title', null,
      h('h1', null, 'Nirmana'),
      h('p', null, 'The workshop. Put models on the box, shape them, chain them, and watch every step of the work.')),
    h('div.seg.nm-seg', { role: 'tablist' }, segBtns));

  const modelsEl = h('div.nm-pane.nm-models');
  const flowsEl = h('div.nm-pane.nm-flows', { hidden: true });
  el.append(h('div.nm-scroll.scroll', null, head, modelsEl, flowsEl));

  function setPane(p) {
    pane = p;
    segBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(['models', 'flows'][i] === p)));
    modelsEl.hidden = p !== 'models';
    flowsEl.hidden = p !== 'flows';
    if (p === 'flows') flows.show();
  }

  // =====================================================================================  MODELS
  const vesselsEl = h('div.nm-vessels');
  const gridEl = h('div.nm-grid');
  const byEl = h('p.nm-by');
  const defaultEl = h('div.nm-default');
  modelsEl.append(
    h('section.nm-sec', null, h('div.nm-sec-h', null, h('h2', null, 'The box'), byEl), vesselsEl, defaultEl),
    h('section.nm-sec', null, h('div.nm-sec-h', null, h('h2', null, 'Instruments'),
      h('button.btn.brand.sm', { onclick: () => openLoad() }, icon('plus', 14), 'Load a model')), gridEl));
  const cards = new Map(); // role -> card api

  async function refresh() {
    try { forge = await get('/api/nirmana/forge'); } catch (e) { if (!forge) clear(vesselsEl).append(h('div.empty-note', null, e.message)); return; }
    paintVessels();
    paintGrid();
    flows.models(forge);
    if (!dflt) loadDefault(); else paintDefault();
  }

  // ---------- the default setup for new boxes (Sabha's preset until you make one) ----------
  let dflt = null;
  const short = (hf) => String(hf || '').split('/').pop();
  async function loadDefault() {
    try { dflt = await get('/api/nirmana/default'); paintDefault(); } catch { /* an older hub */ }
  }
  function paintDefault() {
    if (!dflt || !forge) return;
    const now = (forge.servers || []).map((x) => `${x.role}=${x.hf || ''}`).sort().join('|');
    const want = (dflt.servers || []).map((x) => `${x.role}=${x.hf || ''}`).sort().join('|');
    const same = !!now && now === want;
    const btn = h('button.btn.sm' + (same ? '' : '.brand'), { disabled: same || !(forge.servers || []).length, onclick: async () => {
      btn.disabled = true;
      try {
        dflt = await api('POST', '/api/nirmana/default');
        toast('Default saved', { sub: dflt.in_github ? 'Every new box starts with this setup (kept in your GitHub repository).'
          : (dflt.error || 'Kept on this box. Connect GitHub in your profile so a new box starts with it too.'), tone: dflt.error ? 'sun' : 'mint', timeout: 7000 });
        paintDefault();
      } catch (e) { toast('Not saved', { sub: e.message, tone: 'coral' }); btn.disabled = false; }
    } }, same ? 'This is the default' : 'Make this setup the default');
    clear(defaultEl).append(
      h('div.nm-default-text', null,
        h('span.micro', null, 'new boxes start with'),
        h('div.nm-default-list', null, (dflt.servers || []).map((x) => h('span.nm-default-chip', null, h('b', null, x.role), ' ', x.label || short(x.hf)))),
        h('span.nm-default-src', null, dflt.mine ? `your default${dflt.source ? ' · ' + dflt.source : ''}` : "Sabha's default")),
      btn);
  }
  function loop() { clearTimeout(timer); if (!active) return; refresh().finally(() => { timer = setTimeout(loop, document.hidden ? 9000 : 3000); }); }

  function paintVessels() {
    setText(byEl, forge.by ? forge.by.replace(/^Nirmana: /, 'Last change: ') : '');
    clear(vesselsEl);
    if (!forge.gpus.length) { vesselsEl.append(h('div.empty-note', null, 'No GPU on this box. Models need a GPU session (Kaggle: GPU T4 x2).')); return; }
    for (const g of forge.gpus) {
      const totalGb = g.mem_mb / 1024;
      const usedGb = (g.mem_used_mb || 0) / 1024;
      const on = forge.servers.map((s, i) => [s, i]).filter(([s]) => (s.gpus || []).includes(g.index));
      const bands = on.map(([s, i]) => {
        const share = Number(s.util || 0);
        const w = Math.min(s.weights_gib / (s.tp || 1) / totalGb, share);
        return h('div.nm-band', { class: `hue-${hueOf(s.role, i)} st-${s.status}`, style: { flexGrow: String(Math.max(share, 0.04)) }, title: `${s.label}: ${Math.round(share * 100)}% of this GPU` },
          h('div.nm-band-w', { style: { height: `${Math.round((w / Math.max(share, 0.01)) * 100)}%` } }),
          h('span.nm-band-l', null, s.label));
      });
      const free = Math.max(0.92 - (g.share || 0), 0);
      if (free > 0.05) bands.push(h('div.nm-band.free', { style: { flexGrow: String(free) } }, h('span.nm-band-l', null, 'free')));
      vesselsEl.append(h('div.nm-vessel.card', null,
        h('div.nm-v-top', null, h('span.nm-v-name', null, `GPU ${g.index}`), h('span.nm-v-chip.mono', null, g.name.replace(/NVIDIA |Tesla /g, ''))),
        h('div.nm-v-glass', null, h('div.nm-v-stack', null, bands),
          h('div.nm-v-level', { style: { bottom: `${Math.min(usedGb / totalGb, 1) * 100}%` }, title: 'memory in use right now' })),
        h('div.nm-v-foot.mono', null,
          h('span', null, `${usedGb.toFixed(1)} / ${totalGb.toFixed(0)} GB`),
          h('span', null, g.util_pct != null ? `${Math.round(g.util_pct)}% busy` : ''),
          g.temp_c != null ? h('span', null, `${Math.round(g.temp_c)}°C`) : null)));
    }
  }

  function paintGrid() {
    const seen = new Set();
    forge.servers.forEach((s, i) => {
      seen.add(s.role);
      let c = cards.get(s.role);
      if (!c || c.hf !== s.hf) { if (c) c.el.remove(); c = instrument(s, i); cards.set(s.role, c); }
      c.update(s, i);
    });
    for (const [role, c] of cards) if (!seen.has(role)) { c.el.remove(); cards.delete(role); }
    // order + the load tile last
    for (const s of forge.servers) gridEl.appendChild(cards.get(s.role).el);
    gridEl.appendChild(loadTile);
  }

  const loadTile = h('button.nm-card.nm-load-tile', { onclick: () => openLoad() },
    h('span.nm-plus', null, icon('plus', 26, 1.4)), h('span.nm-load-t', null, 'Load a model'),
    h('span.nm-load-s', null, 'from the catalog, or any repo on Hugging Face'));

  function instrument(s0) {
    const orb = h('span.nm-orb');
    const name = h('div.nm-name');
    const hf = h('a.nm-hf.mono', { target: '_blank', rel: 'noopener' });
    const chips = h('div.nm-chips');
    const state = h('div.nm-state');
    const ctxVal = h('span.nm-ctx-v.mono');
    const range = h('input.nm-range', { type: 'range', min: '0', max: String(CTX_STEPS.length - 1), step: '1', 'aria-label': 'Context window' });
    const useBar = h('span.nm-use-fill');
    const useTxt = h('span.nm-use-t');
    const applyBtn = h('button.btn.brand.sm', { hidden: true, onclick: () => applyCtx() }, 'Apply');
    const revertBtn = h('button.btn.ghost.sm', { hidden: true, onclick: () => { pending = null; paintCtx(); } }, 'Keep');
    const ctxNote = h('div.nm-ctx-note');
    const spark = h('svg.nm-spark', { viewBox: '0 0 120 28', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    const tps = h('span.nm-m-v.mono');
    const run = h('span.nm-m-v.mono');
    const wait = h('span.nm-m-v.mono');
    const rmBtn = h('button.btn.ghost.sm.nm-rm', { onclick: () => askRemove() }, icon('trash', 14), 'Remove');
    const tryBtn = h('button.btn.ghost.sm', { onclick: () => toggleTry() }, icon('send', 14), 'Try');
    const tryBox = h('div.nm-try', { hidden: true });
    let s = s0, pending = null, confirmT = 0, tryApi = null;

    const el = h('article.nm-card.card', null,
      h('div.nm-card-h', null, orb, h('div.nm-id', null, name, hf), chips),
      state,
      h('div.nm-ctx', null,
        h('div.nm-row', null, h('span.micro', null, 'context window'), ctxVal),
        range,
        h('div.nm-use', { title: 'how full the KV cache is right now: the context the model is holding for live requests' }, useBar),
        h('div.nm-row', null, useTxt, h('span.nm-ctx-act', null, revertBtn, applyBtn)),
        ctxNote),
      h('div.nm-metrics', null,
        h('div.nm-m', null, h('span.micro', null, 'tokens / s'), tps, spark),
        h('div.nm-m', null, h('span.micro', null, 'running'), run),
        h('div.nm-m', null, h('span.micro', null, 'waiting'), wait)),
      h('div.nm-actions', null, tryBtn, rmBtn),
      tryBox);

    range.addEventListener('input', () => {
      const max = s.ctx_fit || s.max_ctx;
      const v = snap(CTX_STEPS[+range.value], max);
      range.value = String(CTX_STEPS.indexOf(v));
      pending = v === s.ctx ? null : v;
      paintCtx();
    });

    function paintCtx() {
      const max = s.ctx_fit || s.max_ctx || 32768;
      const v = pending || s.ctx;
      range.max = String(Math.max(CTX_STEPS.filter((c) => c <= max).length - 1, 0));
      range.value = String(CTX_STEPS.indexOf(snap(v, max)));
      range.style.setProperty('--p', `${(+range.value / Math.max(+range.max, 1)) * 100}%`);
      setText(ctxVal, `${v.toLocaleString("en-US")} tokens`);
      applyBtn.hidden = revertBtn.hidden = !pending;
      setText(ctxNote, pending ? `Apply restarts ${s.label} with a ${tok(pending)} window (about 5 minutes on a T4). Longest that fits here: ${tok(max)}.`
        : `Longest that fits on GPU ${s.gpus.join(', ')}: ${tok(max)} tokens${s.max_ctx > max ? ` (the model can read ${tok(s.max_ctx)})` : ''}.`);
    }

    async function applyCtx() {
      applyBtn.disabled = true;
      try { const r = await post(`/api/nirmana/models/${s.role}/ctx`, { ctx: pending }); toast('Context window', { body: r.note }); pending = null; refresh(); } catch (e) { toast('Not applied', { body: e.message, tone: 'bad' }); } finally { applyBtn.disabled = false; }
    }

    function askRemove() {
      if (!rmBtn.classList.contains('armed')) {
        rmBtn.classList.add('armed'); rmBtn.lastChild.textContent = 'Remove it?';
        clearTimeout(confirmT); confirmT = setTimeout(() => { rmBtn.classList.remove('armed'); rmBtn.lastChild.textContent = 'Remove'; }, 3500);
        return;
      }
      clearTimeout(confirmT);
      rmBtn.disabled = true;
      del(`/api/nirmana/models/${s.role}`).then((r) => { toast(`${s.label} removed`, { body: r.note }); el.classList.add('leaving'); setTimeout(refresh, 300); })
        .catch((e) => { toast('Not removed', { body: e.message, tone: 'bad' }); rmBtn.disabled = false; });
    }

    function toggleTry() {
      if (!tryApi) { tryApi = tryPanel(() => s); tryBox.append(tryApi.el); }
      tryBox.hidden = !tryBox.hidden;
      if (!tryBox.hidden) tryApi.focus();
    }

    function update(next, i) {
      s = next;
      el.className = `nm-card card hue-${hueOf(s.role, i)} st-${s.status}`;
      setText(name, s.label);
      hf.textContent = s.hf; hf.href = `https://huggingface.co/${s.hf}`;
      clear(chips).append(h('span.nm-chip', null, s.role), h('span.nm-chip', null, `GPU ${s.gpus.join('+')}`), h('span.nm-chip', null, gb(s.weights_gib)));
      const ready = s.status === 'ready' || s.status === 'demo';
      // while it loads: how long so far against the usual time, so a silent phase (CUDA graphs) reads as progress
      const el_s = !ready && s.secs ? s.secs : 0;  // the launcher saves seconds since this step began
      const exp = Number(s.expect_s || 600);
      fill(state, h('span.nm-dot'), h('span', null, STATUS[s.status] || s.status),
        el_s ? h('span.nm-elapsed.mono', null, `${fmtDur(el_s, true)} of about ${Math.round(exp / 60)} min`) : null,
        el_s && s.status !== 'failed' ? h('span.nm-prog', null, h('span', { style: { width: `${Math.min(el_s / exp, 0.96) * 100}%` } })) : null,
        !ready && s.detail ? h('span.nm-detail.mono', null, truncate(s.detail, 140)) : null);
      if (!pending) paintCtx();
      const L = s.live || {};
      useBar.style.width = `${Math.min(L.kv_pct || 0, 100)}%`;
      setText(useTxt, ready ? (L.kv_pct != null ? `in use now: ${(L.kv_pct || 0).toFixed(L.kv_pct < 10 ? 1 : 0)}% of the cache` : 'in use now: no numbers yet') : 'not serving yet');
      setText(tps, L.gen_tps ? L.gen_tps.toFixed(0) : '0');
      setText(run, String(L.running || 0));
      setText(wait, String(L.waiting || 0));
      const pts = s.spark || [];
      const mx = Math.max(10, ...pts);
      clear(spark);
      if (pts.length > 1) {
        const d = pts.map((v, k) => `${k ? 'L' : 'M'}${(k / (pts.length - 1)) * 120},${26 - (v / mx) * 24}`).join(' ');
        spark.append(h('path', { d: d + ' L120,28 L0,28 Z', class: 'nm-spark-a' }), h('path', { d, class: 'nm-spark-l' }));
      }
      tryBtn.disabled = !s.model_id;
      tryBtn.title = s.model_id ? 'Send a prompt straight to this model' : 'available once the model is registered';
    }
    return { el, hf: s0.hf, update };
  }

  // ---------- try: one streamed prompt ----------
  const tries = new Map();
  function tryPanel(getS) {
    const ta = h('textarea.input.nm-try-in', { rows: 2, placeholder: 'Ask it anything. Shift+Enter for a new line.' });
    const out = h('div.nm-try-out.md');
    const think = h('details.nm-think', { hidden: true }, h('summary', null, 'thinking'), h('div.nm-think-t'));
    const stats = h('div.nm-try-stats.mono');
    const go = h('button.btn.brand.sm', { onclick: () => send() }, 'Send');
    let buf = '', tb = '', raf = 0, tid = null;
    autogrow(ta, 160);
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
    async function send() {
      const p = ta.value.trim();
      if (!p || tid) return;
      buf = ''; tb = ''; clear(out); think.hidden = true; setText(stats, 'sending…'); go.disabled = true;
      try {
        const r = await post('/api/nirmana/try', { model_id: getS().model_id, prompt: p, max_tokens: 700 });
        tid = r.try_id; tries.set(tid, api);
      } catch (e) { setText(stats, e.message); go.disabled = false; }
    }
    const api = {
      el: h('div', null, h('div.nm-try-row', null, ta, go), think, out, stats),
      focus: () => ta.focus(),
      piece(d) {
        if (d.text) buf += d.text;
        if (d.thinking) { tb += d.thinking; think.hidden = false; }
        if (d.done) {
          tries.delete(tid); tid = null; go.disabled = false;
          setText(stats, d.error ? `stopped: ${d.error}` : `first token ${fmtNum(d.ttft_ms)} ms · ${d.tps} tok/s · ${fmtNum(d.tokens)} tokens · ${(d.ms / 1000).toFixed(1)} s`);
        } else if (!/tok\/s/.test(stats.textContent)) setText(stats, 'streaming…');
        if (!raf) raf = requestAnimationFrame(() => { raf = 0; renderInto(out, buf); setText(think.lastChild, tb); });
      },
    };
    return api;
  }

  // ---------- load sheet ----------
  let sheet = null;
  function openLoad() {
    if (sheet) return;
    let catalog = [], pick = null, gpusSel = [], ctx = 16384, planT = 0, planSeq = 0, searchT = 0;
    const q = h('input.input', { placeholder: 'Search Hugging Face, or paste owner/name or a huggingface.co link', 'aria-label': 'Find a model' });
    const list = h('div.nm-pick-list.scroll');
    const detail = h('div.nm-pick-detail');
    const close = () => { sheet.classList.add('closing'); setTimeout(() => { sheet && sheet.remove(); sheet = null; }, 180); };
    sheet = h('div.nm-sheet', { onclick: (e) => { if (e.target === sheet) close(); } },
      h('div.nm-sheet-card.card', { role: 'dialog', 'aria-label': 'Load a model' },
        h('div.nm-sheet-h', null, h('div', null, h('h2', null, 'Load a model'), h('p', null, 'It downloads on the box and starts in its own server. Nothing else stops.')),
          h('button.btn.ghost.icon', { 'aria-label': 'Close', onclick: close }, icon('x', 18))),
        q,
        h('div.nm-sheet-body', null, list, detail)));
    addEventListener('keydown', function esc(e) { if (e.key === 'Escape' && sheet) { close(); removeEventListener('keydown', esc); } });
    document.body.append(sheet);
    requestAnimationFrame(() => q.focus());

    const freeGpu = () => { const g = (forge && forge.gpus) || []; const f = [...g].sort((a, b) => b.free_share - a.free_share)[0]; return f ? [f.index] : []; };
    gpusSel = freeGpu();

    get('/api/catalog').then((d) => { catalog = d.models || []; paintList(catalog, true); }).catch((e) => list.append(h('div.empty-note', null, e.message)));
    q.addEventListener('input', () => {
      clearTimeout(searchT);
      const v = q.value.trim();
      if (!v) { paintList(catalog, true); return; }
      searchT = setTimeout(async () => {
        const m = v.match(/(?:huggingface\.co|hf\.co)\/([\w.-]+\/[\w.-]+)/);
        const query = m ? m[1] : v;
        clear(list).append(h('div.empty-note', null, 'Asking Hugging Face…'));
        try { const r = await get('/api/catalog/search?q=' + encodeURIComponent(query)); paintList(r.models || [], false); if (m || /^[\w.-]+\/[\w.-]+$/.test(query)) { if (r.models && r.models[0]) choose(r.models[0]); } } catch (e) { clear(list).append(h('div.empty-note', null, e.message)); }
      }, 380);
    });

    function paintList(ms, curated) {
      clear(list);
      if (curated) list.append(h('div.micro.nm-list-h', null, 'curated for agent work'));
      if (!ms.length) list.append(h('div.empty-note', null, 'Nothing found. Try another name, or paste owner/name.'));
      for (const m of ms) {
        const fit = (m.solo && m.solo.fits) || 'no';
        list.append(h('button.nm-pick', { class: `fit-${fit}` + (pick && pick.hf === m.hf ? ' on' : ''), onclick: () => choose(m) },
          h('span.nm-pick-n', null, m.label), h('span.nm-pick-s.mono', null, `${gb(m.weights_gib)}${m.gated ? ' · gated' : ''}`),
          h('span.nm-pick-fit', null, { yes: 'fits', tight: 'tight', no: 'too big' }[fit])));
      }
    }

    function choose(m) {
      pick = m;
      for (const b of list.children) b.classList && b.classList.toggle('on', b.firstChild && b.firstChild.textContent === m.label);
      paintDetail(null);
      plan();
    }

    function plan() {
      clearTimeout(planT);
      planT = setTimeout(async () => {
        const seq = ++planSeq;
        try { const p = await post('/api/nirmana/plan', { hf: pick.hf, gpus: gpusSel, ctx }); if (seq === planSeq) paintDetail(p); } catch (e) { if (seq === planSeq) paintDetail({ fits: 'no', why: e.message }); }
      }, 160);
    }

    function paintDetail(p) {
      clear(detail);
      if (!pick) { detail.append(h('div.nm-pick-empty', null, h('span.nm-plus', null, icon('layers', 28, 1.3)), 'Pick a model on the left, or paste any Hugging Face repo above.')); return; }
      const g = (forge && forge.gpus) || [];
      const gpuBtns = g.map((x) => h('button.nm-gpu', { 'aria-pressed': String(gpusSel.includes(x.index)), onclick: () => {
        gpusSel = gpusSel.includes(x.index) ? gpusSel.filter((i) => i !== x.index) : [...gpusSel, x.index].sort();
        if (!gpusSel.length) gpusSel = [x.index];
        paintDetail(p); plan();
      } }, h('b', null, `GPU ${x.index}`), h('span', null, `${Math.round(x.free_share * 100)}% free`)));
      const cap = (p && p.ctx_fit) || pick.max_ctx || 32768;
      ctx = Math.min(ctx, cap) || 2048;
      const r = h('input.nm-range', { type: 'range', min: '0', max: String(Math.max(CTX_STEPS.filter((c) => c <= cap).length - 1, 0)), step: '1', value: String(CTX_STEPS.indexOf(snap(ctx, cap))) });
      const rv = h('span.nm-ctx-v.mono', null, `${snap(ctx, cap).toLocaleString("en-US")} tokens`);
      r.style.setProperty('--p', `${(+r.value / Math.max(+r.max, 1)) * 100}%`);
      r.addEventListener('input', () => { ctx = CTX_STEPS[+r.value]; setText(rv, `${ctx.toLocaleString("en-US")} tokens`); r.style.setProperty('--p', `${(+r.value / Math.max(+r.max, 1)) * 100}%`); });
      r.addEventListener('change', () => plan());
      const fit = (p && p.fits) || 'wait';
      const gated = pick.gated && !(forge && forge.hf_token);
      const goBtn = h('button.btn.brand', { disabled: !p || fit === 'no', onclick: async () => {
        goBtn.disabled = true;
        try { const res = await post('/api/nirmana/load', { hf: pick.hf, gpus: gpusSel, ctx }); toast(`${pick.label} is coming in as ${res.role}`, { body: res.note }); close(); refresh(); } catch (e) { toast('Not loaded', { body: e.message, tone: 'bad' }); goBtn.disabled = false; }
      } }, icon('down', 15), 'Load it');
      fill(detail,
        h('div.nm-pd-h', null, h('h3', null, pick.label), h('a.mono', { href: `https://huggingface.co/${pick.hf}`, target: '_blank', rel: 'noopener' }, pick.hf)),
        h('p.nm-pd-note', null, pick.note || ''),
        h('div.nm-pd-facts', null,
          h('div', null, h('span.micro', null, 'weights'), h('b', null, gb(pick.weights_gib))),
          h('div', null, h('span.micro', null, 'reads up to'), h('b', null, `${tok(pick.max_ctx || 32768)} tokens`)),
          h('div', null, h('span.micro', null, 'family'), h('b', null, pick.family || 'other'))),
        h('div.micro', null, 'on which GPU'), h('div.nm-gpus', null, gpuBtns),
        h('div.nm-row', null, h('span.micro', null, 'context window'), rv), r,
        h('div', { class: `nm-verdict fit-${fit}` }, h('b', null, { yes: 'Fits', tight: 'Fits, tightly', no: 'Does not fit', wait: 'Checking…' }[fit] || fit),
          h('span', null, p ? `${p.why}${p.ctx_fit ? `. Longest context here: ${tok(p.ctx_fit)}.` : ''}${gpusSel.length > 1 ? ' Split over the GPUs (tensor parallel).' : ''}` : '')),
        gated ? h('p.nm-warn', null, 'This repo is gated: accept its terms on Hugging Face and add an HF_TOKEN secret to the notebook, then run the cell again.') : null,
        h('div.nm-pd-go', null, goBtn));
    }
    paintDetail(null);
  }

  // =====================================================================================  FLOWS
  const flows = createFlows(flowsEl);

  subscribe('nirmana', (ev) => {
    const d = ev.data || {};
    if (ev.type === 'nirmana.delta' && d.try_id) { const t = tries.get(d.try_id); if (t) t.piece(d); return; }
    flows.event(ev);
  });

  return {
    el,
    setActive(on) {
      active = on;
      if (on) { loop(); if (pane === 'flows') flows.show(); } else clearTimeout(timer);
    },
  };
}

// =========================================================================================  flows
function createFlows(root) {
  let data = null, flow = null, dirty = false, run = null, forge = null, shownRun = null;
  const buf = new Map(); // stage id -> {text, thinking} for the run on screen
  const railList = h('div.nm-rail-list');
  const tplList = h('div.nm-tpls');
  const runsList = h('div.nm-runs');
  const nameIn = h('input.nm-flow-name', { placeholder: 'Name this flow', 'aria-label': 'Flow name', oninput: () => { flow.name = nameIn.value; touch(); } });
  const stagesEl = h('div.nm-stages');
  const saveBtn = h('button.btn.sm', { onclick: () => save() }, icon('check', 14), 'Save');
  const delBtn = h('button.btn.ghost.sm', { onclick: () => remove() }, icon('trash', 14));
  const input = h('textarea.input.nm-run-in', { rows: 2, placeholder: 'What should the flow work on?' });
  const runBtn = h('button.btn.brand', { onclick: () => start() }, icon('play', 14), 'Run');
  const stopBtn = h('button.btn.sm', { hidden: true, onclick: () => run && post(`/api/nirmana/runs/${run.id}/stop`).catch(() => {}) }, icon('stop', 13), 'Stop');
  const loom = h('div.nm-loom');
  const runHead = h('div.nm-run-h');
  autogrow(input, 180);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); start(); } });

  root.append(
    h('aside.nm-rail', null,
      h('div.nm-rail-h', null, h('span.micro', null, 'your flows'), h('button.btn.ghost.sm.icon', { title: 'New flow', onclick: () => edit(blank()) }, icon('plus', 15))),
      railList,
      h('div.micro.nm-rail-sub', null, 'start from'), tplList,
      h('div.micro.nm-rail-sub', null, 'recent runs'), runsList),
    h('section.nm-editor.card', null,
      h('div.nm-ed-h', null, nameIn, h('div.nm-ed-act', null, saveBtn, delBtn)),
      h('p.nm-ed-hint', null, 'Text flows down from stage to stage. In a prompt, {input} is the text so far and {start} is what you gave the run.'),
      stagesEl,
      h('div.nm-add', null, Object.entries(KIND).map(([k, v]) => h('button.nm-add-b', { title: v.tip, onclick: () => addStage(k) }, icon(v.ic, 15), v.label)))),
    h('section.nm-live', null,
      h('div.nm-run-dock.card', null, input, h('div.nm-dock-act', null, stopBtn, runBtn)),
      runHead, loom));

  const blank = () => ({ id: '', name: 'Untitled flow', stages: [{ id: sid(), kind: 'think', title: 'Think', model: 'deep', prompt: '{input}', options: [] }] });
  const sid = () => 'st_' + Math.random().toString(16).slice(2, 10);
  function touch() { dirty = true; saveBtn.classList.add('brand'); }

  async function load() {
    try { data = await get('/api/nirmana/flows'); } catch (e) { clear(railList).append(h('div.empty-note', null, e.message)); return; }
    paintRail();
    if (!flow) edit(data.flows[0] ? clone(data.flows[0]) : fromTpl(data.templates[0]));
  }
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const fromTpl = (t) => ({ id: '', name: t.name, blurb: t.blurb, stages: clone(t.stages).map((s) => ({ ...s, id: sid() })) });

  function paintRail() {
    clear(railList);
    if (!data.flows.length) railList.append(h('div.nm-rail-empty', null, 'None yet. Start from one below, or press +.'));
    for (const f of data.flows) railList.append(h('button.nm-rail-i', { class: flow && flow.id === f.id ? 'on' : '', onclick: () => edit(clone(f)) },
      h('span', null, f.name), h('span.nm-rail-s', null, f.stages.map((s) => KIND[s.kind].label).join(' → '))));
    clear(tplList);
    for (const t of data.templates) tplList.append(h('button.nm-tpl', { onclick: () => edit(fromTpl(t)) }, h('b', null, t.name), h('span', null, t.blurb)));
    clear(runsList);
    for (const r of data.runs.slice(0, 12)) runsList.append(h('button.nm-run-i', { class: `rs-${r.status}` + (shownRun === r.id ? ' on' : ''), onclick: () => openRun(r.id) },
      h('span.nm-dot'), h('span.nm-run-n', null, truncate(r.input || r.flow_name, 48)), h('span.nm-run-s.mono', null, `${r.flow_name} · ${fmtAgo(r.created_ts)}`)));
  }

  function edit(f) {
    if (dirty && flow && !confirm('Leave this flow without saving?')) return;
    flow = f; dirty = false; saveBtn.classList.remove('brand');
    nameIn.value = f.name;
    delBtn.hidden = !f.id;
    paintStages();
    if (data) paintRail();
  }

  function modelChoices() {
    const out = [];
    const servers = (forge && forge.servers) || [];
    for (const s of servers) out.push([s.role, `${s.label} (${s.role})`]);
    if (!servers.some((s) => s.role === 'deep')) out.unshift(['deep', 'deep (the strongest model)']);
    if (!servers.some((s) => s.role === 'reader')) out.push(['reader', 'reader (the fast model)']);
    for (const m of Object.values(getState().models)) if (!/box launcher/.test(m.notes || '')) out.push([m.id, m.name]);
    return out;
  }

  function paintStages() {
    clear(stagesEl);
    flow.stages.forEach((s, i) => stagesEl.append(stageCard(s, i)));
  }

  function stageCard(s, i) {
    const kinds = Object.entries(KIND).map(([k, v]) => h('button', { 'aria-pressed': String(s.kind === k), title: v.tip, onclick: () => {
      s.kind = k; if (k === 'decide' && (!s.options || s.options.length < 2)) s.options = [{ label: 'yes', goto: 'next' }, { label: 'no', goto: 'end' }];
      touch(); paintStages();
    } }, icon(v.ic, 14)));
    const title = h('input.nm-st-title', { value: s.title, 'aria-label': 'Stage name', oninput: () => { s.title = title.value; touch(); } });
    const prompt = h('textarea.input.nm-st-prompt', { rows: 2, value: s.prompt, oninput: () => { s.prompt = prompt.value; touch(); } });
    autogrow(prompt, 200);
    let who = null;
    if (s.kind === 'agent') {
      const ags = Object.values(getState().agents);
      who = h('select.input.nm-sel', { onchange: (e) => { s.agent = e.target.value; touch(); } },
        h('option', { value: '' }, 'any crew agent'), ags.map((a) => h('option', { value: a.id, selected: s.agent === a.id }, `${a.name} · ${a.role || 'agent'}`)));
    } else if (s.kind === 'decide' || s.kind === 'think') {
      const ch = modelChoices();
      if (s.model && !ch.some(([v]) => v === s.model)) ch.push([s.model, `${s.model} (not on the box)`]);
      who = h('select.input.nm-sel', { onchange: (e) => { s.model = e.target.value; touch(); } }, ch.map(([v, l]) => h('option', { value: v, selected: s.model === v }, l)));
    }
    let opts = null;
    if (s.kind === 'decide') {
      const targets = [['next', 'go on to the next stage'], ['end', 'end the run'], ...flow.stages.filter((x) => x !== s).map((x) => [x.title, `jump to: ${x.title}`])];
      opts = h('div.nm-opts', null, h('span.micro', null, 'options, and where each one goes'),
        s.options.map((o, k) => h('div.nm-opt', null,
          h('input.input', { value: o.label, 'aria-label': 'Option', oninput: (e) => { o.label = e.target.value; touch(); } }),
          h('select.input', { onchange: (e) => { o.goto = e.target.value; touch(); } }, targets.map(([v, l]) => h('option', { value: v, selected: o.goto === v }, l))),
          h('button.btn.ghost.icon.sm', { title: 'Remove option', disabled: s.options.length <= 2, onclick: () => { s.options.splice(k, 1); touch(); paintStages(); } }, icon('x', 13))),
        ),
        h('button.btn.ghost.sm', { onclick: () => { s.options.push({ label: 'other', goto: 'next' }); touch(); paintStages(); } }, icon('plus', 13), 'option'));
    }
    const move = (d) => { const j = i + d; if (j < 0 || j >= flow.stages.length) return; [flow.stages[i], flow.stages[j]] = [flow.stages[j], flow.stages[i]]; touch(); paintStages(); };
    return h('div.nm-st', { class: `k-${s.kind}` },
      h('div.nm-st-bead', null, h('span', null, String(i + 1))),
      h('div.nm-st-card', null,
        h('div.nm-st-h', null, h('div.seg.seg-sm.nm-kinds', null, kinds), title,
          h('div.nm-st-tools', null,
            h('button.btn.ghost.icon.sm', { title: 'Move up', disabled: i === 0, onclick: () => move(-1) }, icon('up', 13)),
            h('button.btn.ghost.icon.sm', { title: 'Move down', disabled: i === flow.stages.length - 1, onclick: () => move(1) }, icon('down', 13)),
            h('button.btn.ghost.icon.sm', { title: 'Remove stage', disabled: flow.stages.length <= 1, onclick: () => { flow.stages.splice(i, 1); touch(); paintStages(); } }, icon('trash', 13)))),
        who ? h('div.nm-st-who', null, h('span.micro', null, s.kind === 'agent' ? 'agent' : 'model'), who) : null,
        h('div.nm-st-p', null, h('span.micro', null, s.kind === 'search' ? 'search for' : s.kind === 'read' ? 'text with a link' : s.kind === 'agent' ? 'the task' : 'instructions'), prompt),
        opts));
  }

  function addStage(kind) {
    if (flow.stages.length >= 12) { toast('Twelve stages at most'); return; }
    const s = { id: sid(), kind, title: KIND[kind].label, model: kind === 'decide' ? 'reader' : 'deep', prompt: kind === 'agent' ? '{start}' : '{input}', options: [], agent: '' };
    if (kind === 'decide') s.options = [{ label: 'yes', goto: 'next' }, { label: 'no', goto: 'end' }];
    let n = 2; while (flow.stages.some((x) => x.title === s.title)) s.title = `${KIND[kind].label} ${n++}`;
    flow.stages.push(s); touch(); paintStages();
    requestAnimationFrame(() => stagesEl.lastChild && stagesEl.lastChild.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
  }

  async function save() {
    try {
      const body = { name: flow.name, blurb: flow.blurb || '', stages: flow.stages };
      const f = flow.id ? await put(`/api/nirmana/flows/${flow.id}`, body) : await post('/api/nirmana/flows', body);
      flow = clone(f); dirty = false; saveBtn.classList.remove('brand'); delBtn.hidden = false;
      toast('Flow saved'); await load(); paintStages();
    } catch (e) { toast('Not saved', { body: e.message, tone: 'bad' }); }
  }
  async function remove() {
    if (!flow.id || !confirm(`Delete the flow "${flow.name}"? Its past runs stay.`)) return;
    try { await del(`/api/nirmana/flows/${flow.id}`); dirty = false; flow = null; await load(); } catch (e) { toast('Not deleted', { body: e.message, tone: 'bad' }); }
  }

  async function start() {
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    runBtn.disabled = true;
    try {
      const body = flow.id && !dirty ? { flow_id: flow.id, input: text } : { flow: { name: flow.name, stages: flow.stages }, input: text };
      const r = await post('/api/nirmana/runs', body);
      showRun(r);
    } catch (e) { toast('Did not start', { body: e.message, tone: 'bad' }); } finally { runBtn.disabled = false; }
  }
  async function openRun(id) { try { showRun(await get(`/api/nirmana/runs/${id}`)); } catch (e) { toast(e.message); } }

  // ---------- the loom: a run, live ----------
  function showRun(r) {
    if (shownRun !== r.id) buf.clear();
    run = r; shownRun = r.id;
    paintRun();
    if (data) { const b = data.runs.find((x) => x.id === r.id); if (!b) data.runs.unshift({ ...r, input: r.input }); paintRail(); }
  }

  const nodes = new Map();
  function paintRun() {
    const r = run;
    const live = r.status === 'running';
    stopBtn.hidden = !live;
    fill(runHead,
      h('div.nm-rh-l', null, h('span', { class: `nm-pill rs-${r.status}` }, h('span.nm-dot'), { running: 'running', done: 'done', failed: 'failed', stopped: 'stopped' }[r.status] || r.status),
        h('b', null, r.flow_name), h('span.mono.nm-rh-t', null, live ? 'live' : `${(r.ms / 1000).toFixed(1)} s`)),
      r.error ? h('div.nm-rh-err', null, r.error) : null);
    if (loom.dataset.run !== r.id) { clear(loom); nodes.clear(); loom.dataset.run = r.id; loom.append(h('div.nm-thread', { 'aria-hidden': 'true' }, h('span.nm-thread-pulse'))); loom.append(h('div.nm-node.nm-in', null, h('div.nm-bead'), h('div.nm-node-c', null, h('span.micro', null, 'input'), h('div.nm-in-t', null, r.input)))); }
    loom.classList.toggle('live', live);
    r.stages.forEach((st, i) => {
      let n = nodes.get(st.id);
      if (!n) { n = node(st, i); nodes.set(st.id, n); loom.append(n.el); }
      n.update(st);
    });
  }

  function node(st0, i) {
    const flowStage = (run.flow && run.flow.stages[i]) || {};
    const bead = h('div.nm-bead', null, icon(KIND[st0.kind].ic, 14));
    const meta = h('div.nm-node-meta.mono');
    const choice = h('div.nm-choice');
    const out = h('div.nm-node-out.md');
    const think = h('details.nm-think', { hidden: true }, h('summary', null, 'thinking'), h('div.nm-think-t'));
    const extra = h('div.nm-node-x');
    const statusEl = h('span.nm-node-s');
    const el = h('div.nm-node', null, bead,
      h('div.nm-node-c', null,
        h('div.nm-node-h', null, h('b', null, st0.title), h('span.nm-node-k', null, KIND[st0.kind].label), statusEl),
        meta, choice, think, out, extra));
    let raf = 0;
    const api = {
      el,
      update(st) {
        el.className = `nm-node k-${st.kind} ns-${st.status}`;
        setText(statusEl, { waiting: '', running: 'working', done: '', skipped: 'skipped', stopped: 'stopped' }[st.status] ?? st.status);
        const bits = [];
        if (st.model) bits.push(st.model);
        if (st.ttft_ms) bits.push(`first token ${fmtNum(st.ttft_ms)} ms`);
        if (st.tokens_in || st.tokens_out) bits.push(`${fmtNum(st.tokens_in)} in · ${fmtNum(st.tokens_out)} out`);
        if (st.ms) bits.push(`${(st.ms / 1000).toFixed(1)} s`);
        setText(meta, bits.join('  ·  '));
        clear(choice);
        if (st.kind === 'decide' && flowStage.options) {
          choice.append(h('div.nm-opts-row', null, flowStage.options.map((o) => h('span', { class: 'nm-optpill' + (st.choice === o.label ? ' on' : '') }, o.label,
            st.choice === o.label ? h('em', null, o.goto === 'next' ? ' → next' : o.goto === 'end' ? ' → end' : ` → ${o.goto}`) : null))));
          if (st.why) choice.append(h('blockquote.nm-why', null, st.why));
        }
        clear(extra);
        if (st.mission_id) extra.append(h('button.btn.ghost.sm', { onclick: () => nav.openMission(st.mission_id) }, icon('board', 13), 'Open the mission'),
          h('button.btn.ghost.sm', { onclick: () => nav.watchMission(st.mission_id) }, icon('orbit', 13), 'Watch it'));
        if (st.error) extra.append(h('div.nm-rh-err', null, st.error));
        const b = buf.get(st.id);
        const text = st.kind === 'decide' ? '' : (st.output || (b && b.text) || '');
        if (st.kind === 'decide' && !st.choice && b && b.text) { renderInto(out, '`' + truncate(b.text, 240) + '`'); } else paintOut(text);
        if (b && b.thinking) { think.hidden = false; setText(think.lastChild, b.thinking); }
        if (st.status === 'skipped' || st.status === 'waiting') { clear(out); }
      },
      stream() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; const st = run.stages[i]; if (st) api.update(st); }); },
    };
    let lastOut = '';
    function paintOut(text) { if (text !== lastOut) { lastOut = text; renderInto(out, text); } }
    return api;
  }

  return {
    show() { if (!data) load(); },
    models(f) { const first = !forge; forge = f; if (first && flow) paintStages(); },
    event(ev) {
      const d = ev.data || {};
      if (ev.type === 'nirmana.run' && d.run) {
        if (data) {
          const i = data.runs.findIndex((x) => x.id === d.run.id);
          const brief = { ...d.run, input: truncate(d.run.input, 120) };
          if (i >= 0) data.runs[i] = brief; else data.runs.unshift(brief);
          if (d.run.status !== 'running' || i < 0) paintRail();
        }
        if (shownRun === d.run.id) { run = d.run; paintRun(); }
        return;
      }
      if (ev.type === 'nirmana.delta' && d.run_id && d.run_id === shownRun) {
        const b = buf.get(d.stage_id) || { text: '', thinking: '' };
        if (d.text) b.text += d.text;
        if (d.thinking) b.thinking += d.thinking;
        buf.set(d.stage_id, b);
        const n = nodes.get(d.stage_id);
        if (n) n.stream();
      }
    },
  };
}
