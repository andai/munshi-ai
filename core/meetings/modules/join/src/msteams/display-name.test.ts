/**
 * The Teams guest name and the disabled "Join now" terminal.
 *
 * Teams' anonymous pre-join accepts letters, numbers, spaces and `- ' . _ @` in the name field;
 * "munshi.ai (recording)" fails that check, the field turns invalid and "Join now" stays
 * disabled. The join must (1) fill a name Teams accepts and that still says it records, and
 * (2) if Teams keeps "Join now" disabled anyway, end with that reason instead of reading the
 * disabled button as a lobby.
 *
 * Fabricated-page test in the style of auth-redirect.test.ts, with a virtual clock.
 *
 * Run: npx tsx src/msteams/display-name.test.ts
 */

import { TEAMS_FALLBACK_GUEST_NAME, TEAMS_JOIN_DISABLED, TeamsJoinDisabledError, teamsGuestName } from './display-name';
import { joinMicrosoftTeams } from './join';

let vnow = 1_000_000;
(Date as any).now = () => vnow;

let captured: string[] = [];
const realConsoleLog = console.log;
function startCapture() {
  captured = [];
  console.log = (...a: any[]) => {
    const s = a.map(String).join(' ');
    try { captured.push(JSON.parse(s).msg ?? s); } catch { captured.push(s); }
  };
}
function stopCapture(): string[] { console.log = realConsoleLog; return captured; }

let passed = 0, failed = 0;
function check(name: string, ok: boolean, detail = '') {
  if (ok) { realConsoleLog(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { realConsoleLog(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` — ${detail}` : ''}`); failed++; }
}

/** Teams' rule as its pre-join states it: letters, numbers, spaces and - ' . _ @ */
const TEAMS_ALLOWED = /^[\p{L}\p{M}\p{N} \-'._@]+$/u;

const TEAMS_MESSAGE = "Your name can only include letters, numbers, spaces, and these symbols: - ' . _ @";

/**
 * A Teams guest pre-join: "Join now" and the name input are visible from the start. `joinEnabled`
 * decides whether Teams enables "Join now" once a name is filled.
 */
function preJoinPage(joinEnabled: (name: string) => boolean) {
  let filled = '';
  const clicks: string[] = [];
  const node = (sel: string): any => ({
    first: () => node(sel),
    isVisible: async () => sel.includes('Join now') || sel.includes('input[') || sel.includes('can only include'),
    isEnabled: async () => (sel.includes('Join now') ? joinEnabled(filled) : true),
    waitFor: async () => {
      if (sel.includes('input[')) return;
      throw new Error(`timeout waiting for ${sel}`);
    },
    fill: async (v: string) => { filled = v; },
    click: async () => { clicks.push(sel); },
    textContent: async () => (sel.includes('can only include') ? TEAMS_MESSAGE : null),
    getAttribute: async () => null,
  });
  const page: any = {
    url: () => 'https://teams.microsoft.com/light-meetings/launch?anon=true',
    goto: async () => {},
    waitForTimeout: async (ms: number) => { vnow += ms; },
    locator: node,
    evaluate: async () => undefined,
    keyboard: { press: async () => {} },
    isClosed: () => false,
  };
  return { page, filledName: () => filled, clicks };
}

async function driveJoin(page: any, botName: string) {
  const t0 = vnow;
  startCapture();
  let error: any = null;
  try {
    await joinMicrosoftTeams(page, 'https://teams.microsoft.com/meet/123456789?p=REDACTED', botName, { platform: 'teams', botName } as any);
  } catch (e) { error = e; }
  return { error, elapsedMs: vnow - t0, logs: stopCapture() };
}

(async () => {
  realConsoleLog('\n=== Teams guest name ===');
  const cases: Array<[string, string]> = [
    ['munshi.ai (recording)', 'munshi.ai - recording'],
    ['munshi.ai - recording', 'munshi.ai - recording'],
    ['Acme Notetaker [records]', 'Acme Notetaker - records'],
    ["O'Brien's bot", "O'Brien's bot"],
    ['मुंशी (रिकॉर्डिंग)', 'मुंशी - रिकॉर्डिंग'],
    ['notes@acme.example', 'notes@acme.example'],
    ['(((', TEAMS_FALLBACK_GUEST_NAME],
    ['', TEAMS_FALLBACK_GUEST_NAME],
  ];
  for (const [input, expected] of cases) {
    const actual = teamsGuestName(input);
    check(`${JSON.stringify(input)} → ${JSON.stringify(expected)}`, actual === expected, `got ${JSON.stringify(actual)}`);
    check(`${JSON.stringify(actual)} passes Teams' rule`, TEAMS_ALLOWED.test(actual));
  }
  check('the default name still says it records', /recording/.test(teamsGuestName('munshi.ai (recording)')));

  realConsoleLog('\n=== the join fills the Teams-legal name and clicks Join now ===');
  {
    const { page, filledName, clicks } = preJoinPage((name) => TEAMS_ALLOWED.test(name));
    const { error, logs } = await driveJoin(page, 'munshi.ai (recording)');
    check('the name field received the Teams-legal name', filledName() === 'munshi.ai - recording', filledName());
    check('Join now was clicked', clicks.some((s) => s.includes('Join now')));
    check('no disabled-join terminal', !(error instanceof TeamsJoinDisabledError), String(error));
    check('the log shows the name Teams got', logs.some((l) => l.includes('Display name set to "munshi.ai - recording"')));
  }

  realConsoleLog('\n=== Teams keeps Join now disabled → typed terminal, no lobby wait ===');
  {
    const { page, clicks } = preJoinPage(() => false);
    const { error, elapsedMs, logs } = await driveJoin(page, 'munshi.ai (recording)');
    check('ends with TeamsJoinDisabledError', error instanceof TeamsJoinDisabledError, String(error));
    check(`reason code is ${TEAMS_JOIN_DISABLED}`, error?.reasonCode === TEAMS_JOIN_DISABLED);
    check("the error carries Teams' own message", String(error?.message).includes('can only include letters'));
    check('Join now was never clicked', !clicks.some((s) => s.includes('Join now')));
    check('the flow stopped at Step 6 (no Step 6c / 6b / 7)', !logs.some((l) => /^Step (6c|6b|7):/.test(l)), logs.filter((l) => l.startsWith('Step')).join(' | '));
    check(`gave up within the 5 s grace after the name (took ${elapsedMs} ms virtual)`, elapsedMs < 20_000);
  }

  realConsoleLog(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
