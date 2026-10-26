// Global teardown: remove the andai-e2e stack and its volumes, so every run starts empty, and the
// owner's credential file. E2E_KEEP_STACK=1 leaves both (for a manual pass in a browser; the file's
// path is printed); remove them afterwards with `docker compose -p andai-e2e down -v`.

import { execFileSync } from "node:child_process";
import { rmSync } from "node:fs";
import { dirname } from "node:path";

import { PROJECT } from "./env";

export default async function globalTeardown() {
  const ownerFile = process.env.E2E_OWNER_FILE;
  if (process.env.E2E_KEEP_STACK === "1") {
    console.log(`E2E_KEEP_STACK=1: leaving compose project ${PROJECT} up; owner credentials in ${ownerFile}`);
    return;
  }
  if (ownerFile) rmSync(dirname(ownerFile), { recursive: true, force: true });
  execFileSync("docker", ["compose", "-p", PROJECT, "down", "-v", "--remove-orphans"], { stdio: "inherit" });
}
