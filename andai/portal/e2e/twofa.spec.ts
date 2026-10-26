import type { APIRequestContext, Page } from "@playwright/test";

import {
  apiAs,
  createMember,
  enrollByApi,
  expect,
  expectNoOverflow,
  owner,
  screenshots,
  sessionCookie,
  sessionStatus,
  signIn,
  signOut,
  test,
  totp,
  uniqueEmail,
  type Account,
} from "./helpers";

/** On /2fa/setup: read the key, confirm a code, save the recovery codes and continue. */
async function enrollInBrowser(page: Page): Promise<{ secret: string; codes: string[] }> {
  await expect(page.getByRole("img", { name: "QR code for your authenticator app" })).toBeVisible();
  await page.getByText("Can't scan it? Enter a key instead").click();
  const secret = ((await page.getByTestId("totp-secret").textContent()) ?? "").replace(/\s+/g, "");
  expect(secret).toMatch(/^[A-Z2-7]{16,}$/);
  await page.getByLabel("Code from the app").fill(await totp(secret));
  await page.getByRole("button", { name: "Turn on two-factor login" }).click();
  const list = page.getByTestId("recovery-codes").locator("li");
  await expect(list).toHaveCount(10);
  const codes = await list.allTextContents();
  // Shown, never stored in the browser.
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  for (const code of codes) expect(stored).not.toContain(code);
  await expectNoOverflow(page);
  await page.getByLabel("I've saved my recovery codes").check();
  await page.getByRole("button", { name: "Continue" }).click();
  return { secret, codes };
}

async function verifyCode(page: Page, code: string): Promise<void> {
  await expect(page).toHaveURL(/\/2fa$/);
  await page.getByLabel("Authentication code").fill(code);
  await page.getByRole("button", { name: "Verify" }).click();
}

test("voluntary: a member turns on two-factor login and is asked for a code at the next sign-in", async ({ page }, testInfo) => {
  const admin = await apiAs(owner());
  const account = await createMember(admin, uniqueEmail("totp", testInfo), "member", "Tara Totp");
  await admin.dispose();

  await signIn(page, account);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "Security settings" }).click();
  await expect(page).toHaveURL(/\/profile\/security$/);
  await expect(page.getByTestId("mfa-status")).toHaveText("Off");
  await expectNoOverflow(page);
  await page.getByRole("link", { name: "Set up two-factor login" }).click();
  await expect(page).toHaveURL(/\/2fa\/setup\?from=profile$/);
  await expect(page.getByRole("img", { name: "QR code for your authenticator app" })).toBeVisible();
  await expectNoOverflow(page);
  await screenshots(page, "2fa-setup", testInfo);

  const { secret } = await enrollInBrowser(page);
  await expect(page).toHaveURL(/\/profile\/security$/);
  await expect(page.getByTestId("mfa-status")).toHaveText("On");

  // The codes were shown once: setup now says it is already on, and shows no codes.
  await page.goto("/2fa/setup");
  await expect(page.getByRole("heading", { name: "Two-factor login is on" })).toBeVisible();
  await expect(page.getByTestId("recovery-codes")).toHaveCount(0);

  await page.goto("/");
  await signOut(page);
  await signIn(page, account);
  await verifyCode(page, await totp(secret));
  await expect(page).toHaveURL(/\/$/);
});

test("SC6: wrong codes end the attempt, and a recovery code signs in", async ({ page }, testInfo) => {
  const admin = await apiAs(owner());
  const account = await createMember(admin, uniqueEmail("locked", testInfo), "member", "Lata Locked");
  await admin.dispose();
  const self = await apiAs(account);
  const { codes } = await enrollByApi(self);
  await self.dispose();

  await signIn(page, account);
  await expect(page).toHaveURL(/\/2fa$/);
  await expectNoOverflow(page);
  const ended = page.getByText("Too many wrong codes, so this sign-in attempt has ended.");
  let attempts = 0;
  for (; attempts < 6 && !(await ended.isVisible()); attempts++) {
    await page.getByLabel("Authentication code").fill("000000");
    const answered = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/mfa/verify"));
    await page.getByRole("button", { name: "Verify" }).click();
    await answered;
  }
  await expect(ended).toBeVisible();
  expect(attempts).toBeLessThanOrEqual(5);

  await page.getByRole("link", { name: "Sign in again" }).click();
  await signIn(page, account);
  await expect(page).toHaveURL(/\/2fa$/);
  await page.getByRole("button", { name: "Use a recovery code" }).click();
  await page.getByLabel("Recovery code").fill(codes[0]);
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
});

// Changes the whole organisation, so it runs last and switches the requirement off afterwards.
test.describe("an organisation that requires two-factor login", () => {
  let adminApi: APIRequestContext | undefined;

  test.afterAll(async () => {
    if (!adminApi) return;
    const response = await adminApi.put("/api/v1/org/security", { data: { require2fa: false } });
    expect(response.status()).toBe(200);
    await adminApi.dispose();
  });

  test("SC6: members are sent to enrollment, and an admin reset ends a member's session", async ({ page, openPage }, testInfo) => {
    const ownerApi = await apiAs(owner());
    const admin: Account = await createMember(ownerApi, uniqueEmail("secadmin", testInfo), "admin", "Sam Admin");
    const member = await createMember(ownerApi, uniqueEmail("forced", testInfo), "member", "Fern Forced");
    await ownerApi.dispose();
    adminApi = await apiAs(admin);
    admin.secret = (await enrollByApi(adminApi)).secret;

    const memberPage = await openPage();
    await signIn(memberPage, member);
    await expect(memberPage).toHaveURL(/\/$/);
    const memberCookie = await sessionCookie(memberPage);

    // The enrolled admin turns the requirement on.
    await signIn(page, admin);
    await verifyCode(page, await totp(admin.secret));
    await expect(page).toHaveURL(/\/$/);
    await page.getByRole("link", { name: "Security", exact: true }).click();
    await expect(page).toHaveURL(/\/admin\/security$/);
    await expectNoOverflow(page);
    await page.getByRole("switch", { name: "Not required" }).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("signed out now and must set it up at their next sign-in");
    await confirm.getByRole("button", { name: "Require it" }).click();
    await expect(page.getByTestId("form-success")).toHaveText("Two-factor login is now required.");
    await screenshots(page, "org-security", testInfo);

    // The member's session ended; their next sign-in lands on enrollment only.
    expect(await sessionStatus(memberCookie)).toBe(401);
    await memberPage.reload();
    await expect(memberPage).toHaveURL(/\/sign-in$/);
    await signIn(memberPage, member);
    await expect(memberPage).toHaveURL(/\/2fa\/setup$/);
    await expect(memberPage.getByText("Your organisation requires two-factor login. Set it up to continue.")).toBeVisible();
    await memberPage.goto("/admin/teams");
    await expect(memberPage).toHaveURL(/\/2fa\/setup$/);
    await enrollInBrowser(memberPage);
    await expect(memberPage).toHaveURL(/\/$/);
    await expect(memberPage.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();

    await memberPage.goto("/profile/security");
    await expect(memberPage.getByTestId("mfa-required-note")).toBeVisible();
    await expect(memberPage.getByRole("button", { name: "Turn off two-factor login" })).toHaveCount(0);
    const enrolledCookie = await sessionCookie(memberPage);

    // The admin resets the member's two-factor login: their session ends, and enrollment is next.
    await page.reload();
    const row = page.getByTestId(`security-${member.email}`);
    await expect(row).toContainText("On");
    await row.getByRole("button", { name: `Reset two-factor login for ${member.email}` }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Reset" }).click();
    await expect(page.getByTestId("form-success")).toHaveText("Two-factor login is reset for Fern Forced.");
    await expect(row).toContainText("Off");

    expect(await sessionStatus(enrolledCookie)).toBe(401);
    await memberPage.reload();
    await expect(memberPage).toHaveURL(/\/sign-in$/);
    await signIn(memberPage, member);
    await expect(memberPage).toHaveURL(/\/2fa\/setup$/);
  });
});
