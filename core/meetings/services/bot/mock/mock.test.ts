/**
 * A:V0 fidelity test (L2) — drive EVERY mock scenario offline through the REAL `createOrchestrator`
 * with in-memory sinks, and assert:
 *   • every emitted lifecycle.v1 event CONFORMS to the sealed lifecycle.schema.json (ajv, P8) and
 *     every transition is legal — the mock CANNOT emit off-contract (fidelity by construction);
 *   • each scenario reaches its intended terminal (completed/stopped · failed/<stage,reason>);
 *   • every published transcript.v1 segment conforms to transcript.schema.json;
 *   • the recording leg fires exactly for the recording scenarios.
 * This is the instrument's self-proof, runnable with no docker/redis/browser (on any host).
 * Run: npx tsx mock/mock.test.ts
 */
import Ajv2020, { type ValidateFunction } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createOrchestrator } from '../src/orchestrator.js';
import { canTransition, type Act, type BotStatus, type CompletionReason, type LifecycleEvent, type TranscriptSegment } from '../src/contracts.js';
import type { ActsSource, LifecycleSink, TranscriptSink } from '../src/ports.js';
import type { Invocation } from '../src/config.js';
import { SCENARIOS, type Scenario, type ScenarioName, fakeAloneness, fakeChatSender, fakeJoinDriver, fakePipeline, getScenario, mockSegment, scenarioFromMeetingUrl } from './scenarios.js';
import { runDisclosure, withPostAdmission } from '../src/disclosure.js';
import { createRemoteAudioActivityTap, createSilenceAlonenessSource } from '../src/aloneness.js';

let failed = 0;
const check = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};

// ── sealed-schema validators (ajv, loaded by path — the goldens are the spec, P8) ──
const HERE = dirname(fileURLToPath(import.meta.url));
const schemaOf = (name: string, def: string): ValidateFunction => {
  const path = join(HERE, '..', '..', '..', 'contracts', `${name}.v1`, `${name}.schema.json`);
  const s = JSON.parse(readFileSync(path, 'utf8'));
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema(s);
  return ajv.compile({ $ref: `${s.$id}#/$defs/${def}` });
};
const validateLifecycle = schemaOf('lifecycle', 'LifecycleEvent');
const validateSegment = schemaOf('transcript', 'TranscriptSegment');

const INV: Invocation = {
  platform: 'google_meet', meetingUrl: 'https://meet.google.com/abc-defg-hij', botName: 'mock-bot',
  redisUrl: 'redis://r:6379', connectionId: 'sess-mock', container_name: 'mtg-mock-bot', nativeMeetingId: 'abc-defg-hij',
  disclosure: { text: 'munshi.ai is transcribing this meeting.', privacy_notice_url: 'https://example.com/privacy' },
};

const seq = (e: LifecycleEvent[]) => e.map((x) => x.status);
const legal = (s: BotStatus[]) => s.every((st, i) => i === 0 || st === s[i - 1] || canTransition(s[i - 1], st));
const timeout = (ms: number) => new Promise<'timeout'>((r) => setTimeout(() => r('timeout'), ms));

interface Expect { status: BotStatus; reason: CompletionReason; stage?: string; recorded?: number; minSegs?: number; leaveAfter?: number; speakAfter?: number; }
const EXPECT: Record<ScenarioName, Expect> = {
  'normal':          { status: 'completed', reason: 'stopped', recorded: 1 },
  'emit-n-segments': { status: 'completed', reason: 'stopped', minSegs: 12 },
  'slow-join':       { status: 'completed', reason: 'stopped' },
  'recording':       { status: 'completed', reason: 'stopped', recorded: 1 },
  'speak-ack':       { status: 'completed', reason: 'stopped', speakAfter: 4 },
  'continue':        { status: 'completed', reason: 'stopped', recorded: 1 },
  'immediate-stop':  { status: 'completed', reason: 'stopped', leaveAfter: 8 },
  'join-timeout':    { status: 'failed', reason: 'awaiting_admission_timeout', stage: 'awaiting_admission' },
  'reject':          { status: 'failed', reason: 'awaiting_admission_rejected', stage: 'awaiting_admission' },
  'crash':           { status: 'failed', reason: 'join_failure', stage: 'active' },
  'silence-left-alone': { status: 'completed', reason: 'left_alone' },
  'left-alone':      { status: 'completed', reason: 'left_alone' },
  'evicted':         { status: 'completed', reason: 'evicted' },
};

async function drive(sc: Scenario, exp: Expect) {
  const events: LifecycleEvent[] = [];
  const lifecycle: LifecycleSink = { async emit(e) { events.push(e); } };
  const segments: TranscriptSegment[] = [];
  const transcript: TranscriptSink = { async publish(s) { segments.push(s); } };
  let recorded = 0;
  let stopRef: (r: CompletionReason) => void = () => {};
  const acts: ActsSource = { subscribe() { return () => {}; } };
  const join = fakeJoinDriver(sc, { joinDelayMs: sc.joinDelayMs ? 20 : undefined, evictAfterMs: sc.evictAfterMs != null ? 15 : undefined });
  const scenarioPipeline = fakePipeline(sc, INV, transcript, {
    endRun: (r) => stopRef(r),
    recordChunk: async () => { recorded++; },
    segGapMs: 1,
    endAfterMs: sc.endAfterMs != null ? 15 : undefined,   // fast self-end; immediate-stop keeps undefined → backend drives it
  });
  // The recording notice, wired as mock/main.ts wires it.
  const channel = sc.disclosure;
  const pipeline = channel
    ? withPostAdmission(scenarioPipeline, () => { void runDisclosure({ inv: INV, send: fakeChatSender(channel), lifecycle, log: () => {} }); })
    : scenarioPipeline;
  const activity = createRemoteAudioActivityTap();
  const aloneness = sc.silenceAlone
    ? createSilenceAlonenessSource({ activity, windowMs: 15, pollMs: 1, log: () => {} })
    : fakeAloneness(sc, sc.aloneAfterMs != null ? 15 : undefined);
  if (sc.silenceAlone) activity.ready();
  const o = createOrchestrator(INV, { lifecycle, join, pipeline, acts, aloneness });
  stopRef = o.stop;
  const runP = o.run();
  if (exp.leaveAfter != null) setTimeout(() => { void o.handle({ action: 'leave' } as Act); }, exp.leaveAfter);
  // speak-ack: mimic the composition-root voice tee — a `speak` act publishes a marker segment (round-trip).
  if (exp.speakAfter != null) setTimeout(() => { void transcript.publish(mockSegment(INV, 99, '[mock spoke]')); }, exp.speakAfter);
  const res = await Promise.race([runP, timeout(3000)]);
  return { events, segments, recorded, res };
}

function scenarioSelection(): void {
  console.log('[scenario selection]');
  const pick = (url: string | null) => scenarioFromMeetingUrl(url);
  check('mock= on a Zoom link selects the scenario', pick('https://zoom.us/j/12345678901?pwd=abc&mock=evicted') === 'evicted');
  check('mock= on a vanity Zoom link selects the scenario', pick('https://acme.zoom.us/j/12345678901?mock=left-alone&pwd=x') === 'left-alone');
  check('a link without mock= selects nothing', pick('https://meet.google.com/abc-defg-hij') === undefined);
  check('an empty mock= selects nothing', pick('https://zoom.us/j/12345678901?mock=') === undefined);
  check('no URL selects nothing', pick(null) === undefined);
  check('a non-URL selects nothing', pick('not a url') === undefined);
  check('the URL-selected names are registered', ['normal', 'reject', 'join-timeout', 'left-alone', 'evicted'].every((n) => getScenario(n).name === n));
  let threw = false;
  try { getScenario('no-such-scenario'); } catch { threw = true; }
  check('an unknown scenario name is refused', threw);
}

async function main(): Promise<void> {
  scenarioSelection();
  for (const name of Object.keys(SCENARIOS) as ScenarioName[]) {
    const sc = SCENARIOS[name];
    const exp = EXPECT[name];
    const { events, segments, recorded, res } = await drive(sc, exp);
    const tag = `[${name}]`;
    if (res === 'timeout') { check(`${tag} terminated`, false, 'run did not reach a terminal state within 3s'); continue; }
    const last = events[events.length - 1];
    check(`${tag} every lifecycle.v1 event conforms`, events.every((e) => validateLifecycle(e)), JSON.stringify(seq(events)));
    check(`${tag} transition sequence legal`, legal(seq(events)), JSON.stringify(seq(events)));
    check(`${tag} first event is joining`, events[0]?.status === 'joining');
    check(`${tag} terminal status=${exp.status} reason=${exp.reason}`,
      last?.status === exp.status && last?.completion_reason === exp.reason, JSON.stringify(last));
    if (exp.stage) check(`${tag} failure_stage=${exp.stage}`, last?.failure_stage === exp.stage, JSON.stringify(last));
    if (exp.status === 'completed') check(`${tag} reached active`, seq(events).includes('active'));
    if (exp.status === 'failed' && exp.stage === 'awaiting_admission') check(`${tag} never reached active`, !seq(events).includes('active'));
    check(`${tag} every transcript.v1 segment conforms`, segments.every((s) => validateSegment(s)), JSON.stringify(segments.slice(0, 1)));
    if (exp.minSegs != null) check(`${tag} published ≥${exp.minSegs} segments`, segments.length >= exp.minSegs, `got ${segments.length}`);
    if (exp.recorded != null) check(`${tag} recording leg fired ×${exp.recorded}`, recorded === exp.recorded, `got ${recorded}`);
    if (exp.recorded == null && !exp.speakAfter) check(`${tag} no recording when not requested`, recorded === 0);
    const reported = events.filter((e) => e.disclosure);
    if (sc.disclosure) {
      check(`${tag} disclosure reported once as ${sc.disclosure} on an active event`,
        reported.length === 1 && reported[0].status === 'active' && reported[0].disclosure?.channel === sc.disclosure, JSON.stringify(reported));
    } else {
      check(`${tag} no disclosure report`, reported.length === 0, JSON.stringify(reported));
    }
  }

  if (failed) { console.error(`\n❌ mock fidelity (L2): ${failed} check(s) FAILED.`); process.exit(1); }
  console.log(`\n✅ mock fidelity (L2): all ${Object.keys(SCENARIOS).length} scenarios drive a schema-valid lifecycle.v1 + transcript.v1 — the mock cannot emit off-contract.`);
}

void main();
