import { randomBytes } from "node:crypto";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  mailsMentioning,
  meetingLink,
  owner,
  screenshots,
  setOrgEmailDomains,
  signIn,
  test,
} from "./helpers";
import { deliver, inviteMail } from "./smtp";

// Invite intake end to end, in an organisation of its own whose test domain signs its mail in the
// stack's fake DNS: an admin sets auto-join for the organisation and a team (a member gets the 403
// page); a forged invite and one from a non-member appear in the admin's quarantine list with plain
// reasons, and a forged invite naming another organisation's member does not; a member's signed
// invite appears under Upcoming at the right time in the viewer's zone, the sender gets a
// confirmation, the Skip switch marks the row and un-skipping restores it, and two minutes before
// the start the mock bot joins (the row moves to Live); with auto-join off, a new invite schedules
// nothing and the confirmation says why.

const ZONE = "America/New_York";
const LOCALE = "en-GB";

function hhmm(at: Date, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(at);
}

function zoneName(at: Date): string {
  const parts = new Intl.DateTimeFormat(LOCALE, { timeZone: ZONE, timeZoneName: "short" }).formatToParts(at);
  return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
}

/** Reload `page` until `check` passes (bounded; never a blind sleep). */
async function untilAfterReload(page: import("@playwright/test").Page, check: () => Promise<void>, timeoutMs: number) {
  await expect(async () => {
    await page.reload();
    await check();
  }).toPass({ timeout: timeoutMs, intervals: [2_000, 3_000, 5_000] });
}

test("INV: auto-join settings, the quarantine list, an invite scheduled, skipped and joined, and auto-join off", async ({ page, openPage }, testInfo) => {
  test.setTimeout(10 * 60_000);
  const tag = testInfo.project.name;
  const stamp = `${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;
  const domain = `i${stamp}.example.test`;

  // An organisation of its own, claiming the test domain; a member on it, and a team without them.
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await (await bossApi.get("/api/v1/me")).json()).org.id as string;
  setOrgEmailDomains(orgId, [domain]);
  const ana = await createMember(bossApi, `ana-${tag}-${stamp}@${domain}`, "member", `Ana Intake ${tag}`);
  const teamName = `Field ${tag} ${stamp}`;
  expect((await bossApi.post("/api/v1/teams", { data: { name: teamName } })).status()).toBe(201);
  await bossApi.dispose();

  // A member sees no admin navigation, and both new admin pages answer with the 403 page.
  const anaPage = await openPage({ timezoneId: ZONE, locale: LOCALE });
  await signIn(anaPage, ana);
  await expect(anaPage).toHaveURL(/\/$/);
  await expect(anaPage.getByRole("link", { name: "Bot" })).toHaveCount(0);
  await expect(anaPage.getByRole("link", { name: "Mail intake" })).toHaveCount(0);
  for (const path of ["/admin/bot", "/admin/intake"]) {
    await anaPage.goto(path);
    await expect(anaPage).toHaveURL(/\/403$/);
    await expect(anaPage.getByRole("heading", { name: "You don't have access to this page" })).toBeVisible();
  }

  // The admin sets the organisation's auto-join and a team override; a reload shows both.
  await signIn(page, boss);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("link", { name: "Bot" }).click();
  await expect(page).toHaveURL(/\/admin\/bot$/);
  const orgSelect = page.getByRole("combobox", { name: "Organisation" });
  await expect(orgSelect).toHaveText("When the notetaker is invited");
  await orgSelect.click();
  await page.getByRole("option", { name: "Every invitation with a link" }).click();
  const teamSelect = page.getByRole("combobox", { name: `Auto-join for ${teamName}` });
  await expect(teamSelect).toContainText("Organisation setting");
  await teamSelect.click();
  await page.getByRole("option", { name: "Off", exact: true }).click();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByTestId("form-success")).toHaveText("Auto-join settings are saved.");
  await page.reload();
  await expect(orgSelect).toHaveText("Every invitation with a link");
  await expect(teamSelect).toHaveText("Off");
  await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
  await expectNoOverflow(page);
  await screenshots(page, "admin-bot", testInfo);

  // The quarantine list starts empty.
  await page.getByRole("link", { name: "Mail intake" }).click();
  await expect(page).toHaveURL(/\/admin\/intake$/);
  await expect(page.getByTestId("quarantine-empty")).toBeVisible();
  await expectNoOverflow(page);

  // A forged invite in the member's name, a signed one from a non-member on the domain, and a forged
  // one in the name of another organisation's member (the suite's first owner).
  const forged = `Forged review ${tag} ${stamp}`;
  const stranger = `Stranger sync ${tag} ${stamp}`;
  const elsewhere = `Elsewhere plan ${tag} ${stamp}`;
  const later = new Date(Date.now() + 2 * 86_400_000);
  deliver(inviteMail({ from: ana.email, title: forged, start: later, link: meetingLink("normal") }), { from: ana.email, signed: false });
  const strangerAddress = `stranger-${stamp}@${domain}`;
  deliver(inviteMail({ from: strangerAddress, title: stranger, start: later, link: meetingLink("normal") }), {
    from: strangerAddress,
    signed: true,
  });
  const other = owner().email;
  deliver(inviteMail({ from: other, title: elsewhere, start: later, link: meetingLink("normal") }), { from: other, signed: false });

  const rows = page.getByTestId("quarantine-row");
  await untilAfterReload(page, async () => expect(rows).toHaveCount(2, { timeout: 1_000 }), 60_000);
  const forgedRow = rows.filter({ hasText: forged });
  await expect(forgedRow.getByTestId("quarantine-sender")).toHaveText(ana.email);
  await expect(forgedRow.getByTestId("quarantine-subject")).toHaveText(`Invitation: ${forged}`);
  await expect(forgedRow.getByTestId("quarantine-reason")).toHaveText("Sender not verified");
  await expect(forgedRow).toContainText("could not be proven to come from the address it names");
  await expect(forgedRow.getByTestId("quarantine-check")).toContainText(domain);
  const strangerRow = rows.filter({ hasText: stranger });
  await expect(strangerRow.getByTestId("quarantine-sender")).toHaveText(strangerAddress);
  await expect(strangerRow.getByTestId("quarantine-reason")).toHaveText("Not a member");
  await expect(strangerRow.getByTestId("quarantine-check")).toHaveText(`DMARC passed for ${domain} (DKIM aligned).`);
  await expect(page.getByText(elsewhere)).toHaveCount(0);
  await expectNoOverflow(page);
  await screenshots(page, "admin-intake", testInfo);

  // The member's signed invite, starting in about four minutes (the bot is due two minutes before).
  const start = new Date(Math.floor((Date.now() + 4 * 60_000) / 60_000) * 60_000 + 30_000);
  const title = `Intake standup ${tag} ${stamp}`;
  deliver(inviteMail({ from: ana.email, title, start, link: meetingLink("immediate-stop") }), { from: ana.email, signed: true });

  const row = anaPage.getByTestId("upcoming-row").filter({ hasText: title });
  await anaPage.goto("/");
  await untilAfterReload(anaPage, async () => expect(row).toHaveCount(1, { timeout: 1_000 }), 60_000);
  const meetingId = (await row.getAttribute("data-meeting-id"))!;
  // The start in the viewer's zone (New York), with the zone, never the UTC clock.
  const time = row.getByTestId("upcoming-time");
  await expect(time).toContainText(hhmm(start, ZONE));
  await expect(time).toContainText(zoneName(start));
  expect(hhmm(start, ZONE)).not.toBe(hhmm(start, "UTC"));
  await expect(time).not.toContainText(hhmm(start, "UTC"));
  await expect(row.getByTestId("status-pill")).toHaveAttribute("data-status", "scheduled");
  await expect(row).toHaveAttribute("data-skipped", "false");

  // The sender's confirmation.
  await expect
    .poll(async () => (await mailsMentioning(title)).filter((m) => m.to.includes(ana.email) && /scheduled/i.test(m.subject)).length, {
      timeout: 60_000,
    })
    .toBe(1);

  // Skip marks the row (and survives a reload); un-skip restores the join.
  const skip = row.getByRole("switch", { name: `Skip ${title}` });
  await expect(skip).not.toBeChecked();
  await skip.click();
  await expect(row).toHaveAttribute("data-skipped", "true");
  await expect(row.getByTestId("skipped-tag")).toHaveText("Skipped · the bot will not join");
  await anaPage.reload();
  await expect(row).toHaveAttribute("data-skipped", "true");
  await expect(skip).toBeChecked();
  await expectNoOverflow(anaPage);
  await screenshots(anaPage, "upcoming-skipped", testInfo);
  await skip.click();
  await expect(row).toHaveAttribute("data-skipped", "false");
  await expect(row.getByTestId("skipped-tag")).toHaveCount(0);
  await anaPage.reload();
  await expect(skip).not.toBeChecked();
  await screenshots(anaPage, "upcoming-joining", testInfo);

  // Two minutes before the start the mock bot joins: the meeting moves to Live.
  const anaApi = await apiAs(ana);
  const live = anaPage.locator(`[data-testid=live-card][data-meeting-id="${meetingId}"]`);
  await untilAfterReload(anaPage, async () => expect(live).toHaveCount(1, { timeout: 1_000 }), 5 * 60_000);
  const joined = await (await anaApi.get(`/api/v1/meetings/${meetingId}`)).json();
  expect(joined.source).toBe("invite_mail");
  expect(Date.parse(joined.scheduled_start)).toBe(start.getTime());
  expect(Date.now()).toBeLessThan(start.getTime());
  await expect(row).toHaveCount(0);
  expect((await anaApi.post(`/api/v1/meetings/${meetingId}/stop`)).ok()).toBe(true);
  await anaApi.dispose();

  // With auto-join off for the organisation, a new invite schedules nothing, and the confirmation
  // says why.
  await page.getByRole("link", { name: "Bot" }).click();
  await orgSelect.click();
  await page.getByRole("option", { name: "Off", exact: true }).click();
  await expect(page.getByTestId("org-auto-join-help")).toContainText("Invitations are not scheduled.");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByTestId("form-success")).toHaveText("Auto-join settings are saved.");
  const offTitle = `Intake planning ${tag} ${stamp}`;
  deliver(inviteMail({ from: ana.email, title: offTitle, start: later, link: meetingLink("normal") }), { from: ana.email, signed: true });
  await expect
    .poll(async () => (await mailsMentioning(offTitle)).filter((m) => m.to.includes(ana.email)), { timeout: 60_000 })
    .toEqual([expect.objectContaining({ text: expect.stringContaining("your organisation has turned off joining meetings from invitations") })]);
  await anaPage.reload();
  await expect(anaPage.getByTestId("section-upcoming")).toBeVisible();
  await expect(anaPage.getByText(offTitle)).toHaveCount(0);
});
