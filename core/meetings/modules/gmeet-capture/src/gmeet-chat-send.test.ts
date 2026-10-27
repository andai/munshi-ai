/**
 * Meet chat sender against jsdom fixtures of the Meet chat panel: composer present, panel closed,
 * no chat at all, panel without a composer, a disabled composer, Enter ignored (Send button), a
 * message that never appears, and the caller's own input port.
 *
 * The fixture plays Meet's part: the toolbar button toggles the panel, Enter or Send in the
 * composer appends the message to the list and clears the box.
 *
 * Run: npx tsx src/gmeet-chat-send.test.ts
 */
import { JSDOM } from 'jsdom';
import { sendGmeetChatMessage, type ChatSendInput } from './gmeet-chat-send.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: unknown): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || detail === undefined ? '' : ` — ${JSON.stringify(detail)}`}`);
  if (!cond) failed++;
};

const NOTICE = 'munshi.ai is transcribing this meeting for its organiser. https://example.com/privacy';

interface Fixture {
  doc: Document;
  panelOpen(): boolean;
  messages(): string[];
}

interface FixtureOpts {
  panelOpen?: boolean;
  noChatButton?: boolean;
  composer?: 'ok' | 'missing' | 'disabled';
  enterSends?: boolean;
  sendButtonSends?: boolean;
  openDelayMs?: number;
  history?: string[];
}

function meet(opts: FixtureOpts = {}): Fixture {
  const dom = new JSDOM('<!doctype html><html><body><div id="call"></div><div id="toolbar"></div></body></html>');
  const { document: doc } = dom.window;
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { width: 120, height: 40, top: 0, left: 0, right: 120, bottom: 40, x: 0, y: 0, toJSON() {} } as DOMRect;
  };
  const messages: string[] = [...(opts.history ?? [])];
  let panel: HTMLElement | null = null;
  const post = (text: string) => {
    if (!text.trim()) return;
    messages.push(text);
    const bubble = doc.createElement('div');
    bubble.className = 'msg';
    bubble.textContent = text;
    panel?.querySelector('.list')?.appendChild(bubble);
  };
  const mount = () => {
    panel = doc.createElement('aside');
    panel.innerHTML = `<div class="list">${messages.map((m) => `<div class="msg">${m}</div>`).join('')}</div>`;
    if (opts.composer !== 'missing') {
      const box = doc.createElement('textarea');
      box.setAttribute('aria-label', 'Send a message');
      if (opts.composer === 'disabled') box.disabled = true;
      box.addEventListener('keydown', (e) => {
        if ((e as KeyboardEvent).key === 'Enter' && opts.enterSends !== false) { post(box.value); box.value = ''; }
      });
      panel.appendChild(box);
      const send = doc.createElement('button');
      send.setAttribute('aria-label', 'Send a message');
      send.addEventListener('click', () => { if (opts.sendButtonSends) { post(box.value); box.value = ''; } });
      panel.appendChild(send);
    }
    doc.body.appendChild(panel);
  };
  if (!opts.noChatButton) {
    const btn = doc.createElement('button');
    btn.setAttribute('aria-label', 'Chat with everyone');
    btn.setAttribute('data-panel-id', '2');
    btn.addEventListener('click', () => {
      if (panel) { panel.remove(); panel = null; return; }
      if (opts.openDelayMs) setTimeout(mount, opts.openDelayMs); else mount();
    });
    doc.getElementById('toolbar')!.appendChild(btn);
  }
  if (opts.panelOpen) mount();
  return { doc, panelOpen: () => panel !== null, messages: () => messages };
}

const fast = { pollMs: 20, timeoutMs: 1500 };

(async () => {
  console.log('\n=== composer already open ===');
  {
    const f = meet({ panelOpen: true });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('posted_at is an ISO time', r.channel === 'chat' && !Number.isNaN(Date.parse(r.posted_at)), r);
    check('the message text and link were sent as one message', f.messages()[0] === NOTICE, f.messages());
    check('a panel the bot did not open stays open', f.panelOpen());
  }

  console.log('\n=== panel closed: open, send, close ===');
  {
    const f = meet({ openDelayMs: 100 });
    const r = await sendGmeetChatMessage(`${NOTICE}\nsecond line`, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('newlines become spaces (Enter would send early)', f.messages()[0] === `${NOTICE} second line`, f.messages());
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== no chat on the page ===');
  {
    const f = meet({ noChatButton: true });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('chat_unavailable', r.channel === 'bot_name_only' && r.detail === 'chat_unavailable', r);
  }

  console.log('\n=== panel opens without a composer ===');
  {
    const f = meet({ composer: 'missing' });
    const t0 = Date.now();
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_unavailable', r.channel === 'bot_name_only' && r.detail === 'composer_unavailable', r);
    check('within the budget', Date.now() - t0 < 2500, Date.now() - t0);
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== host turned chat off: composer disabled ===');
  {
    const f = meet({ composer: 'disabled' });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_disabled', r.channel === 'bot_name_only' && r.detail === 'composer_disabled', r);
    check('nothing was posted', f.messages().length === 0);
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== Enter ignored, Send button works ===');
  {
    const f = meet({ panelOpen: true, enterSends: false, sendButtonSends: true });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat through Send', r.channel === 'chat' && f.messages().length === 1, { r, m: f.messages() });
  }

  console.log('\n=== nothing ever appears ===');
  {
    const f = meet({ panelOpen: true, enterSends: false });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('send_failed', r.channel === 'bot_name_only' && r.detail === 'send_failed', r);
  }

  console.log('\n=== the same notice is already in the history ===');
  {
    const f = meet({ panelOpen: true, enterSends: false, history: [NOTICE] });
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('an old copy does not count as sent', r.channel === 'bot_name_only' && r.detail === 'send_failed', r);
  }

  console.log('\n=== the caller supplies the input (the bot\'s trusted input) ===');
  {
    const f = meet({});
    const calls: string[] = [];
    const input: ChatSendInput = {
      async click(el) { calls.push(`click:${el.getAttribute('aria-label')}`); (el as HTMLElement).click(); },
      async type(el, text) { calls.push('type'); (el as HTMLTextAreaElement).value = text; },
      async pressEnter(el) {
        calls.push('enter');
        el.dispatchEvent(new (el.ownerDocument.defaultView as any).KeyboardEvent('keydown', { key: 'Enter' }));
      },
    };
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc, input });
    check('posts to chat', r.channel === 'chat', r);
    check('open → type → enter → close, all through the port',
      calls.join(',') === 'click:Chat with everyone,type,enter,click:Chat with everyone', calls);
  }

  console.log('\n=== an input that throws ===');
  {
    const f = meet({ panelOpen: true });
    const input: ChatSendInput = {
      async click() { /* */ }, async pressEnter() { /* */ },
      async type() { throw new Error('page closed'); },
    };
    const r = await sendGmeetChatMessage(NOTICE, { ...fast, doc: f.doc, input });
    check('reported, never thrown', r.channel === 'bot_name_only' && r.detail === 'error: page closed', r);
  }

  if (failed) { console.error(`\n❌ gmeet-chat-send: ${failed} check(s) FAILED`); process.exit(1); }
  console.log('\n✅ gmeet-chat-send: posts, falls back and reports as specified');
})();
