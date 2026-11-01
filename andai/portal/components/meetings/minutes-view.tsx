"use client";

import { CalendarDays, CircleUserRound, Quote, UserRoundX } from "lucide-react";
import type { ReactNode } from "react";

import { dueText, offset, whenText } from "@/components/meetings/display";
import { segmentAnchor } from "@/components/meetings/transcript-view";
import { UNASSIGNED, type MomActionItem, type MomDocument, type Segment } from "@/lib/meetings";

// The latest minutes of a meeting, read-only (D-43): summary, decisions, action items (those nobody
// was named for grouped apart as "Unassigned"), topics and risks. An empty section says "None
// recorded". Every item links to what was said: each evidence id is a transcript segment, shown as
// its time into the meeting; selecting it asks the page to show that line (`onEvidence`).

const NONE = "None recorded";

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

function Section({ title, testId, empty, children }: { title: string; testId: string; empty: boolean; children: ReactNode }) {
  return (
    <section className="grid gap-3 border-t border-border py-5" data-testid={testId} data-empty={empty ? "true" : undefined}>
      <h3 className="text-xs font-bold tracking-wider text-saffron-ink uppercase">{title}</h3>
      {empty ? <p className="text-sm text-muted-foreground">{NONE}</p> : children}
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

export function MinutesView({
  minutes,
  segments,
  onEvidence,
}: {
  minutes: MomDocument;
  segments: Segment[];
  onEvidence: (id: string) => void;
}) {
  const byId = new Map(segments.map((s) => [s.id, s]));
  const assigned = minutes.actionItems.filter((a) => a.owner.toLowerCase() !== UNASSIGNED);
  const unassigned = minutes.actionItems.filter((a) => a.owner.toLowerCase() === UNASSIGNED);
  const generated = whenText(minutes.generatedAt);

  return (
    <section
      aria-labelledby="minutes-title"
      className="min-w-0 flex-[999_1_560px] rounded-2xl border border-border bg-card px-5 pt-5 pb-2 sm:px-6"
      data-testid="minutes"
      data-version={minutes.version}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 pb-4">
        <h2 id="minutes-title" className="text-lg font-semibold tracking-tight">
          Minutes
        </h2>
        <span className="text-xs text-muted-foreground">
          Version {minutes.version}
          {generated && ` · ${generated}`}
        </span>
      </div>

      <Section title="Summary" testId="minutes-summary" empty={!minutes.summary.trim()}>
        <p className="text-sm leading-relaxed [overflow-wrap:anywhere]">{minutes.summary}</p>
      </Section>

      <Section title="Decisions" testId="minutes-decisions" empty={minutes.decisions.length === 0}>
        <ul className="grid gap-2">
          {minutes.decisions.map((decision, index) => (
            <Item key={index} testId="decision">
              <span>{decision.text}</span>
              <Evidence ids={decision.evidence} segments={byId} onEvidence={onEvidence} />
            </Item>
          ))}
        </ul>
      </Section>

      <Section title="Action items" testId="minutes-actions" empty={minutes.actionItems.length === 0}>
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
                Unassigned
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
      </Section>

      <Section title="Topics" testId="minutes-topics" empty={minutes.topics.length === 0}>
        <ul className="grid gap-2">
          {minutes.topics.map((topic, index) => (
            <Item key={index} testId="topic">
              <span>{topic.title}</span>
              <Evidence ids={topic.evidence} segments={byId} onEvidence={onEvidence} />
            </Item>
          ))}
        </ul>
      </Section>

      <Section title="Risks" testId="minutes-risks" empty={minutes.risks.length === 0}>
        <ul className="grid gap-2">
          {minutes.risks.map((risk, index) => (
            <Item key={index} testId="risk">
              <span>{risk.text}</span>
              <Evidence ids={risk.evidence} segments={byId} onEvidence={onEvidence} />
            </Item>
          ))}
        </ul>
      </Section>

      <p className="border-t border-border py-3 text-xs text-muted-foreground">
        Every decision, action and risk links to what was said — select a time to see it.
      </p>
    </section>
  );
}
