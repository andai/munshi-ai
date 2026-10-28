// Sending the bot from the portal and following one meeting, against the e2e stack's mock bot (a
// link's `mock=<scenario>` picks what the bot does). Each test makes its own members and links, so
// tests and runs never share a meeting.

import type { Page, TestInfo } from "@playwright/test";

import {
  MEETING_PASSCODE,
  apiAs,
  badgeLags,
  badgeStatuses,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  firstLineAt,
  meetingLink,
  owner,
  pollApiStatuses,
  recordBadgeStatuses,
  screenshots,
  signIn,
  test,
  uniqueEmail,
  type Account,
} from "./helpers";

const UUID = /\/meetings\/([0-9a-f-]{36})$/;
const BADGE_BUDGET_MS = 5_000;

async function newMember(label: string, testInfo: TestInfo): Promise<Account> {
  const admin = await apiAs(owner());
  const member = await createMember(admin, uniqueEmail(label, testInfo), "member", `${label} ${testInfo.project.name}`);
  await admin.dispose();
  return member;
}

async function sendBot(page: Page, link: string, language?: string): Promise<void> {
  await page.getByLabel("Meeting link").fill(link);
  if (language) {
    await page.getByRole("combobox", { name: "Language" }).click();
    await page.getByRole("option", { name: language }).click();
  }
  await page.getByRole("button", { name: "Send the bot" }).click();
}

async function meetingIdOf(page: Page): Promise<string> {
  await expect(page).toHaveURL(UUID, { timeout: 30_000 });
  return UUID.exec(new URL(page.url()).pathname)![1];
}

const badge = (page: Page) => page.getByTestId("meeting-status");
const lines = (page: Page) => page.getByTestId("transcript-line");

test("SC1/SC2/SC5: a member sends the bot and follows the meeting live to the end", async ({ page }, testInfo) => {
  const member = await newMember("Nia", testInfo);
  await recordBadgeStatuses(page);
  await signIn(page, member);
  await expect(page.getByTestId("join-form")).toBeVisible();
  await expectNoOverflow(page);

  await sendBot(page, meetingLink("normal"), "Hindi");
  const id = await meetingIdOf(page);
  const api = await apiAs(member);
  const poller = pollApiStatuses(api, id);

  await expect(page.getByTestId("meeting-title")).toHaveText("Zoom meeting");
  await expect(page.getByTestId("meeting-host")).toHaveText("zoom.us · Hindi");
  await expect(badge(page)).toHaveAttribute("data-status", "done", { timeout: 90_000 });
  // The andai row reaches done too (the reconciler can trail the live stream).
  await expect.poll(async () => (await (await api.get(`/api/v1/meetings/${id}`)).json()).status, { timeout: 15_000 }).toBe("done");
  await new Promise((r) => setTimeout(r, 300)); // one more poller tick records it
  const apiSeen = await poller.stop();
  const badgeSeen = await badgeStatuses(page);
  await api.dispose();

  // Each status reached the badge within 5 s of the andai row (or the badge was first).
  const lags = badgeLags(apiSeen, badgeSeen);
  testInfo.annotations.push({ type: "status lags (ms)", description: JSON.stringify(lags) });
  testInfo.annotations.push({ type: "badge statuses", description: JSON.stringify(badgeSeen) });
  expect(apiSeen.map((s) => s.status)).toEqual(expect.arrayContaining(["recording", "done"]));
  for (const { status, lagMs } of lags) {
    expect(lagMs, `badge for ${status}`).not.toBeNull();
    expect(lagMs!, `badge lag for ${status}`).toBeLessThanOrEqual(BADGE_BUDGET_MS);
  }
  expect(badgeSeen.map((s) => s.status)).toContain("recording");

  // The transcript arrived live: the first line within 5 s of the badge showing the recording.
  // (`normal` speaks all its lines at once right after admission and ends 1.5 s later, so whether
  // they land before or after "done" is the platform's timing; the count on screen is recorded.)
  const recording = badgeSeen.find((s) => s.status === "recording")!;
  const firstLine = await firstLineAt(page);
  const done = badgeSeen.find((s) => s.status === "done")!;
  testInfo.annotations.push({
    type: "transcript timing",
    description: JSON.stringify({ firstLineAfterRecordingMs: firstLine === null ? null : firstLine - recording.at, linesWhenDone: done.lines }),
  });
  expect(firstLine, "a transcript line appeared").not.toBeNull();
  expect(firstLine! - recording.at, "first line after the recording badge").toBeLessThanOrEqual(BADGE_BUDGET_MS);
  await expect(lines(page)).toHaveCount(3);
  await expect(lines(page).first()).toContainText("Speaker A");
  await expect(lines(page).first()).toContainText("00:00");
  await expect(lines(page).first()).toContainText("mock utterance 0");
  await expect(page.getByTestId("meeting-ended")).toHaveText("The bot was stopped and left the meeting.");
  await expect(page.getByRole("button", { name: "Stop the bot" })).toHaveCount(0);

  // Read again from andai-core: the recording notice the bot posted (once the reconciler recorded
  // it) and the stored transcript (the platform persists a finished meeting's lines shortly after).
  let reloads = 0;
  await expect(async () => {
    reloads += 1;
    await page.reload();
    await expect(page.getByTestId("consent-channel")).toHaveAttribute("data-channel", "chat", { timeout: 2_000 });
    await expect(lines(page)).toHaveCount(3, { timeout: 2_000 });
  }).toPass({ timeout: 45_000 });
  testInfo.annotations.push({ type: "reloads until notice and stored transcript", description: String(reloads) });
  await expect(page.getByTestId("consent-note")).toContainText("posted in the meeting chat");
  expect(await page.content()).not.toContain(MEETING_PASSCODE);
  await expectNoOverflow(page);
});

test("SC1/SC2/SC5: admission instructions in the lobby, the same link twice, and polling when the stream drops", async ({ page, expectConsoleErrors }, testInfo) => {
  const member = await newMember("Pia", testInfo);
  await signIn(page, member);
  const link = meetingLink("paced");
  await sendBot(page, link);
  const id = await meetingIdOf(page);

  // The bot waits in the lobby: the page says how to let it in.
  await expect(badge(page)).toHaveAttribute("data-status", "awaiting_admission", { timeout: 30_000 });
  await expect(page.getByTestId("admit-instructions")).toContainText("Zoom waiting room");
  await expect(page.getByTestId("admit-instructions")).toContainText("Participants → Waiting room → Admit");
  await expect(page.getByTestId("consent-note")).toContainText("tells everyone it is recording");
  await expect(page.getByTestId("transcript-mode")).toHaveAttribute("data-mode", "live");
  await expectNoOverflow(page);
  await screenshots(page, "meeting-awaiting-admission", testInfo);

  // From here the live stream fails for this page; it follows the meeting by polling instead.
  await page.route("**/api/v1/meetings/*/live", (route) => route.abort());
  expectConsoleErrors(/net::ERR_FAILED \(.*\/api\/v1\/meetings\/[0-9a-f-]+\/live\)$/);

  // The same link again lands on the same meeting.
  await page.goto("/");
  await sendBot(page, link);
  expect(await meetingIdOf(page)).toBe(id);
  await expect(page.getByTestId("transcript-mode")).toHaveAttribute("data-mode", "polling");
  await expect(page.getByTestId("transcript-mode")).toHaveText("Updating every few seconds");

  // Lines keep arriving while the bot records, through polls of the transcript.
  await expect(badge(page)).toHaveAttribute("data-status", "recording", { timeout: 30_000 });
  await expect(lines(page).first()).toBeVisible({ timeout: 15_000 });
  const early = await lines(page).count();
  await expect.poll(() => lines(page).count(), { timeout: 15_000 }).toBeGreaterThan(early);
  await expect.poll(() => lines(page).count(), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  await expect(badge(page), "at least 3 lines arrived before the meeting ended").toHaveAttribute("data-status", "recording");
  await expectNoOverflow(page);
  await screenshots(page, "meeting-recording", testInfo);

  await expect(badge(page)).toHaveAttribute("data-status", "done", { timeout: 45_000 });
  await expect(lines(page)).toHaveCount(8);
  const ids = await lines(page).evaluateAll((items) => items.map((item) => item.getAttribute("data-segment-id")));
  expect(new Set(ids).size, "each line once").toBe(8);
});

for (const [scenario, reason, title, text] of [
  ["reject", "admission_denied", "The bot was not let in", "declined the bot's request to join"],
  ["join-timeout", "admission_timeout", "Nobody let the bot in", "no one admitted it"],
] as const) {
  test(`SC2: a ${reason} failure is explained in plain words`, async ({ page }, testInfo) => {
    const member = await newMember(scenario === "reject" ? "Rae" : "Tia", testInfo);
    await signIn(page, member);
    await sendBot(page, meetingLink(scenario));
    await meetingIdOf(page);

    await expect(badge(page)).toHaveAttribute("data-status", "failed", { timeout: 60_000 });
    await expect(badge(page)).toHaveText("Could not join");
    const panel = page.getByTestId("failure-panel");
    await expect(panel).toHaveAttribute("data-reason", reason);
    await expect(panel).toContainText(title);
    await expect(panel).toContainText(text);
    await expect(page.getByTestId("admit-instructions")).toHaveCount(0);
    await expect(page.getByTestId("transcript-empty")).toHaveText("No transcript for this meeting.");
    await expectNoOverflow(page);
    if (scenario === "reject") await screenshots(page, "meeting-failed", testInfo);
  });
}

test("SC1: a link that is not a meeting link is refused on the form", async ({ page }, testInfo) => {
  const member = await newMember("Ivo", testInfo);
  await signIn(page, member);

  await page.getByLabel("Meeting link").fill("https://example.com/our-weekly-call");
  await expect(page.getByTestId("join-link-hint")).toHaveText("This doesn't look like a Google Meet, Teams or Zoom link.");
  await page.getByRole("button", { name: "Send the bot" }).click();
  await expect(page.getByTestId("form-error")).toHaveText("Paste a Google Meet, Microsoft Teams or Zoom meeting link.");
  await expect(page).toHaveURL(/\/$/);
  await expectNoOverflow(page);
});

test("SC1: a colleague's live meeting is refused with a message, and its page stops the bot", async ({ page, openPage }, testInfo) => {
  const first = await newMember("Ana", testInfo);
  const second = await newMember("Ben", testInfo);
  const link = meetingLink("immediate-stop");

  await signIn(page, first);
  await sendBot(page, link);
  const id = await meetingIdOf(page);
  const firstApi = await apiAs(first);
  try {
    await expect(badge(page)).toHaveAttribute("data-status", "recording", { timeout: 60_000 });

    // A member who may not read the colleague's meeting gets the message and stays on the form.
    const other = await openPage();
    await signIn(other, second);
    await expect(other.getByTestId("join-form")).toBeVisible();
    await sendBot(other, link);
    await expect(other.getByTestId("form-error")).toHaveText("The bot is already in this meeting for your organisation.");
    await expect(other).toHaveURL(/\/$/);
    // ...and the meeting's own page does not exist for them.
    await other.goto(`/meetings/${id}`);
    await expect(other.getByRole("heading", { name: "Meeting not found" })).toBeVisible();

    // The requester stops the bot from the meeting page.
    await page.getByRole("button", { name: "Stop the bot" }).click();
    await expect(badge(page)).toHaveAttribute("data-status", "done", { timeout: 60_000 });
    await expect(page.getByRole("button", { name: "Stop the bot" })).toHaveCount(0);
  } finally {
    // Never leave a bot running for the specs that follow (an org runs a few bots at most).
    await firstApi.post(`/api/v1/meetings/${id}/stop`).catch(() => null);
    await firstApi.dispose();
  }
});

test("TEAM-02: someone from another organisation gets the not-found page for a meeting", async ({ page, openPage }, testInfo) => {
  const member = await newMember("Kai", testInfo);
  await signIn(page, member);
  await sendBot(page, meetingLink("reject"));
  const id = await meetingIdOf(page);

  const outsider = await createOtherOrg(testInfo);
  const theirs = await openPage();
  await signIn(theirs, outsider);
  await expect(theirs.getByTestId("join-form")).toBeVisible();
  await theirs.goto(`/meetings/${id}`);
  await expect(theirs.getByTestId("meeting-not-found")).toBeVisible();
  await expect(theirs.getByRole("heading", { name: "Meeting not found" })).toBeVisible();
  await expectNoOverflow(theirs);

  const api = await apiAs(outsider);
  for (const path of ["", "/transcript", "/live"]) {
    expect((await api.get(`/api/v1/meetings/${id}${path}`)).status(), `GET …${path}`).toBe(404);
  }
  await api.dispose();
});
