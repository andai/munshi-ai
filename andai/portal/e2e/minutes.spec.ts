// The minutes on the meeting page: where generation stands (preparing, then the minutes without a
// reload; a typed failure with Regenerate; nothing said), editing into a new version (summary, an
// owner, a removed item, an added item citing a transcript line; an uncited item cannot be saved,
// and andai-core's own refusal is shown), the version menu, regenerating with another template and
// output language, and downloads as Markdown and PDF. Meetings are seeded in an organisation of
// this spec's own (one per test project), written inside the stack's andai-core container
// (`seedMeetings`); the e2e stack's minutes pipeline writes generated versions through its fake
// LLM. Sample names only.
//
// Mira (member, Operations) requested every meeting. Lena leads Operations; Omar (Finance) gets
// "Warehouse weekly (sample)" through a share and only reads it.

import { readFileSync } from "node:fs";

import type { APIRequestContext, Download, Page, TestInfo } from "@playwright/test";

import {
  apiAs,
  createMember,
  createOtherOrg,
  expect,
  expectNoOverflow,
  screenshots,
  seedMeetings,
  signIn,
  test,
  uniqueEmail,
  type Account,
  type Seeded,
} from "./helpers";

test.describe.configure({ mode: "serial" });

const TITLE = "Warehouse weekly (sample)";
const FILE_STEM = "warehouse-weekly-sample-minutes";
const GENERATION_TIMEOUT = 90_000;
const DEVANAGARI = /[ऀ-ॿ]/;
// The indian_formal_mom template's headings in Hindi, in its section order (andai-core's).
const FORMAL_HINDI_HEADINGS = [
  "उपस्थित सदस्य",
  "कार्यसूची एवं चर्चा के बिंदु",
  "लिए गए निर्णय",
  "की जाने वाली कार्रवाई (किसके द्वारा, कब तक)",
  "मुद्दे एवं चिंताएँ",
  "सारांश",
];

const LINES = [
  "Kal subah dispatch ka plan final karenge",
  "Bhiwandi dock par do forklift kharab hain",
  "रात की shift अगले हफ़्ते से शुरू होगी",
  "Vendor list Friday tak bhejni hai",
  "Transit insurance renew karna hai",
  "Cut-off chaar baje customers ko batana hai",
  "Pune route pe do gaadi extra chahiye",
  "Theek hai, next week phir milte hain",
].map((text, i) => ({ speaker: i % 2 ? "Arjun Sample" : "Priya Sample", text }));

const SHORT = [
  { speaker: "Priya Sample", text: "Rate card next week final karenge" },
  { speaker: "Arjun Sample", text: "Vendor se quote kal tak aayega" },
  { speaker: "Priya Sample", text: "Theek hai, Friday ko review karte hain" },
];

type World = { mira: Account; lena: Account; omar: Account; seeded: Seeded };

let world: World;

async function json<T>(api: APIRequestContext, method: "get" | "post" | "put", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (await response.json()) as T;
}

async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await json<{ org: { id: string } }>(bossApi, "get", "/api/v1/me")).org.id;
  const member = (label: string, name: string) => createMember(bossApi, uniqueEmail(label, testInfo), "member", name);
  const mira = await member("mom-mira", "Mira Sample");
  const lena = await member("mom-lena", "Lena Sample");
  const omar = await member("mom-omar", "Omar Sample");
  const ops = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Operations" })).id;
  await json(bossApi, "put", `/api/v1/teams/${ops}/members`, {
    members: [
      { user_id: lena.id, role: "lead" },
      { user_id: mira.id, role: "member" },
    ],
  });
  await bossApi.dispose();

  const done = { requestedBy: mira.id!, status: "done" as const, platform: "google_meet" as const, teamId: ops, durationMin: 30 };
  const seeded = seedMeetings(orgId, [
    {
      ...done,
      key: "stored",
      title: TITLE,
      startedMinAgo: 120,
      segments: LINES,
      minutes: "full",
      attendees: [
        { name: "Priya Sample", source: "invite" },
        { name: "Arjun Sample", source: "speaker" },
      ],
      shares: [{ type: "user", id: omar.id! }],
    },
    // Handed to the minutes pipeline, which may start 20 s after the seed.
    { ...done, key: "pending", title: "Rate card sync (sample)", startedMinAgo: 90, segments: SHORT, momState: "pending", momDueInS: 20 },
    { ...done, key: "failed", title: "Vendor call (sample)", startedMinAgo: 80, segments: SHORT, momState: "failed", momError: "llm_unavailable" },
    { ...done, key: "skipped", title: "Silent check-in (sample)", startedMinAgo: 70, momState: "skipped" },
  ]);
  return { mira, lena, omar, seeded };
}

test.beforeAll(async ({}, testInfo) => {
  test.setTimeout(240_000);
  world = await buildWorld(testInfo);
});

const minutes = (page: Page) => page.getByTestId("minutes");
const state = (page: Page) => page.getByTestId("minutes-state");
const line = (page: Page, id: string) => page.locator(`[data-testid=transcript-line][data-segment-id="${id}"]`);

async function openMeeting(page: Page, account: Account, key: string): Promise<void> {
  await signIn(page, account);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await page.goto(`/meetings/${world.seeded.meetings[key].id}`);
  await expect(page.getByTestId("meeting-page")).toBeVisible();
}

/** Marks the loaded document; `stillSameDocument` is true until the page reloads or navigates. */
async function markDocument(page: Page): Promise<void> {
  await page.evaluate(() => ((window as unknown as { __minutesMark: boolean }).__minutesMark = true));
}
async function stillSameDocument(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as unknown as { __minutesMark?: boolean }).__minutesMark === true);
}

async function pick(page: Page, combobox: string, option: string): Promise<void> {
  await page.getByRole("combobox", { name: combobox }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
  await expect(page.getByRole("combobox", { name: combobox })).toHaveText(option);
}

async function chooseVersion(page: Page, version: number): Promise<void> {
  await page.getByTestId("version-menu").click();
  await page.locator(`[data-testid=version-option][data-version="${version}"]`).click();
}

async function download(page: Page, item: "Markdown (.md)" | "PDF (.pdf)"): Promise<{ name: string; bytes: Buffer }> {
  await page.getByRole("button", { name: "Download" }).click();
  const [file] = await Promise.all([page.waitForEvent("download"), page.getByRole("menuitem", { name: item }).click()]);
  const path = await (file as Download).path();
  return { name: file.suggestedFilename(), bytes: readFileSync(path) };
}

test("MoM states: preparing then shown without a reload, a typed failure with Regenerate, nothing said", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  // Being generated: "Preparing minutes…", then version 1 in place, on the same document.
  await openMeeting(page, world.mira, "pending");
  await expect(state(page)).toHaveAttribute("data-state", "pending");
  await expect(state(page)).toContainText("Preparing minutes…");
  await expect(page.getByRole("button", { name: "Regenerate" })).toHaveCount(0);
  await expectNoOverflow(page);
  await markDocument(page);
  await screenshots(page, "minutes-pending", testInfo);
  await expect(minutes(page)).toHaveAttribute("data-version", "1", { timeout: GENERATION_TIMEOUT });
  await expect(state(page)).toHaveCount(0);
  expect(await stillSameDocument(page), "the minutes arrived without a reload").toBe(true);
  await expect(minutes(page).getByTestId("decision")).not.toHaveCount(0);
  await expect(minutes(page).getByTestId("evidence-link").first()).toBeVisible();
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Version 1 · Standard");

  // Failed: the typed reason and Regenerate, which brings the minutes back.
  await page.goto(`/meetings/${world.seeded.meetings.failed.id}`);
  await expect(state(page)).toHaveAttribute("data-state", "failed");
  await expect(page.getByTestId("minutes-error")).toHaveText("The language model did not answer in time.(llm_unavailable)");
  await expect(minutes(page)).toHaveCount(0);
  await expectNoOverflow(page);
  await screenshots(page, "minutes-failed", testInfo);
  await page.getByRole("button", { name: "Regenerate" }).click();
  const dialog = page.getByTestId("regenerate-dialog");
  await expect(dialog.getByRole("combobox", { name: "Template" })).toHaveText("Standard");
  await expect(dialog.getByRole("combobox", { name: "Output language" })).toHaveText("English");
  await dialog.getByRole("button", { name: "Regenerate" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(state(page)).toHaveAttribute("data-state", "pending");
  await expect(minutes(page)).toHaveAttribute("data-version", "1", { timeout: GENERATION_TIMEOUT });
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Standard · English");

  // Skipped: nothing was said, nothing to regenerate.
  await page.goto(`/meetings/${world.seeded.meetings.skipped.id}`);
  await expect(state(page)).toHaveAttribute("data-state", "skipped");
  await expect(state(page)).toContainText("No minutes: nothing was said");
  await expect(page.getByRole("button", { name: "Regenerate" })).toHaveCount(0);
  await expectNoOverflow(page);
});

test("edit as the requester: a new version with the changes, an uncited item blocked, andai-core's refusal shown", async ({ page }, testInfo) => {
  const { stored } = world.seeded.meetings;
  await openMeeting(page, world.mira, "stored");
  await expect(minutes(page)).toHaveAttribute("data-version", "1");
  await expect(page.getByTestId("version-menu")).toHaveText("Version 1");
  await expect(minutes(page).getByTestId("unassigned-actions")).toContainText("Extend the transit insurance");
  await expectNoOverflow(page);
  await screenshots(page, "minutes-stored", testInfo);

  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const editor = page.getByTestId("minutes-editor");
  await expect(editor).toBeVisible();
  await expect(page.getByRole("button", { name: "Save as new version" })).toBeDisabled(); // nothing changed yet

  await editor.getByLabel("Summary").fill("Edited sample: the dispatch plan, the vendor list and the forklifts.");
  await pick(page, "Owner of action item 2", "Priya Sample");
  await editor.getByRole("button", { name: "Remove decision 2" }).click();
  await expect(editor.locator("[data-testid=edit-item][data-section=decisions]")).toHaveCount(1);

  // A new risk with no cited line cannot be saved.
  await editor.getByRole("button", { name: "Add risk" }).click();
  await editor.getByLabel("Text of risk 1").fill("Forklift repair may slip (sample)");
  await expect(editor.getByTestId("edit-uncited")).toHaveCount(1);
  await expect(page.getByTestId("editor-blocked")).toHaveText("1 item needs text and at least one cited line before the minutes can be saved.");
  await expect(page.getByRole("button", { name: "Save as new version" })).toBeDisabled();
  await expectNoOverflow(page);

  // Cite line 2 (00:10) from the transcript.
  await editor.getByRole("button", { name: "Cite lines for risk 1" }).click();
  const picker = page.getByTestId("evidence-picker");
  await expect(picker.getByTestId("pick-line")).toHaveCount(LINES.length);
  await picker.getByTestId("pick-line").nth(1).getByRole("checkbox").click();
  await expectNoOverflow(page);
  await screenshots(page, "minutes-cite", testInfo);
  await picker.getByRole("button", { name: "Done" }).click();
  await expect(editor.locator("[data-testid=edit-item][data-section=risks] [data-testid=edit-evidence]")).toHaveText(["00:10"]);
  await expect(page.getByTestId("editor-blocked")).toHaveCount(0);
  await screenshots(page, "minutes-editor", testInfo);

  // andai-core checks again: the same save with the citation stripped on the way is refused (422).
  await page.route("**/api/v1/meetings/*/mom", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const body = JSON.parse(route.request().postData() ?? "{}");
    body.risks[0].evidence = [];
    await route.continue({ postData: JSON.stringify(body) });
  });
  const refused = page.waitForResponse((r) => r.url().endsWith("/mom") && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Save as new version" }).click();
  expect((await refused).status()).toBe(422);
  await expect(editor.getByTestId("form-error")).toContainText("Risks[0]: no evidence");
  await page.unroute("**/api/v1/meetings/*/mom");

  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(minutes(page)).toHaveAttribute("data-version", "2");
  await expect(editor).toHaveCount(0);
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Edited by Mira Sample");
  await expect(minutes(page).getByTestId("minutes-summary")).toContainText("Edited sample: the dispatch plan");
  await expect(minutes(page).getByTestId("decision")).toHaveCount(1);
  await expect(minutes(page).getByTestId("unassigned-actions")).toHaveCount(0);
  await expect(minutes(page).getByTestId("action-owner")).toHaveText(["Sample Speaker", "Priya Sample"]);
  await expect(minutes(page).getByTestId("risk")).toHaveText(/Forklift repair may slip/);
  await expectNoOverflow(page);

  // The edited version's evidence still shows the cited line.
  await minutes(page).getByTestId("risk").getByTestId("evidence-link").click();
  await expect(line(page, stored.segments[1])).toHaveAttribute("data-highlighted", "true");
  await expect(line(page, stored.segments[1])).toBeInViewport({ ratio: 0.9 });

  // The version menu: both versions; version 1 as it was, then back to the latest.
  await page.getByTestId("version-menu").click();
  await expect(page.getByTestId("version-option")).toHaveCount(2);
  await expect(page.locator("[data-testid=version-option][data-version='2']")).toContainText("Edited by Mira Sample");
  await page.locator("[data-testid=version-option][data-version='1']").click();
  await expect(minutes(page)).toHaveAttribute("data-version", "1");
  await expect(minutes(page)).toHaveAttribute("data-latest", "false");
  await expect(minutes(page).getByTestId("minutes-summary")).toContainText("Sample review of the dispatch plan");
  await expect(minutes(page).getByTestId("decision")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Show the latest (version 2)" }).click();
  await expect(minutes(page)).toHaveAttribute("data-version", "2");

  // Unsaved edits are not dropped silently: switching versions or cancelling asks first.
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await editor.getByLabel("Summary").fill("An edit nobody saves");
  await chooseVersion(page, 1);
  const discard = page.getByTestId("discard-dialog");
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "Keep editing" }).click();
  await expect(editor.getByLabel("Summary")).toHaveValue("An edit nobody saves");
  await page.getByRole("button", { name: "Cancel" }).click();
  await discard.getByRole("button", { name: "Discard changes" }).click();
  await expect(editor).toHaveCount(0);
  await expect(minutes(page)).toHaveAttribute("data-version", "2");
  await expect(minutes(page).getByTestId("minutes-summary")).toContainText("Edited sample: the dispatch plan");
});

test("regenerate as the team lead in another template and language: pending, then the new version in Hindi", async ({ page }, testInfo) => {
  await openMeeting(page, world.lena, "stored");
  await expect(minutes(page)).toHaveAttribute("data-version", "2");
  await page.getByRole("button", { name: "Regenerate" }).click();
  const dialog = page.getByTestId("regenerate-dialog");
  await expect(dialog.getByRole("combobox", { name: "Template" })).toHaveText("Standard");
  await expect(dialog.getByRole("combobox", { name: "Output language" })).toHaveText("English");
  await pick(page, "Template", "Indian formal MoM");
  await pick(page, "Output language", "Hindi");
  await expectNoOverflow(page);
  await screenshots(page, "minutes-regenerate", testInfo);
  await dialog.getByRole("button", { name: "Regenerate" }).click();

  // The current version stays on screen with a notice; nothing else can start meanwhile.
  await expect(state(page)).toHaveAttribute("data-state", "pending");
  await expect(state(page)).toContainText("Preparing a new version of the minutes…");
  await expect(page.getByRole("button", { name: "Regenerate" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);

  await expect(minutes(page)).toHaveAttribute("data-version", "3", { timeout: GENERATION_TIMEOUT });
  await expect(state(page)).toHaveCount(0);
  await expect(minutes(page).getByTestId("minutes-version")).toContainText("Version 3 · Indian formal MoM · Hindi");
  await expect(minutes(page).getByTestId("minutes-heading")).toHaveText(FORMAL_HINDI_HEADINGS);
  await expect(minutes(page).getByTestId("minutes-attendees")).toContainText("Priya Sample, Arjun Sample");
  expect(await minutes(page).getByTestId("minutes-summary").textContent()).toMatch(DEVANAGARI);
  await expectNoOverflow(page);
  await screenshots(page, "minutes-hindi", testInfo);
});

test("a grantee reads and downloads the shown version as Markdown and PDF, and cannot change it", async ({ page }) => {
  const { stored } = world.seeded.meetings;
  await openMeeting(page, world.omar, "stored");
  await expect(minutes(page)).toHaveAttribute("data-version", "3");
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Regenerate" })).toHaveCount(0);
  await expect(page.getByTestId("version-menu")).toBeVisible();

  const markdown = await download(page, "Markdown (.md)");
  expect(markdown.name).toBe(`${FILE_STEM}-v3.md`);
  const text = markdown.bytes.toString("utf8");
  expect(text).toMatch(/^# Warehouse weekly /);
  for (const heading of ["उपस्थित सदस्य", "लिए गए निर्णय", "सारांश"]) expect(text).toContain(`## ${heading}`);

  const pdf = await download(page, "PDF (.pdf)");
  expect(pdf.name).toBe(`${FILE_STEM}-v3.pdf`);
  expect(pdf.bytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");
  expect(pdf.bytes.length).toBeGreaterThan(1_000);

  // The download follows the version on screen.
  await chooseVersion(page, 1);
  await expect(minutes(page)).toHaveAttribute("data-version", "1");
  const older = await download(page, "Markdown (.md)");
  expect(older.name).toBe(`${FILE_STEM}-v1.md`);
  expect(older.bytes.toString("utf8")).toContain("Sample review of the dispatch plan");
  await expectNoOverflow(page);

  // andai-core is the authority: a grantee's edit or regenerate does not exist for him.
  const api = await apiAs(world.omar);
  const edit = await api.put(`/api/v1/meetings/${stored.id}/mom`, {
    data: { summary: "no", decisions: [], actionItems: [], topics: [], risks: [] },
  });
  expect(edit.status()).toBe(404);
  const regenerate = await api.post(`/api/v1/meetings/${stored.id}/mom/regenerate`, { data: { templateId: "standard", outputLanguage: "en" } });
  expect(regenerate.status()).toBe(404);
  await api.dispose();
});
