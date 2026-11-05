"use client";

import { Users, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { LogoMark } from "@/components/brand/logo-mark";
import { FormAlert } from "@/components/forms/form-alert";
import {
  PLATFORM_NAME,
  SECTION_COPY,
  durationText,
  failureCopy,
  meetingTitle,
  shortTime,
  whenText,
  zonedWhenText,
} from "@/components/meetings/display";
import { StatusPill } from "@/components/meetings/status-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ApiError, sentence } from "@/lib/api";
import {
  SECTIONS,
  listMeetings,
  setSkipped,
  type ListFilter,
  type MeetingItem,
  type MeetingPage,
  type Platform,
  type Section,
} from "@/lib/meetings";
import { cn } from "@/lib/utils";

// The dashboard's one list of the meetings the caller may open (D-42): Live now, Upcoming, Past.
// The first request asks andai-core for one page of each section; "Load more" asks for the page
// after a section's last row with that section's cursor and appends it. Nothing is fetched on
// mount beyond that first page, and a filter change starts again from the first page (an answer
// for an older filter is dropped). Under the `md` breakpoint the Past table becomes stacked rows.
// `between` (the dashboard's Unassigned action items) sits between Upcoming and Past once the list
// has loaded. An Upcoming row shows when the meeting starts in the viewer's time zone and, for
// callers andai-core lets share the meeting, a Skip switch (POST/DELETE /meetings/{id}/skip) that
// changes the row at once and puts it back if andai-core refuses; a skipped row is marked.

type Pages = Record<Section, MeetingPage>;

const EMPTY: MeetingPage = { items: [], next_cursor: null };

function sortTime(item: MeetingItem): string | null {
  return item.started_at ?? item.created_at;
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? sentence(err.detail) : "Something went wrong. Try again.";
}

export function MeetingList({ filter, between }: { filter: ListFilter; between?: ReactNode }) {
  const [pages, setPages] = useState<Pages | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState<Section | null>(null);
  const [moreError, setMoreError] = useState<{ section: Section; text: string } | null>(null);
  // Bumped on every filter change; an answer for an older generation is dropped.
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setPages(null);
    setError(null);
    setMoreError(null);
    setLoadingMore(null);
    listMeetings({ filter, signal: controller.signal })
      .then((sections) => {
        if (current !== generation.current) return;
        setPages({ live: sections.live ?? EMPTY, upcoming: sections.upcoming ?? EMPTY, past: sections.past ?? EMPTY });
      })
      .catch((err) => {
        if (controller.signal.aborted || current !== generation.current) return;
        setError(errorText(err));
      });
    return () => controller.abort();
  }, [filter]);

  const loadMore = useCallback(
    async (section: Section) => {
      const cursor = pages?.[section].next_cursor;
      if (!cursor || loadingMore) return;
      const current = generation.current;
      setLoadingMore(section);
      setMoreError(null);
      try {
        const answer = await listMeetings({ section, cursor });
        if (current !== generation.current) return;
        const page = answer[section] ?? EMPTY;
        setPages((before) =>
          before
            ? { ...before, [section]: { items: [...before[section].items, ...page.items], next_cursor: page.next_cursor } }
            : before,
        );
      } catch (err) {
        if (current === generation.current) setMoreError({ section, text: errorText(err) });
      } finally {
        if (current === generation.current) setLoadingMore(null);
      }
    },
    [pages, loadingMore],
  );

  if (error) {
    return (
      <div className="mt-6" data-testid="meeting-list-error">
        <FormAlert>{error}</FormAlert>
      </div>
    );
  }

  if (!pages) {
    return (
      <div aria-busy="true" data-testid="meeting-list-loading" className="mt-6 grid gap-3">
        <span className="sr-only">Loading meetings…</span>
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-16 animate-pulse rounded-2xl bg-muted/70" />
        ))}
      </div>
    );
  }

  if (SECTIONS.every((section) => pages[section].items.length === 0)) {
    return filter === "all" ? <NothingYet /> : <NothingMatches />;
  }

  const patchItem = (section: Section, id: string, patch: Partial<MeetingItem>) =>
    setPages((before) =>
      before
        ? {
            ...before,
            [section]: { ...before[section], items: before[section].items.map((i) => (i.id === id ? { ...i, ...patch } : i)) },
          }
        : before,
    );

  const more = (section: Section): More => ({
    cursor: pages[section].next_cursor,
    loading: loadingMore === section,
    error: moreError !== null && moreError.section === section ? moreError.text : null,
    onLoadMore: () => void loadMore(section),
  });

  return (
    <div className="mt-6 grid gap-10" data-testid="meeting-list">
      <LiveSection page={pages.live} more={more("live")} />
      <UpcomingSection page={pages.upcoming} more={more("upcoming")} onSkipped={(id, skipped) => patchItem("upcoming", id, { skipped })} />
      {between}
      <PastSection page={pages.past} more={more("past")} />
    </div>
  );
}

// ─── empty states ──────────────────────────────────────────────────────────────────────────────

function NothingYet() {
  return (
    <section
      aria-labelledby="empty-title"
      data-testid="dashboard-empty"
      className="mt-8 flex flex-col items-center rounded-2xl border border-dashed border-border bg-card/60 px-6 py-16 text-center"
    >
      <LogoMark size={44} className="opacity-90" />
      <h2 id="empty-title" className="mt-5 text-lg font-semibold">
        No meetings yet
      </h2>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">
        Send the bot above to a meeting. Meetings you request, and meetings shared with you, are listed here.
      </p>
    </section>
  );
}

function NothingMatches() {
  return (
    <p
      data-testid="meeting-list-no-match"
      className="mt-6 rounded-2xl border border-dashed border-border bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground"
    >
      No meetings match this filter.
    </p>
  );
}

// ─── parts ─────────────────────────────────────────────────────────────────────────────────────

const PLATFORM_COLOUR: Record<Platform, string> = {
  google_meet: "bg-[#188038]",
  teams: "bg-[#4B53BC]",
  zoom: "bg-[#0B5CFF]",
};

/** The platform as a small coloured square with an icon; its name is the accessible label. */
export function PlatformMark({ platform, className }: { platform: Platform; className?: string }) {
  const Icon = platform === "teams" ? Users : Video;
  return (
    <span
      role="img"
      aria-label={PLATFORM_NAME[platform] ?? platform}
      className={cn("inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-white", PLATFORM_COLOUR[platform] ?? "bg-muted-foreground", className)}
    >
      <Icon aria-hidden="true" className="size-4" strokeWidth={2} />
    </span>
  );
}

function SectionHeading({ id, section, count }: { id: string; section: Section; count: string }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h2 id={id} className="text-lg font-semibold tracking-tight">
        {SECTION_COPY[section].title}
      </h2>
      <span className="text-sm font-medium text-muted-foreground" data-testid={`${section}-count`}>
        {count}
      </span>
    </div>
  );
}

function SharedTag({ item }: { item: MeetingItem }) {
  if (!item.shared) return null;
  return (
    <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground" data-testid="shared-tag">
      Shared with you
    </span>
  );
}

function plus(page: MeetingPage): string {
  return page.next_cursor ? "+" : "";
}

function LiveSection({ page, more }: { page: MeetingPage; more: More }) {
  return (
    <section aria-labelledby="live-heading" data-testid="section-live">
      <SectionHeading id="live-heading" section="live" count={`${page.items.length}${plus(page)} in progress`} />
      {page.items.length === 0 ? (
        <EmptyLine section="live" />
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {page.items.map((item) => (
            <li
              key={item.id}
              data-testid="live-card"
              data-meeting-id={item.id}
              className="flex min-w-0 flex-col gap-3 rounded-2xl border border-border bg-card p-4 shadow-xs"
            >
              <div className="flex min-w-0 items-start gap-3">
                <PlatformMark platform={item.platform} />
                <div className="grid min-w-0 gap-0.5">
                  <span className="truncate font-semibold" data-testid="meeting-row-title">
                    {meetingTitle(item.title, item.platform, sortTime(item))}
                  </span>
                  <span className="truncate text-xs text-muted-foreground">
                    {[PLATFORM_NAME[item.platform], item.team?.name, item.started_at ? `started ${shortTime(item.started_at)}` : null]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={item.status} />
                <SharedTag item={item} />
              </div>
              <Button asChild variant="outline" size="lg" className="mt-auto h-10 w-full sm:w-fit">
                <Link href={`/meetings/${item.id}`}>Open meeting</Link>
              </Button>
            </li>
          ))}
        </ul>
      )}
      <LoadMore section="live" more={more} />
    </section>
  );
}

function UpcomingSection({
  page,
  more,
  onSkipped,
}: {
  page: MeetingPage;
  more: More;
  onSkipped: (id: string, skipped: boolean) => void;
}) {
  return (
    <section aria-labelledby="upcoming-heading" data-testid="section-upcoming">
      <SectionHeading id="upcoming-heading" section="upcoming" count={`${page.items.length}${plus(page)} scheduled`} />
      {page.items.length === 0 ? (
        <EmptyLine section="upcoming" />
      ) : (
        <ul className="divide-y divide-border rounded-2xl border border-border bg-card">
          {page.items.map((item) => (
            <UpcomingRow key={item.id} item={item} onSkipped={(skipped) => onSkipped(item.id, skipped)} />
          ))}
        </ul>
      )}
      <LoadMore section="upcoming" more={more} />
    </section>
  );
}

function UpcomingRow({ item, onSkipped }: { item: MeetingItem; onSkipped: (skipped: boolean) => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = item.scheduled_start ?? sortTime(item);
  const title = meetingTitle(item.title, item.platform, start);
  const canSkip = item.status === "scheduled" && item.permissions?.can_share === true;

  async function toggle(skip: boolean) {
    setPending(true);
    setError(null);
    onSkipped(skip); // shown at once; put back if andai-core refuses
    try {
      onSkipped((await setSkipped(item.id, skip)).skipped);
    } catch (err) {
      onSkipped(!skip);
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <li
      data-testid="upcoming-row"
      data-meeting-id={item.id}
      data-skipped={item.skipped ? "true" : "false"}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
    >
      <span className={cn("w-full text-sm font-medium tabular-nums sm:w-52", item.skipped && "text-muted-foreground")} data-testid="upcoming-time">
        {start && <time dateTime={start}>{zonedWhenText(start)}</time>}
      </span>
      <PlatformMark platform={item.platform} className={cn(item.skipped && "opacity-50")} />
      <div className="grid min-w-0 flex-1 gap-0.5">
        <Link
          href={`/meetings/${item.id}`}
          className={cn("font-semibold break-words hover:text-primary hover:underline", item.skipped && "text-muted-foreground")}
          data-testid="meeting-row-title"
        >
          {title}
        </Link>
        <span className="truncate text-xs text-muted-foreground">
          {[PLATFORM_NAME[item.platform], item.team?.name].filter(Boolean).join(" · ")}
        </span>
      </div>
      <div className="flex w-full flex-wrap items-center justify-between gap-x-4 gap-y-2 sm:w-auto">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill status={item.status} />
          {item.skipped && (
            <span className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-muted-foreground" data-testid="skipped-tag">
              Skipped · the bot will not join
            </span>
          )}
          <SharedTag item={item} />
        </div>
        {canSkip && (
          <div className="flex items-center gap-2">
            <Switch
              id={`skip-${item.id}`}
              checked={item.skipped}
              disabled={pending}
              aria-busy={pending || undefined}
              aria-label={`Skip ${title}`}
              onCheckedChange={(checked) => void toggle(checked)}
              data-testid="skip-switch"
            />
            <Label htmlFor={`skip-${item.id}`} className="font-normal">
              Skip
            </Label>
          </div>
        )}
      </div>
      {error && (
        <p role="alert" className="w-full text-xs text-destructive" data-testid="skip-error">
          {error}
        </p>
      )}
    </li>
  );
}

function minutesText(item: MeetingItem): string {
  if (item.has_minutes) return "Minutes ready";
  if (item.status === "done") return "No minutes yet";
  return "—";
}

const PAST_COLUMNS = "md:grid-cols-[minmax(0,2.4fr)_minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,1.6fr)_minmax(0,1fr)]";

function PastSection({ page, more }: { page: MeetingPage; more: More }) {
  return (
    <section aria-labelledby="past-heading" data-testid="section-past">
      <SectionHeading
        id="past-heading"
        section="past"
        count={page.next_cursor ? `Showing ${page.items.length} · more below` : `Showing ${page.items.length}`}
      />
      {page.items.length === 0 ? (
        <EmptyLine section="past" />
      ) : (
        <div role="table" aria-label="Past meetings" className="rounded-2xl border border-border bg-card">
          <div
            role="row"
            className={cn("hidden gap-4 border-b border-border px-4 py-3 text-xs font-medium text-muted-foreground md:grid", PAST_COLUMNS)}
          >
            <span role="columnheader">Meeting</span>
            <span role="columnheader">When</span>
            <span role="columnheader">Team</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">Minutes</span>
          </div>
          <div role="rowgroup" className="divide-y divide-border">
            {page.items.map((item) => (
              <PastRow key={item.id} item={item} />
            ))}
          </div>
        </div>
      )}
      <LoadMore section="past" more={more} />
    </section>
  );
}

function PastRow({ item }: { item: MeetingItem }) {
  const failed = item.status === "failed";
  const duration = durationText(item.duration_s);
  return (
    <div
      role="row"
      data-testid="past-row"
      data-meeting-id={item.id}
      className={cn("grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 px-4 py-3 text-sm md:gap-4 md:items-start", PAST_COLUMNS)}
    >
      <div role="cell" className="col-span-2 flex min-w-0 items-start gap-3 md:col-span-1">
        <PlatformMark platform={item.platform} />
        <div className="grid min-w-0 gap-0.5">
          <Link href={`/meetings/${item.id}`} className="font-semibold break-words hover:text-primary hover:underline" data-testid="meeting-row-title">
            {meetingTitle(item.title, item.platform, sortTime(item))}
          </Link>
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span>
              {PLATFORM_NAME[item.platform]} · {item.requested_by_name}
            </span>
            <SharedTag item={item} />
          </span>
        </div>
      </div>
      <Cell label="When">
        <span className="tabular-nums">{whenText(sortTime(item))}</span>
        {duration && <span className="block text-xs text-muted-foreground">{duration}</span>}
      </Cell>
      <Cell label="Team">
        <span data-testid="meeting-row-team">{item.team?.name ?? "—"}</span>
      </Cell>
      <Cell label="Status">
        <StatusPill status={item.status} />
        {failed && (
          <span className="mt-1 block text-xs text-muted-foreground" data-testid="meeting-row-reason">
            {failureCopy(item.failure_reason).title}
          </span>
        )}
      </Cell>
      <Cell label="Minutes">
        <span data-testid="meeting-row-minutes" className={cn(item.has_minutes ? "font-medium text-success" : "text-muted-foreground")}>
          {minutesText(item)}
        </span>
      </Cell>
    </div>
  );
}

/** A table cell; under `md` it is a labelled line of a stacked row. */
function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="cell" className="col-span-2 grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-2 md:col-span-1 md:block">
      <span className="text-xs font-medium text-muted-foreground md:sr-only">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function EmptyLine({ section }: { section: Section }) {
  return (
    <p
      data-testid={`${section}-empty`}
      className="rounded-2xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground"
    >
      {SECTION_COPY[section].empty}
    </p>
  );
}

type More = { cursor: string | null; loading: boolean; error: string | null; onLoadMore: () => void };

/** "Load more" under a section that has a next page. */
function LoadMore({ section, more }: { section: Section; more: More }) {
  if (!more.cursor) return null;
  return (
    <div className="mt-3 grid justify-items-start gap-2">
      <Button
        type="button"
        variant="outline"
        size="lg"
        className="h-10 px-4"
        onClick={more.onLoadMore}
        disabled={more.loading}
        aria-busy={more.loading || undefined}
        data-testid={`load-more-${section}`}
      >
        {more.loading ? "Loading…" : "Load more"}
      </Button>
      {more.error && <FormAlert>{more.error}</FormAlert>}
    </div>
  );
}
