// Synthetic invitations into the e2e stack's notetaker mailbox. `inviteMail` builds a calendar
// invitation (multipart/alternative with a METHOD:REQUEST VEVENT whose LOCATION is the meeting
// link); `deliver` runs inside the stack's `fake-dns` container (compose project andai-e2e only):
// `python -m andai_intake.testing.sign` publishes the From domain's SPF and DMARC records and, when
// `signed`, its DKIM key in fake-dns's zone and signs the message (unsigned is what a forged From
// looks like), then `python -m andai_intake.replay` sends it over SMTP to andai-intake's receiver
// on 127.0.0.1:2525 (fake-dns shares andai-intake's network). Only synthetic addresses on test
// domains are used; the message never leaves the stack.

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { PROJECT, envFile, parseEnvFile } from "./env";

const ZONE = "/tmp/fake-dns/zone.json";
const SMTP_PORT = "2525";
const DOMAIN_RE = /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/;

/** The notetaker address the stack accepts mail for (the first of ANDAI_INTAKE_ADDRESSES). */
export function notetaker(): string {
  const address = (parseEnvFile(envFile()).ANDAI_INTAKE_ADDRESSES ?? "").split(",")[0]?.trim();
  if (!address) throw new Error("the e2e env file must set ANDAI_INTAKE_ADDRESSES");
  return address;
}

export type Invite = {
  /** The organiser's address: the From header and the envelope sender. */
  from: string;
  title: string;
  start: Date;
  minutes?: number;
  link: string;
  uid?: string;
};

function icsTime(at: Date): string {
  return at.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

/** RFC 5545 line folding: at most 75 octets a line (the content here is ASCII). */
function fold(line: string): string {
  const parts = [line.slice(0, 75)];
  for (let i = 75; i < line.length; i += 74) parts.push(line.slice(i, i + 74));
  return parts.join("\r\n ");
}

/** A calendar invitation from `invite.from` to the notetaker, CRLF line endings. */
export function inviteMail(invite: Invite): string {
  const domain = invite.from.split("@")[1];
  const end = new Date(invite.start.getTime() + (invite.minutes ?? 30) * 60_000);
  const boundary = `b-${randomUUID()}`;
  const ics = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//munshi e2e//EN",
    "METHOD:REQUEST",
    "BEGIN:VEVENT",
    `UID:${invite.uid ?? `${randomUUID()}@${domain}`}`,
    `DTSTAMP:${icsTime(new Date())}`,
    `DTSTART:${icsTime(invite.start)}`,
    `DTEND:${icsTime(end)}`,
    "SEQUENCE:0",
    `SUMMARY:${invite.title}`,
    `ORGANIZER:mailto:${invite.from}`,
    `ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:${notetaker()}`,
    `LOCATION:${invite.link}`,
    "END:VEVENT",
    "END:VCALENDAR",
  ].map(fold);
  return [
    `From: ${invite.from}`,
    `To: ${notetaker()}`,
    `Subject: Invitation: ${invite.title}`,
    `Date: ${new Date().toUTCString().replace(/GMT$/, "+0000")}`,
    `Message-ID: <${randomUUID()}@${domain}>`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`,
    "",
    `--${boundary}`,
    "Content-Type: text/plain; charset=utf-8",
    "",
    `You are invited to ${invite.title}.`,
    "",
    `--${boundary}`,
    "Content-Type: text/calendar; charset=utf-8; method=REQUEST",
    "",
    ...ics,
    "",
    `--${boundary}--`,
    "",
  ].join("\r\n");
}

/** A running fake-dns container of the e2e stack. */
function fakeDns(): string {
  const container = execFileSync(
    "docker",
    ["ps", "-q", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--filter", "label=com.docker.compose.service=fake-dns"],
    { encoding: "utf8" },
  )
    .trim()
    .split(/\s+/)[0];
  if (!container) throw new Error(`no fake-dns container in compose project ${PROJECT} (the e2e profile runs it)`);
  return container;
}

/**
 * Sign `raw` for its From domain (or leave it unsigned, with the domain's records published) and
 * send it to andai-intake's SMTP receiver with `from` as the envelope sender; throws unless the
 * receiver answers 250.
 */
export function deliver(raw: string, options: { from: string; signed: boolean }): void {
  const domain = options.from.split("@")[1]?.toLowerCase() ?? "";
  if (!DOMAIN_RE.test(domain)) throw new Error("not a test address");
  const container = fakeDns();
  const signed = execFileSync(
    "docker",
    [
      "exec", "-i", container, "python", "-m", "andai_intake.testing.sign",
      "--zone", ZONE, "--domain", domain, ...(options.signed ? [] : ["--unsigned"]),
    ],
    { input: raw, stdio: ["pipe", "pipe", "inherit"], timeout: 60_000 },
  );
  const answer = execFileSync(
    "docker",
    [
      "exec", "-i", container, "python", "-m", "andai_intake.replay",
      "--host", "127.0.0.1", "--port", SMTP_PORT, "--mail-from", options.from, "--rcpt", notetaker(), "/dev/stdin",
    ],
    { input: signed, encoding: "utf8", stdio: ["pipe", "pipe", "inherit"], timeout: 60_000 },
  );
  if (!/:\s250\b/.test(answer)) throw new Error(`the notetaker mailbox refused the message: ${answer.trim()}`);
}
