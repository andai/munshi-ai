import { request } from "@playwright/test";

import {
  PASSWORD,
  apiAs,
  createMember,
  expect,
  expectNoOverflow,
  mailpit,
  owner,
  portalOrigin,
  screenshots,
  sessionCookie,
  sessionStatus,
  signIn,
  signOut,
  test,
  uniqueEmail,
} from "./helpers";

test("SC1: the owner invites someone, who accepts from the mail, signs in and signs out", async ({ page, openPage }, testInfo) => {
  const boss = owner();
  const email = uniqueEmail("invitee", testInfo);

  await signIn(page, boss);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("link", { name: "Members" }).click();
  await expect(page).toHaveURL(/\/admin\/members$/);
  await page.getByRole("button", { name: "Invite people" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Email").fill(email);
  await dialog.getByRole("button", { name: "Send invite" }).click();
  await expect(page.getByTestId("form-success")).toContainText(`Invite sent to ${email}`);
  await expect(page.getByTestId(`invite-${email}`)).toBeVisible();
  await expectNoOverflow(page);

  const link = await mailpit.link(email, "invite", portalOrigin);
  const guest = await openPage();
  await guest.goto(link);
  await expectNoOverflow(guest);
  await guest.getByLabel("Your name").fill("Ravi Invitee");
  await guest.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await guest.getByLabel("Confirm new password").fill(PASSWORD);
  await guest.getByRole("button", { name: "Activate account" }).click();
  await expect(guest.getByText("Your account is ready.")).toBeVisible();
  await guest.getByRole("link", { name: "Sign in" }).click();
  await expect(guest).toHaveURL(/\/sign-in\?notice=account-ready$/);
  await screenshots(guest, "sign-in", testInfo);

  await signIn(guest, { email, password: PASSWORD, name: "Ravi Invitee" });
  await expect(guest).toHaveURL(/\/$/);
  await expect(guest.getByText("Welcome, Ravi Invitee")).toBeVisible();
  await expectNoOverflow(guest);
  const cookie = await sessionCookie(guest);
  await signOut(guest);
  expect(await sessionStatus(cookie)).toBe(401);

  await page.reload();
  await expect(page.getByTestId(`member-${email}`)).toContainText("Active");
  await expect(page.getByTestId(`invite-${email}`)).toHaveCount(0);
});

test("SC2: a forgotten password is reset from the mail, and the old session ends", async ({ openPage }, testInfo) => {
  const admin = await apiAs(owner());
  const account = await createMember(admin, uniqueEmail("reset", testInfo), "member", "Meera Reset");
  await admin.dispose();
  const newPassword = "e2e-new-password-for-meera";

  const before = await openPage();
  await signIn(before, account);
  await expect(before).toHaveURL(/\/$/);
  const oldCookie = await sessionCookie(before);

  const page = await openPage();
  await page.goto("/sign-in");
  await page.getByRole("link", { name: "Forgot password?" }).click();
  await expect(page).toHaveURL(/\/forgot-password$/);
  await expectNoOverflow(page);
  await page.getByLabel("Email").fill(account.email);
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();

  const link = await mailpit.link(account.email, "reset-password", portalOrigin);
  await page.goto(link);
  await expectNoOverflow(page);
  await page.getByLabel("New password", { exact: true }).fill(newPassword);
  await page.getByLabel("Confirm new password").fill(newPassword);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText("Your password is changed")).toBeVisible();

  expect(await sessionStatus(oldCookie)).toBe(401);
  await before.reload();
  await expect(before).toHaveURL(/\/sign-in$/);

  await signIn(page, account);
  await expect(page.getByTestId("form-error")).toHaveText("The email or password is not right.");
  await signIn(page, { ...account, password: newPassword });
  await expect(page).toHaveURL(/\/$/);
});

test("SC2: six wrong passwords show the rate-limit message", async ({ page }, testInfo) => {
  const email = uniqueEmail("nobody", testInfo);
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  for (let attempt = 1; attempt <= 6; attempt++) {
    await page.getByLabel("Password", { exact: true }).fill(`wrong-password-${attempt}`);
    const answered = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button", { name: "Sign in" }).click();
    const status = (await answered).status();
    expect(status, `attempt ${attempt}`).toBe(attempt <= 5 ? 401 : 429);
  }
  await expect(page.getByTestId("form-error")).toContainText("Too many sign-in attempts. Try again in 15 minutes.");
  await expectNoOverflow(page);
});

test("SC2: the rate limit holds when the client names its own address", async ({}, testInfo) => {
  // The portal replaces a client's X-Forwarded-For with the address it sees, so rotating the
  // header cannot spread failures over many limiter keys.
  const api = await request.newContext({ baseURL: portalOrigin, extraHTTPHeaders: { Origin: portalOrigin } });
  const email = uniqueEmail("spoof", testInfo);
  const statuses: number[] = [];
  for (let attempt = 1; attempt <= 6; attempt++) {
    const response = await api.post("/api/v1/auth/login", {
      data: { email, password: "wrong-password" },
      headers: { "X-Forwarded-For": `203.0.113.${attempt}` },
    });
    statuses.push(response.status());
  }
  await api.dispose();
  expect(statuses).toEqual([401, 401, 401, 401, 401, 429]);
});
