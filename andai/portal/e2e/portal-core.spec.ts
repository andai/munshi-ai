// The portal's core chain on one meeting, end to end through the mock bot and andai-worker: a member
// sends the bot to a Hinglish meeting from the join form (`mock=hindi-lines`: six lines mixing
// Devanagari and Roman, and one recording chunk), the meeting finishes and andai stores its
// transcript, a Roman query on the dashboard finds the Devanagari speech and opens the meeting at
// the line, the transcript reads in both scripts, a team share lets a colleague in and its removal
// shuts him out, seeded minutes scroll to their evidence, and the requester deletes the meeting:
// hidden at once, then purged from andai's tables and from storage within 15 minutes.
//
// The meeting lives in an organisation of its own (one per run), so that organisation's recordings
// prefix holds this meeting's objects only. Each step's time is recorded as a test annotation, and
// with E2E_CHAIN_LOG=/abs/file also appended there as one JSON line (counts and seconds only).

import { appendFileSync } from "node:fs";
import { isAbsolute } from "node:path";

import type { APIRequestContext, Locator, Page, TestInfo } from "@playwright/test";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  meetingLink,
  meetingRows,
  recordingObjectCount,
  seedMinutes,
  signIn,
  test,
  uniqueEmail,
  type Account,
} from "./helpers";

const UUID_PATH = /\/meetings\/([0-9a-f-]{36})$/;
const LINES = 6; // HINDI_LINES in core/meetings/services/bot/mock/scenarios.ts
const PURGE_BUDGET_MS = 15 * 60_000;
const DEVANAGARI = /[ऀ-ॿ]/;

type World = { orgId: string; mira: Account; omar: Account };

async function json<T>(api: APIRequestContext, method: "get" | "post" | "put", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (await response.json()) as T;
}

/** An organisation of this run's own: Mira (member, Operations) and Omar (member, Finance). */
async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await json<{ org: { id: string } }>(bossApi, "get", "/api/v1/me")).org.id;
  const mira = await createMember(bossApi, uniqueEmail("chain-mira", testInfo), "member", "Mira Sample");
  const omar = await createMember(bossApi, uniqueEmail("chain-omar", testInfo), "member", "Omar Sample");
  const ops = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Operations" })).id;
  const finance = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Finance" })).id;
  await json(bossApi, "put", `/api/v1/teams/${ops}/members`, { members: [{ user_id: mira.id, role: "member" }] });
  await json(bossApi, "put", `/api/v1/teams/${finance}/members`, { members: [{ user_id: omar.id, role: "member" }] });
  await bossApi.dispose();
  return { orgId, mira, omar };
}

const lines = (page: Page) => page.getByTestId("transcript-line");
const line = (page: Page, id: string) => page.locator(`[data-testid=transcript-line][data-segment-id="${id}"]`);
const texts = (page: Page) => page.getByTestId("transcript-text").allTextContents();

async function expectShownAndHighlighted(target: Locator): Promise<void> {
  await expect(target).toHaveAttribute("data-highlighted", "true");
  await expect(target).toBeInViewport({ ratio: 0.9 });
}

const seconds = (ms: number) => Math.round(ms / 100) / 10;

test("SC1–SC5: one Hinglish meeting from the join form to its purge", async ({ page, openPage }, testInfo) => {
  test.setTimeout(20 * 60_000);
  const world = await buildWorld(testInfo);
  const timings: Record<string, number> = {};
  const counts: Record<string, unknown> = {};
  const record = () => {
    const entry = { at: new Date().toISOString(), project: testInfo.project.name, repeat: testInfo.repeatEachIndex, timings_s: timings, counts };
    testInfo.annotations.push({ type: "chain", description: JSON.stringify(entry) });
    const log = process.env.E2E_CHAIN_LOG;
    if (log && isAbsolute(log)) appendFileSync(log, `${JSON.stringify(entry)}\n`);
  };
  const api = await apiAs(world.mira);
  const meeting = async (id: string) => json<{ status: string; transcript_state: string }>(api, "get", `/api/v1/meetings/${id}`);

  // ── Dispatch from the join form; the meeting finishes and andai stores its transcript. ──
  await signIn(page, world.mira);
  await expect(page.getByTestId("join-form")).toBeVisible();
  await page.getByLabel("Meeting link").fill(meetingLink("hindi-lines"));
  const sent = Date.now();
  await page.getByRole("button", { name: "Send the bot" }).click();
  await expect(page).toHaveURL(UUID_PATH, { timeout: 30_000 });
  const id = UUID_PATH.exec(new URL(page.url()).pathname)![1];

  await expect.poll(async () => (await meeting(id)).status, { timeout: 180_000, intervals: [500] }).toBe("done");
  const done = Date.now();
  await expect.poll(async () => (await meeting(id)).transcript_state, { timeout: 180_000, intervals: [500] }).toBe("stored");
  const stored = Date.now();
  timings.dispatch_to_done = seconds(done - sent);
  timings.done_to_stored = seconds(stored - done);

  await expect(page.getByTestId("meeting-status")).toHaveAttribute("data-status", "done");
  await expect(lines(page)).toHaveCount(LINES, { timeout: 60_000 });
  await expect(page.getByTestId("attendee")).toHaveText(["SASpeaker A", "SBSpeaker B"]);

  const transcript = await json<{ segments: { id: string; text_raw: string; text_canonical: string; script: string }[] }>(
    api,
    "get",
    `/api/v1/meetings/${id}/transcript`,
  );
  const segments = transcript.segments;
  expect(segments).toHaveLength(LINES);
  expect(segments.filter((s) => s.script !== "latn").length, "Devanagari lines were stored").toBeGreaterThan(0);

  const objectsBefore = recordingObjectCount(world.orgId);
  const before = meetingRows(id);
  counts.objects_before = objectsBefore;
  counts.rows_before = before.rows;
  expect(objectsBefore, "the recording chunk is in storage").toBeGreaterThan(0);
  expect(before.rows.meetings).toBe(1);
  expect(before.rows.transcript_segments).toBe(LINES);

  // ── A Roman query on the dashboard finds the Devanagari speech and opens the meeting at its line. ──
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await expect(page.locator(`[data-testid=past-row][data-meeting-id="${id}"]`)).toHaveCount(1);
  await page.getByRole("searchbox", { name: "Search meetings" }).fill("kal");
  await expect(page.getByTestId("search-count")).toHaveText("1 meeting · only meetings you can open");
  const result = page.locator(`[data-testid=search-result][data-meeting-id="${id}"]`);
  const snippet = result.locator("[data-testid=search-snippet][data-source=transcript]").first();
  await expect(snippet.getByTestId("snippet-raw")).toHaveText(segments[0].text_raw);
  await expect(snippet.getByTestId("snippet-raw").locator("mark")).toHaveText(["कल"]);
  await expect(snippet.getByTestId("snippet-roman").locator("mark")).toHaveText(["kal"]);
  await expect(result.getByTestId("search-result-title")).toHaveAttribute("href", `/meetings/${id}#seg-${segments[0].id}`);
  await expectNoOverflow(page);
  await result.getByTestId("search-result-title").click();
  await expect(page).toHaveURL(new RegExp(`/meetings/${id}#seg-${segments[0].id}$`));
  await expectShownAndHighlighted(line(page, segments[0].id));

  // ── Both scripts: as said, then every line in Roman Hinglish. ──
  await expect(lines(page)).toHaveCount(LINES);
  expect(await texts(page)).toEqual(segments.map((s) => s.text_raw));
  expect((await texts(page))[0]).toMatch(/^कल /);
  const toggle = page.getByRole("group", { name: "Transcript script" });
  await toggle.getByRole("button", { name: "Roman Hinglish" }).click();
  await expect(page.getByTestId("transcript")).toHaveAttribute("data-script", "roman");
  const roman = await texts(page);
  expect(roman).toEqual(segments.map((s) => s.text_canonical));
  expect(roman.join(" ")).not.toMatch(DEVANAGARI);
  expect(roman[0]).toMatch(/^kal /i);
  await toggle.getByRole("button", { name: "Original script" }).click();
  expect(await texts(page)).toEqual(segments.map((s) => s.text_raw));
  await expectNoOverflow(page);

  // ── Share with a team: Omar (Finance) reads it; after removal he gets the not-found page. ──
  await page.getByRole("button", { name: "Share" }).click();
  const dialog = page.getByTestId("share-dialog");
  await dialog.getByTestId("share-find").fill("fin");
  await dialog.getByRole("button", { name: "Share with Finance" }).click();
  await expect(dialog.getByTestId("share-grant")).toContainText("Finance");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("meeting-meta")).toContainText("Shared with Finance");

  const omar = await openPage();
  await signIn(omar, world.omar);
  await expect(omar.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await expect(omar.locator(`[data-testid=past-row][data-meeting-id="${id}"]`)).toHaveCount(1);
  await omar.goto(`/meetings/${id}`);
  await expect(lines(omar)).toHaveCount(LINES);
  await expect(omar.getByRole("button", { name: "Share" })).toHaveCount(0);
  await expect(omar.getByRole("button", { name: "Delete" })).toHaveCount(0);

  await page.getByRole("button", { name: "Share" }).click();
  await dialog.getByRole("button", { name: "Stop sharing with Finance" }).click();
  await expect(dialog.getByTestId("share-grant")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("meeting-meta")).not.toContainText("Shared with");
  await omar.reload();
  await expect(omar.getByTestId("meeting-not-found")).toBeVisible();
  const omarApi = await apiAs(world.omar);
  expect((await omarApi.get(`/api/v1/meetings/${id}`)).status()).toBe(404);
  await omarApi.dispose();

  // ── Minutes (seeded for this meeting): an evidence time shows the cited line. ──
  const cited = seedMinutes(world.orgId, id).segments;
  expect(cited).toEqual(segments.map((s) => s.id));
  await page.goto(`/meetings/${id}`);
  const minutes = page.getByTestId("minutes");
  await expect(minutes.getByTestId("decision")).toHaveCount(2);
  const last = segments[LINES - 1].id;
  await minutes.getByTestId("unassigned-actions").locator(`[data-testid=evidence-link][data-segment-id="${last}"]`).click();
  await expectShownAndHighlighted(line(page, last));
  await expect(page).toHaveURL(new RegExp(`#seg-${last}$`));
  await expect(page.locator("[data-highlighted=true]")).toHaveCount(1);
  counts.rows_with_minutes = meetingRows(id).rows;

  // ── The requester deletes it: hidden at once, then purged from andai and from storage. ──
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByTestId("delete-dialog").getByRole("button", { name: "Delete meeting" }).click();
  const deleted = Date.now();
  await expect(page.getByTestId("meeting-not-found")).toBeVisible();
  for (const path of ["", "/transcript", "/mom"]) {
    expect((await api.get(`/api/v1/meetings/${id}${path}`)).status(), `GET …${path} after delete`).toBe(404);
  }
  const search = await json<{ total: number }>(api, "get", "/api/v1/search?q=kal");
  expect(search.total, "search finds nothing once deleted").toBe(0);

  let polls = 0;
  let left = { rows: before.rows, objects: objectsBefore };
  while (true) {
    polls += 1;
    left = { rows: meetingRows(id).rows, objects: recordingObjectCount(world.orgId) };
    if (left.rows.meetings === 0 && left.objects === 0) break;
    expect(Date.now() - deleted, `purge still pending: ${JSON.stringify(left)}`).toBeLessThanOrEqual(PURGE_BUDGET_MS);
    await new Promise((r) => setTimeout(r, 5_000));
  }
  timings.delete_to_purged = seconds(Date.now() - deleted);
  const after = meetingRows(id);
  counts.objects_after = recordingObjectCount(world.orgId);
  counts.rows_after = after.rows;
  counts.audit = after.audit;
  counts.purge_polls = polls;
  record();

  expect(counts.objects_after).toBe(0);
  expect(after.rows).toEqual({ meetings: 0, transcript_segments: 0, mom_documents: 0, meeting_shares: 0, consent_records: 0 });
  expect(after.audit).toEqual(["meeting.dispatch", "share.grant", "share.revoke", "meeting.delete", "meeting.purge"]);
  await page.reload();
  await expect(page.getByTestId("meeting-not-found")).toBeVisible();
  await api.dispose();
});
