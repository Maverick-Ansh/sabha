// Alerts tab of the Crew slide-over: how the box reaches the owner's phone (GET/PUT /api/notify, POST
// /api/notify/test, POST /api/notify/telegram/find; sabha/notify.py). Secrets are write-only: the hub
// answers "set …1234", never the value. Every channel is optional; ntfy needs nothing but a topic name.
import { h, clear } from './dom.js';
import { get, post, api } from './api.js';
import { toast } from './notify.js';

const CH = [
  { id: 'ntfy', title: 'Phone push (ntfy)', free: 'free, no account',
    how: 'Install the ntfy app (Android or iPhone), tap +, subscribe to a topic name you invent here. Make it long and odd: anyone who knows it can read the messages.',
    fields: [['topic', 'topic name, e.g. sabha-7f3k9q2xw (it is the password)', true], ['server', 'server (empty = https://ntfy.sh)', false]] },
  { id: 'telegram', title: 'Telegram', free: 'free, two-way',
    how: 'In Telegram open @BotFather, send /newbot, copy the token here and save. Then send your new bot any message and press Find my chat. '
      + 'You can text it back: /status, /report, /research <question> for 4h, /tell <instruction>, /finish, /stop.',
    fields: [['bot_token', 'bot token from @BotFather', true], ['chat_id', 'chat id (Find my chat fills it)', false]] },
  { id: 'whatsapp', title: 'WhatsApp', free: 'CallMeBot (free) or Twilio (free trial sandbox)',
    how: 'Two ways. CallMeBot, free: open callmebot.com, WhatsApp API page, save the number shown there (it is hidden while their bot is full), '
      + 'send it "I allow callmebot to send me messages" and it answers with your API key; provider callmebot + your number + that key. '
      + 'Twilio, works any time: in the Twilio console open Messaging, Try WhatsApp, and send the sandbox number the "join <code>" it shows; '
      + 'provider twilio + your number + the sandbox number as sender (+14155238886) + account SID and auth token (or leave them empty if Phone call has them). '
      + 'Twilio only delivers within 24 h of your last WhatsApp message to it, so say hi to it when you start a box. Numbers with country code, e.g. +919876543210.',
    fields: [['provider', 'callmebot or twilio', false], ['phone', 'your WhatsApp number, +country code', false], ['apikey', 'CallMeBot API key', true],
      ['twilio_from', 'Twilio WhatsApp sender, e.g. +14155238886', false], ['twilio_sid', 'Twilio account SID (optional)', false],
      ['twilio_token', 'Twilio auth token (optional)', true]] },
  { id: 'email', title: 'Email', free: 'your own mail account',
    how: 'For Gmail: turn on 2-step verification, create an app password (Google account, Security, App passwords) and use it here. Host smtp.gmail.com, port 465.',
    fields: [['host', 'SMTP host, e.g. smtp.gmail.com', false], ['port', 'port (465 or 587)', false], ['user', 'login, e.g. you@gmail.com', false],
      ['password', 'app password', true], ['to', 'send to', false]] },
  { id: 'call', title: 'Phone call', free: 'Twilio (paid, trial credit) or a free Telegram call',
    how: 'Rings you when a run finishes or something breaks, and reads the message out. Free: provider telegram + your Telegram @username (authorise @CallMeBot_txtbot once). '
      + 'Real phone call: provider twilio + account SID, auth token, your Twilio number and your number.',
    fields: [['provider', 'twilio or telegram', false], ['telegram_user', 'Telegram @username (telegram provider)', false],
      ['twilio_sid', 'Twilio account SID', false], ['twilio_token', 'Twilio auth token', true], ['twilio_from', 'Twilio number, +1…', false], ['to', 'your number, +country code', false]] },
];
const LEVEL = [['all', 'everything: every wave'], ['important', 'important: link, finished, problems'], ['off', 'off']];

export function renderAlerts(body, cleanup) {
  let alive = true;
  cleanup.push(() => { alive = false; });
  const recentEl = h('div.r-list');
  const cards = h('div.al-cards');
  body.append(
    h('p.bx-sub.al-lede', null, 'The box tells you when it is up (with the new link), after every research wave, when a run finishes and when something breaks. '
      + 'Set any channel once; Kaggle / Colab secrets with the names in the README fill these on every new box.'),
    cards, h('section.so-sec', null, h('h3.so-h', null, 'Recent'), recentEl));

  async function load() {
    let d;
    try { d = await get('/api/notify'); } catch (e) { clear(cards).appendChild(h('div.empty-note', null, e.message)); return; }
    if (!alive) return;
    clear(cards);
    for (const c of CH) cards.appendChild(card(c, d[c.id] || {}));
    clear(recentEl);
    for (const r of d.recent || []) {
      recentEl.appendChild(h('div.r-row', null, h('span', { class: 'fit fit-' + (r.ok ? 'mint' : 'coral') }, r.ok ? 'sent' : 'failed'),
        h('div.r-row-text', null, h('div', null, `${r.channel} · ${r.title}`), h('div.r-row-sub', null, `${r.kind} · ${r.info} · ${new Date(r.t * 1000).toLocaleTimeString()}`))));
    }
    if (!(d.recent || []).length) recentEl.appendChild(h('div.r-row-sub', null, 'Nothing sent yet.'));
  }
  function card(c, cur) {
    const inputs = {};
    const grid = h('div.al-grid');
    for (const [f, ph, secret] of c.fields) {
      const v = cur[f];
      const isSet = secret && v && v.set;
      const inp = h('input.input' + (secret ? '.mono' : ''), { type: secret ? 'password' : 'text', placeholder: isSet ? `set ${v.hint || ''} (type to replace)` : ph,
        value: secret ? '' : (v || ''), autocomplete: 'off', 'aria-label': ph });
      inputs[f] = inp;
      grid.appendChild(inp);
    }
    const level = h('select.select.sm', { 'aria-label': 'What to send' }, LEVEL.map(([v, t]) => h('option', { value: v, selected: cur.level === v }, t)));
    const state = h('span', { class: 'fit fit-' + (cur.ready ? 'mint' : 'sun') }, cur.ready ? 'on' : 'not set');
    const save = h('button.btn.sm.brand', { onclick: async () => {
      const vals = { level: level.value };
      for (const [f, , secret] of c.fields) { const x = inputs[f].value.trim(); if (!secret || x) vals[f] = x; }
      try { await api('PUT', '/api/notify', { [c.id]: vals }); toast(`${c.title} saved`, { tone: 'mint', timeout: 2500 }); load(); }
      catch (e) { toast('Not saved', { sub: e.message, tone: 'coral' }); }
    } }, 'Save');
    const test = h('button.btn.sm', { onclick: async () => {
      test.disabled = true;
      try {
        const r = await post('/api/notify/test', { channel: c.id });
        const x = (r.results || [])[0];
        toast(x && x.ok ? `${c.title}: test sent` : `${c.title}: test failed`, { sub: x ? x.info : '', tone: x && x.ok ? 'mint' : 'coral', timeout: 6000 });
        load();
      } catch (e) { toast('Test not sent', { sub: e.message, tone: 'coral' }); } finally { test.disabled = false; }
    } }, 'Test');
    const extra = c.id === 'telegram' ? h('button.btn.sm', { onclick: async () => {
      try { const r = await post('/api/notify/telegram/find', {}); toast('Chat found', { sub: `${r.name || ''} (${r.id}). Press Test.`, tone: 'mint' }); load(); }
      catch (e) { toast('No chat yet', { sub: e.message, tone: 'sun', timeout: 8000 }); }
    } }, 'Find my chat') : null;
    return h('details.al-card.card' + (cur.ready ? '.on' : ''), { open: !!cur.ready || c.id === 'ntfy' },
      h('summary.al-sum', null, h('b', null, c.title), h('span.bx-sub', null, ' · ' + c.free), h('span.spacer'), state),
      h('p.bx-sub', null, c.how), grid,
      h('div.al-foot', null, level, h('span.spacer'), extra, test, save));
  }
  load();
}

export function alertsReady(d) {
  return !!d && CH.some((c) => d[c.id] && d[c.id].ready);
}
