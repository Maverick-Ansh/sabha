// Box tab of the Crew slide-over: which GPUs this box has, which model serves which role, ready presets for
// this hardware, the curated catalog with a fit verdict per model, and live Hugging Face search for any other
// model. "Use" writes the layout (POST /api/catalog/use); the box supervisor switches the model live and the
// boot strip shows the download and the warm-up (PROTOCOL.md section 11).
import { h, setText, clear } from './dom.js';
import { get, post, enc } from './api.js';
import { toast } from './notify.js';

const FIT = { yes: ['fits', 'mint'], tight: ['tight', 'sun'], no: ['too big', 'coral'] };
const gb = (x) => `${Number(x || 0).toFixed(x >= 10 ? 0 : 1)} GB`;

function badge(p) {
  const [t, tone] = FIT[(p && p.fits) || 'no'] || FIT.no;
  return h('span', { class: `fit fit-${tone}`, title: (p && p.why) || '' }, t);
}

export function renderBox(body, cleanup) {
  let data = null, alive = true;
  cleanup.push(() => { alive = false; });
  const gpuEl = h('div.bx-gpus');
  const layEl = h('div.bx-layout');
  const preEl = h('div.bx-presets');
  const listEl = h('div.bx-list');
  const qIn = h('input.input', { placeholder: 'Search Hugging Face: qwen3.5, gemma-4, llama, gpt-oss, or owner/name…', 'aria-label': 'Search models' });
  const searchEl = h('div.bx-list');
  const filterBtns = ['fits here', 'all'].map((t) => h('button', { 'aria-pressed': String(t === 'fits here'), onclick: () => { filter = t; filterBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === t))); paintList(); } }, t));
  let filter = 'fits here';
  body.append(
    h('section.so-sec', null, h('h3.so-h', null, 'This box'), gpuEl, layEl),
    h('section.so-sec', null, h('h3.so-h', null, 'Ready layouts for these GPUs'), preEl),
    h('section.so-sec', null, h('div.bx-head', null, h('h3.so-h', null, 'Models'), h('div.seg.seg-sm', null, filterBtns)), listEl),
    h('section.so-sec', null, h('h3.so-h', null, 'Any model on Hugging Face'), qIn, searchEl,
      h('p.bx-sub', null, 'Sizes and fit come from the repo\'s safetensors metadata and config.json, so they are estimates; vLLM has the last word '
        + '(the boot strip shows its message if it refuses). GGUF, MLX and NVFP4 repos are left out: vLLM cannot serve them on these GPUs.')));

  async function load() {
    try {
      data = await get('/api/catalog');
      if (alive) paint();
    } catch (e) { clear(gpuEl).appendChild(h('div.empty-note', null, e.message)); }
  }
  function paint() {
    const d = data;
    clear(gpuEl);
    if (!d.gpus.length) gpuEl.appendChild(h('div.empty-note', null, 'No GPU reported. Start the box from the Kaggle / Colab notebook.'));
    for (const g of d.gpus) gpuEl.appendChild(h('div.bx-gpu', null, h('b', null, `GPU ${g.index}`), ` ${g.name} · ${gb(g.mem_mb / 1024)} · compute ${(g.sm / 10).toFixed(1)}`));
    clear(layEl);
    const servers = (d.layout && d.layout.servers) || [];
    const roles = new Set(servers.map((s) => s.role));
    for (const s of servers) {
      layEl.appendChild(h('div.bx-srv', null,
        h('span.bx-role', null, s.role), h('div.bx-srv-text', null, h('div', null, s.label || s.hf),
          h('div.bx-sub', null, `${s.hf} · GPU ${(s.gpus || []).join(',')}${s.tp > 1 ? ` (split over ${s.tp})` : ''} · ${gb(s.weights_gib)} · context ${Math.round((s.ctx || 0) / 1024)}k`)),
        h('span', { class: 'bx-st st-' + (s.status || 'pending') }, s.status || 'pending')));
    }
    if (servers.length && !roles.has('reader')) layEl.appendChild(h('div.bx-sub', null, 'One server serves every role: the reader seat uses the deep model.'));
    clear(preEl);
    for (const p of d.presets) {
      const cur = servers.length && p.servers.length === servers.length && p.servers.every((x) => servers.some((y) => y.role === x.role && y.hf === x.hf));
      preEl.appendChild(h('div.bx-pre' + (cur ? '.on' : ''), null,
        h('div.bx-pre-text', null, h('div.bx-pre-t', null, p.title, ' ', badge(p)), h('div.bx-sub', null, p.why),
          h('div.bx-sub.mono', null, p.servers.map((s) => `${s.role}: ${s.label}`).join('  ·  '))),
        cur ? h('span.micro', null, 'in use') : armed('Use', () => use({ preset: p.id }, p.title))));
    }
    paintList();
  }
  function rowFor(m) {
    const seats = m.seats || {};
    const btns = [];
    if (m.solo && m.solo.fits !== 'no') btns.push(armed('Only model', () => use({ hf: m.hf, as: 'solo' }, m.label), 'Serve every role with this model, on all GPUs it needs'));
    for (const r of ['deep', 'reader']) if (seats[r] && seats[r].fits !== 'no') btns.push(armed(`As ${r}`, () => use({ hf: m.hf, as: r }, `${m.label} as ${r}`), `Replace the ${r} model, same GPU`));
    const best = m.solo || {};
    return h('div.bx-row', null,
      h('div.bx-row-text', null,
        h('div.bx-row-t', null, m.label, ' ', badge(best)),
        h('div.bx-sub', null, `${m.hf} · ${gb(m.weights_gib)}${best.tp > 1 ? ` · split over ${best.tp} GPUs` : ''}${best.ctx ? ` · context ${Math.round(best.ctx / 1024)}k` : ''}`),
        h('div.bx-sub', null, m.note || ''), best.fits === 'no' ? h('div.bx-sub', null, best.why) : null),
      h('div.bx-btns', null, btns));
  }
  function paintList() {
    if (!data) return;
    clear(listEl);
    const ms = data.models.filter((m) => filter === 'all' || (m.solo && m.solo.fits !== 'no'));
    for (const m of ms) listEl.appendChild(rowFor(m));
    if (!ms.length) listEl.appendChild(h('div.empty-note', null, 'Nothing in the catalog fits these GPUs. Try search, or a bigger GPU.'));
  }
  let t = 0, n = 0;
  qIn.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = qIn.value.trim(), k = ++n;
      clear(searchEl);
      if (q.length < 2) return;
      searchEl.appendChild(h('div.r-skel', null, h('i'), h('i')));
      try {
        const r = await get('/api/catalog/search?q=' + enc(q));
        if (k !== n || !alive) return;
        clear(searchEl);
        for (const m of r.models) searchEl.appendChild(rowFor(m));
        if (!r.models.length) searchEl.appendChild(h('div.empty-note', null, 'No servable model found (vLLM needs safetensors weights).'));
      } catch (e) { if (k === n) clear(searchEl).appendChild(h('div.empty-note', null, e.message)); }
    }, 450);
  });
  async function use(bodyArg, what) {
    try {
      const r = await post('/api/catalog/use', bodyArg);
      toast(`Switching to ${what}`, { sub: r.note, tone: 'mint', timeout: 9000 });
      load();
    } catch (e) { toast('Not switched', { sub: e.message, tone: 'coral', timeout: 9000 }); }
  }
  load();
  const timer = setInterval(() => { if (alive && !document.hidden) load(); }, 15000);
  cleanup.push(() => clearInterval(timer));
}

// a button that asks for a second press (switching a model stops the old one)
function armed(label, fn, title) {
  const b = h('button.btn.sm', { title: title || '' }, label);
  b.addEventListener('click', () => {
    if (!b.classList.contains('armed')) {
      b.classList.add('armed'); setText(b, 'Sure?');
      setTimeout(() => { b.classList.remove('armed'); setText(b, label); }, 3000);
      return;
    }
    b.classList.remove('armed'); setText(b, label); fn();
  });
  return b;
}
