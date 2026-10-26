import {
  apiAs,
  createMember,
  expect,
  expectNoOverflow,
  owner,
  screenshots,
  sessionCookie,
  sessionStatus,
  signIn,
  test,
  uniqueEmail,
} from "./helpers";

test("SC3: an admin runs teams, roles and accounts; a member gets the 403 page; a lead manages their team", async ({ page, openPage }, testInfo) => {
  const boss = owner();
  const api = await apiAs(boss);
  // Both projects share the organisation, so names carry the project (they appear in pickers).
  const tag = testInfo.project.name;
  const lead = await createMember(api, uniqueEmail("lead", testInfo), "member", `Lena Lead ${tag}`);
  const mate = await createMember(api, uniqueEmail("mate", testInfo), "member", `Mo Mate ${tag}`);
  const leaver = await createMember(api, uniqueEmail("leaver", testInfo), "member", `Lou Leaver ${tag}`);
  await api.dispose();

  // A member sees no admin navigation, and the admin pages answer with the 403 page.
  const matePage = await openPage();
  await signIn(matePage, mate);
  await expect(matePage).toHaveURL(/\/$/);
  await expect(matePage.getByRole("link", { name: "Members" })).toHaveCount(0);
  for (const path of ["/admin/members", "/admin/security"]) {
    await matePage.goto(path);
    await expect(matePage).toHaveURL(/\/403$/);
    await expect(matePage.getByRole("heading", { name: "You don't have access to this page" })).toBeVisible();
    await expectNoOverflow(matePage);
  }

  const leaverPage = await openPage();
  await signIn(leaverPage, leaver);
  await expect(leaverPage).toHaveURL(/\/$/);
  const leaverCookie = await sessionCookie(leaverPage);

  // The owner creates a team with a lead and a member.
  await signIn(page, boss);
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/admin/teams");
  await expectNoOverflow(page);
  const teamName = `Sales ${testInfo.project.name} ${Date.now() % 100000}`;
  await page.getByRole("button", { name: "Create team" }).click();
  const create = page.getByRole("dialog");
  await create.getByLabel("Team name").fill(teamName);
  await create.getByRole("button", { name: "Create team" }).click();
  await expect(page.getByTestId("form-success")).toHaveText(`Team ${teamName} is created.`);

  const card = page.getByTestId(`team-${teamName}`);
  await card.getByRole("button", { name: `Manage ${teamName}` }).click();
  const manage = page.getByRole("dialog");
  await manage.getByRole("combobox", { name: "Person to add" }).click();
  await page.getByRole("option", { name: lead.name }).click();
  await manage.getByRole("combobox", { name: "Team role for the new person" }).click();
  await page.getByRole("option", { name: "Lead" }).click();
  await manage.getByRole("button", { name: "Add" }).click();
  await manage.getByRole("combobox", { name: "Person to add" }).click();
  await page.getByRole("option", { name: mate.name }).click();
  await manage.getByRole("button", { name: "Add" }).click();
  await expectNoOverflow(page);
  await manage.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByTestId("form-success")).toHaveText(`Team ${teamName} is saved.`);
  await expect(card).toContainText(`2 members · led by ${lead.name}`);

  // Role change and disabling on the members page.
  await page.goto("/admin/members");
  await expectNoOverflow(page);
  const mateRow = page.getByTestId(`member-${mate.email}`);
  await expect(mateRow).toContainText(teamName);
  await mateRow.getByRole("combobox", { name: `Role for ${mate.email}` }).click();
  await page.getByRole("option", { name: "Admin" }).click();
  await expect(page.getByTestId("form-success")).toHaveText(`${mate.name} is now an admin.`);
  await mateRow.getByRole("combobox", { name: `Role for ${mate.email}` }).click();
  await page.getByRole("option", { name: "Member" }).click();
  await expect(page.getByTestId("form-success")).toHaveText(`${mate.name} is now a member.`);

  // The owner's own row is locked.
  const ownRow = page.getByTestId(`member-${boss.email}`);
  await expect(ownRow.getByText("Your own role")).toBeVisible();
  await expect(ownRow.getByRole("button", { name: `Actions for ${boss.email}` })).toBeDisabled();

  const leaverRow = page.getByTestId(`member-${leaver.email}`);
  await leaverRow.getByRole("button", { name: `Actions for ${leaver.email}` }).click();
  await page.getByRole("menuitem", { name: "Disable account" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toContainText("signed out everywhere");
  await confirm.getByRole("button", { name: "Disable" }).click();
  await expect(page.getByTestId("form-success")).toHaveText(`${leaver.name} is disabled.`);
  await expect(leaverRow).toContainText("Disabled");
  await screenshots(page, "members", testInfo);

  expect(await sessionStatus(leaverCookie)).toBe(401);
  await leaverPage.reload();
  await expect(leaverPage).toHaveURL(/\/sign-in$/);
  await signIn(leaverPage, leaver);
  await expect(leaverPage.getByTestId("form-error")).toHaveText("The email or password is not right.");

  // The team's lead changes its members but not who leads it.
  const leadPage = await openPage();
  await signIn(leadPage, lead);
  await expect(leadPage).toHaveURL(/\/$/);
  await leadPage.getByRole("link", { name: "Teams" }).click();
  await expect(leadPage).toHaveURL(/\/admin\/teams$/);
  await expect(leadPage.getByRole("button", { name: "Create team" })).toHaveCount(0);
  const leadCard = leadPage.getByTestId(`team-${teamName}`);
  await leadCard.getByRole("button", { name: `Manage ${teamName}` }).click();
  const leadDialog = leadPage.getByRole("dialog");
  await expect(leadDialog.getByRole("combobox", { name: `Team role for ${lead.email}` })).toBeDisabled();
  await expect(leadDialog.getByRole("button", { name: `Remove ${lead.email}` })).toBeDisabled();
  await expect(leadDialog.getByText("Only admins change who leads a team.")).toBeVisible();
  await leadDialog.getByRole("button", { name: `Remove ${mate.email}` }).click();
  await leadDialog.getByRole("button", { name: "Save changes" }).click();
  await expect(leadPage.getByTestId("form-success")).toHaveText(`Team ${teamName} is saved.`);
  await expect(leadCard).toContainText(`1 member · led by ${lead.name}`);
  await expectNoOverflow(leadPage);
});
