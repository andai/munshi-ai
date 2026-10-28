"use client";

import { ArrowDown } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";

import { offset } from "@/components/meetings/display";
import { Button } from "@/components/ui/button";
import type { LiveMode, Segment } from "@/lib/meetings";

// The transcript as it arrives: speaker, time into the meeting and text per line. The list follows
// the newest line until the reader scrolls up, and offers a way back to the latest line.

const NEAR_BOTTOM_PX = 48;

function ModeNote({ mode }: { mode: LiveMode }) {
  if (mode === "ended") return null;
  return (
    <span className="text-xs text-muted-foreground" data-testid="transcript-mode" data-mode={mode}>
      {mode === "live" ? "Live" : "Updating every few seconds"}
    </span>
  );
}

export function LiveTranscript({ segments, mode, waiting }: { segments: Segment[]; mode: LiveMode; waiting: boolean }) {
  const scroller = useRef<HTMLOListElement>(null);
  const [following, setFollowing] = useState(true);

  useLayoutEffect(() => {
    const list = scroller.current;
    if (list && following) list.scrollTop = list.scrollHeight;
  }, [segments, following]);

  const onScroll = () => {
    const list = scroller.current;
    if (!list) return;
    setFollowing(list.scrollHeight - list.scrollTop - list.clientHeight <= NEAR_BOTTOM_PX);
  };

  return (
    <section aria-labelledby="transcript-title" className="relative rounded-xl border border-border bg-card" data-testid="transcript">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <h2 id="transcript-title" className="text-sm font-semibold">
          Transcript
        </h2>
        <ModeNote mode={mode} />
      </div>
      {segments.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-muted-foreground" data-testid="transcript-empty">
          {waiting ? "Lines appear here as people speak." : "No transcript for this meeting."}
        </p>
      ) : (
        <ol ref={scroller} onScroll={onScroll} aria-live="polite" aria-relevant="additions" className="max-h-[60vh] overflow-y-auto px-4 py-2">
          {segments.map((segment) => (
            <li key={segment.id} data-testid="transcript-line" data-segment-id={segment.id} className="grid gap-0.5 py-2.5">
              <div className="flex items-baseline gap-2 text-xs">
                <span className="truncate font-semibold text-foreground">{segment.speaker}</span>
                <time className="shrink-0 font-mono text-muted-foreground tabular-nums">{offset(segment.start)}</time>
              </div>
              <p className={segment.final ? "text-sm [overflow-wrap:anywhere]" : "text-sm text-muted-foreground [overflow-wrap:anywhere]"}>
                {segment.text}
              </p>
            </li>
          ))}
        </ol>
      )}
      {!following && segments.length > 0 && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <Button
            type="button"
            size="sm"
            variant="secondary"
            className="pointer-events-auto shadow-sm"
            onClick={() => {
              const list = scroller.current;
              if (list) list.scrollTop = list.scrollHeight;
              setFollowing(true);
            }}
          >
            <ArrowDown aria-hidden="true" />
            Latest line
          </Button>
        </div>
      )}
    </section>
  );
}
