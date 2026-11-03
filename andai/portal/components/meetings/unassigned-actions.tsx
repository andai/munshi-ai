"use client";

import { CalendarDays } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { dueText, meetingTitle, whenText } from "@/components/meetings/display";
import { PlatformMark } from "@/components/meetings/meeting-list";
import { segmentAnchor } from "@/components/meetings/transcript-view";
import { Button } from "@/components/ui/button";
import { ApiError, sentence } from "@/lib/api";
import { listUnassigned, type UnassignedItem, type UnassignedPage } from "@/lib/meetings";

// The dashboard's "Unassigned action items" (D-42): the action items nobody owns yet in the latest
// minutes of the meetings the caller may open, newest meeting first, as andai-core lists them. Each
// item links to its meeting at the line it cites. Nothing shows while the first page loads or when
// there are none, so the list does not jump; "Load more" appends the next page by cursor.

function errorText(err: unknown): string {
  return err instanceof ApiError ? sentence(err.detail) : "Something went wrong. Try again.";
}

function itemHref(item: UnassignedItem): string {
  const meeting = `/meetings/${encodeURIComponent(item.meeting_id)}`;
  return item.evidence_first_id ? `${meeting}#${segmentAnchor(encodeURIComponent(item.evidence_first_id))}` : meeting;
}

export function UnassignedActions() {
  const [page, setPage] = useState<UnassignedPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    listUnassigned({ signal: controller.signal })
      .then(setPage)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, []);

  async function loadMore() {
    if (!page?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const next = await listUnassigned({ cursor: page.next_cursor });
      if (!mounted.current) return;
      setPage((before) => (before ? { items: [...before.items, ...next.items], next_cursor: next.next_cursor } : next));
    } catch (err) {
      if (mounted.current) setMoreError(errorText(err));
    } finally {
      if (mounted.current) setLoadingMore(false);
    }
  }

  if (error) {
    return (
      <section aria-labelledby="unassigned-heading" data-testid="section-unassigned">
        <Heading count={null} />
        <div data-testid="unassigned-error">
          <FormAlert>{error}</FormAlert>
        </div>
      </section>
    );
  }

  if (!page || page.items.length === 0) return null;

  const count = `${page.items.length}${page.next_cursor ? "+" : ""} to assign`;
  return (
    <section aria-labelledby="unassigned-heading" data-testid="section-unassigned">
      <Heading count={count} />
      <ul className="divide-y divide-border rounded-2xl border border-border bg-card">
        {page.items.map((item) => (
          <Row key={`${item.meeting_id}:${item.item_id}`} item={item} />
        ))}
      </ul>
      {page.next_cursor && (
        <div className="mt-3 grid justify-items-start gap-2">
          <Button
            type="button"
            variant="outline"
            size="lg"
            className="h-10 px-4"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            aria-busy={loadingMore || undefined}
            data-testid="load-more-unassigned"
          >
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
          {moreError && <FormAlert>{moreError}</FormAlert>}
        </div>
      )}
    </section>
  );
}

function Heading({ count }: { count: string | null }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h2 id="unassigned-heading" className="text-lg font-semibold tracking-tight">
        Unassigned action items
      </h2>
      {count && (
        <span className="text-sm font-medium text-muted-foreground" data-testid="unassigned-count">
          {count}
        </span>
      )}
    </div>
  );
}

function Row({ item }: { item: UnassignedItem }) {
  const at = item.started_at ?? item.created_at;
  const due = dueText(item.due ?? undefined);
  return (
    <li
      data-testid="unassigned-row"
      data-meeting-id={item.meeting_id}
      data-item-id={item.item_id}
      className="flex min-w-0 items-start gap-3 px-4 py-3"
    >
      <PlatformMark platform={item.platform} />
      <div className="grid min-w-0 flex-1 gap-1">
        <Link href={itemHref(item)} className="font-medium [overflow-wrap:anywhere] hover:text-primary hover:underline" data-testid="unassigned-text">
          {item.text}
        </Link>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span className="min-w-0 [overflow-wrap:anywhere]" data-testid="unassigned-meeting">
            {[meetingTitle(item.title, item.platform, at), whenText(at), item.team?.name].filter(Boolean).join(" · ")}
          </span>
          {due && (
            <span className="inline-flex items-center gap-1.5 font-medium text-foreground" data-testid="unassigned-due">
              <CalendarDays aria-hidden="true" className="size-3.5" />
              Due {due}
            </span>
          )}
        </span>
      </div>
    </li>
  );
}
