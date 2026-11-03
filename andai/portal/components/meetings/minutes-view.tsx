"use client";

import { CalendarDays, ChevronDown, CircleAlert, CircleUserRound, LoaderCircle, MessageSquareOff, Quote, UserRoundX } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { OUTPUT_LANGUAGE_NAME, dueText, momErrorText, offset, templateName, whenText } from "@/components/meetings/display";
import { MinutesEditor } from "@/components/meetings/minutes-editor";
import { MinutesToolbar } from "@/components/meetings/minutes-toolbar";
import { RegenerateDialog } from "@/components/meetings/regenerate-dialog";
import { segmentAnchor } from "@/components/meetings/transcript-view";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ApiError, sentence } from "@/lib/api";
import {
  POLL_INTERVAL_MS,
  UNASSIGNED,
  getMeeting,
  getMinutes,
  listMinuteVersions,
  type Meeting,
  type Minutes,
  type MinutesVersion,
  type MomActionItem,
  type MomSection,
  type MomState,
  type OutputLanguage,
  type Segment,
} from "@/lib/meetings";

// A finished meeting's minutes, where generation stands, and what the reader may do with them.
//
// * States (the meeting's `mom_state`): "Preparing minutes…" while they are generated (the page
//   re-reads the meeting while it is `pending`, and only then, and shows the new version when it is
//   stored, without a reload); a typed failure with Regenerate; "No minutes: nothing was said" when
//   the transcript was empty. A regenerate under way shows the current version with a notice.
// * Versions: the header names the shown version, its template, output language and who edited it;
//   the version menu shows any earlier one. Sections, their order and their headings are the
//   template's, in the version's language, as andai-core sends them (`sections`, `headings`,
//   `labels`); the portal keeps no table of its own. Every item links to what was said: each
//   evidence id is a transcript segment, shown as its time; selecting it asks the page to show that
//   line (`onEvidence`).
// * Edit and Regenerate for callers andai-core lets change the meeting (`permissions.can_share`),
//   Download (Markdown, PDF) for every reader. Unsaved edits are not dropped without asking.

const PANEL = "min-w-0 flex-[999_1_560px] rounded-2xl border border-border bg-card px-5 pt-5 pb-2 sm:px-6";
const LONG_WAIT_POLLS = 40; // after this many polls (2 minutes) the page re-reads less often
const SLOW_POLL_MS = 15_000;
// `none`: generation has not been claimed yet (it is within seconds when the pipeline runs); the
// page looks a while for it to start, then stops.
const NONE_POLLS = 40;

function Evidence({ ids, segments, onEvidence }: { ids: string[]; segments: Map<string, Segment>; onEvidence: (id: string) => void }) {
  if (ids.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1.5">
      {ids.map((id) => {
        const segment = segments.get(id);
        const time = segment ? offset(segment.start) : null;
        return (
          <a
            key={id}
            href={`#${segmentAnchor(id)}`}
            data-testid="evidence-link"
            data-segment-id={id}
            aria-label={time ? `Show transcript at ${time}` : "Show the transcript line"}
            onClick={(event) => {
              event.preventDefault();
              onEvidence(id);
            }}
            className="inline-flex min-h-7 items-center gap-1 rounded-md border border-border bg-card px-2 font-mono text-xs font-medium text-primary hover:border-primary hover:bg-primary/10 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <Quote aria-hidden="true" className="size-3" />
            {time ?? "Line"}
          </a>
        );
      })}
    </span>
  );
}

function Section({ title, testId, empty, none, children }: { title: string; testId: string; empty: boolean; none: string; children: ReactNode }) {
  return (
    <section className="grid gap-3 border-t border-border py-5" data-testid={testId} data-empty={empty ? "true" : undefined}>
      <h3 className="text-xs font-bold tracking-wider text-saffron-ink uppercase" data-testid="minutes-heading">
        {title}
      </h3>
      {empty ? <p className="text-sm text-muted-foreground">{none}</p> : children}
    </section>
  );
}

function Item({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <li data-testid={testId} className="grid gap-2 rounded-xl bg-muted/60 px-3.5 py-3 text-sm [overflow-wrap:anywhere]">
      {children}
    </li>
  );
}

function ActionItem({ item, segments, onEvidence }: { item: MomActionItem; segments: Map<string, Segment>; onEvidence: (id: string) => void }) {
  const due = dueText(item.due);
  const assigned = item.owner.toLowerCase() !== UNASSIGNED;
  return (
    <Item testId="action-item">
      <span>{item.text}</span>
      <span className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
        {assigned && (
          <span className="inline-flex items-center gap-1.5 font-medium text-foreground" data-testid="action-owner">
            <CircleUserRound aria-hidden="true" className="size-3.5" />
            {item.owner}
          </span>
        )}
        {due && (
          <span className="inline-flex items-center gap-1.5" data-testid="action-due">
            <CalendarDays aria-hidden="true" className="size-3.5" />
            Due {due}
          </span>
        )}
        <Evidence ids={item.evidence} segments={segments} onEvidence={onEvidence} />
      </span>
    </Item>
  );
}

const SECTION_TEST_ID: Record<MomSection, string> = {
  summary: "minutes-summary",
  attendees: "minutes-attendees",
  topics: "minutes-topics",
  decisions: "minutes-decisions",
  action_items: "minutes-actions",
  risks: "minutes-risks",
};

/** One version of the minutes, laid out by its template. */
function MinutesDocument({
  minutes,
  attendees,
  segments,
  onEvidence,
}: {
  minutes: Minutes;
  attendees: string[];
  segments: Segment[];
  onEvidence: (id: string) => void;
}) {
  const byId = new Map(segments.map((s) => [s.id, s]));
  const none = minutes.labels?.noneRecorded ?? "None recorded";
  const assigned = minutes.actionItems.filter((a) => a.owner.toLowerCase() !== UNASSIGNED);
  const unassigned = minutes.actionItems.filter((a) => a.owner.toLowerCase() === UNASSIGNED);
  const sections: MomSection[] = minutes.sections?.length ? minutes.sections : ["summary", "decisions", "action_items", "topics", "risks"];

  const body = (section: MomSection): { empty: boolean; content: ReactNode } => {
    switch (section) {
      case "summary":
        return { empty: !minutes.summary.trim(), content: <p className="text-sm leading-relaxed [overflow-wrap:anywhere]">{minutes.summary}</p> };
      case "attendees":
        return {
          empty: attendees.length === 0,
          content: <p className="text-sm [overflow-wrap:anywhere]">{attendees.join(", ")}</p>,
        };
      case "decisions":
        return {
          empty: minutes.decisions.length === 0,
          content: (
            <ul className="grid gap-2">
              {minutes.decisions.map((decision, index) => (
                <Item key={index} testId="decision">
                  <span>{decision.text}</span>
                  <Evidence ids={decision.evidence} segments={byId} onEvidence={onEvidence} />
                </Item>
              ))}
            </ul>
          ),
        };
      case "action_items":
        return {
          empty: minutes.actionItems.length === 0,
          content: (
            <>
              {assigned.length > 0 && (
                <ul className="grid gap-2">
                  {assigned.map((item) => (
                    <ActionItem key={item.id} item={item} segments={byId} onEvidence={onEvidence} />
                  ))}
                </ul>
              )}
              {unassigned.length > 0 && (
                <div className="grid gap-2" data-testid="unassigned-actions">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-saffron/15 px-2 py-0.5 text-xs font-semibold text-saffron-ink">
                      <UserRoundX aria-hidden="true" className="size-3.5" />
                      {minutes.labels?.unassigned ?? "Unassigned"}
                    </span>
                    <span className="text-xs text-muted-foreground">nobody was clearly named in the meeting</span>
                  </div>
                  <ul className="grid gap-2">
                    {unassigned.map((item) => (
                      <ActionItem key={item.id} item={item} segments={byId} onEvidence={onEvidence} />
                    ))}
                  </ul>
                </div>
              )}
            </>
          ),
        };
      case "topics":
        return {
          empty: minutes.topics.length === 0,
          content: (
            <ul className="grid gap-2">
              {minutes.topics.map((topic, index) => (
                <Item key={index} testId="topic">
                  <span>{topic.title}</span>
                  <Evidence ids={topic.evidence} segments={byId} onEvidence={onEvidence} />
                </Item>
              ))}
            </ul>
          ),
        };
      case "risks":
        return {
          empty: minutes.risks.length === 0,
          content: (
            <ul className="grid gap-2">
              {minutes.risks.map((risk, index) => (
                <Item key={index} testId="risk">
                  <span>{risk.text}</span>
                  <Evidence ids={risk.evidence} segments={byId} onEvidence={onEvidence} />
                </Item>
              ))}
            </ul>
          ),
        };
    }
  };

  return (
    <div lang={minutes.outputLanguage === "hi" ? "hi" : undefined}>
      {sections.map((section) => {
        const { empty, content } = body(section);
        return (
          <Section key={section} title={minutes.headings?.[section] ?? section} testId={SECTION_TEST_ID[section]} empty={empty} none={none}>
            {content}
          </Section>
        );
      })}
      <p className="border-t border-border py-3 text-xs text-muted-foreground" lang="en">
        Every decision, action and risk links to what was said — select a time to see it.
      </p>
    </div>
  );
}

function versionLine(entry: MinutesVersion | undefined, minutes: Minutes): string {
  const parts = [templateName(minutes.templateId), OUTPUT_LANGUAGE_NAME[minutes.outputLanguage] ?? minutes.outputLanguage];
  if (minutes.editedBy) parts.push(`Edited by ${entry?.editedByName ?? "a colleague"}`);
  const when = whenText(minutes.generatedAt);
  if (when) parts.push(when);
  return parts.join(" · ");
}

function VersionMenu({ versions, shown, onPick }: { versions: MinutesVersion[]; shown: number; onPick: (version: number) => void }) {
  const latest = versions[0]?.version;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm" data-testid="version-menu" aria-label={`Minutes version ${shown}; show another version`}>
          Version {shown}
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
        <DropdownMenuLabel>Versions</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={String(shown)} onValueChange={(value) => onPick(Number(value))}>
          {versions.map((entry) => (
            <DropdownMenuRadioItem key={entry.version} value={String(entry.version)} data-testid="version-option" data-version={entry.version}>
              <span className="grid min-w-0 gap-0.5">
                <span className="font-medium">
                  Version {entry.version}
                  {entry.version === latest && <span className="font-normal text-muted-foreground"> · latest</span>}
                </span>
                <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {entry.editedBy
                    ? `Edited by ${entry.editedByName ?? "a colleague"}`
                    : `${templateName(entry.templateId)} · ${OUTPUT_LANGUAGE_NAME[entry.outputLanguage] ?? entry.outputLanguage}`}
                  {entry.createdAt && ` · ${whenText(entry.createdAt)}`}
                </span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A minutes state without a document to show: pending, failed or skipped. */
function StateCard({ state, children }: { state: MomState; children: ReactNode }) {
  return (
    <section aria-labelledby="minutes-title" className={`${PANEL} pb-5`} data-testid="minutes-panel">
      <h2 id="minutes-title" className="pb-4 text-lg font-semibold tracking-tight">
        Minutes
      </h2>
      <div className="grid gap-3" data-testid="minutes-state" data-state={state} role={state === "pending" ? "status" : undefined}>
        {children}
      </div>
    </section>
  );
}

function Failure({ code }: { code: string | null }) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 px-3.5 py-3 text-sm">
      <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
      <p className="grid gap-0.5">
        <span className="font-semibold">The minutes could not be prepared.</span>
        <span className="text-muted-foreground" data-testid="minutes-error">
          {momErrorText(code)}
          {code && <span className="ml-1 font-mono text-xs">({code})</span>}
        </span>
      </p>
    </div>
  );
}

export function MinutesPanel({
  meeting,
  segments,
  onEvidence,
  onSettled,
}: {
  meeting: Meeting;
  segments: Segment[];
  onEvidence: (id: string) => void;
  /** Called once, when the first read of the minutes has finished (the page's layout is final). */
  onSettled: () => void;
}) {
  const id = meeting.id;
  const allowed = meeting.permissions?.can_share === true;
  const attendees = [...new Set((meeting.attendees ?? []).map((a) => a.name.trim()).filter(Boolean))];

  const [state, setState] = useState<MomState>(meeting.mom_state ?? "none");
  const [error, setError] = useState<string | null>(meeting.mom_error ?? null);
  const [latest, setLatest] = useState<Minutes | null>(null);
  const [shown, setShown] = useState<Minutes | null>(null);
  const [versions, setVersions] = useState<MinutesVersion[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState<(() => void) | null>(null);
  const editingRef = useRef(false);
  editingRef.current = editing;

  // A fresher read of the meeting (the page's own) carries the newest state.
  useEffect(() => {
    if (meeting.mom_state) setState(meeting.mom_state);
    setError(meeting.mom_error ?? null);
  }, [meeting.mom_state, meeting.mom_error]);

  // The latest version and the version list; the shown version follows the latest unless the
  // reader is editing (an edit stays on the version it started from).
  const refresh = useCallback(async () => {
    const doc = await getMinutes(id);
    const list = doc ? await listMinuteVersions(id) : [];
    setLatest(doc);
    setVersions(list);
    if (!editingRef.current) setShown(doc);
  }, [id]);

  const settledOnce = useRef(onSettled);
  useEffect(() => {
    let active = true;
    refresh()
      .catch(() => {
        // nothing shown; a reload tries again
      })
      .finally(() => {
        if (!active) return;
        setLoaded(true);
        settledOnce.current();
      });
    return () => {
      active = false;
    };
  }, [refresh]);

  // Re-read the meeting while the minutes are being generated (and, for a while, before generation
  // has started); a state that is not `pending` ends it.
  useEffect(() => {
    if (state !== "pending" && state !== "none") return;
    let active = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      polls += 1;
      try {
        const fresh = await getMeeting(id);
        if (!active) return;
        setError(fresh.mom_error ?? null);
        if (fresh.mom_state !== state) {
          setState(fresh.mom_state);
          return;
        }
      } catch {
        // the next poll tries again
      }
      if (!active || (state === "none" && polls >= NONE_POLLS)) return;
      timer = setTimeout(() => void poll(), polls >= LONG_WAIT_POLLS ? SLOW_POLL_MS : POLL_INTERVAL_MS);
    };
    timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [state, id]);

  // Generation finished (stored, or failed or skipped after a regenerate): show what is there now.
  const previous = useRef<MomState>(state);
  useEffect(() => {
    const was = previous.current;
    previous.current = state;
    if ((was === "pending" || was === "none") && state !== "pending" && state !== "none") {
      void refresh().catch(() => {});
    }
  }, [state, refresh]);

  // Leave edit mode, asking first when there are unsaved changes.
  const leaveEditing = useCallback(
    (then: () => void) => {
      if (editing && dirty) {
        setConfirmDiscard(() => () => {
          setEditing(false);
          setDirty(false);
          then();
        });
        return;
      }
      setEditing(false);
      setDirty(false);
      then();
    },
    [editing, dirty],
  );

  const showVersion = useCallback(
    (version: number) => {
      leaveEditing(() => {
        setReadError(null);
        if (latest && version === latest.version) {
          setShown(latest);
          return;
        }
        getMinutes(id, version)
          .then((doc) => setShown(doc))
          .catch((err) => setReadError(err instanceof ApiError ? sentence(err.detail) : "Could not open that version. Try again."));
      });
    },
    [id, latest, leaveEditing],
  );

  if (!loaded) return null;

  const pending = state === "pending";
  const regenerateDefaults: { templateId: string; outputLanguage: OutputLanguage } = shown
    ? { templateId: shown.templateId, outputLanguage: shown.outputLanguage }
    : { templateId: "standard", outputLanguage: meeting.language ?? "en" };
  const dialog = allowed && (
    <RegenerateDialog
      meetingId={id}
      templateId={regenerateDefaults.templateId}
      outputLanguage={regenerateDefaults.outputLanguage}
      open={regenerating}
      onOpenChange={setRegenerating}
      onRequested={() => {
        setError(null);
        setState("pending");
      }}
    />
  );

  if (!shown) {
    if (pending) {
      return (
        <StateCard state="pending">
          <p className="flex items-center gap-2 text-sm font-medium">
            <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-primary motion-reduce:animate-none" />
            Preparing minutes…
          </p>
          <p className="text-sm text-muted-foreground">They appear here as soon as they are ready.</p>
        </StateCard>
      );
    }
    if (state === "failed") {
      return (
        <StateCard state="failed">
          <Failure code={error} />
          {allowed && (
            <Button type="button" variant="outline" className="w-fit" onClick={() => setRegenerating(true)}>
              Regenerate
            </Button>
          )}
          {dialog}
        </StateCard>
      );
    }
    if (state === "skipped") {
      return (
        <StateCard state="skipped">
          <p className="flex items-center gap-2 text-sm font-medium">
            <MessageSquareOff aria-hidden="true" className="size-4 text-muted-foreground" />
            No minutes: nothing was said
          </p>
          <p className="text-sm text-muted-foreground">The transcript is empty, so there was nothing to write up.</p>
        </StateCard>
      );
    }
    return null;
  }

  const isLatest = latest !== null && shown.version === latest.version;
  const entry = versions.find((v) => v.version === shown.version);
  return (
    <section
      aria-labelledby="minutes-title"
      className={PANEL}
      data-testid="minutes"
      data-version={shown.version}
      data-latest={isLatest ? "true" : "false"}
      data-editing={editing ? "true" : undefined}
    >
      <div className="grid gap-3 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <h2 id="minutes-title" className="text-lg font-semibold tracking-tight">
            {editing ? "Edit the minutes" : "Minutes"}
          </h2>
          {versions.length > 0 && <VersionMenu versions={versions} shown={shown.version} onPick={showVersion} />}
        </div>
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="minutes-version">
          Version {shown.version} · {versionLine(entry, shown)}
        </p>
        {!isLatest && latest && !editing && (
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm" data-testid="minutes-older">
            <span className="text-muted-foreground">This is an earlier version.</span>
            <Button type="button" variant="link" className="h-auto p-0" onClick={() => showVersion(latest.version)}>
              Show the latest (version {latest.version})
            </Button>
          </p>
        )}
        {pending && (
          <p className="flex items-center gap-2 rounded-xl bg-secondary px-3.5 py-2.5 text-sm" data-testid="minutes-state" data-state="pending" role="status">
            <LoaderCircle aria-hidden="true" className="size-4 shrink-0 animate-spin text-primary motion-reduce:animate-none" />
            Preparing a new version of the minutes…
          </p>
        )}
        {state === "failed" && !pending && (
          <div data-testid="minutes-state" data-state="failed">
            <Failure code={error} />
          </div>
        )}
        {readError && <FormAlert>{readError}</FormAlert>}
        {!editing && (
          <MinutesToolbar
            meetingId={id}
            version={shown.version}
            canEdit={allowed && isLatest && !pending}
            canRegenerate={allowed && !pending}
            onEdit={() => setEditing(true)}
            onRegenerate={() => setRegenerating(true)}
          />
        )}
      </div>

      {editing ? (
        <MinutesEditor
          meetingId={id}
          minutes={shown}
          segments={segments}
          attendees={attendees}
          onDirtyChange={setDirty}
          onCancel={() => leaveEditing(() => {})}
          onSaved={(saved) => {
            setEditing(false);
            setDirty(false);
            setLatest(saved);
            setShown(saved);
            void listMinuteVersions(id)
              .then(setVersions)
              .catch(() => {});
          }}
        />
      ) : (
        <MinutesDocument minutes={shown} attendees={attendees} segments={segments} onEvidence={onEvidence} />
      )}

      {dialog}
      <AlertDialog open={confirmDiscard !== null} onOpenChange={(open) => !open && setConfirmDiscard(null)}>
        <AlertDialogContent data-testid="discard-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Discard your changes?</AlertDialogTitle>
            <AlertDialogDescription>Your edits to the minutes have not been saved. They are lost if you leave the editor.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={() => {
                const then = confirmDiscard;
                setConfirmDiscard(null);
                then?.();
              }}
            >
              Discard changes
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
