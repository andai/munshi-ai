// Shared e2e helpers: the Mailpit reader, a TOTP generator that never reuses a time step, unique
// test addresses, account setup through the API (a second organisation too), mock-bot meeting
// links, status timing, and a `test` whose pages are watched for unexpected console errors and
// requests that leave the portal's origin.

import {
  expect,
  request,
  test as base,
  type APIRequestContext,
  type Browser,
  type BrowserContextOptions,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import * as OTPAuth from "otpauth";

import { PROJECT, settings } from "./env";
import { Mailpit } from "./mailpit";

export const { portalOrigin, mailpit: mailpitUrl } = settings();
export const mailpit = new Mailpit(mailpitUrl);

export const PASSWORD = "e2e-correct-horse-battery";

// ─── TOTP ───────────────────────────────────────────────────────────────────────────────────────

const PERIOD = 30;
const usedSteps = new Map<string, number>();

/** A valid code for `secret` from a time step not used before (andai-core rejects a replay). */
export async function totp(secret: string): Promise<string> {
  const normalized = secret.replace(/\s+/g, "").toUpperCase();
  const last = usedSteps.get(normalized);
  let step = Math.floor(Date.now() / 1000 / PERIOD);
  if (last !== undefined && step <= last) {
    const wait = (last + 1) * PERIOD * 1000 - Date.now() + 500;
    await new Promise((r) => setTimeout(r, wait));
    step = Math.floor(Date.now() / 1000 / PERIOD);
  }
  usedSteps.set(normalized, step);
  const generator = new OTPAuth.TOTP({
    secret: OTPAuth.Secret.fromBase32(normalized),
    algorithm: "SHA1",
    digits: 6,
    period: PERIOD,
  });
  return generator.generate({ timestamp: step * PERIOD * 1000 });
}

export function secretFromUri(uri: string): string {
  return new URL(uri).searchParams.get("secret") ?? "";
}

// ─── accounts ───────────────────────────────────────────────────────────────────────────────────

export type Account = { email: string; password: string; name: string; id?: string; secret?: string };

export function owner(): Account & { org: string } {
  const file = process.env.E2E_OWNER_FILE;
  if (!file) throw new Error("E2E_OWNER_FILE is not set; run the suite through global-setup");
  return JSON.parse(readFileSync(file, "utf8"));
}

/** A unique address per spec, test project and run. */
export function uniqueEmail(label: string, testInfo: TestInfo): string {
  return `${label}-${testInfo.project.name}-${Date.now()}-${randomBytes(2).toString("hex")}@example.test`;
}

/** An API client with the portal's Origin (andai-core refuses state changes without it). */
export async function apiAs(account?: Account): Promise<APIRequestContext> {
  const api = await request.newContext({ baseURL: portalOrigin, extraHTTPHeaders: { Origin: portalOrigin } });
  if (account) {
    const response = await api.post("/api/v1/auth/login", { data: { email: account.email, password: account.password } });
    expect(response.status(), `login for ${account.email}`).toBe(200);
    const { stage } = await response.json();
    if (stage === "mfa_pending") {
      if (!account.secret) throw new Error(`${account.email} needs a TOTP secret`);
      const verified = await api.post("/api/v1/auth/mfa/verify", { data: { code: await totp(account.secret) } });
      expect(verified.status()).toBe(200);
    }
  }
  return api;
}

/** Invite `email` as `role` (with an admin's API client) and accept the invite through the API. */
export async function createMember(
  admin: APIRequestContext,
  email: string,
  role: "member" | "admin" = "member",
  name = "",
): Promise<Account> {
  const invited = await admin.post("/api/v1/org/users", { data: { email, role } });
  expect(invited.status(), await invited.text()).toBe(201);
  const link = await mailpit.link(email, "invite", portalOrigin);
  const token = new URL(link).searchParams.get("token");
  const anonymous = await apiAs();
  const accepted = await anonymous.post("/api/v1/auth/invite/accept", {
    data: { token, password: PASSWORD, display_name: name || null },
  });
  expect(accepted.status(), await accepted.text()).toBe(200);
  await anonymous.dispose();
  const list = await (await admin.get("/api/v1/org/users")).json();
  const id = list.users.find((u: { email: string }) => u.email === email)?.id;
  return { email, password: PASSWORD, name: name || email, id };
}

/** Turn on TOTP for a signed-in API client; returns the secret and recovery codes. */
export async function enrollByApi(api: APIRequestContext): Promise<{ secret: string; codes: string[] }> {
  const started = await api.post("/api/v1/me/mfa");
  expect(started.status()).toBe(200);
  const secret = secretFromUri((await started.json()).otpauth_uri);
  const confirmed = await api.post("/api/v1/me/mfa/confirm", { data: { code: await totp(secret) } });
  expect(confirmed.status()).toBe(200);
  return { secret, codes: (await confirmed.json()).recovery_codes };
}

/**
 * A second organisation, made inside the stack's andai-core the way bootstrap makes the first (with
 * its platform principal and an owner invite by mail), and its owner signed up through the invite.
 * Bootstrap itself refuses once anyone exists, so the suite reaches past it here.
 */
export async function createOtherOrg(testInfo: TestInfo): Promise<Account & { org: string }> {
  const email = uniqueEmail("other-owner", testInfo);
  const org = `Other Org ${testInfo.project.name} ${Date.now() % 100000}`;
  const container = execFileSync(
    "docker",
    ["ps", "-q", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--filter", "label=com.docker.compose.service=andai-core"],
    { encoding: "utf8" },
  ).trim();
  if (!container) throw new Error(`no andai-core container in compose project ${PROJECT}`);
  execFileSync("docker", ["exec", "-i", container, "python", "-", email, org], {
    input: OTHER_ORG_SCRIPT,
    stdio: ["pipe", "ignore", "inherit"],
  });
  const link = await mailpit.link(email, "invite", portalOrigin);
  const anonymous = await apiAs();
  const accepted = await anonymous.post("/api/v1/auth/invite/accept", {
    data: { token: new URL(link).searchParams.get("token"), password: PASSWORD, display_name: "Oda Other" },
  });
  expect(accepted.status(), await accepted.text()).toBe(200);
  await anonymous.dispose();
  return { email, password: PASSWORD, name: "Oda Other", org };
}

const OTHER_ORG_SCRIPT = `
import asyncio, sys
from datetime import UTC, datetime
from andai_core.auth.invites import create_invite, invite_mail
from andai_core.crypto import load_keyring
from andai_core.db.session import create_app_engine
from andai_core.mail import SmtpMailer
from andai_core.org.service import create_org
from andai_core.platform.admin_client import HttpPlatformAdmin
from andai_core.settings import get_settings

email, org = sys.argv[1], sys.argv[2]
settings = get_settings()
now = datetime.now(UTC)
mailer = SmtpMailer(settings)

def invite_the_owner(tx, created):
    token = create_invite(tx, org_id=created.org_id, email=email, role="owner", invited_by=None, settings=settings, now=now)
    asyncio.run(mailer.send(invite_mail(settings, email=email, org_name=org, token=token, owner=True)))

engine = create_app_engine(settings=settings)
try:
    create_org(
        engine, name=org, owner_email=email, owner_display_name="Oda Other",
        admin=HttpPlatformAdmin.from_settings(settings), keyring=load_keyring(settings), now=now,
        finish=invite_the_owner, max_concurrent_bots=settings.org_max_concurrent_bots,
    )
finally:
    engine.dispose()
`;

// ─── meetings ───────────────────────────────────────────────────────────────────────────────────

/**
 * A Zoom-shaped link with a meeting number unique to this call (uuid-derived), so the one-bot-per-
 * link rule never couples two tests or two runs; the scenario rides only the `mock=` query, which
 * the e2e stack's mock bot plays (core/meetings/services/bot/mock/scenarios.ts).
 */
export function meetingLink(scenario: string): string {
  const number = BigInt(`0x${randomUUID().replace(/-/g, "")}`).toString().slice(-11).replace(/^0+/, "").padStart(11, "1");
  return `https://zoom.us/j/${number}?pwd=${MEETING_PASSCODE}&mock=${scenario}`;
}

/** The passcode every e2e meeting link carries; it must never appear on a page. */
export const MEETING_PASSCODE = "e2eSecretPwd";

export type StatusSeen = { status: string; at: number };

/**
 * Record, in the page, each status the meeting badge shows and when, and when the first transcript
 * line appeared (the browser's clock, which is this machine's). Call before the page loads; read
 * with `badgeStatuses` and `firstLineAt`.
 */
export async function recordBadgeStatuses(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const seen: { status: string; at: number; lines: number }[] = [];
    const state = window as unknown as { __badge: typeof seen; __firstLine: number | null };
    state.__badge = seen;
    state.__firstLine = null;
    const check = () => {
      const badge = document.querySelector("[data-testid=meeting-status]");
      const status = badge?.getAttribute("data-status");
      const lines = document.querySelectorAll("[data-testid=transcript-line]").length;
      if (status && seen[seen.length - 1]?.status !== status) seen.push({ status, at: Date.now(), lines });
      if (lines > 0 && state.__firstLine === null) state.__firstLine = Date.now();
    };
    new MutationObserver(check).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-status"] });
  });
}

export async function badgeStatuses(page: Page): Promise<(StatusSeen & { lines: number })[]> {
  return page.evaluate(() => (window as unknown as { __badge: { status: string; at: number; lines: number }[] }).__badge ?? []);
}

export async function firstLineAt(page: Page): Promise<number | null> {
  return page.evaluate(() => (window as unknown as { __firstLine: number | null }).__firstLine);
}

/** Poll GET /api/v1/meetings/{id} every 200 ms and keep each status change with its time. */
export function pollApiStatuses(api: APIRequestContext, id: string): { stop: () => Promise<StatusSeen[]> } {
  const seen: StatusSeen[] = [];
  let running = true;
  const loop = (async () => {
    while (running) {
      const response = await api.get(`/api/v1/meetings/${id}`).catch(() => null);
      if (response?.ok()) {
        const { status } = await response.json();
        if (seen[seen.length - 1]?.status !== status) seen.push({ status, at: Date.now() });
      }
      await new Promise((r) => setTimeout(r, 200));
    }
  })();
  return {
    stop: async () => {
      running = false;
      await loop;
      return seen;
    },
  };
}

/**
 * How long after the API showed each status the badge showed it (or a later one, when the badge
 * moved past a short-lived status), in ms. A negative lag means the badge was first (the live
 * stream can beat the 2 s reconciler that writes the API row).
 */
export function badgeLags(api: StatusSeen[], badge: StatusSeen[]): { status: string; lagMs: number | null }[] {
  return api.map((row, index) => {
    const reached = new Set(api.slice(index).map((r) => r.status));
    const first = badge.find((b) => reached.has(b.status)); // the badge log is in time order
    return { status: row.status, lagMs: first ? first.at - row.at : null };
  });
}

// ─── page checks ────────────────────────────────────────────────────────────────────────────────

// API refusals the pages answer on screen (sign-in steps, 403, not found, already in a meeting, a
// link that is not a meeting link, rate limits); the browser logs each one as a console error.
const EXPECTED_STATUS = /status of (401|403|404|409|422|429)\b/;

type Watch = { errors: string[]; foreign: string[] };

function watch(page: Page): Watch {
  const seen: Watch = { errors: [], foreign: [] };
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const url = message.location().url ?? "";
    const expected = EXPECTED_STATUS.test(message.text()) && new URL(url || portalOrigin).pathname.startsWith("/api/");
    if (!expected) seen.errors.push(`${message.text()} (${url})`);
  });
  page.on("pageerror", (error) => seen.errors.push(`page error: ${error.message}`));
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("data:") || url.startsWith("blob:")) return;
    if (new URL(url).origin !== portalOrigin) seen.foreign.push(url);
  });
  return seen;
}

/** On a phone-width page, nothing scrolls sideways. */
export async function expectNoOverflow(page: Page): Promise<void> {
  const viewport = page.viewportSize();
  if (!viewport || viewport.width >= 768) return;
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth, `horizontal overflow on ${page.url()}`).toBeLessThanOrEqual(innerWidth);
}

/** Light and dark screenshots of the current page; asserts the dark theme is really applied. */
export async function screenshots(page: Page, name: string, testInfo: TestInfo): Promise<void> {
  // Under test-results/ (cleared at the start of every run, git-ignored, skipped by the repo gates).
  const dir = join(__dirname, "..", "test-results", "screenshots");
  mkdirSync(dir, { recursive: true });
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect(page.locator("html")).toHaveClass(scheme === "dark" ? /\bdark\b/ : /^(?!.*\bdark\b).*$/);
    // No colour transition still running when the picture is taken.
    await page.waitForFunction(() =>
      document.getAnimations().every((a) => !(a instanceof CSSTransition) || a.playState !== "running"),
    );
    if (scheme === "dark") {
      const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(background).toBe("rgb(18, 17, 32)");
    }
    await page.screenshot({ path: join(dir, `${testInfo.project.name}-${name}-${scheme}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: null });
}

// ─── the test fixture ───────────────────────────────────────────────────────────────────────────

type Fixtures = {
  /** Open a page in a fresh context (its own cookies); watched like the default page. */
  openPage: (options?: BrowserContextOptions) => Promise<Page>;
  /** Console errors this test causes on purpose (e.g. a request it aborts); others still fail it. */
  expectConsoleErrors: (pattern: RegExp) => void;
};

type Watched = Watch[] & { allowed: RegExp[] };

export const test = base.extend<Fixtures & { watched: Watched }>({
  // Every watched page of a test; checked when the test ends.
  watched: async ({}, use) => {
    const watched = Object.assign([] as Watch[], { allowed: [] as RegExp[] });
    await use(watched);
    const errors = watched.flatMap((w) => w.errors).filter((e) => !watched.allowed.some((p) => p.test(e)));
    expect(errors, "unexpected console errors").toEqual([]);
    expect(watched.flatMap((w) => w.foreign), "requests that left the portal origin").toEqual([]);
  },
  expectConsoleErrors: async ({ watched }, use) => {
    await use((pattern) => {
      watched.allowed.push(pattern);
    });
  },
  page: async ({ page, watched }, use) => {
    watched.push(watch(page));
    await use(page);
  },
  openPage: async ({ browser, watched }, use, testInfo) => {
    const contexts: Awaited<ReturnType<Browser["newContext"]>>[] = [];
    await use(async (options) => {
      const context = await browser.newContext({ ...testInfo.project.use, baseURL: portalOrigin, ...options });
      contexts.push(context);
      const page = await context.newPage();
      watched.push(watch(page));
      return page;
    });
    for (const context of contexts) await context.close();
  },
});

export { expect };

// ─── UI steps ───────────────────────────────────────────────────────────────────────────────────

export async function signIn(page: Page, account: Account): Promise<void> {
  await page.goto("/sign-in");
  await expectNoOverflow(page);
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password", { exact: true }).fill(account.password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/sign-in$/);
}

export async function sessionCookie(page: Page): Promise<string> {
  const cookie = (await page.context().cookies()).find((c) => c.name === "andai_session");
  if (!cookie) throw new Error("no session cookie");
  return cookie.value;
}

/** Whether andai-core still accepts a session cookie (GET /api/v1/me through the portal). */
export async function sessionStatus(cookie: string): Promise<number> {
  const api = await request.newContext({ baseURL: portalOrigin, extraHTTPHeaders: { Cookie: `andai_session=${cookie}` } });
  const status = (await api.get("/api/v1/me")).status();
  await api.dispose();
  return status;
}
