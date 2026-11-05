"use client";

import { ArrowLeft, Share2, Square, Trash2, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import MeetingNotFound from "@/app/(app)/meetings/[id]/not-found";
import { FormAlert } from "@/components/forms/form-alert";
import { AdmitInstructions } from "@/components/meetings/admit-instructions";
import { Attendees } from "@/components/meetings/attendees";
import { ConsentNote } from "@/components/meetings/consent-note";
import { DeleteDialog } from "@/components/meetings/delete-dialog";
import { LANGUAGE_NAME, PLATFORM_NAME, endText, linkHost, meetingWhen } from "@/components/meetings/display";
import { FailurePanel } from "@/components/meetings/failure-panel";
import { LiveTranscript } from "@/components/meetings/live-transcript";
import { MinutesPanel } from "@/components/meetings/minutes-view";
import { ShareDialog } from "@/components/meetings/share-dialog";
import { StatusBadge } from "@/components/meetings/status-badge";
import {
  TranscriptView,
  segmentAnchor,
  segmentFromHash,
  type ScriptChoice,
  type SegmentTarget,
} from "@/components/meetings/transcript-view";
import { Button } from "@/components/ui/button";
import { ApiError, sentence } from "@/lib/api";
import {
  getMeeting,
  getTranscript,
  isTerminal,
  mergeSegments,
  stopMeeting,
  subscribeLive,
  type LiveMode,
  type Meeting,
  type Segment,
  type StatusUpdate,
} from "@/lib/meetings";
import { useSession } from "@/lib/session";

// One meeting, as andai-core reports it. While the bot is in it: status and when it changed, why it
// failed, how to admit the bot while it waits, the recording notice, and the transcript as it
// arrives (live stream, polling when the stream drops). Once it has ended: the transcript andai
// stored (original script, or Roman Hinglish), the attendees and the minutes (`minutes-view.tsx`:
// where generation stands, versions, edit, regenerate, downloads), whose evidence links show the
// line they cite. Share, Delete and Stop appear for whoever andai-core's `permissions` allow. A meeting this account may not read does not exist here (404),
// and neither does one it has just deleted.

const FINAL_READ_DELAY_MS = 3_000;
// After the end, andai-worker stores the transcript within seconds; the page re-reads the meeting
// until it has (for at most STORE_POLLS × STORE_POLL_MS, after which a reload shows it).
const STORE_POLL_MS = 3_000;
const STORE_POLLS = 40;

type Loaded =
  | { state: "loading" }
  | { state: "missing" }
  | { state: "error"; message: string }
  | { state: "ready"; meeting: Meeting };

function Header({ meeting }: { meeting: Meeting }) {
  const platform = PLATFORM_NAME[meeting.platform] ?? meeting.platform;
  const host = linkHost(meeting.join_url);
  const language = meeting.language ? LANGUAGE_NAME[meeting.language] : null;
  return (
    <div className="flex min-w-0 items-start gap-3">
      <span aria-hidden="true" className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-secondary text-primary">
        <Video className="size-5" />
      </span>
      <div className="grid min-w-0 gap-0.5">
        <h1 className="text-2xl font-semibold tracking-tight break-words" data-testid="meeting-title">
          {meeting.title?.trim() || `${platform} meeting`}
        </h1>
        <p className="truncate text-sm text-muted-foreground" data-testid="meeting-host">
          {[host, language].filter(Boolean).join(" · ")}
        </p>
      </div>
    </div>
  );
}

function Meta({ meeting }: { meeting: Meeting }) {
  const when = meetingWhen(meeting.started_at ?? meeting.created_at, meeting.ended_at);
  const shared = (meeting.shared_with ?? []).map((s) => s.name).filter(Boolean);
  const parts = [
    when,
    meeting.team?.name,
    meeting.requested_by_name ? `Added by ${meeting.requested_by_name}` : null,
    shared.length > 0 ? `Shared with ${shared.join(", ")}` : null,
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return (
    <p className="flex flex-wrap gap-x-2 gap-y-1 text-sm text-muted-foreground" data-testid="meeting-meta">
      {parts.map((part, index) => (
        <span key={index} className="inline-flex min-w-0 gap-2 [overflow-wrap:anywhere]">
          {index > 0 && <span aria-hidden="true">·</span>}
          {part}
        </span>
      ))}
    </p>
  );
}

export function MeetingView({ id }: { id: string }) {
  const { me } = useSession();
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [segments, setSegments] = useState<Segment[]>([]);
  // Whether the stored transcript and the minutes have both been read: the layout is final, so a
  // line can be scrolled to without the minutes arriving above it and pushing it away.
  const [transcriptRead, setTranscriptRead] = useState(false);
  const [minutesRead, setMinutesRead] = useState(false);
  const settled = transcriptRead && minutesRead;
  const [mode, setMode] = useState<LiveMode>("live");
  const [changedAt, setChangedAt] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  // Kept here, not in the transcript, so the choice survives the live view giving way to the stored one.
  const [script, setScript] = useState<ScriptChoice>("original");
  const [target, setTarget] = useState<SegmentTarget | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  // The newest status the live stream (or a poll) reported. The stored meeting can trail the
  // stream by a reconcile cycle, so a re-read takes everything from it except these fields.
  const latest = useRef<StatusUpdate | null>(null);

  const withLatestStatus = useCallback((meeting: Meeting): Meeting => {
    const update = latest.current;
    if (!update) return meeting;
    return {
      ...meeting,
      status: update.status,
      failure_reason: update.failure_reason,
      end_reason: update.end_reason,
      admit_instructions: update.admit_instructions,
    };
  }, []);

  // After a status or share change: the consent record, times and shares that came with it.
  const refresh = useCallback(async () => {
    try {
      const fresh = await getMeeting(id);
      setLoaded((current) => (current.state === "ready" ? { state: "ready", meeting: withLatestStatus(fresh) } : current));
    } catch {
      // the next status or poll brings it
    }
  }, [id, withLatestStatus]);

  // First load: the meeting (a 404 or a malformed id is "not found"). A running meeting's transcript
  // comes with following it; a finished one's is read below.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const first = await getMeeting(id);
        if (!active) return;
        setLoaded({ state: "ready", meeting: first });
        setChangedAt(first.updated_at);
      } catch (err) {
        if (!active) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 422)) setLoaded({ state: "missing" });
        else setLoaded({ state: "error", message: err instanceof ApiError ? sentence(err.detail) : "Something went wrong." });
      }
    })();
    return () => {
      active = false;
    };
  }, [id]);

  // The line the URL names (`#seg-<id>`, e.g. from a search result), now and whenever it changes.
  useEffect(() => {
    const fromHash = () => {
      const segment = segmentFromHash(window.location.hash);
      if (segment) setTarget((current) => ({ id: segment, n: (current?.n ?? 0) + 1 }));
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, []);

  // An evidence link: name the line in the URL (without a new history entry) and show it.
  const showSegment = useCallback((segment: string) => {
    window.history.replaceState(window.history.state, "", `#${segmentAnchor(segment)}`);
    setTarget((current) => ({ id: segment, n: (current?.n ?? 0) + 1 }));
  }, []);

  // Follow the meeting until it ends (a finished meeting needs no stream).
  const follow = loaded.state === "ready" && !isTerminal(loaded.meeting.status);
  useEffect(() => {
    if (!follow) return;
    const stop = subscribeLive(id, {
      onStatus: (update: StatusUpdate) => {
        latest.current = update;
        setChangedAt(update.at);
        setLoaded((current) => (current.state === "ready" ? { state: "ready", meeting: withLatestStatus(current.meeting) } : current));
        void refresh();
      },
      onSegments: setSegments,
      // A poll's meeting is the source of its own status (the poll reports it as onStatus too).
      onMeeting: (fresh) => setLoaded((current) => (current.state === "ready" ? { state: "ready", meeting: fresh } : current)),
      onMode: setMode,
    });
    return stop;
  }, [follow, id, refresh, withLatestStatus]);

  const ended = loaded.state === "ready" && isTerminal(loaded.meeting.status);
  const transcriptState = loaded.state === "ready" ? loaded.meeting.transcript_state : null;
  const stored = ended && transcriptState === "stored";
  const storing = ended && (transcriptState === "none" || transcriptState === "pending");

  // Ended, not stored by andai yet: the platform's transcript, now and once more a little later,
  // merged by id (the platform can deliver a meeting's last lines after its final status).
  useEffect(() => {
    if (!ended || stored) return;
    let active = true;
    const read = async () => {
      try {
        const platform = await getTranscript(id);
        if (active) setSegments((current) => mergeSegments(current, platform));
      } catch {
        // what is on screen stays
      }
    };
    void read();
    const again = setTimeout(() => void read(), FINAL_READ_DELAY_MS);
    return () => {
      active = false;
      clearTimeout(again);
    };
  }, [ended, stored, id]);

  // ...and the meeting again until andai has stored its transcript (or gives up).
  useEffect(() => {
    if (!storing) return;
    let active = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      polls += 1;
      try {
        const fresh = await getMeeting(id);
        if (!active) return;
        setLoaded((current) => (current.state === "ready" ? { state: "ready", meeting: fresh } : current));
        if (fresh.transcript_state !== "none" && fresh.transcript_state !== "pending") return;
      } catch (err) {
        if (!active) return;
        if (err instanceof ApiError && err.status === 404) {
          setLoaded({ state: "missing" });
          return;
        }
      }
      if (active && polls < STORE_POLLS) timer = setTimeout(() => void poll(), STORE_POLL_MS);
    };
    timer = setTimeout(() => void poll(), STORE_POLL_MS);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [storing, id]);

  // Stored: andai's copy replaces whatever was on screen (same ids, with both scripts). The
  // minutes, which cite its lines, are read beside it (`MinutesPanel` reports when).
  useEffect(() => {
    if (!stored) return;
    let active = true;
    setTranscriptRead(false);
    getTranscript(id)
      .then((copy) => {
        if (active) setSegments(copy);
      })
      .catch(() => {
        // a failed read leaves what is on screen; a reload tries again
      })
      .finally(() => {
        if (active) setTranscriptRead(true);
      });
    return () => {
      active = false;
    };
  }, [stored, id]);
  const minutesSettled = useCallback(() => setMinutesRead(true), []);

  async function onStop() {
    setStopping(true);
    setStopError(null);
    try {
      const updated = await stopMeeting(id);
      // A stop before admission ends the meeting at once (cancelled); otherwise the stream reports the end.
      setLoaded({ state: "ready", meeting: isTerminal(updated.status) ? updated : withLatestStatus(updated) });
    } catch (err) {
      setStopError(err instanceof ApiError ? sentence(err.detail) : "Could not stop the bot. Try again.");
    } finally {
      setStopping(false);
    }
  }

  if (loaded.state === "loading") {
    return (
      <p role="status" className="py-16 text-center text-sm text-muted-foreground">
        Loading the meeting…
      </p>
    );
  }
  if (loaded.state === "missing") return <MeetingNotFound />;
  if (loaded.state === "error") return <FormAlert>{loaded.message}</FormAlert>;

  const m = loaded.meeting;
  const ending = endText(m.end_reason);
  const allowed = m.permissions;
  return (
    <div className="grid gap-6" data-testid="meeting-page" data-meeting-id={m.id}>
      <Link
        href="/"
        className="inline-flex w-fit items-center gap-1.5 rounded-md text-sm text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Meetings
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <Header meeting={m} />
        <div className="flex flex-wrap gap-2">
          {allowed?.can_share && (
            <Button type="button" variant="outline" size="lg" onClick={() => setShareOpen(true)}>
              <Share2 aria-hidden="true" />
              Share
            </Button>
          )}
          {ended && allowed?.can_delete && (
            <Button type="button" variant="ghost" size="lg" className="text-destructive hover:text-destructive" onClick={() => setDeleteOpen(true)}>
              <Trash2 aria-hidden="true" />
              Delete
            </Button>
          )}
          {!ended && allowed?.can_stop !== false && (
            <Button type="button" variant="outline" size="lg" onClick={() => void onStop()} disabled={stopping} aria-busy={stopping}>
              <Square aria-hidden="true" />
              {stopping ? "Stopping…" : "Stop the bot"}
            </Button>
          )}
        </div>
      </div>
      <Meta meeting={m} />
      <StatusBadge status={m.status} changedAt={changedAt ?? m.updated_at} />
      {stopError && <FormAlert>{stopError}</FormAlert>}
      {m.status === "failed" && <FailurePanel reason={m.failure_reason} />}
      {m.status === "awaiting_admission" && m.admit_instructions && <AdmitInstructions text={m.admit_instructions} />}
      {(m.status === "done" || m.status === "cancelled") && ending && (
        <p className="text-sm text-muted-foreground" data-testid="meeting-ended">
          {ending}
        </p>
      )}
      {ended ? (
        <>
          {stored && <Attendees attendees={m.attendees ?? []} />}
          <div className="flex flex-wrap items-start gap-6">
            {stored && <MinutesPanel meeting={m} segments={segments} onEvidence={showSegment} onSettled={minutesSettled} />}
            <TranscriptView segments={segments} script={script} onScriptChange={setScript} target={target} ready={stored ? settled : !storing} preparing={storing} />
          </div>
          <div className="max-w-xl">
            <ConsentNote botName={m.bot_name} consent={m.consent} />
          </div>
        </>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
          <LiveTranscript segments={segments} mode={mode} waiting />
          <ConsentNote botName={m.bot_name} consent={m.consent} />
        </div>
      )}
      {allowed?.can_share && (
        <ShareDialog
          meetingId={m.id}
          orgName={me.org.name}
          hidden={[me.user.id, m.requested_by]}
          open={shareOpen}
          onOpenChange={setShareOpen}
          onChanged={() => void refresh()}
        />
      )}
      {allowed?.can_delete && (
        <DeleteDialog
          meetingId={m.id}
          open={deleteOpen}
          onOpenChange={setDeleteOpen}
          onDeleted={() => {
            setSegments([]);
            setLoaded({ state: "missing" });
          }}
        />
      )}
    </div>
  );
}
