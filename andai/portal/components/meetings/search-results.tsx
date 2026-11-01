"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { PLATFORM_NAME, meetingTitle, offset, whenText } from "@/components/meetings/display";
import { PlatformMark } from "@/components/meetings/meeting-list";
import { Button } from "@/components/ui/button";
import { ApiError, sentence } from "@/lib/api";
import { searchMeetings, type Mark, type SearchHit, type SearchPage, type SearchResult } from "@/lib/meetings";

// Keyword search results for the dashboard: what andai-core's `GET /search` returns for the query,
// as it returns it — the meetings the caller may open, best first, the total it counts, and up to
// three snippets per meeting with the matched words marked. A transcript snippet shows the line as
// it was said (its original script) and, when that is not Roman, the Roman Hinglish form search
// matched. The title links to the meeting at the first cited line (`#seg-<id>`).
//
// The dashboard passes the query after the reader stops typing (250 ms); a newer query cancels the
// request for an older one, so a slow answer never replaces a newer one.

export const MIN_QUERY = 2;
const EMPTY_TEXT = "Search covers titles, transcripts and minutes, in Roman or Devanagari script.";

function errorText(err: unknown): string {
  return err instanceof ApiError ? sentence(err.detail) : "Something went wrong. Try again.";
}

/** Whether a query has enough letters or digits to send (andai-core decides finally). */
export function searchable(query: string): boolean {
  return (query.match(/[\p{L}\p{N}]/gu)?.length ?? 0) >= MIN_QUERY;
}

/** `text` with the `[start, end)` character ranges in `marks` wrapped in <mark>. */
export function highlighted(text: string, marks: Mark[]): ReactNode[] {
  // andai-core counts characters as code points; index the same way.
  const chars = Array.from(text);
  const parts: ReactNode[] = [];
  let at = 0;
  for (const [start, end] of [...marks].sort((a, b) => a[0] - b[0])) {
    if (start < at || end <= start || end > chars.length) continue;
    if (start > at) parts.push(chars.slice(at, start).join(""));
    parts.push(
      <mark key={start} className="rounded-sm bg-saffron/30 px-0.5 text-foreground dark:bg-saffron/25">
        {chars.slice(start, end).join("")}
      </mark>,
    );
    at = end;
  }
  if (at < chars.length) parts.push(chars.slice(at).join(""));
  return parts;
}

function meetingHref(result: SearchResult): string {
  const cited = result.hits.find((hit) => hit.source === "transcript");
  const base = `/meetings/${result.id}`;
  return cited && cited.source === "transcript" ? `${base}#seg-${encodeURIComponent(cited.segment_id)}` : base;
}

export function SearchResults({ query }: { query: string }) {
  const [page, setPage] = useState<SearchPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setPage(null);
    setError(null);
    setLoadingMore(false);
    if (!searchable(query)) return;
    const controller = new AbortController();
    searchMeetings(query, { signal: controller.signal })
      .then((answer) => {
        if (current === generation.current) setPage(answer);
      })
      .catch((err) => {
        if (controller.signal.aborted || current !== generation.current) return;
        setError(errorText(err));
      });
    return () => controller.abort();
  }, [query]);

  async function loadMore() {
    if (!page?.next_cursor || loadingMore) return;
    const current = generation.current;
    setLoadingMore(true);
    try {
      const next = await searchMeetings(query, { cursor: page.next_cursor });
      if (current !== generation.current) return;
      setPage((before) => (before ? { ...next, results: [...before.results, ...next.results] } : before));
    } catch (err) {
      if (current === generation.current) setError(errorText(err));
    } finally {
      if (current === generation.current) setLoadingMore(false);
    }
  }

  let body: ReactNode;
  if (!searchable(query)) {
    body = <p className="text-sm text-muted-foreground">Type at least two letters or digits. {EMPTY_TEXT}</p>;
  } else if (error && !page) {
    body = <FormAlert>{error}</FormAlert>;
  } else if (!page) {
    body = (
      <p className="text-sm text-muted-foreground" aria-busy="true" data-testid="search-loading">
        Searching…
      </p>
    );
  } else if (page.results.length === 0) {
    body = (
      <p
        data-testid="search-empty"
        className="rounded-2xl border border-dashed border-border bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground"
      >
        Nothing matches. {EMPTY_TEXT}
      </p>
    );
  } else {
    body = (
      <>
        <ul className="grid gap-3">
          {page.results.map((result) => (
            <ResultCard key={result.id} result={result} />
          ))}
        </ul>
        {page.next_cursor && (
          <div className="mt-3">
            <Button
              type="button"
              variant="outline"
              size="lg"
              className="h-10 px-4"
              onClick={() => void loadMore()}
              disabled={loadingMore}
              data-testid="load-more-search"
            >
              {loadingMore ? "Loading…" : "Load more"}
            </Button>
          </div>
        )}
        {error && (
          <div className="mt-3">
            <FormAlert>{error}</FormAlert>
          </div>
        )}
      </>
    );
  }

  return (
    <section aria-labelledby="search-heading" aria-live="polite" data-testid="search-results" className="mt-6">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id="search-heading" className="text-lg font-semibold tracking-tight">
          Search results
        </h2>
        {page && (
          <span className="text-sm font-medium text-muted-foreground" data-testid="search-count">
            {page.total} {page.total === 1 ? "meeting" : "meetings"} · only meetings you can open
          </span>
        )}
      </div>
      {body}
    </section>
  );
}

function ResultCard({ result }: { result: SearchResult }) {
  const when = whenText(result.started_at ?? result.created_at);
  return (
    <li data-testid="search-result" data-meeting-id={result.id} className="grid gap-3 rounded-2xl border border-border bg-card p-4 shadow-xs">
      <div className="flex min-w-0 items-start gap-3">
        <PlatformMark platform={result.platform} />
        <div className="grid min-w-0 gap-0.5">
          <Link href={meetingHref(result)} className="font-semibold break-words hover:text-primary hover:underline" data-testid="search-result-title">
            {meetingTitle(result.title, result.platform, result.started_at ?? result.created_at)}
          </Link>
          <span className="text-xs text-muted-foreground">
            {[PLATFORM_NAME[result.platform], result.team?.name, when].filter(Boolean).join(" · ")}
          </span>
        </div>
      </div>
      {result.hits.length > 0 && (
        <ul className="grid gap-2 border-l-2 border-saffron/60 pl-3">
          {result.hits.map((hit, index) => (
            <li key={index} data-testid="search-snippet" data-source={hit.source} className="grid min-w-0 gap-0.5 text-sm">
              <Snippet hit={hit} meetingId={result.id} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Snippet({ hit, meetingId }: { hit: SearchHit; meetingId: string }) {
  if (hit.source !== "transcript") {
    return (
      <>
        <span className="text-xs font-medium text-muted-foreground">{hit.source === "title" ? "Title" : "Minutes"}</span>
        <span className="break-words" data-testid="snippet-text">
          {highlighted(hit.text, hit.marks)}
        </span>
      </>
    );
  }
  const roman = hit.text_raw !== hit.text;
  return (
    <>
      <span className="text-xs font-medium text-muted-foreground">
        Transcript · {hit.speaker} ·{" "}
        <Link href={`/meetings/${meetingId}#seg-${encodeURIComponent(hit.segment_id)}`} className="tabular-nums hover:text-primary hover:underline">
          {offset(hit.start)}
        </Link>
      </span>
      <span className="break-words" data-testid="snippet-raw">
        {highlighted(hit.text_raw, hit.raw_marks)}
      </span>
      {roman && (
        <span className="break-words text-xs text-muted-foreground" data-testid="snippet-roman">
          Roman: {highlighted(hit.text, hit.marks)}
        </span>
      )}
    </>
  );
}
