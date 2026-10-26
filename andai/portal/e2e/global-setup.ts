// Global setup: a fresh, isolated stack for every run.
//
// 1. Refuse to start while anything listens on the stack's host ports (the dev stack uses the same
//    ports): "stop the dev stack (make -C andai down) before running e2e".
// 2. Rebuild the andai-core and andai-portal images from this tree (E2E_SKIP_BUILD=1 skips it when
//    iterating on specs only).
// 3. Start the whole stack as compose project andai-e2e (`make up`: db-init and db-migrate run as
//    part of it).
// 4. Bootstrap the first organisation through the portal (the only bootstrap in the suite), and
//    set the owner's password in a browser from the emailed link.
// 5. Hand the owner's credentials to the specs in a temporary file (E2E_OWNER_FILE).

import { chromium, request, type FullConfig } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { PROJECT, envFile, settings } from "./env";
import { Mailpit } from "./mailpit";

const REPO_ROOT = resolve(__dirname, "../../..");

function make(args: string[], options: { quiet?: boolean } = {}): string {
  const output = execFileSync("make", ["--no-print-directory", "-C", join(REPO_ROOT, "andai"), ...args], {
    encoding: "utf8",
    stdio: options.quiet ? ["ignore", "pipe", "pipe"] : ["ignore", "inherit", "inherit"],
    maxBuffer: 64 * 1024 * 1024,
  });
  return output ?? "";
}

function publishedPorts(env: string): number[] {
  // The merged config holds secrets: it is parsed here and never printed.
  const config = JSON.parse(make(["-s", "config-json", `ENV_FILE=${env}`, `PROJECT=${PROJECT}`], { quiet: true }));
  const ports = new Set<number>();
  for (const service of Object.values<{ ports?: { published?: string | number }[] }>(config.services ?? {})) {
    for (const port of service.ports ?? []) {
      const published = Number(port.published);
      if (Number.isInteger(published) && published > 0) ports.add(published);
    }
  }
  return [...ports].sort((a, b) => a - b);
}

function portIsFree(port: number): Promise<boolean> {
  return new Promise((done) => {
    const server = createServer();
    server.once("error", () => done(false));
    server.listen(port, "127.0.0.1", () => server.close(() => done(true)));
  });
}

async function bootstrapOwner(portalOrigin: string, mailpit: Mailpit) {
  const stamp = `${Date.now()}-${randomBytes(3).toString("hex")}`;
  const owner = {
    email: `owner-${stamp}@example.test`,
    password: `e2e-${randomBytes(9).toString("base64url")}-owner`,
    name: "Asha Owner",
    org: `E2E Org ${stamp}`,
  };
  const api = await request.newContext({ baseURL: portalOrigin, extraHTTPHeaders: { Origin: portalOrigin } });
  const created = await api.post("/api/v1/bootstrap", {
    data: { email: owner.email, org_name: owner.org, display_name: owner.name },
  });
  if (created.status() !== 201) {
    throw new Error(`bootstrap answered ${created.status()}: ${await created.text()}`);
  }
  await api.dispose();

  const link = await mailpit.link(owner.email, "invite", portalOrigin);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(link);
  await page.getByLabel("New password", { exact: true }).fill(owner.password);
  await page.getByLabel("Confirm new password").fill(owner.password);
  await page.getByRole("button", { name: "Activate account" }).click();
  await page.getByText("Your account is ready.").waitFor();
  await browser.close();
  return owner;
}

export default async function globalSetup(_config: FullConfig) {
  const env = envFile();
  const { portalOrigin, mailpit } = settings();

  const busy: number[] = [];
  for (const port of publishedPorts(env)) if (!(await portIsFree(port))) busy.push(port);
  if (busy.length) {
    throw new Error(`stop the dev stack (make -C andai down) before running e2e — ports in use: ${busy.join(", ")}`);
  }

  if (process.env.E2E_SKIP_BUILD !== "1") make(["images", "ONLY=andai-core,andai-portal"]);
  let owner: Awaited<ReturnType<typeof bootstrapOwner>>;
  try {
    make(["up", `ENV_FILE=${env}`, `PROJECT=${PROJECT}`]);
    owner = await bootstrapOwner(portalOrigin, new Mailpit(mailpit));
  } catch (error) {
    // Leave nothing half-started behind (teardown does not run when setup fails).
    if (process.env.E2E_KEEP_STACK !== "1") {
      execFileSync("docker", ["compose", "-p", PROJECT, "down", "-v", "--remove-orphans"], { stdio: "inherit" });
    }
    throw error;
  }
  const dir = mkdtempSync(join(tmpdir(), "munshi-e2e-"));
  const file = join(dir, "owner.json");
  writeFileSync(file, JSON.stringify(owner), { mode: 0o600 });
  process.env.E2E_OWNER_FILE = file;
}
