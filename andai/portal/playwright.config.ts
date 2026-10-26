import { defineConfig, devices } from "@playwright/test";

import { settings } from "./e2e/env";

// Browser tests against a disposable compose stack (project andai-e2e) that global-setup builds,
// starts and bootstraps, and global-teardown removes with its volumes. Serial on purpose: the
// specs share one organisation, and the require-two-factor spec changes it for everyone.
const { portalOrigin } = settings();

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    baseURL: portalOrigin,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      // Chromium at phone size (touch, mobile viewport); runs after desktop so the
      // organisation-wide require-two-factor change never overlaps another spec.
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
      dependencies: ["desktop"],
    },
  ],
});
