"use client";

import { Search, X } from "lucide-react";
import { useEffect, useState } from "react";

import { PageHeader } from "@/components/app/page-header";
import { JoinForm } from "@/components/meetings/join-form";
import { MeetingFilters } from "@/components/meetings/meeting-filters";
import { MeetingList } from "@/components/meetings/meeting-list";
import { SearchResults } from "@/components/meetings/search-results";
import { UnassignedActions } from "@/components/meetings/unassigned-actions";
import { Input } from "@/components/ui/input";
import type { ListFilter } from "@/lib/meetings";
import { displayName, useSession } from "@/lib/session";

// The meetings dashboard: the search box in the header, "Add notetaker", then the list of the
// meetings the caller may open with its filters: Live now, Upcoming, the Unassigned action items
// and Past (D-42). The Unassigned items are not narrowed by the filters, so they show only under
// "All I can see". While the search box holds a query the list is swapped for the search results
// (kept mounted, so its pages and filter survive a search); the query is sent 250 ms after the last
// keystroke.

const DEBOUNCE_MS = 250;

export function Dashboard() {
  const { me } = useSession();
  const [filter, setFilter] = useState<ListFilter>("all");
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    const next = typed.trim();
    if (next === "") {
      setQuery("");
      return;
    }
    const timer = setTimeout(() => setQuery(next), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  const searching = typed.trim() !== "";

  return (
    <>
      <PageHeader
        title="Meetings"
        description={`Welcome, ${displayName(me.user)} · ${me.org.name}`}
        actions={<SearchBox value={typed} onChange={setTyped} />}
      />
      <JoinForm />
      {searching && query !== "" && <SearchResults query={query} />}
      {searching && query === "" && (
        <p className="mt-6 text-sm text-muted-foreground" aria-busy="true">
          Searching…
        </p>
      )}
      <div hidden={searching} className="mt-8" data-testid="dashboard-list">
        <MeetingFilters filter={filter} onChange={setFilter} />
        <MeetingList filter={filter} between={filter === "all" ? <UnassignedActions /> : null} />
      </div>
    </>
  );
}

function SearchBox({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div role="search" className="relative w-full sm:w-80">
      <label htmlFor="meeting-search" className="sr-only">
        Search meetings
      </label>
      <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        id="meeting-search"
        type="search"
        autoComplete="off"
        spellCheck={false}
        maxLength={200}
        placeholder="Search titles, transcripts, minutes"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onChange("");
        }}
        className="h-10 pr-9 pl-9 md:text-sm [&::-webkit-search-cancel-button]:hidden"
      />
      {value && (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => onChange("")}
          className="absolute top-1/2 right-1.5 inline-flex size-7 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      )}
    </div>
  );
}
