#!/usr/bin/env node
/**
 * gate:schema for export-bundle.v1 — validate every golden against export-bundle.schema.json.
 * The filename prefix names the bundle file the golden is (same pattern as logevent.v1):
 *   manifest-* → manifest.json        ($defs/Manifest)
 *   account-*  → account.json         ($defs/Account)
 *   meeting-*  → meetings/<id>.json   ($defs/MeetingExport)
 * A manifest golden must also list no file twice and leave itself out of `files`.
 * Run: node validate.mjs [--check]
 */
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { readdirSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(readFileSync(join(HERE, "export-bundle.schema.json"), "utf8"));
const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
ajv.addSchema(schema);

const SHAPE = { manifest: "Manifest", account: "Account", meeting: "MeetingExport" };
const EXTRA = {
  manifest: (d) => {
    const paths = d.files.map((f) => f.path);
    if (new Set(paths).size !== paths.length) return "a file is listed twice";
    if (paths.includes("manifest.json")) return "the manifest must not list itself";
    return null;
  },
  account: () => null,
  meeting: (d) =>
    d.recordings.every((r) => r.file.startsWith(`recordings/${d.id}/`))
      ? null
      : "a meeting's recordings live under recordings/<meeting id>/",
};

const dir = join(HERE, "golden");
const files = readdirSync(dir).filter((n) => n.endsWith(".json"));
let failed = 0;
for (const f of files) {
  const prefix = f.split("-")[0];
  const shape = SHAPE[prefix];
  if (!shape) { console.error(`  ✗ ${f}: filename must start with manifest- / account- / meeting-`); failed++; continue; }
  const validate = ajv.compile({ $ref: `${schema.$id}#/$defs/${shape}` });
  const data = JSON.parse(readFileSync(join(dir, f), "utf8"));
  if (!validate(data)) { console.error(`  ✗ ${f} (${shape}): ${ajv.errorsText(validate.errors)}`); failed++; continue; }
  const extra = EXTRA[prefix](data);
  if (extra) { console.error(`  ✗ ${f}: ${extra}`); failed++; continue; }
  console.log(`  ✓ ${f} ≡ ${shape}`);
}
if (!files.length) { console.error("  ✗ no goldens"); failed++; }
console.log(failed ? `export-bundle.v1: ${failed} golden(s) FAILED` : `export-bundle.v1: ${files.length} goldens conform`);
process.exit(failed ? 1 : 0);
