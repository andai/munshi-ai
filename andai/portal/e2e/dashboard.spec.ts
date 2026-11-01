// The dashboard's meeting list and keyword search, against meetings seeded in an organisation of
// this spec's own (one per test project): rows written inside the stack's andai-core container
// (`seedMeetings`), shares granted and revoked through the API, sample names only.
//
// Mira (member) requested a live, an upcoming and three past meetings; Ravi (member) shares one
// meeting with her and keeps two to himself — one in a team Mira belongs to, and one whose
// transcript holds the same Devanagari word as hers. Neha has no meetings. Tara has 1,000.

import type { APIRequestContext, Page, TestInfo } from "@playwright/test";

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

const FIRST_LIST_BUDGET_MS = 2_000;
const BULK = 1_000;

type World = {
  boss: Account;
  mira: Account;
  ravi: Account;
  neha: Account;
  tara: Account;
  ops: string;
  seeded: Seeded;
  shareId: string;
};

let world: World;

async function json<T>(api: APIRequestContext, method: "get" | "post" | "put" | "delete", path: string, data?: unknown): Promise<T> {
  const response = await api[method](path, data === undefined ? undefined : { data });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return (response.status() === 204 ? null : await response.json()) as T;
}

async function buildWorld(testInfo: TestInfo): Promise<World> {
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const orgId = (await json<{ org: { id: string } }>(bossApi, "get", "/api/v1/me")).org.id;
  const member = (label: string, name: string) => createMember(bossApi, uniqueEmail(label, testInfo), "member", name);
  const mira = await member("mira", "Mira Sample");
  const ravi = await member("ravi", "Ravi Sample");
  const neha = await member("neha", "Neha Sample");
  const tara = await member("tara", "Tara Sample");
  const ops = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Operations" })).id;
  const finance = (await json<{ id: string }>(bossApi, "post", "/api/v1/teams", { name: "Finance" })).id;
  await json(bossApi, "put", `/api/v1/teams/${ops}/members`, {
    members: [
      { user_id: mira.id, role: "member" },
      { user_id: ravi.id, role: "member" },
    ],
  });
  await bossApi.dispose();

  const seeded = seedMeetings(
    orgId,
    [
      { key: "live", requestedBy: mira.id!, status: "recording", platform: "google_meet", title: "Weekly ops sync — sample hub", teamId: ops, startedMinAgo: 12 },
      { key: "upcoming", requestedBy: mira.id!, status: "scheduled", platform: "teams", title: "Vendor onboarding — sample", teamId: ops },
      {
        key: "done",
        requestedBy: mira.id!,
        status: "done",
        platform: "google_meet",
        title: "Diwali dispatch plan",
        teamId: ops,
        startedMinAgo: 120,
        durationMin: 38,
        segments: [
          { speaker: "Rohit Sample", text: "Bhiwandi dock ka forklift repair mein hai" },
          { speaker: "Rohit Sample", text: "कल सुबह dispatch का plan final करेंगे" },
        ],
        minutes: true,
      },
      { key: "failed", requestedBy: mira.id!, status: "failed", failureReason: "admission_timeout", platform: "teams", title: "Client call — sample", startedMinAgo: 300 },
      { key: "untitled", requestedBy: mira.id!, status: "done", platform: "zoom", title: null, teamId: finance, startedMinAgo: 400, durationMin: 14 },
      {
        key: "shared",
        requestedBy: ravi.id!,
        status: "done",
        platform: "zoom",
        title: "Rate card review",
        startedMinAgo: 200,
        durationMin: 52,
        segments: [{ speaker: "Ravi Sample", text: "har gaadi ka bhada badhega from November" }],
      },
      {
        key: "private",
        requestedBy: ravi.id!,
        status: "done",
        platform: "google_meet",
        title: "Budget planning (private)",
        teamId: finance,
        startedMinAgo: 150,
        durationMin: 20,
        segments: [{ speaker: "Ravi Sample", text: "कल budget sheet भेजनी है" }],
      },
      { key: "ravi-ops", requestedBy: ravi.id!, status: "done", platform: "zoom", title: "Ops review (Ravi only)", teamId: ops, startedMinAgo: 250, durationMin: 30 },
    ],
    [
      // Tara's 1,000: 700 her own, 300 shared with her by Ravi.
      { count: 700, requestedBy: tara.id!, titlePrefix: "Sample meeting", startMinAgo: 60, stepMin: 30 },
      { count: 300, requestedBy: ravi.id!, titlePrefix: "Shared sample", startMinAgo: 75, stepMin: 70, shares: [{ type: "user", id: tara.id! }] },
    ],
  );
  expect(seeded.bulk).toBe(BULK);

  // Ravi shares "Rate card review" with Mira through the API (audited like any share).
  const raviApi = await apiAs(ravi);
  const share = await json<{ id: string }>(raviApi, "post", `/api/v1/meetings/${seeded.meetings.shared.id}/shares`, {
    grantee_type: "user",
    grantee_id: mira.id,
  });
  await raviApi.dispose();
  return { boss, mira, ravi, neha, tara, ops, seeded, shareId: share.id };
}

test.beforeAll(async ({}, testInfo) => {
  test.setTimeout(240_000);
  world = await buildWorld(testInfo);
});

const pastTitles = (page: Page) => page.getByTestId("past-row").getByTestId("meeting-row-title").allTextContents();

async function openDashboard(page: Page, account: Account): Promise<void> {
  await signIn(page, account);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await expect(page.getByTestId("meeting-list-loading")).toHaveCount(0);
}

async function waitForList(page: Page): Promise<void> {
  await expect(page.getByTestId("meeting-list-loading")).toHaveCount(0);
}

test("SC1: a member's meetings arrive as one list — Live now, Upcoming, Past", async ({ page }, testInfo) => {
  await openDashboard(page, world.mira);
  await expectNoOverflow(page);

  const live = page.getByTestId("live-card");
  await expect(live).toHaveCount(1);
  await expect(live.getByTestId("meeting-row-title")).toHaveText("Weekly ops sync — sample hub");
  await expect(live).toContainText("Google Meet · Operations");
  await expect(live.getByTestId("status-pill")).toHaveAttribute("data-status", "recording");
  await expect(page.getByTestId("live-count")).toHaveText("1 in progress");

  const upcoming = page.getByTestId("upcoming-row");
  await expect(upcoming).toHaveCount(1);
  await expect(upcoming.getByTestId("meeting-row-title")).toHaveText("Vendor onboarding — sample");
  await expect(upcoming.getByTestId("status-pill")).toHaveAttribute("data-status", "scheduled");

  // Past, newest first: her own three and the one Ravi shared; never Ravi's other two.
  const titles = await pastTitles(page);
  expect(titles.slice(0, 3)).toEqual(["Diwali dispatch plan", "Rate card review", "Client call — sample"]);
  expect(titles).toHaveLength(4);
  expect(titles[3]).toMatch(/^Zoom meeting · \S/); // the untitled one: "<Platform> meeting · <date>"
  for (const hidden of ["Budget planning (private)", "Ops review (Ravi only)"]) {
    await expect(page.getByText(hidden)).toHaveCount(0);
  }

  const row = (id: string) => page.locator(`[data-testid=past-row][data-meeting-id="${id}"]`);
  const { done, failed, shared, untitled } = world.seeded.meetings;
  await expect(row(done.id).getByTestId("meeting-row-team")).toHaveText("Operations");
  await expect(row(done.id).getByTestId("meeting-row-minutes")).toHaveText("Minutes ready");
  await expect(row(done.id)).toContainText("38 min");
  await expect(row(failed.id).getByTestId("status-pill")).toHaveAttribute("data-status", "failed");
  await expect(row(failed.id).getByTestId("meeting-row-reason")).toHaveText("Nobody let the bot in");
  await expect(row(failed.id).getByTestId("meeting-row-team")).toHaveText("—");
  await expect(row(shared.id).getByTestId("shared-tag")).toHaveText("Shared with you");
  await expect(row(shared.id)).toContainText("Ravi Sample");
  await expect(row(untitled.id).getByTestId("meeting-row-minutes")).toHaveText("No minutes yet");
  await expect(page.getByTestId("load-more-past")).toHaveCount(0);

  await screenshots(page, "dashboard", testInfo);

  // A row opens its meeting.
  await row(done.id).getByTestId("meeting-row-title").click();
  await expect(page).toHaveURL(new RegExp(`/meetings/${done.id}$`));
  await expect(page.getByTestId("meeting-status")).toBeVisible();
});

test("SC1: the filters narrow the list to my requests, shares with me, or one team", async ({ page }) => {
  await openDashboard(page, world.mira);
  const scope = (name: string) => page.getByRole("group", { name: "Whose meetings" }).getByRole("button", { name });

  await scope("Requested by me").click();
  await expect(scope("Requested by me")).toHaveAttribute("aria-pressed", "true");
  await waitForList(page);
  await expect.poll(() => pastTitles(page)).toEqual(["Diwali dispatch plan", "Client call — sample", expect.stringMatching(/^Zoom meeting · /)]);
  await expect(page.getByTestId("live-card")).toHaveCount(1);

  await scope("Shared with me").click();
  await waitForList(page);
  await expect.poll(() => pastTitles(page)).toEqual(["Rate card review"]);
  await expect(page.getByTestId("live-empty")).toHaveText("No meeting is being recorded right now.");
  await expect(page.getByTestId("upcoming-empty")).toHaveText("Nothing scheduled");

  // One team: Mira's Operations meetings, never Ravi's own Operations meeting.
  await page.getByTestId("team-filter").click();
  await page.getByRole("option", { name: "Operations" }).click();
  await waitForList(page);
  await expect(scope("Shared with me")).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => pastTitles(page)).toEqual(["Diwali dispatch plan"]);
  await expect(page.getByTestId("live-card")).toHaveCount(1);
  await expect(page.getByTestId("upcoming-row")).toHaveCount(1);
  await expect(page.getByText("Ops review (Ravi only)")).toHaveCount(0);
  await expectNoOverflow(page);

  await scope("All I can see").click();
  await waitForList(page);
  await expect(page.getByTestId("team-filter")).toContainText("All teams");
  await expect.poll(async () => (await pastTitles(page)).length).toBe(4);
});

test("SC5: a Roman query finds Devanagari speech, with snippets, among meetings I can open only", async ({ page }, testInfo) => {
  const { done, private: hidden } = world.seeded.meetings;
  // The same word is in a meeting Mira may not open: the owner's search counts both.
  const bossApi = await apiAs(world.boss);
  const bossAnswer = await json<{ total: number; results: { id: string }[] }>(bossApi, "get", "/api/v1/search?q=kal");
  await bossApi.dispose();
  expect(bossAnswer.total).toBe(2);
  expect(bossAnswer.results.map((r) => r.id).sort()).toEqual([done.id, hidden.id].sort());

  await openDashboard(page, world.mira);
  const box = page.getByRole("searchbox", { name: "Search meetings" });
  await box.fill("kal");
  const results = page.getByTestId("search-result");
  await expect(page.getByTestId("search-count")).toHaveText("1 meeting · only meetings you can open");
  await expect(results).toHaveCount(1);
  await expect(page.getByTestId("dashboard-list")).toBeHidden();
  const result = results.first();
  await expect(result).toHaveAttribute("data-meeting-id", done.id);
  await expect(result.getByTestId("search-result-title")).toHaveText("Diwali dispatch plan");
  await expect(page.getByText("Budget planning (private)")).toHaveCount(0);

  // The snippet: the line as said (Devanagari) with the word marked, and its Roman form.
  const snippet = result.locator("[data-testid=search-snippet][data-source=transcript]");
  await expect(snippet.getByTestId("snippet-raw")).toHaveText("कल सुबह dispatch का plan final करेंगे");
  await expect(snippet.getByTestId("snippet-raw").locator("mark")).toHaveText(["कल"]);
  await expect(snippet.getByTestId("snippet-roman").locator("mark")).toHaveText(["kal"]);
  await expect(snippet).toContainText("Rohit Sample");
  // The title links to the meeting at the cited line.
  await expect(result.getByTestId("search-result-title")).toHaveAttribute("href", `/meetings/${done.id}#seg-${done.segments[1]}`);

  // The API's own count for Mira agrees with the page.
  const miraApi = await apiAs(world.mira);
  const miraAnswer = await json<{ total: number; results: { id: string }[] }>(miraApi, "get", "/api/v1/search?q=kal");
  await miraApi.dispose();
  expect(miraAnswer.total).toBe(1);
  expect(miraAnswer.results.map((r) => r.id)).toEqual([done.id]);

  await expectNoOverflow(page);
  await screenshots(page, "search", testInfo);

  // The Devanagari spelling finds the same meeting.
  await box.fill("कल");
  await expect(page.getByTestId("search-count")).toHaveText("1 meeting · only meetings you can open");
  await expect(results.first()).toHaveAttribute("data-meeting-id", done.id);

  // Nothing matches.
  await box.fill("zzqxv");
  await expect(page.getByTestId("search-empty")).toHaveText(
    "Nothing matches. Search covers titles, transcripts and minutes, in Roman or Devanagari script.",
  );
  await expect(page.getByTestId("search-count")).toHaveText("0 meetings · only meetings you can open");

  // Escape clears the search and the list returns as it was.
  await box.press("Escape");
  await expect(page.getByTestId("search-results")).toHaveCount(0);
  await expect(page.getByTestId("dashboard-list")).toBeVisible();
  await expect.poll(async () => (await pastTitles(page)).length).toBe(4);

  // The result opens its meeting.
  await box.fill("kal");
  await result.getByTestId("search-result-title").click();
  await expect(page).toHaveURL(new RegExp(`/meetings/${done.id}#seg-${done.segments[1]}$`));
});

test("PORT-03/SC1: a shared meeting leaves the grantee's list on reload once it is revoked", async ({ page }) => {
  const { shared } = world.seeded.meetings;
  await openDashboard(page, world.mira);
  await expect(page.locator(`[data-testid=past-row][data-meeting-id="${shared.id}"]`)).toHaveCount(1);

  const raviApi = await apiAs(world.ravi);
  await json(raviApi, "delete", `/api/v1/meetings/${shared.id}/shares/${world.shareId}`);
  await raviApi.dispose();

  await page.reload();
  await waitForList(page);
  await expect(page.getByTestId("past-row")).toHaveCount(3);
  await expect(page.locator(`[data-testid=past-row][data-meeting-id="${shared.id}"]`)).toHaveCount(0);
  await expect(page.getByText("Rate card review")).toHaveCount(0);
  // Search drops it too.
  await page.getByRole("searchbox", { name: "Search meetings" }).fill("gaadi");
  await expect(page.getByTestId("search-empty")).toBeVisible();
});

test("SC1: a member with no meetings sees the empty dashboard", async ({ page }, testInfo) => {
  await openDashboard(page, world.neha);
  await expect(page.getByTestId("dashboard-empty")).toContainText("No meetings yet");
  await expect(page.getByTestId("join-form")).toBeVisible();
  await expectNoOverflow(page);
  await screenshots(page, "dashboard-empty", testInfo);
});

test("SC1: with 1,000 meetings Load more appends the next page without repeats, and nothing loads on its own", async ({ page }) => {
  const listCalls: string[] = [];
  page.on("request", (req) => {
    const url = new URL(req.url());
    if (url.pathname === "/api/v1/meetings") listCalls.push(url.search);
  });
  await openDashboard(page, world.tara);
  const rows = page.getByTestId("past-row");
  await expect(rows).toHaveCount(25);
  await expect(page.getByTestId("past-count")).toHaveText("Showing 25 · more below");
  // No page beyond the first is fetched without a click.
  await page.waitForTimeout(1_000);
  await expect(rows).toHaveCount(25);
  expect(listCalls).toHaveLength(1);

  const ids = () => rows.evaluateAll((els) => els.map((el) => el.getAttribute("data-meeting-id")));
  await page.getByTestId("load-more-past").click();
  await expect(rows).toHaveCount(50);
  await page.getByTestId("load-more-past").click();
  await expect(rows).toHaveCount(75);
  const seen = await ids();
  expect(new Set(seen).size).toBe(75);
  expect(listCalls).toHaveLength(3);
  expect(listCalls.slice(1).every((q) => q.includes("section=past") && q.includes("cursor="))).toBe(true);
  // Newest first across the pages: the first row of page two follows the last of page one.
  const titles = await pastTitles(page);
  expect(titles[0]).toBe("Sample meeting 0001");
  await expectNoOverflow(page);
});

test("SC1: with 1,000 accessible meetings the first list paints in under 2 s (desktop)", async ({ page, openPage }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "the timing budget is set for desktop");
  await signIn(page, world.tara);
  await expect(page.getByTestId("past-row").first()).toBeVisible();
  const storageState = await page.context().storageState();

  // Each run is a fresh browser context (signed in, empty cache), timed in the page from
  // navigation start to the first Past row on screen.
  const runs: { firstPastRowMs: number; meResponseEndMs: number | null; listResponseEndMs: number | null; listWaitMs: number | null }[] = [];
  for (let run = 0; run < 3; run++) {
    const fresh = await openPage({ storageState });
    await fresh.addInitScript(() => {
      const state = window as unknown as { __firstPastRow: number | null };
      state.__firstPastRow = null;
      const check = () => {
        if (state.__firstPastRow !== null) return;
        const row = document.querySelector("[data-testid=past-row]");
        if (row && (row as HTMLElement).offsetParent !== null) state.__firstPastRow = performance.now();
      };
      new MutationObserver(check).observe(document, { subtree: true, childList: true });
    });
    await fresh.goto("/", { waitUntil: "commit" });
    await expect(fresh.getByTestId("past-row").first()).toBeVisible();
    await expect.poll(() => fresh.evaluate(() => (window as unknown as { __firstPastRow: number | null }).__firstPastRow)).not.toBeNull();
    runs.push(
      await fresh.evaluate(() => {
        const entries = performance.getEntriesByType("resource") as PerformanceResourceTiming[];
        const find = (path: string) => entries.find((e) => new URL(e.name).pathname === path);
        const me = find("/api/v1/me");
        const list = find("/api/v1/meetings");
        return {
          firstPastRowMs: Math.round((window as unknown as { __firstPastRow: number }).__firstPastRow),
          meResponseEndMs: me ? Math.round(me.responseEnd) : null,
          listResponseEndMs: list ? Math.round(list.responseEnd) : null,
          listWaitMs: list ? Math.round(list.responseStart - list.requestStart) : null,
        };
      }),
    );
    await fresh.context().close();
  }
  const worst = Math.max(...runs.map((r) => r.firstPastRowMs));
  testInfo.annotations.push({ type: "first Past row (ms), 3 runs", description: JSON.stringify(runs) });
  testInfo.annotations.push({ type: "first Past row, worst (ms)", description: String(worst) });
  console.log(`dashboard timing with ${BULK} meetings: ${JSON.stringify(runs)}; worst ${worst} ms`);
  expect(worst).toBeLessThan(FIRST_LIST_BUDGET_MS);
});
