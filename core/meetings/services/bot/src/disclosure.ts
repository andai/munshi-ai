/**
 * The recording notice (BOT-03): once the bot is admitted, post the invocation's `disclosure`
 * (`text` + the privacy-notice link, one chat message) and report how that went on lifecycle.v1.
 *
 * The notice never fails or delays the join. It starts after the pipeline has started (so after
 * the orchestrator reported `active`), runs in the background, and every fault — no chat on this
 * platform, a composer the host disabled, a sender that throws or hangs — becomes the outcome
 * `bot_name_only` with a `detail`, meaning only the bot's display name told the meeting it records.
 * The outcome rides a repeated `active` event (`disclosure`), which meeting-api stores at
 * `meeting.data.disclosure`. An invocation without `disclosure` posts and reports nothing.
 *
 * The chat senders are page code in the capture modules (`window.MunshiBrowserUtils.send*Chat*`).
 * `browserChatSender` calls them with an input port that routes clicks and typing through
 * Playwright's trusted mouse and keyboard, which every platform's chat accepts.
 */
import type { Page } from '@munshi/remote-browser';
import type { Invocation, Platform } from './config.js';
import type { DisclosureOutcome } from './contracts.js';
import type { LifecycleSink, Pipeline } from './ports.js';

/** The sender's own budget: open the panel, type, send, see the message. */
export const DISCLOSURE_TIMEOUT_MS = 20_000;
/** Extra time before a sender that ignores its budget is cut off as `timeout`. */
const HARD_STOP_GRACE_MS = 5_000;
const DETAIL_MAX = 500;

/** Posts one chat message within `timeoutMs` and resolves how it went. */
export type ChatSender = (message: string, timeoutMs: number) => Promise<unknown>;

/** The one chat line: the notice, a space, the link. Newlines collapse (Enter sends). */
export function disclosureMessage(d: { text: string; privacy_notice_url: string }): string {
  return `${d.text} ${d.privacy_notice_url}`.replace(/\s+/g, ' ').trim();
}

const capDetail = (s: string): string => s.slice(0, DETAIL_MAX);

/** Whatever a sender resolved → a contract-valid outcome (a `chat` needs `posted_at`). */
export function normalizeOutcome(raw: unknown, now: () => string): DisclosureOutcome {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  if (r.channel === 'chat') {
    const posted = typeof r.posted_at === 'string' && r.posted_at ? r.posted_at : now();
    return { channel: 'chat', posted_at: posted };
  }
  if (r.channel === 'bot_name_only') {
    return { channel: 'bot_name_only', detail: capDetail(typeof r.detail === 'string' && r.detail ? r.detail : 'unknown') };
  }
  return { channel: 'bot_name_only', detail: 'invalid_sender_result' };
}

export interface RunDisclosureDeps {
  inv: Invocation;
  /** null when this platform has no chat sender. */
  send: ChatSender | null;
  lifecycle: LifecycleSink;
  timeoutMs?: number;
  /** Extra time before a sender that ignores its budget is cut off (default 5 s). */
  hardStopGraceMs?: number;
  now?: () => string;
  log?: (m: string) => void;
}

/** Post the notice once and report the outcome. Resolves the outcome (null when the invocation
 *  carries no disclosure). Never rejects. */
export async function runDisclosure(deps: RunDisclosureDeps): Promise<DisclosureOutcome | null> {
  const d = deps.inv.disclosure;
  if (!d) return null;
  const now = deps.now ?? (() => new Date().toISOString());
  const log = deps.log ?? ((m: string) => console.log(`[bot] disclosure: ${m}`));
  const timeoutMs = deps.timeoutMs ?? DISCLOSURE_TIMEOUT_MS;
  let outcome: DisclosureOutcome;
  if (!deps.send) {
    outcome = { channel: 'bot_name_only', detail: 'platform_unsupported' };
  } else {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const hardStop = new Promise<DisclosureOutcome>((resolve) => {
      timer = setTimeout(() => resolve({ channel: 'bot_name_only', detail: 'timeout' }), timeoutMs + (deps.hardStopGraceMs ?? HARD_STOP_GRACE_MS));
    });
    try {
      outcome = normalizeOutcome(await Promise.race([deps.send(disclosureMessage(d), timeoutMs), hardStop]), now);
    } catch (e) {
      outcome = { channel: 'bot_name_only', detail: capDetail(`error: ${String((e as Error)?.message ?? e)}`) };
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  log(outcome.channel === 'chat' ? `posted in chat at ${outcome.posted_at}` : `not posted (${outcome.detail}); the bot name carries the notice`);
  try {
    await deps.lifecycle.emit({ connection_id: deps.inv.connectionId ?? '', status: 'active', timestamp: now(), disclosure: outcome });
  } catch (e) {
    log(`report failed: ${String(e)}`);
  }
  return outcome;
}

/** Run `onAdmitted` once, in the background, after the pipeline has started. The orchestrator
 *  starts the pipeline only after admission and after reporting `active`. */
export function withPostAdmission(pipeline: Pipeline, onAdmitted: () => void): Pipeline {
  let fired = false;
  return {
    async start() {
      await pipeline.start();
      if (fired) return;
      fired = true;
      try { onAdmitted(); } catch (e) { console.error(`[bot] post-admission hook failed: ${String(e)}`); }
    },
    stop: () => pipeline.stop(),
  };
}

/** The page function each platform exposes on `window.MunshiBrowserUtils`. */
export const CHAT_SENDER_BY_PLATFORM: Partial<Record<Platform, string>> = {
  google_meet: 'sendGmeetChatMessage',
  teams: 'sendTeamsChatMessage',
  zoom: 'sendZoomChatMessage',
};

/** The page→Node binding the sender's trusted input calls. */
export const TRUSTED_INPUT_BINDING = '__munshiChatInput';

type TrustedInput = { kind: 'click'; x: number; y: number } | { kind: 'type'; text: string } | { kind: 'enter' };

/** The chat sender for `platform` on the live meeting page, or null when it has none. Input goes
 *  through Playwright's mouse and keyboard, and only while a send is running and only to type the
 *  notice itself (the meeting page cannot use the binding to type anything else). */
export function browserChatSender(page: Page, platform: Platform): ChatSender | null {
  const fnName = CHAT_SENDER_BY_PLATFORM[platform];
  if (!fnName) return null;
  let sending: string | null = null;
  let exposed = false;
  return async (message, timeoutMs) => {
    if (!exposed) {
      exposed = true;
      try {
        await page.exposeFunction(TRUSTED_INPUT_BINDING, async (a: TrustedInput) => {
          if (sending === null || !a || typeof a !== 'object') return false;
          if (a.kind === 'click' && Number.isFinite(a.x) && Number.isFinite(a.y)) { await page.mouse.click(a.x, a.y); return true; }
          if (a.kind === 'type' && a.text === sending) { await page.keyboard.insertText(a.text); return true; }
          if (a.kind === 'enter') { await page.keyboard.press('Enter'); return true; }
          return false;
        });
      } catch (e) {
        console.error(`[bot] disclosure: trusted input unavailable, using DOM events: ${String(e)}`);
      }
    }
    sending = message;
    try {
      return await page.evaluate(
        async ({ fnName, message, timeoutMs, binding }) => {
          const w = globalThis as any;
          const send = w.MunshiBrowserUtils?.[fnName];
          if (typeof send !== 'function') return { channel: 'bot_name_only', detail: 'sender_missing' };
          const call = w[binding];
          const input = typeof call === 'function'
            ? {
                async click(el: any) {
                  el.scrollIntoView?.({ block: 'center', inline: 'center' });
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.height > 0) await call({ kind: 'click', x: r.left + r.width / 2, y: r.top + r.height / 2 });
                  else el.click();
                },
                async type(el: any, text: string) { el.focus(); await call({ kind: 'type', text }); },
                async pressEnter(el: any) { el.focus(); await call({ kind: 'enter' }); },
              }
            : undefined;
          return send(message, { input, timeoutMs, log: (m: string) => w.logBot?.(`[disclosure] ${m}`) });
        },
        { fnName, message, timeoutMs, binding: TRUSTED_INPUT_BINDING },
      );
    } finally {
      sending = null;
    }
  };
}
