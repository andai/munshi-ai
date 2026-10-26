import { expect, expectNoOverflow, owner, sessionCookie, sessionStatus, signIn, signOut, test } from "./helpers";

test("the owner signs in to the portal shell and signs out", async ({ page }) => {
  const account = owner();
  await signIn(page, account);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Meetings", exact: true })).toBeVisible();
  await expect(page.getByTestId("dashboard-empty")).toContainText("No meetings yet");
  await expectNoOverflow(page);
  const cookie = await sessionCookie(page);
  expect(await sessionStatus(cookie)).toBe(200);

  await signOut(page);
  expect(await sessionStatus(cookie)).toBe(401);
});

test.describe("in dark mode", () => {
  test.use({ colorScheme: "dark" });

  test("the sign-in page loads in the dark theme", async ({ page }) => {
    await page.goto("/sign-in");
    await expect(page.locator("html")).toHaveClass(/\bdark\b/);
    expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe("rgb(18, 17, 32)");
    await expectNoOverflow(page);
  });
});
