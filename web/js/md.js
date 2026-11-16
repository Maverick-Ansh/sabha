// Small, safe markdown renderer. Builds DOM nodes only; never touches innerHTML.
// Supports headings, paragraphs, bold/italic, inline code, fenced code, lists (one nesting level),
// blockquotes, rules, links (http/https/mailto only, rel=noopener) and simple pipe tables.
import { h } from './dom.js';

// ---------- math: $…$, \(…\), $$…$$, \[…\] rendered with KaTeX (loaded on first use from jsDelivr) ----------
// Math is cut out before bold/italic so `$K_u$` never turns into emphasis. Until KaTeX is loaded (or if it cannot be),
// the TeX source shows as plain text without the dollars; every pending formula is rendered once it arrives.
const KATEX = 'https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/';
let katexState = 'none'; // none | loading | ready | failed
const pending = new Set();
function ensureKatex() {
  if (katexState !== 'none') return;
  katexState = 'loading';
  document.head.appendChild(h('link', { rel: 'stylesheet', href: KATEX + 'katex.min.css', crossorigin: 'anonymous' }));
  const s = h('script', { src: KATEX + 'katex.min.js', crossorigin: 'anonymous' });
  s.onload = () => { katexState = window.katex ? 'ready' : 'failed'; for (const el of pending) if (el.isConnected) paintMath(el); pending.clear(); };
  s.onerror = () => { katexState = 'failed'; pending.clear(); };
  document.head.appendChild(s);
}
function paintMath(el) {
  try {
    window.katex.render(el.dataset.tex, el, { displayMode: el.classList.contains('math-block'), throwOnError: false, output: 'htmlAndMathml', strict: 'ignore' });
  } catch { el.textContent = el.dataset.tex; }
}
function mathNode(tex, display) {
  const el = h(display ? 'div' : 'span', { class: display ? 'math math-block' : 'math', 'data-tex': tex.trim() });
  if (katexState === 'ready') paintMath(el);
  else { el.textContent = tex.trim(); if (katexState !== 'failed') { pending.add(el); ensureKatex(); } }
  return el;
}

const INLINE = /\$\$([^$]+?)\$\$|\\\(([\s\S]+?)\\\)|\$([^\s$](?:[^$\n]*?[^\s$\\])?)\$(?![\d$])|(`+)([\s\S]*?[^`]|[^`])\4(?!`)|\*\*([^*][\s\S]*?)\*\*|__([^_][\s\S]*?)__|\*([^*\s][^*]*?)\*|(^|[^\w])_([^_\s][^_]*?)_(?!\w)|\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"*_])/g;

function safeHref(url) {
  const u = String(url || '').trim();
  return /^(https?:|mailto:)/i.test(u) ? u : null;
}
function link(text, url) {
  const href = safeHref(url);
  if (!href) return document.createTextNode(text);
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, text);
}

export function inline(text, depth = 0) {
  const out = [];
  if (depth > 4) { out.push(text); return out; }
  const re = new RegExp(INLINE.source, 'g');
  let last = 0, m;
  while ((m = re.exec(text))) {
    let start = m.index;
    if (m[9] !== undefined) start += m[9].length;
    if (start > last) pushText(out, text.slice(last, start));
    if (m[1] !== undefined) out.push(mathNode(m[1], true));
    else if (m[2] !== undefined) out.push(mathNode(m[2], false));
    else if (m[3] !== undefined) out.push(mathNode(m[3], false));
    else if (m[4] !== undefined) out.push(h('code', null, m[5]));
    else if (m[6] !== undefined) out.push(h('strong', null, inline(m[6], depth + 1)));
    else if (m[7] !== undefined) out.push(h('strong', null, inline(m[7], depth + 1)));
    else if (m[8] !== undefined) out.push(h('em', null, inline(m[8], depth + 1)));
    else if (m[10] !== undefined) out.push(h('em', null, inline(m[10], depth + 1)));
    else if (m[11] !== undefined) out.push(link(m[11], m[12]));
    else if (m[13] !== undefined) out.push(link(m[13], m[13]));
    last = re.lastIndex;
    if (m[0].length === 0) re.lastIndex++;
  }
  if (last < text.length) pushText(out, text.slice(last));
  return out;
}
// keep single newlines inside a paragraph as line breaks
function pushText(out, s) {
  const parts = s.split('\n');
  parts.forEach((p, i) => { if (i) out.push(h('br')); if (p) out.push(p); });
}

const RE_FENCE = /^\s{0,3}(```+|~~~+)\s*([\w+-]*)\s*$/;
const RE_HEAD = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RE_HR = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const RE_LI = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const RE_QUOTE = /^\s{0,3}>\s?(.*)$/;
const RE_TSEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const RE_MATH_OPEN = /^\s{0,3}(\$\$|\\\[)(.*)$/;

function cells(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function blocks(lines, depth) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m;
    if ((m = line.match(RE_FENCE))) {
      const fence = m[1];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) body.push(lines[i++]);
      i++;
      out.push(h('pre', m[2] ? { 'data-lang': m[2] } : null, h('code', null, body.join('\n'))));
      continue;
    }
    if ((m = line.match(RE_MATH_OPEN))) { // display math: $$ … $$ or \[ … \], on one line or over several
      const close = m[1] === '$$' ? '$$' : '\\]';
      let body = m[2];
      let end = body.indexOf(close);
      i++;
      while (end < 0 && i < lines.length) { body += '\n' + lines[i++]; end = body.indexOf(close); }
      const tail = end >= 0 ? body.slice(end + close.length).trim() : '';
      out.push(mathNode(end >= 0 ? body.slice(0, end) : body, true));
      if (tail) out.push(h('p', null, inline(tail)));
      continue;
    }
    if ((m = line.match(RE_HEAD))) { out.push(h('h' + m[1].length, null, inline(m[2]))); i++; continue; }
    if (RE_HR.test(line)) { out.push(h('hr')); i++; continue; }
    if (RE_QUOTE.test(line) && depth < 3) {
      const inner = [];
      while (i < lines.length && (m = lines[i].match(RE_QUOTE))) { inner.push(m[1]); i++; }
      out.push(h('blockquote', null, blocks(inner, depth + 1)));
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && RE_TSEP.test(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push(h('table', null,
        h('thead', null, h('tr', null, head.map((c) => h('th', null, inline(c))))),
        h('tbody', null, rows.map((r) => h('tr', null, r.map((c) => h('td', null, inline(c))))))));
      continue;
    }
    if ((m = line.match(RE_LI))) {
      const ordered = /\d/.test(m[2]);
      const baseIndent = m[1].length;
      const list = h(ordered ? 'ol' : 'ul', ordered && parseInt(m[2], 10) !== 1 ? { start: parseInt(m[2], 10) } : null);
      let li = null, sub = null;
      while (i < lines.length) {
        const l = lines[i];
        const lm = l.match(RE_LI);
        if (lm && lm[1].length <= baseIndent + 1) {
          li = h('li', null, inline(lm[3]));
          list.appendChild(li); sub = null; i++;
        } else if (lm && li && lm[1].length > baseIndent + 1) {
          if (!sub) { sub = h(/\d/.test(lm[2]) ? 'ol' : 'ul'); li.appendChild(sub); }
          sub.appendChild(h('li', null, inline(lm[3]))); i++;
        } else if (l.trim() && li && /^\s{2,}/.test(l)) {
          li.appendChild(h('br')); inline(l.trim()).forEach((n) => li.append(n)); i++;
        } else break;
      }
      out.push(list);
      continue;
    }
    // paragraph
    const para = [];
    while (i < lines.length && lines[i].trim() && !RE_FENCE.test(lines[i]) && !RE_HEAD.test(lines[i]) &&
      !RE_HR.test(lines[i]) && !RE_LI.test(lines[i]) && !RE_QUOTE.test(lines[i]) && !RE_MATH_OPEN.test(lines[i])) para.push(lines[i++]);
    if (!para.length) { out.push(h('p', null, line)); i++; continue; }
    out.push(h('p', null, inline(para.join('\n'))));
  }
  return out;
}

// Render markdown text into a .md container element.
export function md(text, cls = '') {
  const el = h('div', { class: 'md' + (cls ? ' ' + cls : '') });
  renderInto(el, text);
  return el;
}
export function renderInto(el, text) {
  while (el.firstChild) el.removeChild(el.firstChild);
  const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
  for (const node of blocks(src.split('\n'), 0)) el.appendChild(node);
  return el;
}
