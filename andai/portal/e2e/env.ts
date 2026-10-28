// Settings the e2e suite shares, read from the stack's env file (E2E_ENV_FILE, an absolute path
// given by whoever runs the suite; the repo holds no path to it).

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

export const PROJECT = "andai-e2e";

export function parseEnvFile(path: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line.trim());
    if (match) values[match[1]] = match[2].replace(/^"(.*)"$/, "$1").replace(/^'(.*)'$/, "$1");
  }
  return values;
}

export function envFile(): string {
  const path = process.env.E2E_ENV_FILE;
  if (!path || !isAbsolute(path)) {
    throw new Error("set E2E_ENV_FILE to the absolute path of the stack's env file (see e2e/README.md)");
  }
  return path;
}

/** The image tag the stack runs (IMAGE_TAG in the env file, else the repo's VERSION). */
export function imageTag(path: string): string {
  const tag = parseEnvFile(path).IMAGE_TAG;
  if (tag) return tag;
  return readFileSync(resolve(__dirname, "../../../VERSION"), "utf8").trim();
}

export function settings() {
  const values = parseEnvFile(envFile());
  // Never `localhost`: andai-core compares the Origin header with this exact origin.
  const portalOrigin = values.ANDAI_PORTAL_ORIGIN || `http://127.0.0.1:${values.ANDAI_PORTAL_PORT || "13100"}`;
  const mailpit = `http://127.0.0.1:${values.MAILPIT_UI_PORT || "8025"}`;
  return { portalOrigin: new URL(portalOrigin).origin, mailpit };
}

/**
 * Remove the e2e stack with its volumes, and first the bot containers its runtime started (they are
 * not compose services, so `compose down` leaves them; they sit on the stack's network).
 */
export function stackDown(): void {
  const bots = execFileSync(
    "docker",
    ["ps", "-aq", "--filter", "label=runtime.managed=true", "--filter", `network=${PROJECT}_munshi`],
    { encoding: "utf8" },
  )
    .split(/\s+/)
    .filter(Boolean);
  if (bots.length) execFileSync("docker", ["rm", "-f", ...bots], { stdio: "ignore" });
  execFileSync("docker", ["compose", "-p", PROJECT, "down", "-v", "--remove-orphans"], { stdio: "inherit" });
}
