/**
 * The recording notice (BOT-03), offline: `runDisclosure` posts once and reports a contract-valid
 * outcome on a repeated `active` event, turns every sender fault into `bot_name_only`, and does
 * nothing for an invocation without a disclosure; `withPostAdmission` fires once, after the
 * pipeline started, without holding it up; `browserChatSender` picks the platform's page sender and
 * guards its trusted-input binding.
 *
 * Run: npx tsx src/disclosure.test.ts
 */
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page } from '@munshi/remote-browser';
import type { Invocation } from './config.js';
import type { LifecycleEvent } from './contracts.js';
import type { LifecycleSink, Pipeline } from './ports.js';
import {
  TRUSTED_INPUT_BINDING, browserChatSender, disclosureMessage, normalizeOutcome, runDisclosure, withPostAdmission,
} from './disclosure.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: unknown): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || detail === undefined ? '' : ` — ${JSON.stringify(detail)}`}`);
  if (!cond) failed++;
};

const HERE = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(readFileSync(join(HERE, '..', '..', '..', 'contracts', 'lifecycle.v1', 'lifecycle.schema.json'), 'utf8'));
const ajv = new (Ajv2020 as any)({ strict: false, allErrors: true });
(addFormats as any)(ajv);
ajv.addSchema(schema);
const conforms = ajv.compile({ $ref: `${schema.$id}#/$defs/LifecycleEvent` });

const DISCLOSURE = { text: 'munshi.ai is transcribing this meeting.\nRemove the bot to stop it.', privacy_notice_url: 'https://example.com/privacy' };
const INV: Invocation = {
  platform: 'google_meet', meetingUrl: 'https://meet.google.com/abc-defg-hij', botName: 'munshi.ai (recording)',
  redisUrl: 'redis://r:6379', connectionId: 'sess-1', disclosure: DISCLOSURE,
};
const NOW = '2026-10-27T10:00:07.000Z';

function sink(): LifecycleSink & { events: LifecycleEvent[] } {
  const events: LifecycleEvent[] = [];
  return { events, async emit(e) { events.push(e); } };
}

const quiet = () => { /* */ };

(async () => {
  console.log('\n=== the message ===');
  check('text + space + link, newlines collapsed',
    disclosureMessage(DISCLOSURE) === 'munshi.ai is transcribing this meeting. Remove the bot to stop it. https://example.com/privacy');

  console.log('\n=== posted in chat ===');
  {
    const lc = sink();
    let sent = '';
    const out = await runDisclosure({
      inv: INV, lifecycle: lc, now: () => NOW, log: quiet,
      send: async (m) => { sent = m; return { channel: 'chat', posted_at: '2026-10-27T10:00:06.500Z' }; },
    });
    check('outcome chat with the sender\'s posted_at', out?.channel === 'chat' && out.posted_at === '2026-10-27T10:00:06.500Z', out);
    check('the sender got the one-line message', sent === disclosureMessage(DISCLOSURE), sent);
    check('reported once', lc.events.length === 1, lc.events);
    const ev = lc.events[0];
    check('as a repeated active event for this session', ev?.status === 'active' && ev.connection_id === 'sess-1' && ev.timestamp === NOW, ev);
    check('the report conforms to lifecycle.v1', conforms(ev) === true, conforms.errors);
  }

  console.log('\n=== chat unavailable → bot_name_only with the reason ===');
  {
    const lc = sink();
    const out = await runDisclosure({ inv: INV, lifecycle: lc, now: () => NOW, log: quiet,
      send: async () => ({ channel: 'bot_name_only', detail: 'composer_disabled' }) });
    check('bot_name_only / composer_disabled', out?.channel === 'bot_name_only' && out.detail === 'composer_disabled', out);
    check('reported and conforming', lc.events.length === 1 && conforms(lc.events[0]) === true, lc.events);
  }

  console.log('\n=== platform without a sender ===');
  {
    const lc = sink();
    const out = await runDisclosure({ inv: { ...INV, platform: 'jitsi' }, send: null, lifecycle: lc, log: quiet });
    check('bot_name_only / platform_unsupported', out?.channel === 'bot_name_only' && out.detail === 'platform_unsupported', out);
    check('still reported', lc.events.length === 1);
  }

  console.log('\n=== sender throws ===');
  {
    const lc = sink();
    const out = await runDisclosure({ inv: INV, lifecycle: lc, log: quiet, send: async () => { throw new Error('Target page closed'); } });
    check('bot_name_only / error: …', out?.channel === 'bot_name_only' && out.detail === 'error: Target page closed', out);
    check('reported', lc.events.length === 1);
  }

  console.log('\n=== sender hangs past its budget ===');
  {
    const lc = sink();
    const t0 = Date.now();
    const out = await runDisclosure({ inv: INV, lifecycle: lc, log: quiet, timeoutMs: 50, hardStopGraceMs: 50,
      send: () => new Promise(() => { /* never settles */ }) });
    check('bot_name_only / timeout', out?.channel === 'bot_name_only' && out.detail === 'timeout', out);
    check('cut off at budget + grace', Date.now() - t0 < 1000, Date.now() - t0);
  }

  console.log('\n=== the report itself fails ===');
  {
    const out = await runDisclosure({ inv: INV, log: quiet, send: async () => ({ channel: 'chat', posted_at: NOW }),
      lifecycle: { async emit() { throw new Error('callback down'); } } });
    check('resolves the outcome, never rejects', out?.channel === 'chat');
  }

  console.log('\n=== no disclosure in the invocation ===');
  {
    const lc = sink();
    let called = false;
    const { disclosure: _omit, ...plain } = INV;
    const out = await runDisclosure({ inv: plain, lifecycle: lc, log: quiet, send: async () => { called = true; return {}; } });
    check('posts nothing, reports nothing', out === null && !called && lc.events.length === 0, { out, called, events: lc.events });
  }

  console.log('\n=== off-contract sender results ===');
  check('chat without posted_at gets the report time', JSON.stringify(normalizeOutcome({ channel: 'chat' }, () => NOW)) === JSON.stringify({ channel: 'chat', posted_at: NOW }));
  check('unknown shape → invalid_sender_result', normalizeOutcome('ok', () => NOW).detail === 'invalid_sender_result');
  check('detail capped at 500', (normalizeOutcome({ channel: 'bot_name_only', detail: 'x'.repeat(900) }, () => NOW).detail ?? '').length === 500);

  console.log('\n=== withPostAdmission ===');
  {
    const order: string[] = [];
    let releaseHook: () => void = () => {};
    const base: Pipeline = { async start() { order.push('start'); }, async stop() { order.push('stop'); } };
    const p = withPostAdmission(base, () => {
      order.push('hook');
      void new Promise<void>((r) => { releaseHook = r; }).then(() => order.push('hook-done'));
    });
    await p.start();
    check('the hook runs after start resolves', order.join(',') === 'start,hook', order);
    check('start does not wait for the hook to finish', !order.includes('hook-done'));
    await p.start();
    check('fires once', order.filter((x) => x === 'hook').length === 1, order);
    await p.stop();
    check('stop passes through', order.includes('stop'));
    releaseHook();

    let hooked = false;
    const crashing = withPostAdmission({ async start() { throw new Error('capture init'); }, async stop() {} }, () => { hooked = true; });
    let threw = false;
    try { await crashing.start(); } catch { threw = true; }
    check('a failed start propagates and posts nothing', threw && !hooked);

    const throwingHook = withPostAdmission({ async start() {}, async stop() {} }, () => { throw new Error('boom'); });
    let startThrew = false;
    const origErr = console.error; console.error = () => {};
    try { await throwingHook.start(); } catch { startThrew = true; } finally { console.error = origErr; }
    check('a throwing hook never fails the pipeline start', !startThrew);
  }

  console.log('\n=== browserChatSender ===');
  {
    const g = globalThis as any;
    const calls: string[] = [];
    let binding: ((a: unknown) => Promise<unknown>) | undefined;
    const page = {
      async exposeFunction(name: string, fn: (a: unknown) => Promise<unknown>) { calls.push(`expose:${name}`); binding = fn; g[name] = fn; },
      async evaluate(fn: (arg: unknown) => unknown, arg: unknown) { return fn(arg); },
      mouse: { async click(x: number, y: number) { calls.push(`mouse:${x},${y}`); } },
      keyboard: {
        async insertText(t: string) { calls.push(`insert:${t.length}`); },
        async press(k: string) { calls.push(`press:${k}`); },
      },
    } as unknown as Page;

    check('no sender for jitsi', browserChatSender(page, 'jitsi') === null);

    let pageSenderArgs: { message?: string; timeoutMs?: number } = {};
    g.MunshiBrowserUtils = {
      async sendZoomChatMessage(message: string, opts: any) {
        pageSenderArgs = { message, timeoutMs: opts.timeoutMs };
        const el = { getBoundingClientRect: () => ({ left: 10, top: 20, width: 100, height: 40 }), focus() {}, click() { calls.push('dom-click'); } };
        await opts.input.click(el);
        await opts.input.type(el, message);
        await opts.input.pressEnter(el);
        const off = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }), focus() {}, click() { calls.push('dom-click'); } };
        await opts.input.click(off);
        return { channel: 'chat', posted_at: NOW };
      },
    };
    const send = browserChatSender(page, 'zoom')!;
    const msg = disclosureMessage(DISCLOSURE);
    const res = await send(msg, 1234) as { channel: string };
    check('calls the zoom page sender with the message and budget', pageSenderArgs.message === msg && pageSenderArgs.timeoutMs === 1234, pageSenderArgs);
    check('returns what the page sender resolved', res.channel === 'chat');
    check('clicks at the element centre, types the notice, presses Enter, falls back to a DOM click for a box-less element',
      calls.join('|') === `expose:${TRUSTED_INPUT_BINDING}|mouse:60,40|insert:${msg.length}|press:Enter|dom-click`, calls);

    calls.length = 0;
    check('the binding is inert outside a send', (await binding!({ kind: 'enter' })) === false && calls.length === 0, calls);

    g.MunshiBrowserUtils = {
      async sendZoomChatMessage(_m: string, opts: any) {
        const el = { focus() {} };
        await opts.input.type(el, 'something else');
        return { channel: 'bot_name_only', detail: 'send_failed' };
      },
    };
    await send(msg, 1000);
    check('the binding only ever types the notice itself', !calls.some((c) => c.startsWith('insert:')), calls);
    check('exposed once across sends', !calls.includes(`expose:${TRUSTED_INPUT_BINDING}`), calls);

    g.MunshiBrowserUtils = {};
    const missing = await browserChatSender(page, 'google_meet')!(msg, 1000) as { detail?: string };
    check('a page without the sender reports sender_missing', missing.detail === 'sender_missing', missing);
    delete g.MunshiBrowserUtils;
    delete g[TRUSTED_INPUT_BINDING];
  }

  if (failed) { console.error(`\n❌ disclosure: ${failed} check(s) FAILED`); process.exit(1); }
  console.log('\n✅ disclosure: posts once after admission, reports a contract-valid outcome, never blocks the join');
})();
