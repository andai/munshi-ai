/**
 * Google Meet chat sender — page code (bot + extension), pure DOM, no node/Playwright imports.
 *
 * Posts one message in the meeting's "Chat with everyone" panel: opens the panel when it is closed,
 * finds the composer by its aria-label ("Send a message"), types, presses Enter, and confirms the
 * message is on the page before reporting `chat`. Anything short of that is `bot_name_only` with a
 * reason, so the caller can record that only the bot's name told the meeting it is recording. It
 * never throws and never waits longer than `timeoutMs`.
 *
 * The bot calls it once, after admission, through `window.MunshiBrowserUtils.sendGmeetChatMessage`.
 * Selectors are candidates, most specific first; the English UI is pinned by the join layer.
 */

// ── the sender (identical in the gmeet, teams and zoom capture modules; page code may not import
//    across bricks, so each module carries its own copy beside its own selectors) ──

/** How the sender operates the page. The default dispatches DOM events; the bot passes one that
 *  routes through Playwright's trusted mouse and keyboard, which every platform accepts. */
export interface ChatSendInput {
  /** Activate a control (the chat-panel button, the Send button). */
  click(el: Element): Promise<void>;
  /** Put `text` into the composer (it is focused first). */
  type(el: Element, text: string): Promise<void>;
  /** Submit the composer. */
  pressEnter(el: Element): Promise<void>;
}

export interface ChatSendOptions {
  input?: ChatSendInput;
  /** Whole-send budget in ms (chat ready → confirmed bubble). Default 20 000. */
  timeoutMs?: number;
  /** Poll interval while waiting for the composer or the sent bubble. Default 200. */
  pollMs?: number;
  doc?: Document;
  log?: (m: string) => void;
}

/** Why the notice could not be posted. The bot reports it as `bot_name_only` with this detail. */
export type ChatSendFailure =
  | 'empty_message'
  | 'chat_unavailable'       // no composer on the page and no chat button to open one
  | 'composer_unavailable'   // the panel was opened but no usable composer appeared
  | 'composer_disabled'      // the composer is there but read-only (the host limits chat)
  | 'send_failed'            // typed and submitted, but the message never appeared in the chat
  | `error: ${string}`;

export type ChatSendResult =
  | { channel: 'chat'; posted_at: string }
  | { channel: 'bot_name_only'; detail: ChatSendFailure };

export interface ChatSelectors {
  open: readonly string[];
  close: readonly string[];
  composer: readonly string[];
  send: readonly string[];
}

/** How long to wait for the chat to exist at all (a composer or the button that opens one): some
 *  toolbars render it seconds after admission (Teams' guest page: 8–15 s). Bounded by the budget. */
const READY_BUDGET_MS = 30_000;
const OPEN_BUDGET_MS = 8_000;
const ENTER_GRACE_MS = 3_000;
const PROBE_LEN = 48;

const norm = (s: string): string => s.replace(/\s+/g, ' ').trim();
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));
const isField = (el: Element): boolean => el.tagName === 'TEXTAREA' || el.tagName === 'INPUT';

function isShown(el: Element, needBox: boolean): boolean {
  const view = el.ownerDocument?.defaultView;
  for (let cur: Element | null = el; cur; cur = cur.parentElement) {
    if (cur.hasAttribute('hidden')) return false;
    const st = view?.getComputedStyle(cur);
    if (st && (st.display === 'none' || st.visibility === 'hidden')) return false;
  }
  if (!needBox) return true;
  // On screen, not just laid out: a closed panel can keep its composer rendered outside the window.
  const r = el.getBoundingClientRect();
  const w = view?.innerWidth ?? Infinity;
  const h = view?.innerHeight ?? Infinity;
  return (r.width > 0 || r.height > 0) && r.right > 0 && r.bottom > 0 && r.left < w && r.top < h;
}

function find(doc: Document, selectors: readonly string[], needBox: boolean): HTMLElement | null {
  for (const sel of selectors) {
    let nodes: Element[];
    try { nodes = Array.from(doc.querySelectorAll(sel)); } catch { continue; }
    for (const n of nodes) if (isShown(n, needBox)) return n as HTMLElement;
  }
  return null;
}

function isDisabled(el: Element): boolean {
  return (el as HTMLTextAreaElement).disabled === true
    || el.getAttribute('aria-disabled') === 'true'
    || el.getAttribute('aria-readonly') === 'true'
    || el.hasAttribute('readonly')
    || el.getAttribute('contenteditable') === 'false';
}

function count(hay: string, needle: string): number {
  let n = 0;
  for (let i = hay.indexOf(needle); i !== -1; i = hay.indexOf(needle, i + needle.length)) n++;
  return n;
}

/** Copies of the message on the page outside the composer (a contenteditable composer holds the
 *  typed text in the DOM until it is sent; a textarea holds it in `value`, which is not page text). */
function bubbles(doc: Document, probe: string, composer: Element): number {
  const page = count(norm(doc.body?.textContent ?? ''), probe);
  const draft = composer.isConnected && !isField(composer) ? count(norm(composer.textContent ?? ''), probe) : 0;
  return page - draft;
}

async function until<T>(probe: () => T | null | false, deadline: number, pollMs: number): Promise<T | null> {
  for (;;) {
    const v = probe();
    if (v) return v;
    if (Date.now() >= deadline) return null;
    await sleep(Math.min(pollMs, Math.max(0, deadline - Date.now())));
  }
}

/** The default input: DOM events only (no trusted input available). */
export const domChatInput: ChatSendInput = {
  async click(el) { (el as HTMLElement).click(); },
  async type(el, text) {
    const h = el as HTMLElement;
    const W = h.ownerDocument.defaultView as unknown as typeof globalThis;
    h.focus();
    if (isField(h)) {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(h), 'value')?.set;
      if (setter) setter.call(h, text); else (h as HTMLTextAreaElement).value = text;
      h.dispatchEvent(new W.Event('input', { bubbles: true }));
      return;
    }
    let inserted = false;
    try { inserted = h.ownerDocument.execCommand?.('insertText', false, text) ?? false; } catch { inserted = false; }
    if (!inserted || !norm(h.textContent ?? '').includes(norm(text))) {
      h.textContent = text;
      h.dispatchEvent(new W.Event('input', { bubbles: true }));
    }
  },
  async pressEnter(el) {
    const W = el.ownerDocument.defaultView as unknown as typeof globalThis;
    for (const type of ['keydown', 'keypress', 'keyup']) {
      const ev = new W.KeyboardEvent(type, { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true });
      Object.defineProperty(ev, 'keyCode', { get: () => 13 });
      Object.defineProperty(ev, 'which', { get: () => 13 });
      el.dispatchEvent(ev);
    }
  },
};

/** Post `text` as one chat message: open the panel if needed, type, submit, confirm the sent
 *  message is on the page, and close the panel again if it was opened here. Never throws. */
async function sendChatMessage(sel: ChatSelectors, text: string, opts: ChatSendOptions = {}): Promise<ChatSendResult> {
  const doc = opts.doc ?? document;
  const input = opts.input ?? domChatInput;
  const log = opts.log ?? (() => {});
  const pollMs = opts.pollMs ?? 200;
  const deadline = Date.now() + (opts.timeoutMs ?? 20_000);
  const nameOnly = (detail: ChatSendFailure): ChatSendResult => { log(`chat send: ${detail}`); return { channel: 'bot_name_only', detail }; };
  const message = norm(text);   // one line: Enter submits
  if (!message) return nameOnly('empty_message');
  let opened = false;
  try {
    const ready = await until(() => find(doc, sel.composer, true) ?? find(doc, sel.open, false),
      Math.min(deadline, Date.now() + READY_BUDGET_MS), pollMs);
    if (!ready) return nameOnly('chat_unavailable');
    let composer = find(doc, sel.composer, true);
    if (!composer) {
      const toggle = find(doc, sel.open, false);
      if (!toggle) return nameOnly('chat_unavailable');
      await input.click(toggle);
      opened = true;
      composer = await until(() => find(doc, sel.composer, true), Math.min(deadline, Date.now() + OPEN_BUDGET_MS), pollMs);
      if (!composer) return nameOnly('composer_unavailable');
    }
    if (isDisabled(composer)) return nameOnly('composer_disabled');
    const probe = message.slice(0, PROBE_LEN);
    const before = bubbles(doc, probe, composer);
    const c = composer;
    const sent = (): boolean => bubbles(doc, probe, c) > before;
    await input.type(c, message);
    await input.pressEnter(c);
    let ok = await until(sent, Math.min(deadline, Date.now() + ENTER_GRACE_MS), pollMs);
    if (!ok) {
      const button = find(doc, sel.send, false);
      if (button && !isDisabled(button)) { log('chat send: Enter did not post; pressing Send'); await input.click(button); }
      ok = await until(sent, deadline, pollMs);
    }
    if (!ok) return nameOnly('send_failed');
    log('chat send: posted');
    return { channel: 'chat', posted_at: new Date().toISOString() };
  } catch (e) {
    return nameOnly(`error: ${String((e as Error)?.message ?? e).slice(0, 200)}`);
  } finally {
    if (opened) {
      try {
        const close = find(doc, sel.close, false);
        if (close) await input.click(close);
      } catch { /* leaving the panel open is harmless */ }
    }
  }
}

export const gmeetChatSelectors: ChatSelectors = {
  // The toolbar button keeps one label in both states; data-panel-id 2 is Meet's chat panel.
  open: [
    'button[aria-label="Chat with everyone"]',
    'button[aria-label^="Chat with everyone"]',
    'button[data-panel-id="2"]',
    'button[aria-label="Open chat"]',
  ],
  close: [
    'button[aria-label="Close"][data-panel-id="2"]',
    'button[aria-label="Chat with everyone"]',
    'button[aria-label^="Chat with everyone"]',
    'button[data-panel-id="2"]',
  ],
  composer: [
    'textarea[aria-label="Send a message"]',
    'textarea[aria-label^="Send a message"]',
    'textarea[placeholder^="Send a message"]',
    '[contenteditable="true"][aria-label^="Send a message"]',
  ],
  send: [
    'button[aria-label="Send a message"]',
    'button[aria-label="Send message"]',
    'button[aria-label^="Send a message"]',
  ],
};

/** Post `text` in Meet's chat (see the module comment). */
export function sendGmeetChatMessage(text: string, opts?: ChatSendOptions): Promise<ChatSendResult> {
  return sendChatMessage(gmeetChatSelectors, text, opts);
}
