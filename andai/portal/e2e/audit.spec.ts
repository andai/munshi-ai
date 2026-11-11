import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";

import { PROJECT } from "./env";
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
} from "./helpers";

// The audit log, in an organisation of its own: 60 events written inside the stack's andai-core
// through `audit.record` (a registered action), then real actions on top (a share, a role change,
// a wrong password for a member, the owner's sign-in). The owner opens Audit log from the
// navigation: newest first with names, labels and targets, Load more to the end without
// duplicates, the action filter (in the URL), a date range that matches nothing, and clearing the
// filters. A member gets the 403 page and no navigation entry.

const SEEDED = 60;
const SEED_ACTION = "bot.auto_join.update";

/** The e2e stack's andai-core container (compose project andai-e2e only). */
function coreContainer(): string {
  const id = execFileSync(
    "docker",
    ["ps", "-q", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--filter", "label=com.docker.compose.service=andai-core"],
    { encoding: "utf8" },
  )
    .trim()
    .split(/\s+/)[0];
  if (!id) throw new Error(`no andai-core container in compose project ${PROJECT}`);
  return id;
}

const SEED_SCRIPT = `
import sys, uuid
from andai_core import audit
from andai_core.db.session import create_app_engine, transaction
from andai_core.settings import get_settings

org, actor, action, count = uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2]), sys.argv[3], int(sys.argv[4])
engine = create_app_engine(settings=get_settings())
try:
    with transaction(engine, org_id=org) as tx:
        for _ in range(count):
            audit.record(tx, org, actor, action, f"org:{org}", "198.51.100.20")
finally:
    engine.dispose()
`;

function seedAuditEvents(orgId: string, actorId: string, action: string, count: number): void {
  execFileSync("docker", ["exec", "-i", coreContainer(), "python", "-", orgId, actorId, action, String(count)], {
    input: SEED_SCRIPT,
    stdio: ["pipe", "ignore", "inherit"],
  });
}

type AuditItem = {
  id: string;
  at: string;
  action: string;
  actor: { id: string | null; name: string | null };
  target: { type: string; id: string; label: string | null };
};

test("OPS-04: the audit log lists, pages and filters the organisation's events; members get 403", async ({ page, openPage }, testInfo) => {
  test.setTimeout(5 * 60_000);
  const tag = testInfo.project.name;
  const stamp = `${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

  // An organisation of its own, a member and a meeting the owner requested.
  const boss = await createOtherOrg(testInfo);
  const bossApi = await apiAs(boss);
  const me = await (await bossApi.get("/api/v1/me")).json();
  const orgId = me.org.id as string;
  const bossId = me.user.id as string;
  const mia = await createMember(bossApi, `mia-${tag}-${stamp}@example.test`, "member", `Mia Audit ${tag}`);
  const title = `Audit review ${tag} ${stamp}`;
  const meetingId = seedMeetings(orgId, [
    { key: "review", requestedBy: bossId, status: "done", title, startedMinAgo: 90, durationMin: 30 },
  ]).meetings.review.id;

  // 60 older events, then real actions on top of them.
  seedAuditEvents(orgId, bossId, SEED_ACTION, SEEDED);
  const share = await bossApi.post(`/api/v1/meetings/${meetingId}/shares`, {
    data: { grantee_type: "user", grantee_id: mia.id },
  });
  expect(share.status(), await share.text()).toBeLessThan(300);
  for (const role of ["admin", "member"]) {
    const changed = await bossApi.patch(`/api/v1/org/users/${mia.id}`, { data: { role } });
    expect(changed.status(), await changed.text()).toBe(200);
  }
  const anonymous = await apiAs();
  const wrong = await anonymous.post("/api/v1/auth/login", { data: { email: mia.email, password: "not-her-password" } });
  expect(wrong.status()).toBe(401);
  await anonymous.dispose();

  // A member: no navigation entry, and the page answers with the 403 page.
  const miaPage = await openPage();
  await signIn(miaPage, mia);
  await expect(miaPage).toHaveURL(/\/$/);
  await expect(miaPage.getByRole("link", { name: "Audit log" })).toHaveCount(0);
  await miaPage.goto("/admin/audit");
  await expect(miaPage).toHaveURL(/\/403$/);
  await expect(miaPage.getByRole("heading", { name: "You don't have access to this page" })).toBeVisible();

  // The owner signs in (a login.success) and opens the audit log from the navigation.
  await signIn(page, boss);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("link", { name: "Audit log" }).click();
  await expect(page).toHaveURL(/\/admin\/audit$/);
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();

  // Every event of the last 30 days, from the API, newest first, to compare the page with.
  const all: AuditItem[] = [];
  for (let cursor: string | null = null, first = true; first || cursor; first = false) {
    const body: { items: AuditItem[]; next: string | null } = await (
      await bossApi.get(`/api/v1/org/audit${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`)
    ).json();
    all.push(...body.items);
    cursor = body.next;
  }
  expect(all.length).toBeGreaterThan(SEEDED);
  expect(all.filter((e) => e.action === SEED_ACTION)).toHaveLength(SEEDED);

  const rows = page.getByTestId("audit-row");
  await expect(rows).toHaveCount(50);
  // Newest first: the owner's sign-in, as the owner, about the owner.
  const newest = rows.first();
  await expect(newest).toHaveAttribute("data-action", "login.success");
  await expect(newest.getByTestId("audit-actor")).toHaveText(boss.name);
  await expect(newest.getByTestId("audit-action-label")).toHaveText("Signed in (password accepted)");
  await expect(newest.getByTestId("audit-target")).toHaveText(boss.name);
  await expect(newest.locator("time")).toHaveAttribute("datetime", all[0].at);
  // The wrong password is a failed sign-in by nobody, about Mia.
  const failure = page.locator('[data-testid="audit-row"][data-action="login.failure"]').first();
  await expect(failure.getByTestId("audit-actor")).toHaveText("Not signed in");
  await expect(failure.getByTestId("audit-target")).toHaveText(mia.name);
  // The role changes and the share, with names and the meeting's title.
  await expect(page.locator('[data-testid="audit-row"][data-action="member.role_change"]')).toHaveCount(2);
  const shared = page.locator('[data-testid="audit-row"][data-action="share.grant"]');
  await expect(shared.getByTestId("audit-target")).toHaveText(title);
  await expectNoOverflow(page);
  await screenshots(page, "audit-log", testInfo);

  // Load more until the end: the page holds exactly the API's events, once each, in order.
  const more = page.getByTestId("load-more-audit");
  while (await more.isVisible()) {
    const before = await rows.count();
    await more.click();
    await expect(rows).not.toHaveCount(before);
  }
  const ids = await rows.evaluateAll((elements) => elements.map((e) => e.getAttribute("data-event-id")));
  expect(ids).toEqual(all.map((e) => e.id));
  expect(new Set(ids).size).toBe(ids.length);

  // Filter by action: the URL keeps it, and only shares remain.
  await page.getByTestId("audit-action").click();
  await page.getByRole("option", { name: "Shared a meeting" }).click();
  await expect(page).toHaveURL(/[?&]action=share\.grant(&|$)/);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toHaveAttribute("data-action", "share.grant");
  await expect(page.getByTestId("load-more-audit")).toHaveCount(0);
  await page.reload();
  await expect(rows).toHaveCount(1);
  await expect(page.getByTestId("audit-action")).toContainText("Shared a meeting");
  await screenshots(page, "audit-log-filtered", testInfo);

  // A date range with nothing in it: the empty state (`to` first, so no step is a range andai-core
  // refuses).
  await page.getByTestId("audit-to").fill("2020-01-31");
  await expect(page).toHaveURL(/[?&]to=2020-01-31/);
  await page.getByTestId("audit-from").fill("2020-01-01");
  await expect(page).toHaveURL(/[?&]from=2020-01-01/);
  await expect(page.getByTestId("audit-empty")).toBeVisible();
  await expect(page.getByTestId("audit-empty")).toContainText("No events match");
  await expectNoOverflow(page);
  await screenshots(page, "audit-log-empty", testInfo);

  // Clearing the filters brings back the first page.
  await page.getByTestId("audit-clear").click();
  await expect(page).toHaveURL(/\/admin\/audit$/);
  await expect(rows).toHaveCount(50);

  await bossApi.dispose();
});
