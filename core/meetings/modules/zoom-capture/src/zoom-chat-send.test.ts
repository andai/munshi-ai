/**
 * Zoom chat sender against jsdom fixtures of the Zoom web client's chat: composer present, panel
 * closed, no chat button, panel without a composer, a disabled composer (host limits chat), the
 * older textarea composer, Enter ignored (Send button), and a message that never appears.
 *
 * The fixture plays Zoom's part: the footer button reads "open the chat panel" / "close the chat
 * panel" and toggles the panel; Enter or Send appends the message and empties the composer.
 *
 * Run: npx tsx src/zoom-chat-send.test.ts
 */
import { JSDOM } from 'jsdom';
import { sendZoomChatMessage } from './zoom-chat-send.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: unknown): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || detail === undefined ? '' : ` — ${JSON.stringify(detail)}`}`);
  if (!cond) failed++;
};

const NOTICE = 'munshi.ai is transcribing this meeting for its organiser. https://example.com/privacy';

interface FixtureOpts {
  panelOpen?: boolean;
  noChatButton?: boolean;
  composer?: 'rich' | 'textarea' | 'missing' | 'disabled';
  enterSends?: boolean;
  sendButtonSends?: boolean;
}

function zoom(opts: FixtureOpts = {}) {
  const dom = new JSDOM('<!doctype html><html><body><div id="footer"></div></body></html>');
  const { document: doc } = dom.window;
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { width: 120, height: 40, top: 0, left: 0, right: 120, bottom: 40, x: 0, y: 0, toJSON() {} } as DOMRect;
  };
  const messages: string[] = [];
  let panel: HTMLElement | null = null;
  const kind = opts.composer ?? 'rich';
  const readDraft = (box: HTMLElement) => (kind === 'textarea' ? (box as HTMLTextAreaElement).value : box.textContent ?? '');
  const clearDraft = (box: HTMLElement) => { if (kind === 'textarea') (box as HTMLTextAreaElement).value = ''; else box.textContent = ''; };
  const post = (box: HTMLElement) => {
    const text = readDraft(box).trim();
    if (!text) return;
    messages.push(text);
    const item = doc.createElement('div');
    item.className = 'new-chat-message__container';
    item.textContent = text;
    panel?.querySelector('[aria-label="Chat Message List"]')?.appendChild(item);
    clearDraft(box);
  };
  let btn: HTMLElement | null = null;
  const mount = () => {
    panel = doc.createElement('div');
    panel.innerHTML = '<div aria-label="Chat Message List"></div>';
    if (kind !== 'missing') {
      let box: HTMLElement;
      if (kind === 'textarea') {
        box = doc.createElement('textarea');
        box.className = 'chat-box__chat-textarea';
      } else {
        const outer = doc.createElement('div');
        outer.className = 'chat-rtf-box__editor-outer';
        box = doc.createElement('div');
        box.setAttribute('contenteditable', 'true');
        if (kind === 'disabled') box.setAttribute('aria-disabled', 'true');
        outer.appendChild(box);
        panel.appendChild(outer);
      }
      box.addEventListener('keydown', (e) => {
        if ((e as KeyboardEvent).key === 'Enter' && opts.enterSends !== false) post(box);
      });
      if (kind === 'textarea') panel.appendChild(box);
      const send = doc.createElement('button');
      send.className = 'chat-rtf-box__send';
      send.addEventListener('click', () => { if (opts.sendButtonSends) post(box); });
      panel.appendChild(send);
    }
    doc.body.appendChild(panel);
    btn?.setAttribute('aria-label', 'close the chat panel');
  };
  const unmount = () => { panel?.remove(); panel = null; btn?.setAttribute('aria-label', 'open the chat panel'); };
  if (!opts.noChatButton) {
    btn = doc.createElement('button');
    btn.setAttribute('aria-label', 'open the chat panel');
    btn.addEventListener('click', () => (panel ? unmount() : mount()));
    doc.getElementById('footer')!.appendChild(btn);
  }
  if (opts.panelOpen) mount();
  return { doc, panelOpen: () => panel !== null, messages: () => messages };
}

const fast = { pollMs: 20, timeoutMs: 1500 };

(async () => {
  console.log('\n=== composer already open ===');
  {
    const f = zoom({ panelOpen: true });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('one message with the link', f.messages()[0] === NOTICE, f.messages());
    check('a panel the bot did not open stays open', f.panelOpen());
  }

  console.log('\n=== panel closed: open, send, close with the "close" button ===');
  {
    const f = zoom({});
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== older build: textarea composer ===');
  {
    const f = zoom({ composer: 'textarea' });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat' && f.messages()[0] === NOTICE, { r, m: f.messages() });
  }

  console.log('\n=== no chat button, no composer ===');
  {
    const f = zoom({ noChatButton: true });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('chat_unavailable', r.channel === 'bot_name_only' && r.detail === 'chat_unavailable', r);
  }

  console.log('\n=== panel opens without a composer ===');
  {
    const f = zoom({ composer: 'missing' });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_unavailable', r.channel === 'bot_name_only' && r.detail === 'composer_unavailable', r);
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== host limits chat: composer disabled ===');
  {
    const f = zoom({ composer: 'disabled' });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_disabled', r.channel === 'bot_name_only' && r.detail === 'composer_disabled', r);
    check('the panel it opened is closed again', !f.panelOpen());
  }

  console.log('\n=== Enter ignored, Send button works ===');
  {
    const f = zoom({ panelOpen: true, enterSends: false, sendButtonSends: true });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat through Send', r.channel === 'chat' && f.messages().length === 1, { r, m: f.messages() });
  }

  console.log('\n=== nothing ever appears ===');
  {
    const f = zoom({ panelOpen: true, enterSends: false });
    const r = await sendZoomChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('send_failed', r.channel === 'bot_name_only' && r.detail === 'send_failed', r);
  }

  if (failed) { console.error(`\n❌ zoom-chat-send: ${failed} check(s) FAILED`); process.exit(1); }
  console.log('\n✅ zoom-chat-send: posts, falls back and reports as specified');
})();
