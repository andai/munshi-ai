// Shared e2e helpers: the Mailpit reader, a TOTP generator that never reuses a time step, unique
// test addresses, account setup through the API, and a `test` whose pages are watched for
// unexpected console errors and requests that leave the portal's origin.

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
import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import * as OTPAuth from "otpauth";

import { settings } from "./env";
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

// ─── page checks ────────────────────────────────────────────────────────────────────────────────

const EXPECTED_STATUS = /status of (401|403|429)\b/;

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
};

export const test = base.extend<Fixtures & { watched: Watch[] }>({
  // Every watched page of a test; checked when the test ends.
  watched: async ({}, use) => {
    const watched: Watch[] = [];
    await use(watched);
    expect(watched.flatMap((w) => w.errors), "unexpected console errors").toEqual([]);
    expect(watched.flatMap((w) => w.foreign), "requests that left the portal origin").toEqual([]);
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
