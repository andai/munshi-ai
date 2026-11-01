// The finished-meeting page: the stored transcript in its original script and in Roman Hinglish,
// the attendees, the minutes with evidence links that show the cited line, a search result landing
// on its line, sharing inside the organisation (and what a grantee and a team lead see), and
// deletion. Meetings are seeded in an organisation of this spec's own (one per test project),
// written inside the stack's andai-core container (`seedMeetings`); sample names only.
//
// Mira (member) requested three finished meetings: "Diwali dispatch plan (sample)" in team
// Operations, with 24 transcript lines (Devanagari and Roman), attendees and full minutes; "Rate
// review (sample)" with a transcript and no minutes; and "Old sync (sample)", which she deletes.
// Lena leads Operations; Omar is in Finance and gets the first meeting through a team share.

import type { APIRequestContext, Locator, Page, TestInfo } from "@playwright/test";

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

const TITLE = "Diwali dispatch plan (sample)";
const PLAIN = "Rate review (sample)";
const DOOMED = "Old sync (sample)";
const LINES = 24;
const SEARCH_WORD = "Kalyanpur"; // only in line 21 of the first meeting
const SEARCH_LINE = 20;

const DEVANAGARI = [
  "कल सुबह dispatch का plan final करेंगे",
  "Bhiwandi dock पर दो forklift खराब हैं",
  "रात की shift अगले हफ़्ते से शुरू होगी",
  "vendor list शुक्रवार तक भेजनी है",
  "हर गाड़ी का भाड़ा November से बढ़ेगा",
  "customers को cut-off चार बजे बताना है",
];
const ROMAN = [
  "Haan, main aaj hi mail kar deta hoon",
  "Loaders ka count kal confirm karenge",
  "Insurance policy 31 October ko expire ho rahi hai",
  "Pune route pe do gaadi extra chahiye",
];

function transcriptLines(): { speaker: string; text: string }[] {
  return Array.from({ length: LINES }, (_, i) => {
    const speaker = i % 3 === 2 ? "Arjun Sample" : "Priya Sample";
    if (i === SEARCH_LINE) return { speaker, text: `${SEARCH_WORD} hub पर loaders कम हैं` };
    const text = i % 2 === 0 ? DEVANAGARI[(i / 2) % DEVANAGARI.length] : ROMAN[((i - 1) / 2) % ROMAN.length];
    return { speaker, text: `${text} (${i + 1})` };
  });
}

type World = {
  mira: Account;
  lena: Account;
  omar: Account;
  finance: string;
  seeded: Seeded;
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
  const lena = await member("lena", "Lena Sample");
  const omar = await member("omar", "Omar Sample");
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

  const seeded = seedMeetings(orgId, [
    {
      key: "full",
      requestedBy: mira.id!,
      status: "done",
      platform: "google_meet",
      title: TITLE,
      teamId: ops,
      startedMinAgo: 90,
      durationMin: 38,
      segments: transcriptLines(),
      minutes: "full",
      attendees: [
        { name: "Priya Sample", source: "invite" },
        { name: "Farah Sample", source: "invite" },
        { name: "Arjun Sample", source: "speaker" },
      ],
    },
    {
      key: "plain",
      requestedBy: mira.id!,
      status: "done",
      platform: "zoom",
      title: PLAIN,
      startedMinAgo: 200,
      durationMin: 20,
      segments: [
        { speaker: "Mira Sample", text: "Rate card next week final karenge" },
        { speaker: "Mira Sample", text: "हर गाड़ी का भाड़ा बढ़ेगा" },
      ],
      attendees: [{ name: "Mira Sample", source: "speaker" }],
    },
    {
      key: "doomed",
      requestedBy: mira.id!,
      status: "done",
      platform: "teams",
      title: DOOMED,
      startedMinAgo: 300,
      durationMin: 15,
      segments: [{ speaker: "Mira Sample", text: "Purana sync, delete kar sakte hain" }],
    },
  ]);
  return { mira, lena, omar, finance, seeded };
}

test.beforeAll(async ({}, testInfo) => {
  test.setTimeout(240_000);
  world = await buildWorld(testInfo);
});

const lines = (page: Page) => page.getByTestId("transcript-line");
const line = (page: Page, id: string) => page.locator(`[data-testid=transcript-line][data-segment-id="${id}"]`);
const texts = (page: Page) => page.getByTestId("transcript-text").allTextContents();

async function openMeeting(page: Page, account: Account, key: string, hash = ""): Promise<void> {
  await signIn(page, account);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await page.goto(`/meetings/${world.seeded.meetings[key].id}${hash}`);
  await expect(page.getByTestId("meeting-page")).toBeVisible();
}

async function expectShownAndHighlighted(target: Locator): Promise<void> {
  await expect(target).toHaveAttribute("data-highlighted", "true");
  await expect(target).toBeInViewport({ ratio: 0.9 });
  await expect(target).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
}

test("SC2: the stored transcript in both scripts, the attendees, and the minutes with evidence", async ({ page }, testInfo) => {
  const { full } = world.seeded.meetings;
  await openMeeting(page, world.mira, "full");
  await expectNoOverflow(page);

  await expect(page.getByTestId("meeting-title")).toHaveText(TITLE);
  await expect(page.getByTestId("meeting-meta")).toContainText("Operations");
  await expect(page.getByTestId("meeting-meta")).toContainText("Added by Mira Sample");

  // The transcript opens in the script each line was said in; the toggle shows every line's Roman form.
  const api = await apiAs(world.mira);
  const stored = (await json<{ segments: { id: string; text_raw: string; text_canonical: string; script: string }[] }>(api, "get", `/api/v1/meetings/${full.id}/transcript`)).segments;
  await api.dispose();
  expect(stored.map((s) => s.id)).toEqual(full.segments);
  expect(stored.filter((s) => s.script !== "latn").length, "the seed has Devanagari lines").toBeGreaterThan(5);
  await expect(lines(page)).toHaveCount(LINES);
  const toggle = page.getByRole("group", { name: "Transcript script" });
  await expect(toggle.getByRole("button", { name: "Original script" })).toHaveAttribute("aria-pressed", "true");
  expect(await texts(page)).toEqual(stored.map((s) => s.text_raw));
  expect((await texts(page))[0]).toContain("कल सुबह");

  await toggle.getByRole("button", { name: "Roman Hinglish" }).click();
  await expect(page.getByTestId("transcript")).toHaveAttribute("data-script", "roman");
  const roman = await texts(page);
  expect(roman).toEqual(stored.map((s) => s.text_canonical));
  expect(roman.join(" ")).not.toMatch(/[ऀ-ॿ]/);
  expect(roman[0]).toMatch(/^kal /i);
  await toggle.getByRole("button", { name: "Original script" }).click();
  expect(await texts(page)).toEqual(stored.map((s) => s.text_raw));

  // Attendees: invitees first, then speakers.
  await expect(page.getByTestId("attendee")).toHaveText(["PSPriya Sample", "FSFarah Sample", "ASArjun Sample"]);

  // The minutes: every section, "None recorded" for the empty one, unassigned actions apart.
  const minutes = page.getByTestId("minutes");
  await expect(minutes.getByTestId("minutes-summary")).toContainText("Sample review of the dispatch plan");
  await expect(minutes.getByTestId("decision")).toHaveCount(2);
  await expect(minutes.getByTestId("minutes-actions").getByTestId("action-item")).toHaveCount(2);
  const assigned = minutes.getByTestId("action-item").filter({ hasText: "Share the vendor list" });
  await expect(assigned.getByTestId("action-owner")).toHaveText("Sample Speaker");
  await expect(assigned.getByTestId("action-due")).toHaveText(/^Due \S+ \S+$/);
  const unassigned = minutes.getByTestId("unassigned-actions");
  await expect(unassigned).toContainText("Unassigned");
  await expect(unassigned.getByTestId("action-item")).toHaveText(/Extend the transit insurance/);
  await expect(unassigned.getByTestId("action-owner")).toHaveCount(0);
  await expect(minutes.getByTestId("topic")).toHaveText(/Dispatch/);
  await expect(minutes.getByTestId("minutes-risks")).toHaveAttribute("data-empty", "true");
  await expect(minutes.getByTestId("minutes-risks")).toContainText("None recorded");
  await screenshots(page, "meeting-minutes", testInfo);

  // An evidence time shows the cited line: scrolled into view and highlighted, named in the URL.
  const last = full.segments[LINES - 1];
  await expect(line(page, last)).not.toBeInViewport();
  const evidence = unassigned.locator(`[data-testid=evidence-link][data-segment-id="${last}"]`);
  await expect(evidence).toHaveText("02:44"); // line 24 starts 23 × 7 + 3 s in
  await expect(evidence).toHaveAttribute("aria-label", "Show transcript at 02:44");
  await evidence.click();
  await expectShownAndHighlighted(line(page, last));
  await expect(page).toHaveURL(new RegExp(`#seg-${last}$`));
  await expect(page.locator("[data-highlighted=true]")).toHaveCount(1);

  // Another one moves the highlight.
  const first = full.segments[0];
  await minutes.getByTestId("decision").first().getByTestId("evidence-link").click();
  await expectShownAndHighlighted(line(page, first));
  await expect(line(page, last)).not.toHaveAttribute("data-highlighted", "true");
  await expectNoOverflow(page);

  // A meeting without minutes shows its transcript alone.
  await page.goto(`/meetings/${world.seeded.meetings.plain.id}`);
  await expect(page.getByTestId("meeting-title")).toHaveText(PLAIN);
  await expect(lines(page)).toHaveCount(2);
  await expect(page.getByTestId("minutes")).toHaveCount(0);
  await expect(page.getByTestId("attendee")).toHaveText(["MSMira Sample"]);
  await expectNoOverflow(page);

  // Loading a page with #seg-<id> in the URL shows that line once the transcript is on screen.
  const middle = full.segments[12];
  await page.goto(`/meetings/${full.id}#seg-${middle}`);
  await expectShownAndHighlighted(line(page, middle));
});

test("SC2/PORT-05: a search result opens the meeting at the line it matched", async ({ page }) => {
  const { full } = world.seeded.meetings;
  await signIn(page, world.mira);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await page.getByRole("searchbox", { name: "Search meetings" }).fill(SEARCH_WORD.toLowerCase());
  const result = page.locator(`[data-testid=search-result][data-meeting-id="${full.id}"]`);
  await expect(result.getByTestId("search-result-title")).toHaveAttribute("href", `/meetings/${full.id}#seg-${full.segments[SEARCH_LINE]}`);
  await result.getByTestId("search-result-title").click();
  await expect(page).toHaveURL(new RegExp(`/meetings/${full.id}#seg-${full.segments[SEARCH_LINE]}$`));
  await expectShownAndHighlighted(line(page, full.segments[SEARCH_LINE]));
  await expect(line(page, full.segments[SEARCH_LINE])).toContainText(SEARCH_WORD);
});

test("SC3: share with a team inside the org, and remove it; who sees Share and Delete", async ({ page, openPage }, testInfo) => {
  const { full } = world.seeded.meetings;
  await openMeeting(page, world.mira, "full");

  // The requester (a member) shares: the picker lists the org's people and teams by name only.
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible();
  await page.getByRole("button", { name: "Share" }).click();
  const dialog = page.getByTestId("share-dialog");
  await expect(dialog.getByRole("heading", { name: "Share this meeting" })).toBeVisible();
  await expect(dialog).toContainText("Meetings can only be shared inside your organisation.");
  await expect(dialog.getByText(/^Add a person or team in Other Org /)).toBeVisible();
  const options = dialog.getByTestId("share-option");
  const team = (name: string) => dialog.locator("[data-testid=share-option][data-type=team]").filter({ hasText: name });
  const person = (name: string) => dialog.locator("[data-testid=share-option][data-type=user]").filter({ hasText: name });
  await expect(team("Finance")).toHaveCount(1);
  await expect(team("Finance")).toContainText("team, 1 person");
  await expect(person("Omar Sample")).toHaveCount(1);
  await expect(person("Omar Sample")).toContainText("Finance");
  await expect(options.filter({ hasText: "Mira Sample" }), "the requester is not offered").toHaveCount(0);
  expect(await dialog.textContent()).not.toContain("@");
  await expectNoOverflow(page);
  await screenshots(page, "meeting-share", testInfo);

  await dialog.getByTestId("share-find").fill("fin");
  await expect(options).toHaveCount(1);
  await dialog.getByRole("button", { name: "Share with Finance" }).click();
  await expect(dialog.getByTestId("share-grant")).toHaveCount(1);
  await expect(dialog.getByTestId("share-grant")).toContainText("Finance");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("meeting-meta")).toContainText("Shared with Finance");

  // Omar is in Finance: he can open the meeting, read only.
  const omar = await openPage();
  await signIn(omar, world.omar);
  await expect(omar.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await expect(omar.locator(`[data-testid=past-row][data-meeting-id="${full.id}"]`)).toHaveCount(1);
  await omar.goto(`/meetings/${full.id}`);
  await expect(omar.getByTestId("meeting-title")).toHaveText(TITLE);
  await expect(lines(omar)).toHaveCount(LINES);
  await expect(omar.getByRole("button", { name: "Share" })).toHaveCount(0);
  await expect(omar.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(omar.getByTestId("meeting-meta")).not.toContainText("Shared with");

  // Lena leads Operations: she may share (and sees the picker), not delete.
  const lena = await openPage();
  await signIn(lena, world.lena);
  await expect(lena.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await lena.goto(`/meetings/${full.id}`);
  await expect(lena.getByTestId("meeting-title")).toHaveText(TITLE);
  await expect(lena.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await lena.getByRole("button", { name: "Share" }).click();
  const leadDialog = lena.getByTestId("share-dialog");
  await expect(leadDialog.getByTestId("share-grant")).toContainText("Finance");
  await expect(leadDialog.getByTestId("share-option").filter({ hasText: "Omar Sample" })).toHaveCount(1);
  await expect(leadDialog.locator("[data-testid=share-option][data-type=team]").filter({ hasText: "Operations" })).toHaveCount(1);
  expect(await leadDialog.textContent()).not.toContain("@");
  await leadDialog.getByRole("button", { name: "Done" }).click();

  // Mira removes the share: on his next load Omar gets the not-found page.
  await page.getByRole("button", { name: "Share" }).click();
  await dialog.getByRole("button", { name: "Stop sharing with Finance" }).click();
  await expect(dialog.getByTestId("share-grant")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("meeting-meta")).not.toContainText("Shared with");
  await omar.reload();
  await expect(omar.getByTestId("meeting-not-found")).toBeVisible();
  await expect(omar.getByRole("heading", { name: "Meeting not found" })).toBeVisible();
});

test("SC4: the requester deletes a meeting; it is gone from the page and the dashboard", async ({ page }, testInfo) => {
  const { doomed } = world.seeded.meetings;
  await openMeeting(page, world.mira, "doomed");
  await page.getByRole("button", { name: "Delete" }).click();
  const dialog = page.getByTestId("delete-dialog");
  await expect(dialog.getByRole("heading", { name: "Delete this meeting?" })).toBeVisible();
  await expect(dialog).toContainText("The audio, transcript and every version of the minutes are removed for everyone within 15 minutes.");
  await expectNoOverflow(page);
  await screenshots(page, "meeting-delete", testInfo);

  // "Keep it" leaves it as it was.
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("meeting-title")).toHaveText(DOOMED);

  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByTestId("delete-dialog").getByRole("button", { name: "Delete meeting" }).click();
  await expect(page.getByTestId("meeting-not-found")).toBeVisible();
  await expectNoOverflow(page);

  const api = await apiAs(world.mira);
  expect((await api.get(`/api/v1/meetings/${doomed.id}`)).status()).toBe(404);
  await api.dispose();

  await page.reload();
  await expect(page.getByTestId("meeting-not-found")).toBeVisible();
  await page.goto("/");
  await expect(page.getByTestId("meeting-list-loading")).toHaveCount(0);
  await expect(page.locator(`[data-testid=past-row][data-meeting-id="${world.seeded.meetings.full.id}"]`)).toHaveCount(1);
  await expect(page.locator(`[data-testid=past-row][data-meeting-id="${doomed.id}"]`)).toHaveCount(0);
  await expect(page.getByText(DOOMED)).toHaveCount(0);
});
