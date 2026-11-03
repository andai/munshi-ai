// The post-meeting minutes pipeline end to end on one stack (the mock bot, andai-core and
// andai-worker, the e2e stack's fake LLM):
//
// 1. The chain. A member sends the bot to a Hinglish meeting from the join form (`mock=hindi-lines`,
//    team Operations) and shares it with a colleague outside the team while it runs. The meeting
//    finishes, the platform's `meeting.completed` webhook reaches andai-core once, andai stores the
//    transcript, and andai-worker writes version 1 of the minutes, which appears on the open page
//    without a reload. One recap mail reaches exactly the requester and the team (nobody else gets
//    mail about the meeting, the colleague with the share included). The requester edits the minutes
//    (version 2) and regenerates them in another template and language (version 3, Indian formal MoM
//    in Hindi), and downloads version 3 as Markdown and PDF; the grantee reads it and cannot change
//    it; the dashboard's Unassigned section lists the latest version's unassigned items and opens the
//    meeting at the cited line. Edits and regenerates send no further mail.
// 2. The restart. While the bot records a `mock=paced` meeting, the andai-core and andai-worker
//    containers are stopped; the platform ends the meeting while they are down; once they start
//    again, the meeting still gets exactly one version of the minutes and one recap.
//
// Times are andai's own (its database clock): meeting end → webhook, → transcript stored,
// → minutes stored, → recap sent. Each test records them as a test annotation, and with
// E2E_CHAIN_LOG=/abs/file also appends them there as one JSON line (counts and seconds only).
//
// E2E_REAL_LLM_ENV_FILE=/abs/file (a file holding only `ANDAI_LLM_*` keys) runs the chain against
// that route instead of the fake one: andai-core and andai-worker are recreated on it before the
// chain and back on the env file's fake route afterwards, and the restart test is skipped. Only the
// mock bot's synthetic lines reach the route. Assertions that depend on what a model writes (an
// unassigned item exists) hold for the fake route only; on a real route they are recorded.
//
// Every meeting lives in an organisation of this run's own; sample names only.

import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";

import type { APIRequestContext, Download, Locator, Page, TestInfo } from "@playwright/test";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  mailsMentioning,
  meetingLink,
  pipelineRow,
  platformStatus,
  screenshots,
  serviceRunning,
  signIn,
  startServices,
  stopServices,
  test,
  uniqueEmail,
  useLlmRoute,
  waitHealthy,
  type Account,
  type PipelineRow,
} from "./helpers";

test.describe.configure({ mode: "serial" });

const UUID_PATH = /\/meetings\/([0-9a-f-]{36})$/;
const LINES = 6; // HINDI_LINES in core/meetings/services/bot/mock/scenarios.ts
const DEVANAGARI = /[ऀ-ॿ]/;
const PIPELINE_TIMEOUT = 180_000;
const ANDAI = ["andai-core", "andai-worker"];
// The indian_formal_mom template's headings in Hindi, in its section order (andai-core's).
const FORMAL_HINDI_HEADINGS = [
  "उपस्थित सदस्य",
  "कार्यसूची एवं चर्चा के बिंदु",
  "लिए गए निर्णय",
  "की जाने वाली कार्रवाई (किसके द्वारा, कब तक)",
  "मुद्दे एवं चिंताएँ",
  "सारांश",
];

const realRouteFile = process.env.E2E_REAL_LLM_ENV_FILE || null;
if (realRouteFile && !isAbsolute(realRouteFile)) throw new Error("E2E_REAL_LLM_ENV_FILE must be an absolute path");
const ROUTE = realRouteFile ? "real" : "fake";

type World = { orgId: string; teamId: string; mira: Account; lena: Account; omar: Account; boss: Account };

let world: World;

async function json<T>(api: APIRequestContext, method: "get" | "post" | "put", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (await response.json()) as T;
}

/** An organisation of this run's own: Lena leads Operations, Mira is in it, Omar is in Finance. */
async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await json<{ org: { id: string } }>(bossApi, "get", "/api/v1/me")).org.id;
  const member = (label: string, name: string) => createMember(bossApi, uniqueEmail(label, testInfo), "member", name);
  const mira = await member("pipe-mira", "Mira Sample");
  const lena = await member("pipe-lena", "Lena Sample");
  const omar = await member("pipe-omar", "Omar Sample");
  const ops = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Operations" })).id;
  const finance = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Finance" })).id;
  await json(bossApi, "put", `/api/v1/teams/${ops}/members`, {
    members: [
      { user_id: lena.id, role: "lead" },
      { user_id: mira.id, role: "member" },
    ],
  });
  await json(bossApi, "put", `/api/v1/teams/${finance}/members`, { members: [{ user_id: omar.id, role: "member" }] });
  await bossApi.dispose();
  return { orgId, teamId: ops, mira, lena, omar, boss };
}

test.beforeAll(async ({}, testInfo) => {
  test.setTimeout(300_000);
  if (realRouteFile) await useLlmRoute(realRouteFile);
  world = await buildWorld(testInfo);
});

test.afterAll(async () => {
  test.setTimeout(300_000);
  if (realRouteFile) await useLlmRoute(null);
});

// ─── shared steps ───────────────────────────────────────────────────────────────────────────────

const seconds = (later: string | number | null, earlier: string | number | null): number | null => {
  if (later === null || earlier === null) return null;
  return Math.round((new Date(later).getTime() - new Date(earlier).getTime()) / 100) / 10;
};

/** andai's pipeline times for a meeting whose recap is settled. */
function pipelineTimings(row: PipelineRow): Record<string, number | null> {
  return {
    end_to_webhook_s: seconds(row.webhooks[0]?.received_at ?? null, row.ended_at),
    end_to_transcript_s: seconds(row.transcript_at, row.ended_at),
    transcript_to_mom_s: seconds(row.mom_at, row.transcript_at),
    end_to_mom_s: seconds(row.mom_at, row.ended_at),
    mom_to_recap_s: seconds(row.recap_at, row.mom_at),
    end_to_recap_s: seconds(row.recap_at, row.ended_at),
  };
}

function record(testInfo: TestInfo, kind: "chain" | "restart", timings: Record<string, unknown>, counts: Record<string, unknown>): void {
  const entry = {
    kind,
    llm: ROUTE,
    at: new Date().toISOString(),
    project: testInfo.project.name,
    repeat: testInfo.repeatEachIndex,
    timings_s: timings,
    counts,
  };
  testInfo.annotations.push({ type: kind, description: JSON.stringify(entry) });
  const log = process.env.E2E_CHAIN_LOG;
  if (log && isAbsolute(log)) appendFileSync(log, `${JSON.stringify(entry)}\n`);
}

async function waitFor(meetingId: string, check: (row: PipelineRow) => boolean, what: string, timeout = PIPELINE_TIMEOUT): Promise<PipelineRow> {
  let row = pipelineRow(meetingId);
  await expect
    .poll(
      () => {
        row = pipelineRow(meetingId);
        return check(row);
      },
      { message: what, timeout, intervals: [500] },
    )
    .toBe(true);
  return row;
}

/** The single recap: exactly one mail names the meeting, to the requester and the team, nobody else. */
async function expectOneRecap(meetingId: string, recipients: Account[]): Promise<void> {
  const mails = await mailsMentioning(meetingId);
  expect(mails.map((m) => ({ to: m.to, cc: m.cc, bcc: m.bcc })), "mail about the meeting").toEqual([
    { to: recipients.map((a) => a.email.toLowerCase()).sort(), cc: [], bcc: [] },
  ]);
  expect(mails[0].text).toContain(`/meetings/${meetingId}`);
}

const minutes = (page: Page) => page.getByTestId("minutes");
const state = (page: Page) => page.getByTestId("minutes-state");
const line = (page: Page, id: string) => page.locator(`[data-testid=transcript-line][data-segment-id="${id}"]`);

/** Choose `option` in the combobox named `combobox` inside `scope` (the dashboard has two "Team"s). */
async function pick(page: Page, combobox: string, option: string, scope: Page | Locator = page): Promise<void> {
  await scope.getByRole("combobox", { name: combobox }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
  await expect(scope.getByRole("combobox", { name: combobox })).toHaveText(option);
}

async function download(page: Page, item: "Markdown (.md)" | "PDF (.pdf)", testInfo: TestInfo): Promise<{ name: string; bytes: Buffer }> {
  await page.getByRole("button", { name: "Download" }).click();
  const [file] = await Promise.all([page.waitForEvent("download"), page.getByRole("menuitem", { name: item }).click()]);
  const bytes = readFileSync(await (file as Download).path());
  // Kept under test-results/ (git-ignored, cleared every run) for a look at the file itself.
  const dir = join(__dirname, "..", "test-results", "downloads");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${testInfo.project.name}-${testInfo.repeatEachIndex}-${file.suggestedFilename()}`), bytes);
  return { name: file.suggestedFilename(), bytes };
}

// ─── the chain ──────────────────────────────────────────────────────────────────────────────────

test("SC1/SC2/SC4/SC5/SC6: a Hinglish meeting from the join form to its minutes, recap, edits, downloads and the Unassigned section", async ({
  page,
  openPage,
}, testInfo) => {
  test.setTimeout(20 * 60_000);
  const { mira, lena, omar } = world;
  const timings: Record<string, number | null> = {};
  const counts: Record<string, unknown> = {};
  const api = await apiAs(mira);

  // ── Dispatch from the join form, team Operations; share with Omar (Finance) while it runs. ──
  await signIn(page, mira);
  await expect(page.getByTestId("join-form")).toBeVisible();
  await page.getByLabel("Meeting link").fill(meetingLink("hindi-lines"));
  const joinForm = page.getByTestId("join-form");
  await pick(page, "Language", "Hinglish", joinForm);
  await pick(page, "Team", "Operations", joinForm);
  await page.getByRole("button", { name: "Send the bot" }).click();
  await expect(page).toHaveURL(UUID_PATH, { timeout: 30_000 });
  const id = UUID_PATH.exec(new URL(page.url()).pathname)![1];
  await page.evaluate(() => ((window as unknown as { __chainMark: boolean }).__chainMark = true));
  await json(api, "post", `/api/v1/meetings/${id}/shares`, { grantee_type: "user", grantee_id: omar.id });

  // ── The meeting ends; the webhook arrives once; the transcript and the minutes are stored. ──
  let row = await waitFor(id, (r) => r.status === "done", "meeting done");
  row = await waitFor(id, (r) => r.transcript_state === "stored", "transcript stored");
  row = await waitFor(id, (r) => r.mom_state === "stored", "minutes stored");
  expect(row.webhooks.map((w) => w.event_type), "platform webhooks accepted for the meeting").toEqual(["meeting.completed"]);

  // Version 1 appears on the page the requester left open, without a reload.
  await expect(minutes(page)).toHaveAttribute("data-version", "1", { timeout: 30_000 });
  expect(await page.evaluate(() => (window as unknown as { __chainMark?: boolean }).__chainMark === true), "no reload").toBe(true);
  await expect(page.getByTestId("meeting-status")).toHaveAttribute("data-status", "done");
  await expect(page.getByTestId("transcript-line")).toHaveCount(LINES, { timeout: 60_000 });
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Version 1 · Standard");
  await expectNoOverflow(page);
  await screenshots(page, "pipeline-minutes-v1", testInfo);

  // ── One recap: the requester and the team only. ──
  row = await waitFor(id, (r) => r.recap_state !== "pending" && r.recap_state !== "none", "recap settled", 120_000);
  expect(row.recap_state).toBe("sent");
  Object.assign(timings, pipelineTimings(row));
  await expectOneRecap(id, [mira, lena]);

  // ── The requester edits: version 2. ──
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const editor = page.getByTestId("minutes-editor");
  await expect(editor).toBeVisible();
  await editor.getByLabel("Summary").fill("Edited in the pipeline run: the deadline, the report and the budget (sample).");
  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(minutes(page)).toHaveAttribute("data-version", "2");
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Edited by Mira Sample");
  await expect(minutes(page).getByTestId("minutes-summary")).toContainText("Edited in the pipeline run");

  // ── …and regenerates as Indian formal MoM in Hindi: version 3. ──
  await page.getByRole("button", { name: "Regenerate" }).click();
  const dialog = page.getByTestId("regenerate-dialog");
  await pick(page, "Template", "Indian formal MoM");
  await pick(page, "Output language", "Hindi");
  const regenerated = Date.now();
  await dialog.getByRole("button", { name: "Regenerate" }).click();
  await expect(state(page)).toHaveAttribute("data-state", "pending");
  await expect(minutes(page)).toHaveAttribute("data-version", "3", { timeout: PIPELINE_TIMEOUT });
  timings.regenerate_to_shown_s = seconds(Date.now(), regenerated);
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Version 3 · Indian formal MoM · Hindi");
  await expect(minutes(page).getByTestId("minutes-heading")).toHaveText(FORMAL_HINDI_HEADINGS);
  expect(await minutes(page).getByTestId("minutes-summary").textContent()).toMatch(DEVANAGARI);
  await expectNoOverflow(page);
  await screenshots(page, "pipeline-minutes-v3", testInfo);

  // ── Downloads of the version on screen. ──
  const markdown = await download(page, "Markdown (.md)", testInfo);
  expect(markdown.name).toMatch(/-v3\.md$/);
  const text = markdown.bytes.toString("utf8");
  for (const heading of ["उपस्थित सदस्य", "लिए गए निर्णय", "सारांश"]) expect(text).toContain(`## ${heading}`);
  const pdf = await download(page, "PDF (.pdf)", testInfo);
  expect(pdf.name).toMatch(/-v3\.pdf$/);
  expect(pdf.bytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");
  expect(pdf.bytes.length).toBeGreaterThan(1_000);
  counts.download_bytes = { markdown: markdown.bytes.length, pdf: pdf.bytes.length };

  // Every version is stored as written: the pipeline's two on the configured route, the edit between.
  row = pipelineRow(id);
  expect(row.versions.map(({ version, template_id, output_language }) => [version, template_id, output_language])).toEqual([
    [1, "standard", "hinglish"],
    [2, "standard", "hinglish"],
    [3, "indian_formal_mom", "hi"],
  ]);
  const latest = await json<{ actionItems: { text: string; owner: string; evidence: string[] }[]; llmProfileId: string; model: string }>(
    api,
    "get",
    `/api/v1/meetings/${id}/mom`,
  );
  const first = await json<{ llmProfileId: string; model: string }>(api, "get", `/api/v1/meetings/${id}/mom?version=1`);
  expect(latest.llmProfileId, "one deployment profile wrote both generated versions").toBe(first.llmProfileId);
  counts.models = row.versions.map((v) => v.model);

  // ── The grantee (not on the team) reads version 3 and cannot change it. ──
  const omarPage = await openPage();
  await signIn(omarPage, omar);
  await expect(omarPage.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await omarPage.goto(`/meetings/${id}`);
  await expect(minutes(omarPage)).toHaveAttribute("data-version", "3");
  await expect(omarPage.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(omarPage.getByRole("button", { name: "Regenerate" })).toHaveCount(0);
  await expectNoOverflow(omarPage);
  await screenshots(omarPage, "pipeline-grantee", testInfo);

  // ── The dashboard's Unassigned section: the latest version's unassigned items, at the cited line. ──
  const unassigned = latest.actionItems.filter((item) => item.owner.toLowerCase() === "unassigned");
  counts.unassigned_latest = unassigned.length;
  if (ROUTE === "fake") expect(unassigned.length, "the fake route always leaves one item unassigned").toBeGreaterThan(0);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  const rows = page.locator(`[data-testid=unassigned-row][data-meeting-id="${id}"]`);
  await expect(rows).toHaveCount(unassigned.length);
  if (unassigned.length > 0) {
    await expect(page.getByTestId("section-unassigned")).toBeVisible();
    await expect(rows.getByTestId("unassigned-text")).toHaveText(unassigned.map((item) => item.text));
    await expectNoOverflow(page);
    await screenshots(page, "pipeline-dashboard-unassigned", testInfo);
    const cited = unassigned[0].evidence[0];
    await expect(rows.first().getByTestId("unassigned-text")).toHaveAttribute("href", `/meetings/${id}#seg-${cited}`);
    await rows.first().getByTestId("unassigned-text").click();
    await expect(page).toHaveURL(new RegExp(`/meetings/${id}#seg-${cited}$`));
    await expect(line(page, cited)).toHaveAttribute("data-highlighted", "true");
    await expect(line(page, cited)).toBeInViewport({ ratio: 0.9 });
  }

  // ── Edits and regenerates sent nothing more: still the one recap. ──
  await new Promise((r) => setTimeout(r, 6_000)); // more than one recap-runner tick
  await expectOneRecap(id, [mira, lena]);
  counts.recap_mails = 1;
  counts.webhooks = row.webhooks.length;
  counts.versions = row.versions.length;
  record(testInfo, "chain", timings, counts);
  await api.dispose();
});

// ─── the restart ────────────────────────────────────────────────────────────────────────────────

test("SC1: andai-core and andai-worker stopped while the bot records still yield the minutes after restart", async ({ page }, testInfo) => {
  test.skip(ROUTE === "real", "the real route gets the chain only");
  test.setTimeout(10 * 60_000);
  const { mira, lena } = world;
  const api = await apiAs(mira);
  const joined = await json<{ id: string }>(api, "post", "/api/v1/meetings/join", {
    join_url: meetingLink("paced"),
    language: "hinglish",
    team_id: world.teamId,
  });
  const id = joined.id;

  // Recording (the paced scenario talks for about 14 s): stop both andai services.
  await expect.poll(async () => (await json<{ status: string }>(api, "get", `/api/v1/meetings/${id}`)).status, {
    timeout: 120_000,
    intervals: [200],
  }).toBe("recording");
  const platformId = pipelineRow(id).platform_meeting_id;
  expect(platformId, "the platform's meeting id").not.toBeNull();
  stopServices(ANDAI);
  const stopped = Date.now();
  const platformAtStop = platformStatus(platformId!);
  expect(serviceRunning("andai-core") || serviceRunning("andai-worker"), "both stopped").toBe(false);
  expect(platformAtStop, "the bot was still in the meeting when andai stopped").not.toBe("completed");

  // The platform ends the meeting while andai is down; nobody writes andai's row meanwhile.
  await expect.poll(() => platformStatus(platformId!), { timeout: 180_000, intervals: [1_000] }).toBe("completed");
  const down = pipelineRow(id);
  expect(down.status, "andai's row while it was down").toBe("recording");
  expect(serviceRunning("andai-core") || serviceRunning("andai-worker"), "still stopped when the bot ended").toBe(false);

  startServices(ANDAI);
  const started = Date.now();
  await waitHealthy(ANDAI);
  const row = await waitFor(id, (r) => r.mom_state === "stored" && r.recap_state === "sent", "minutes and recap after the restart");
  expect(row.versions.map(({ version, template_id, output_language }) => [version, template_id, output_language])).toEqual([
    [1, "standard", "hinglish"],
  ]);
  await expectOneRecap(id, [mira, lena]);

  await signIn(page, mira);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await page.goto(`/meetings/${id}`);
  await expect(minutes(page)).toHaveAttribute("data-version", "1");
  await expect(page.getByTestId("meeting-status")).toHaveAttribute("data-status", "done");

  record(
    testInfo,
    "restart",
    {
      down_s: seconds(started, stopped),
      restart_to_mom_s: seconds(row.mom_at, started),
      ...pipelineTimings(row),
    },
    { platform_status_at_stop: platformAtStop, webhooks: row.webhooks.length, versions: row.versions.length, recap_mails: 1 },
  );
  await api.dispose();
});
