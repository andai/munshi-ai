"use client";

import { useEffect, useRef } from "react";

import { initials, offset } from "@/components/meetings/display";
import type { Segment } from "@/lib/meetings";
import { cn } from "@/lib/utils";

// A finished meeting's transcript, every stored line: speaker, time into the meeting and text. The
// lines open in the script they were said in (D-41); "Roman Hinglish" shows each line's Roman form
// instead (`text_canonical`, what search matches). The toggle appears when some line has a Roman
// form that differs from what was said; the choice lives in the page, so it survives the meeting
// ending and the stored copy replacing the live lines.
//
// Each line is an anchor (`id="seg-<id>"`). The page passes the line to show (from the URL's
// `#seg-<id>` or an evidence link in the minutes); it is scrolled to the middle of the list and
// highlighted, once each time it is asked for, as soon as that line is on screen and the page says
// its layout is final (nothing will arrive above the line and push it away).

export type ScriptChoice = "original" | "roman";

/** Which line to show; `n` changes each time the same line is asked for again. */
export type SegmentTarget = { id: string; n: number };

export const SEGMENT_ANCHOR = "seg-";

export function segmentAnchor(id: string): string {
  return `${SEGMENT_ANCHOR}${id}`;
}

/** The segment id in a `#seg-<id>` hash, or null. */
export function segmentFromHash(hash: string): string | null {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!value.startsWith(SEGMENT_ANCHOR)) return null;
  try {
    return decodeURIComponent(value.slice(SEGMENT_ANCHOR.length)) || null;
  } catch {
    return null;
  }
}

function lineText(segment: Segment, script: ScriptChoice): string {
  if (script === "roman") return segment.text_canonical ?? segment.text;
  return segment.text_raw ?? segment.text;
}

/** Whether any line reads differently in Roman Hinglish (otherwise the toggle has nothing to do). */
export function hasRomanForm(segments: Segment[]): boolean {
  return segments.some((s) => s.text_canonical !== undefined && s.text_canonical !== (s.text_raw ?? s.text));
}

function ScriptToggle({ value, onChange }: { value: ScriptChoice; onChange: (value: ScriptChoice) => void }) {
  const options: { value: ScriptChoice; label: string }[] = [
    { value: "original", label: "Original script" },
    { value: "roman", label: "Roman Hinglish" },
  ];
  return (
    <div role="group" aria-label="Transcript script" className="inline-flex gap-0.5 rounded-xl bg-muted p-1" data-testid="script-toggle">
      {options.map((option) => {
        const pressed = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={pressed}
            onClick={() => onChange(option.value)}
            className={cn(
              "min-h-8 rounded-lg px-3 text-[13px] font-medium whitespace-nowrap text-muted-foreground transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
              pressed ? "bg-card text-foreground shadow-xs" : "hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function TranscriptView({
  segments,
  script,
  onScriptChange,
  target,
  ready,
  preparing,
}: {
  segments: Segment[];
  script: ScriptChoice;
  onScriptChange: (value: ScriptChoice) => void;
  target: SegmentTarget | null;
  /** The page's layout is final (the stored lines and the minutes are in), so scrolling may start. */
  ready: boolean;
  /** andai is still storing its copy of the transcript. */
  preparing: boolean;
}) {
  const shown = useRef<SegmentTarget | null>(null);
  const toggle = hasRomanForm(segments);

  useEffect(() => {
    if (!ready || !target || shown.current === target) return;
    const line = document.getElementById(segmentAnchor(target.id));
    if (!line) return; // not on screen yet: tried again when the lines change
    shown.current = target;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    line.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
  }, [ready, target, segments]);

  const highlighted = target?.id ?? null;

  return (
    <section
      aria-labelledby="transcript-title"
      className="min-w-0 flex-[1_1_400px] rounded-2xl border border-border bg-card"
      data-testid="transcript"
      data-script={script}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <h2 id="transcript-title" className="text-sm font-semibold">
          Transcript
        </h2>
        {toggle && <ScriptToggle value={script} onChange={onScriptChange} />}
      </div>
      {preparing && (
        <p role="status" className="border-b border-border px-4 py-2 text-xs text-muted-foreground" data-testid="transcript-preparing">
          Preparing transcript…
        </p>
      )}
      {segments.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-muted-foreground" data-testid="transcript-empty">
          {preparing ? "The transcript appears here once it is ready." : "No transcript for this meeting."}
        </p>
      ) : (
        <ol className="max-h-[75vh] overflow-y-auto px-2 py-2" data-testid="transcript-lines">
          {segments.map((segment) => {
            const text = lineText(segment, script);
            const on = segment.id === highlighted;
            return (
              <li
                key={segment.id}
                id={segmentAnchor(segment.id)}
                data-testid="transcript-line"
                data-segment-id={segment.id}
                data-highlighted={on ? "true" : undefined}
                className={cn(
                  "flex scroll-mt-4 gap-3 rounded-xl p-2.5 transition-colors duration-300",
                  on && "bg-saffron/15 ring-[1.5px] ring-saffron ring-inset",
                )}
              >
                <span
                  aria-hidden="true"
                  className="flex size-8 shrink-0 items-center justify-center rounded-full bg-secondary text-[11px] font-bold text-secondary-foreground"
                >
                  {initials(segment.speaker)}
                </span>
                <div className="grid min-w-0 gap-0.5">
                  <div className="flex items-baseline gap-2 text-xs">
                    <span className="truncate font-semibold text-foreground">{segment.speaker}</span>
                    <time className="shrink-0 font-mono text-muted-foreground tabular-nums">{offset(segment.start)}</time>
                  </div>
                  <p
                    data-testid="transcript-text"
                    lang={script === "original" && segment.script === "deva" ? "hi" : undefined}
                    className="text-sm [overflow-wrap:anywhere]"
                  >
                    {text}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
