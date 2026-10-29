/**
 * Teams chat sender against jsdom fixtures of the Teams meeting chat: editor present, pane closed,
 * no chat button, a Chat button that appears late, a closed pane whose editor stays rendered off
 * screen (the guest page), pane without an editor, a read-only editor (guests barred from chat), Enter
 * ignored (Send button), and a message that never appears.
 *
 * The fixture plays Teams' part: the toolbar Chat button toggles the pane, and Enter or Send in the
 * contenteditable editor appends the message to the list and empties the editor.
 *
 * Run: npx tsx src/teams-chat-send.test.ts
 */
import { JSDOM } from 'jsdom';
import { sendTeamsChatMessage } from './teams-chat-send.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: unknown): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || detail === undefined ? '' : ` — ${JSON.stringify(detail)}`}`);
  if (!cond) failed++;
};

const NOTICE = 'munshi.ai is transcribing this meeting for its organiser. https://example.com/privacy';

interface FixtureOpts {
  paneOpen?: boolean;
  noChatButton?: boolean;
  editor?: 'ok' | 'missing' | 'readonly';
  enterSends?: boolean;
  sendButtonSends?: boolean;
  /** The Chat button is added this many ms after the call starts (the guest toolbar). */
  chatButtonAfterMs?: number;
  /** The pane is always mounted; closed means rendered off screen (the guest page). */
  offscreenWhenClosed?: boolean;
}

function teams(opts: FixtureOpts = {}) {
  const dom = new JSDOM('<!doctype html><html><body><div id="toolbar"></div></body></html>');
  const { document: doc } = dom.window;
  dom.window.Element.prototype.getBoundingClientRect = function (this: Element) {
    const left = this.closest('[data-offscreen]') ? -400 : 0;
    return { width: 120, height: 40, top: 0, left, right: left + 120, bottom: 40, x: left, y: 0, toJSON() {} } as DOMRect;
  };
  let opens = 0;
  const messages: string[] = [];
  let pane: HTMLElement | null = null;
  const post = (editor: HTMLElement) => {
    const text = (editor.textContent ?? '').trim();
    if (!text) return;
    messages.push(text);
    const row = doc.createElement('div');
    row.setAttribute('data-tid', 'chat-pane-message');
    row.textContent = text;
    pane?.querySelector('[data-tid="chat-pane-list"]')?.appendChild(row);
    editor.textContent = '';
  };
  const mount = () => {
    pane = doc.createElement('div');
    pane.innerHTML = '<div data-tid="chat-pane-list"></div>';
    if (opts.editor !== 'missing') {
      const editor = doc.createElement('div');
      editor.setAttribute('data-tid', 'ckeditor');
      editor.setAttribute('role', 'textbox');
      editor.setAttribute('aria-label', 'Type a message');
      editor.setAttribute('contenteditable', opts.editor === 'readonly' ? 'false' : 'true');
      editor.addEventListener('keydown', (e) => {
        if ((e as KeyboardEvent).key === 'Enter' && opts.enterSends !== false) post(editor);
      });
      pane.appendChild(editor);
      const send = doc.createElement('button');
      send.setAttribute('data-tid', 'newMessageCommands-send');
      send.addEventListener('click', () => { if (opts.sendButtonSends) post(editor); });
      pane.appendChild(send);
    }
    doc.body.appendChild(pane);
  };
  const isOpen = (): boolean => pane !== null && !(opts.offscreenWhenClosed && pane.hasAttribute('data-offscreen'));
  const addChatButton = () => {
    const btn = doc.createElement('button');
    btn.id = 'chat-button';
    btn.setAttribute('aria-label', 'Chat');
    btn.addEventListener('click', () => {
      if (opts.offscreenWhenClosed && pane) {
        if (pane.hasAttribute('data-offscreen')) { pane.removeAttribute('data-offscreen'); opens++; }
        else pane.setAttribute('data-offscreen', '');
      } else if (pane) { pane.remove(); pane = null; } else { mount(); opens++; }
    });
    doc.getElementById('toolbar')!.appendChild(btn);
  };
  if (!opts.noChatButton) {
    if (opts.chatButtonAfterMs) setTimeout(addChatButton, opts.chatButtonAfterMs);
    else addChatButton();
  }
  if (opts.offscreenWhenClosed) { mount(); if (!opts.paneOpen) pane!.setAttribute('data-offscreen', ''); }
  else if (opts.paneOpen) mount();
  return { doc, paneOpen: isOpen, messages: () => messages, opens: () => opens };
}

const fast = { pollMs: 20, timeoutMs: 1500 };

(async () => {
  console.log('\n=== editor already open ===');
  {
    const f = teams({ paneOpen: true });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('one message with the link', f.messages()[0] === NOTICE, f.messages());
    check('the typed draft does not count as the sent message', f.messages().length === 1);
    check('a pane the bot did not open stays open', f.paneOpen());
  }

  console.log('\n=== pane closed: open, send, close ===');
  {
    const f = teams({});
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat', r.channel === 'chat', r);
    check('the pane it opened is closed again', !f.paneOpen());
  }

  console.log('\n=== no chat button, no editor ===');
  {
    const f = teams({ noChatButton: true });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('chat_unavailable', r.channel === 'bot_name_only' && r.detail === 'chat_unavailable', r);
  }

  console.log('\n=== the Chat button appears after the call starts (guest toolbar) ===');
  {
    const f = teams({ chatButtonAfterMs: 300 });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('waits for the button, then posts to chat', r.channel === 'chat' && f.messages().length === 1, { r, m: f.messages() });
  }

  console.log('\n=== closed pane keeps its editor rendered off screen (guest page) ===');
  {
    const f = teams({ offscreenWhenClosed: true });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('opens the pane instead of typing into the off-screen editor', f.opens() === 1, f.opens());
    check('posts to chat', r.channel === 'chat' && f.messages().length === 1, { r, m: f.messages() });
    check('the pane it opened is closed again', !f.paneOpen());
  }

  console.log('\n=== pane opens without an editor ===');
  {
    const f = teams({ editor: 'missing' });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_unavailable', r.channel === 'bot_name_only' && r.detail === 'composer_unavailable', r);
    check('the pane it opened is closed again', !f.paneOpen());
  }

  console.log('\n=== guests barred from chat: read-only editor ===');
  {
    const f = teams({ editor: 'readonly', paneOpen: true });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('composer_disabled', r.channel === 'bot_name_only' && r.detail === 'composer_disabled', r);
    check('nothing was posted', f.messages().length === 0);
  }

  console.log('\n=== Enter ignored, Send button works ===');
  {
    const f = teams({ paneOpen: true, enterSends: false, sendButtonSends: true });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('posts to chat through Send', r.channel === 'chat' && f.messages().length === 1, { r, m: f.messages() });
  }

  console.log('\n=== nothing ever appears ===');
  {
    const f = teams({ paneOpen: true, enterSends: false });
    const r = await sendTeamsChatMessage(NOTICE, { ...fast, doc: f.doc });
    check('send_failed (the draft in the editor is not a sent message)',
      r.channel === 'bot_name_only' && r.detail === 'send_failed', r);
  }

  if (failed) { console.error(`\n❌ teams-chat-send: ${failed} check(s) FAILED`); process.exit(1); }
  console.log('\n✅ teams-chat-send: posts, falls back and reports as specified');
})();
