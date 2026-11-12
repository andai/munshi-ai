// A copy of a person's data (OPS-02, DPDP access request) end to end on the e2e stack. In an
// organisation of its own, a member sends the mock bot to a Hinglish meeting (`mock=hindi-lines`, one
// recording) and andai stores its transcript. The member opens "Your data" from the account menu,
// asks for a copy (the button stays off while it is prepared), watches it turn ready with its expiry
// date, and downloads the zip. An admin asks for a copy of the member's data from the members page
// and downloads it from the "Data exports" section. Another member opening a download link gets "not
// found".
//
// Each zip is opened here (a small reader over node's zlib): `manifest.json` lists every other entry
// with the right size and SHA-256, `account.json` is the member's, and `meetings/*.json` holds the one
// meeting the member owns with as many transcript segments as andai serves for it. The downloaded
// files are deleted at the end of the test.

import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inflateRawSync } from "node:zlib";

import type { APIRequestContext, Download, Page, TestInfo } from "@playwright/test";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  meetingLink,
  screenshots,
  signIn,
  test,
  uniqueEmail,
  type Account,
} from "./helpers";

// andai-worker looks every 10 s; building a copy of one short meeting takes seconds.
const READY_BUDGET_MS = 3 * 60_000;

type World = { boss: Account; ada: Account; sam: Account; tom: Account };

async function json<T>(api: APIRequestContext, method: "get" | "post", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (await response.json()) as T;
}

/** An organisation of this run's own: its owner, Ada (admin), Sam and Tom (members). */
async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const ada = await createMember(bossApi, uniqueEmail("export-ada", testInfo), "admin", "Ada Sample");
  const sam = await createMember(bossApi, uniqueEmail("export-sam", testInfo), "member", "Sam Sample");
  const tom = await createMember(bossApi, uniqueEmail("export-tom", testInfo), "member", "Tom Sample");
  await bossApi.dispose();
  return { boss, ada, sam, tom };
}

// ─── a small zip reader: the central directory, then each entry stored or deflated ──────────────

function unzip(buffer: Buffer): Map<string, Buffer> {
  let end = buffer.length - 22;
  while (end >= 0 && buffer.readUInt32LE(end) !== 0x06054b50) end--;
  expect(end, "end of central directory").toBeGreaterThanOrEqual(0);
  const count = buffer.readUInt16LE(end + 10);
  let at = buffer.readUInt32LE(end + 16);
  const entries = new Map<string, Buffer>();
  for (let i = 0; i < count; i++) {
    expect(buffer.readUInt32LE(at), "central directory entry").toBe(0x02014b50);
    const method = buffer.readUInt16LE(at + 10);
    const compressed = buffer.readUInt32LE(at + 20);
    const size = buffer.readUInt32LE(at + 24);
    const nameLength = buffer.readUInt16LE(at + 28);
    const extraLength = buffer.readUInt16LE(at + 30);
    const commentLength = buffer.readUInt16LE(at + 32);
    const local = buffer.readUInt32LE(at + 42);
    const name = buffer.subarray(at + 46, at + 46 + nameLength).toString("utf8");
    const start = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
    const raw = buffer.subarray(start, start + compressed);
    const data = method === 0 ? Buffer.from(raw) : inflateRawSync(raw);
    expect([0, 8], `${name}: stored or deflated`).toContain(method);
    expect(data.length, `${name}: size`).toBe(size);
    entries.set(name, data);
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

type Manifest = {
  schema: string;
  subject: { userId: string };
  files: { path: string; bytes: number; sha256: string; mediaType: string }[];
  recordingsUnavailable: { meetingId: string; reason: string }[];
};

/** Save a download, open it and check the manifest against every entry; returns the entries. */
async function checkZip(download: Download, dir: string, subject: Account): Promise<{ entries: Map<string, Buffer>; manifest: Manifest }> {
  expect(download.suggestedFilename()).toMatch(/^munshi-export-\d{4}-\d{2}-\d{2}\.zip$/);
  const path = join(dir, `${Date.now()}-${download.suggestedFilename()}`);
  await download.saveAs(path);
  const entries = unzip(readFileSync(path));
  expect([...entries.keys()][0]).toBe("manifest.json");
  const manifest = JSON.parse(entries.get("manifest.json")!.toString("utf8")) as Manifest;
  expect(manifest.schema).toBe("munshi.export-bundle/v1");
  expect(manifest.subject.userId).toBe(subject.id);
  expect(new Set(manifest.files.map((f) => f.path))).toEqual(new Set([...entries.keys()].filter((n) => n !== "manifest.json")));
  for (const file of manifest.files) {
    const data = entries.get(file.path)!;
    expect(data.length, file.path).toBe(file.bytes);
    expect(createHash("sha256").update(data).digest("hex"), file.path).toBe(file.sha256);
  }
  const account = JSON.parse(entries.get("account.json")!.toString("utf8"));
  expect(account.email).toBe(subject.email);
  return { entries, manifest };
}

async function downloadFrom(page: Page, link: ReturnType<Page["getByTestId"]>): Promise<Download> {
  const [download] = await Promise.all([page.waitForEvent("download"), link.click()]);
  expect(await download.failure()).toBeNull();
  return download;
}

test("a member and an admin download a verified copy of the member's data", async ({ page, openPage }, testInfo) => {
  test.setTimeout(15 * 60_000);
  const world = await buildWorld(testInfo);
  const dir = mkdtempSync(join(tmpdir(), "munshi-export-e2e-"));
  try {
    // ── Sam's meeting: the mock bot records a Hinglish meeting and andai stores its transcript. ──
    const samApi = await apiAs(world.sam);
    const sent = await json<{ id: string }>(samApi, "post", "/api/v1/meetings/join", { join_url: meetingLink("hindi-lines") });
    const meeting = async () => json<{ status: string; transcript_state: string }>(samApi, "get", `/api/v1/meetings/${sent.id}`);
    await expect.poll(async () => (await meeting()).status, { timeout: 180_000, intervals: [1000] }).toBe("done");
    await expect.poll(async () => (await meeting()).transcript_state, { timeout: 180_000, intervals: [1000] }).toBe("stored");
    const served = await json<{ segments: unknown[] }>(samApi, "get", `/api/v1/meetings/${sent.id}/transcript`);
    expect(served.segments.length, "the mock meeting has a transcript").toBeGreaterThan(0);

    // ── Sam asks for a copy on "Your data", reached from the account menu. ──
    await signIn(page, world.sam);
    await expect(page).toHaveURL(/\/$/);
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("menuitem", { name: "Your data" }).click();
    await expect(page).toHaveURL(/\/profile\/data$/);
    await expect(page.getByRole("heading", { name: "Your data", level: 1 })).toBeVisible();
    await expect(page.getByText("Meetings other people own, even if they were shared with you")).toBeVisible();
    await expect(page.getByText("You haven’t asked for a copy yet.")).toBeVisible();
    await expectNoOverflow(page);
    const ask = page.getByRole("button", { name: "Request a copy" });
    await ask.click();
    await expect(ask).toBeDisabled();
    const mine = page.locator('[data-testid^="export-"][data-state]');
    await expect(mine).toHaveCount(1);
    await expect(mine.first()).toHaveAttribute("data-state", /^(pending|running|ready)$/);
    await screenshots(page, "export-pending", testInfo);
    await expect(mine.first()).toHaveAttribute("data-state", "ready", { timeout: READY_BUDGET_MS });
    await expect(mine.first().getByTestId("export-status")).toContainText("Available until");
    await expect(ask).toBeEnabled();
    await expectNoOverflow(page);
    await screenshots(page, "export-ready", testInfo);

    const own = await checkZip(await downloadFrom(page, mine.first().getByTestId("export-download")), dir, world.sam);
    const meetings = [...own.entries.keys()].filter((name) => name.startsWith("meetings/"));
    expect(meetings).toEqual([`meetings/${sent.id}.json`]);
    const exported = JSON.parse(own.entries.get(meetings[0])!.toString("utf8"));
    expect(exported.transcript.length).toBe(served.segments.length);
    expect(exported.transcript.some((s: { textRaw: string }) => /[ऀ-ॿ]/.test(s.textRaw)), "Devanagari kept as spoken").toBe(true);
    const recordings = [...own.entries.keys()].filter((name) => name.startsWith("recordings/"));
    testInfo.annotations.push({
      type: "export",
      description: JSON.stringify({
        project: testInfo.project.name,
        entries: own.entries.size,
        segments: exported.transcript.length,
        recordings: recordings.length,
        unavailable: own.manifest.recordingsUnavailable,
      }),
    });
    expect(recordings.length + own.manifest.recordingsUnavailable.length, "the recording is in the copy or said to be missing").toBeGreaterThan(0);
    expect(own.manifest.recordingsUnavailable, "the mock recording's master is readable").toEqual([]);

    // ── Ada asks for a copy of Sam's data from the members page and downloads it. ──
    const adminPage = await openPage();
    await signIn(adminPage, world.ada);
    await expect(adminPage).toHaveURL(/\/$/);
    await adminPage.goto("/admin/members");
    const samRow = adminPage.getByTestId(`member-${world.sam.email}`);
    await expect(samRow).toBeVisible();
    await samRow.getByRole("button", { name: `Actions for ${world.sam.email}` }).click();
    await adminPage.getByRole("menuitem", { name: "Export data" }).click();
    await expect(adminPage.getByText("A copy of Sam Sample's data is being prepared.")).toBeVisible();
    const entry = adminPage.locator(`[data-subject="${world.sam.id}"]`);
    await expect(entry).toHaveCount(1);
    await expect(entry).toContainText("Sam Sample");
    await expect(entry).toHaveAttribute("data-state", "ready", { timeout: READY_BUDGET_MS });
    await expect(entry.getByTestId("export-status")).toContainText("Available until");
    await expectNoOverflow(adminPage);
    await screenshots(adminPage, "export-admin-ready", testInfo);
    const admins = await checkZip(await downloadFrom(adminPage, entry.getByTestId("export-download")), dir, world.sam);
    expect([...admins.entries.keys()].filter((name) => name.startsWith("meetings/"))).toEqual(meetings);

    // Sam sees the admin's copy too, marked as asked by an admin.
    await page.reload();
    await expect(page.locator('[data-testid^="export-"][data-state]')).toHaveCount(2);
    await expect(page.getByText("by an admin")).toBeVisible();

    // ── Anyone else opening a download link gets "not found". ──
    const ids = (await json<{ items: { id: string; source: string }[] }>(samApi, "get", "/api/v1/me/exports")).items;
    const tomApi = await apiAs(world.tom);
    const bossApi = await apiAs(world.boss);
    for (const { id } of ids) {
      for (const outsider of [tomApi, bossApi]) {
        const refused = await outsider.get(`/api/v1/exports/${id}/download`);
        expect(refused.status()).toBe(404);
        expect((await refused.json()).code).toBe("no_such_export");
      }
    }
    // The owner sees the admin's copy listed, with no download.
    const listed = await json<{ items: { canDownload: boolean }[] }>(bossApi, "get", "/api/v1/org/exports");
    expect(listed.items.map((item) => item.canDownload)).toEqual([false]);

    // ── The audit log has the requests, the finished copies and the downloads. ──
    await adminPage.goto("/admin/audit?action=user.export.download");
    await expect(adminPage.locator('[data-testid="audit-row"][data-action="user.export.download"]')).toHaveCount(2);
    await tomApi.dispose();
    await bossApi.dispose();
    await samApi.dispose();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
