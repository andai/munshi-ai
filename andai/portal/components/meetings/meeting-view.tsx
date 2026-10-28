"use client";

import { ArrowLeft, Square, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { AdmitInstructions } from "@/components/meetings/admit-instructions";
import { ConsentNote } from "@/components/meetings/consent-note";
import { LANGUAGE_NAME, PLATFORM_NAME, endText, linkHost } from "@/components/meetings/display";
import { FailurePanel } from "@/components/meetings/failure-panel";
import { LiveTranscript } from "@/components/meetings/live-transcript";
import { StatusBadge } from "@/components/meetings/status-badge";
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

// One meeting, as andai-core reports it: status and when it changed, why it failed, how to admit
// the bot while it waits, the recording notice, and the transcript as it arrives (live stream,
// polling when the stream drops). A meeting this account may not read does not exist here (404).

const FINAL_READ_DELAY_MS = 3_000;

type Loaded =
  | { state: "loading" }
  | { state: "missing" }
  | { state: "error"; message: string }
  | { state: "ready"; meeting: Meeting };

function NotFound() {
  return (
    <section data-testid="meeting-not-found" className="mx-auto grid max-w-md justify-items-center gap-2 py-16 text-center">
      <p className="text-sm font-medium text-muted-foreground">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">Meeting not found</h1>
      <p className="text-sm text-muted-foreground">This meeting does not exist, or it is not shared with you.</p>
      <Button asChild variant="secondary" className="mt-4">
        <Link href="/">Back to meetings</Link>
      </Button>
    </section>
  );
}

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
        <h1 className="text-2xl font-semibold tracking-tight" data-testid="meeting-title">
          {platform} meeting
        </h1>
        <p className="truncate text-sm text-muted-foreground" data-testid="meeting-host">
          {[host, language].filter(Boolean).join(" · ")}
        </p>
      </div>
    </div>
  );
}

export function MeetingView({ id }: { id: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [segments, setSegments] = useState<Segment[]>([]);
  const [mode, setMode] = useState<LiveMode>("live");
  const [changedAt, setChangedAt] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
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

  // After a status change: the consent record and times that came with it.
  const refresh = useCallback(async () => {
    try {
      const fresh = await getMeeting(id);
      setLoaded({ state: "ready", meeting: withLatestStatus(fresh) });
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
      onMeeting: (fresh) => setLoaded({ state: "ready", meeting: fresh }),
      onMode: setMode,
    });
    return stop;
  }, [follow, id, refresh, withLatestStatus]);

  // Once the meeting has ended: the stored transcript, now and once more a little later, merged by id
  // (the platform can deliver a meeting's last lines after its final status).
  const ended = loaded.state === "ready" && isTerminal(loaded.meeting.status);
  useEffect(() => {
    if (!ended) return;
    let active = true;
    const read = async () => {
      try {
        const stored = await getTranscript(id);
        if (active) setSegments((current) => mergeSegments(current, stored));
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
  }, [ended, id]);

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
  if (loaded.state === "missing") return <NotFound />;
  if (loaded.state === "error") return <FormAlert>{loaded.message}</FormAlert>;

  const m = loaded.meeting;
  const ending = endText(m.end_reason);
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
        {!ended && (
          <Button type="button" variant="outline" onClick={() => void onStop()} disabled={stopping} aria-busy={stopping}>
            <Square aria-hidden="true" />
            {stopping ? "Stopping…" : "Stop the bot"}
          </Button>
        )}
      </div>
      <StatusBadge status={m.status} changedAt={changedAt ?? m.updated_at} />
      {stopError && <FormAlert>{stopError}</FormAlert>}
      {m.status === "failed" && <FailurePanel reason={m.failure_reason} />}
      {m.status === "awaiting_admission" && m.admit_instructions && <AdmitInstructions text={m.admit_instructions} />}
      {m.status === "done" && ending && (
        <p className="text-sm text-muted-foreground" data-testid="meeting-ended">
          {ending}
        </p>
      )}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
        <LiveTranscript segments={segments} mode={ended ? "ended" : mode} waiting={!ended} />
        <ConsentNote botName={m.bot_name} consent={m.consent} />
      </div>
    </div>
  );
}
