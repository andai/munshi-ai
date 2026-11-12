// Erasing a member's data (OPS-02) end to end on the e2e stack. In an organisation of its own, a
// member sends the mock bot to a Hinglish meeting (`mock=hindi-lines`, one recording chunk) and
// andai stores its transcript. An admin opens /admin/members: the owner's row and their own carry no
// actions, the member's row offers "Erase data…", whose dialog keeps its button disabled until the
// member's email is typed. After the request the member is gone from the list and signed out, the
// "Data erasure" section shows the request as pending and then erased, and the audit log lists the
// request and its completion. andai-worker stops nothing (the meeting is over), hides the meeting for
// the purge, which removes it from the platform (its recording objects and transcript rows) and from
// andai, then scrubs the organisation and anonymises the account.
//
// Measured and recorded (test annotations, and with E2E_ERASURE_LOG=/abs/file one JSON line per
// run): seconds from the request to `done`, andai's rows of the meeting, the objects under the
// organisation's recordings prefix, the platform's transcript rows, and the Valkey keys naming the
// platform meeting with their TTLs (names, types and numbers only; never a value).

import { appendFileSync } from "node:fs";
import { isAbsolute } from "node:path";

import type { APIRequestContext, Page, TestInfo } from "@playwright/test";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  meetingLink,
  meetingRows,
  pipelineRow,
  platformTranscriptRows,
  recordingObjectCount,
  screenshots,
  signIn,
  test,
  uniqueEmail,
  valkeyKeysFor,
  type Account,
} from "./helpers";

// The SLA is 24 hours; on the e2e stack it takes minutes. The budget fails the run long before.
const ERASE_BUDGET_MS = 15 * 60_000;

type World = { orgId: string; boss: Account; ada: Account; sam: Account };

async function json<T>(api: APIRequestContext, method: "get" | "post", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (await response.json()) as T;
}

/** An organisation of this run's own: its owner, Ada (admin) and Sam (member). */
async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await json<{ org: { id: string } }>(bossApi, "get", "/api/v1/me")).org.id;
  const ada = await createMember(bossApi, uniqueEmail("erase-ada", testInfo), "admin", "Ada Sample");
  const sam = await createMember(bossApi, uniqueEmail("erase-sam", testInfo), "member", "Sam Sample");
  await bossApi.dispose();
  return { orgId, boss, ada, sam };
}

const row = (page: Page, account: Account) => page.getByTestId(`member-${account.email}`);
const seconds = (ms: number) => Math.round(ms / 100) / 10;

test("an admin erases a member's data from the members page", async ({ page }, testInfo) => {
  test.setTimeout(25 * 60_000);
  const world = await buildWorld(testInfo);
  const counts: Record<string, unknown> = {};
  const timings: Record<string, number> = {};

  // ── Sam's meeting: the mock bot records a Hinglish meeting and andai stores its transcript. ──
  const samApi = await apiAs(world.sam);
  const sent = await json<{ id: string }>(samApi, "post", "/api/v1/meetings/join", { join_url: meetingLink("hindi-lines") });
  const meetingId = sent.id;
  const meeting = async () => json<{ status: string; transcript_state: string }>(samApi, "get", `/api/v1/meetings/${meetingId}`);
  await expect.poll(async () => (await meeting()).status, { timeout: 180_000, intervals: [1000] }).toBe("done");
  await expect.poll(async () => (await meeting()).transcript_state, { timeout: 180_000, intervals: [1000] }).toBe("stored");
  const platformId = pipelineRow(meetingId).platform_meeting_id;
  expect(platformId, "the meeting reached the platform").not.toBeNull();
  counts.before = {
    rows: meetingRows(meetingId).rows,
    objects: recordingObjectCount(world.orgId),
    platform_transcript_rows: platformTranscriptRows(platformId!),
    valkey: valkeyKeysFor(platformId!),
  };
  expect(recordingObjectCount(world.orgId), "the recording chunk is in storage").toBeGreaterThan(0);
  expect(platformTranscriptRows(platformId!), "the platform holds the transcript").toBeGreaterThan(0);

  // ── The admin's members page: who can be erased, and the dialog's typed confirmation. ──
  await signIn(page, world.ada);
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/admin/members");
  await expect(row(page, world.sam)).toBeVisible();
  await expectNoOverflow(page);
  for (const locked of [world.boss, world.ada]) {
    await expect(row(page, locked).getByRole("button", { name: `Actions for ${locked.email}` })).toBeDisabled();
  }
  await row(page, world.sam).getByRole("button", { name: `Actions for ${world.sam.email}` }).click();
  await page.getByRole("menuitem", { name: "Erase data…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Erase Sam Sample’s data?" })).toBeVisible();
  await expect(dialog).toContainText("What they said in other people’s meetings");
  const erase = dialog.getByRole("button", { name: "Erase data" });
  await expect(erase).toBeDisabled();
  const confirm = dialog.getByLabel("Type their email address to confirm");
  await confirm.fill("someone-else@example.test");
  await expect(erase).toBeDisabled();
  await confirm.fill(world.sam.email.toUpperCase());
  await expect(erase).toBeEnabled();
  await expectNoOverflow(page);
  await screenshots(page, "erasure-dialog", testInfo);

  await erase.click();
  const asked = Date.now();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Sam Sample no longer has access.")).toBeVisible();
  await expect(row(page, world.sam)).toHaveCount(0);
  const adminApi = await apiAs(world.ada);
  const [{ id: requestId, subjectId }] = (
    await json<{ items: { id: string; subjectId: string; state: string }[] }>(adminApi, "get", "/api/v1/org/erasures")
  ).items;
  expect(subjectId).toBe(world.sam.id);
  const entry = page.getByTestId(`erasure-${world.sam.id}`);
  await expect(entry).toContainText("Sam Sample");
  await expect(entry).toHaveAttribute("data-state", /^(pending|running|done)$/);
  await screenshots(page, "erasure-pending", testInfo);

  // Sam is out at once: the session ends and signing in fails like a wrong password.
  expect((await samApi.get("/api/v1/me")).status()).toBe(401);
  const anonymous = await apiAs();
  const relogin = await anonymous.post("/api/v1/auth/login", { data: { email: world.sam.email, password: world.sam.password } });
  expect(relogin.status()).toBe(401);
  await anonymous.dispose();

  // ── andai-worker finishes it: the page turns to "Erased" on its own. ──
  const state = async () =>
    (await json<{ items: { id: string; state: string; finishedAt: string | null }[] }>(adminApi, "get", "/api/v1/org/erasures")).items.find(
      (item) => item.id === requestId,
    )!;
  await expect.poll(async () => (await state()).state, { timeout: ERASE_BUDGET_MS, intervals: [2000] }).toBe("done");
  timings.request_to_done = seconds(Date.now() - asked);
  await expect(entry).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  await expect(entry).toContainText("Erased");
  await screenshots(page, "erasure-done", testInfo);

  // ── Nothing of the meeting is left in andai, in storage or in the platform's transcript. ──
  const after = meetingRows(meetingId);
  counts.after = {
    rows: after.rows,
    objects: recordingObjectCount(world.orgId),
    platform_transcript_rows: platformTranscriptRows(platformId!),
    valkey: valkeyKeysFor(platformId!),
    audit: after.audit,
  };
  expect(after.rows).toEqual({ meetings: 0, transcript_segments: 0, mom_documents: 0, meeting_shares: 0, consent_records: 0 });
  expect(recordingObjectCount(world.orgId)).toBe(0);
  expect(platformTranscriptRows(platformId!)).toBe(0);
  expect(after.audit).toEqual(expect.arrayContaining(["meeting.dispatch", "meeting.delete", "meeting.purge"]));
  // The meeting is gone for every reader.
  const bossApi = await apiAs(world.boss);
  expect((await bossApi.get(`/api/v1/meetings/${meetingId}`)).status()).toBe(404);
  await bossApi.dispose();

  // ── The audit log shows the request and its completion. ──
  for (const action of ["user.erase.request", "user.erase.complete"]) {
    await page.goto(`/admin/audit?action=${action}`);
    await expect(page.locator(`[data-testid="audit-row"][data-action="${action}"]`)).toHaveCount(1);
  }
  await expectNoOverflow(page);
  await screenshots(page, "erasure-audit", testInfo);

  const record = { at: new Date().toISOString(), project: testInfo.project.name, timings_s: timings, counts };
  testInfo.annotations.push({ type: "erasure", description: JSON.stringify(record) });
  const log = process.env.E2E_ERASURE_LOG;
  if (log && isAbsolute(log)) appendFileSync(log, `${JSON.stringify(record)}\n`);
  await samApi.dispose();
  await adminApi.dispose();
});
