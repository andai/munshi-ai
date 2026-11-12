"use client";

import { ScrollText } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { errorText } from "@/components/admin/shared";
import { PageHeader } from "@/components/app/page-header";
import { FormAlert } from "@/components/forms/form-alert";
import { zonedWhenText } from "@/components/meetings/display";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  listAudit,
  listAuditActions,
  type AuditAction,
  type AuditEvent,
  type AuditFilters,
  type AuditPage,
} from "@/lib/admin";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

// Audit log (owners and admins; a 403 on load shows /403): who did what in this organisation,
// newest first, 50 at a time with "Load more" (andai-core's keyset cursor). The filters (from and to
// as whole UTC days, one action) live in the URL query, so a filtered view can be reloaded or
// shared with another admin. andai-core returns only this organisation's events; rows are never
// changed or removed.

const ALL = "all";
const FILTER_KEYS = ["from", "to", "action"] as const;

const CATEGORY_LABEL: Record<string, string> = {
  auth: "Sign-in and security",
  members: "Members and teams",
  settings: "Settings",
  sharing: "Sharing",
  meetings: "Meetings",
  minutes: "Minutes",
  platform: "Organisation",
  erasure: "Data erasure",
};

const TARGET_LABEL: Record<string, string> = {
  user: "Person",
  team: "Team",
  meeting: "Meeting",
  org: "Organisation",
  invite: "Invite",
};

const COLUMNS = "md:grid-cols-[minmax(0,1.1fr)_minmax(0,1.2fr)_minmax(0,1.5fr)_minmax(0,1.6fr)_minmax(0,0.8fr)]";

function actorText(event: AuditEvent): { text: string; muted: boolean } {
  if (event.actor.id === null) {
    return { text: event.action === "login.failure" ? "Not signed in" : "munshi.ai", muted: true };
  }
  return event.actor.name ? { text: event.actor.name, muted: false } : { text: "Unknown user", muted: true };
}

function targetText(event: AuditEvent): { kind: string; text: string; named: boolean } {
  const { type, id, label } = event.target;
  const kind = TARGET_LABEL[type] ?? (type || "Item");
  if (label) return { kind, text: label, named: true };
  return { kind, text: type ? `${type}:${id.slice(0, 8)}` : id.slice(0, 16), named: false };
}

function groupByCategory(actions: AuditAction[]): [string, AuditAction[]][] {
  const groups = new Map<string, AuditAction[]>();
  for (const action of actions) groups.set(action.category, [...(groups.get(action.category) ?? []), action]);
  return [...groups.entries()];
}

export function AuditLog() {
  const { me } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const from = search.get("from") ?? "";
  const to = search.get("to") ?? "";
  const action = search.get("action") ?? "";
  const filters = useMemo<AuditFilters>(
    () => ({ from: from || undefined, to: to || undefined, action: action || undefined }),
    [from, to, action],
  );
  const filtered = Boolean(from || to || action);

  const [actions, setActions] = useState<AuditAction[] | null>(null);
  const [page, setPage] = useState<AuditPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listAuditActions({ signal: controller.signal })
      .then(setActions)
      .catch((err) => {
        if (!controller.signal.aborted) setActions([]);
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    setMoreError(null);
    listAudit(filters, { signal: controller.signal })
      .then(setPage)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, [filters]);

  const labels = useMemo(() => new Map((actions ?? []).map((a) => [a.key, a.label])), [actions]);

  const setFilter = useCallback(
    (name: (typeof FILTER_KEYS)[number], value: string) => {
      const params = new URLSearchParams(search.toString());
      if (value) params.set(name, value);
      else params.delete(name);
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [router, pathname, search],
  );

  const clearFilters = useCallback(() => router.replace(pathname, { scroll: false }), [router, pathname]);

  const loadMore = useCallback(async () => {
    if (!page?.next || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const next = await listAudit(filters, { cursor: page.next });
      setPage((before) => {
        if (!before) return before;
        const seen = new Set(before.items.map((item) => item.id));
        return { items: [...before.items, ...next.items.filter((item) => !seen.has(item.id))], next: next.next };
      });
    } catch (err) {
      setMoreError(errorText(err));
    } finally {
      setLoadingMore(false);
    }
  }, [page, loadingMore, filters]);

  return (
    <>
      <PageHeader title="Audit log" description={`Who did what in ${me.org.name}. Entries are never changed or removed.`} />
      <form
        className="mb-6 grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[10.5rem_10.5rem_minmax(0,18rem)_auto]"
        aria-label="Filter the audit log"
        onSubmit={(event) => event.preventDefault()}
      >
        <Field id="audit-from" label="From">
          <Input
            id="audit-from"
            type="date"
            value={from}
            max={to || undefined}
            onChange={(event) => setFilter("from", event.target.value)}
            data-testid="audit-from"
            className="h-9"
          />
        </Field>
        <Field id="audit-to" label="To">
          <Input
            id="audit-to"
            type="date"
            value={to}
            min={from || undefined}
            onChange={(event) => setFilter("to", event.target.value)}
            data-testid="audit-to"
            className="h-9"
          />
        </Field>
        <Field id="audit-action" label="Action">
          <Select value={action || ALL} onValueChange={(value) => setFilter("action", value === ALL ? "" : value)}>
            <SelectTrigger id="audit-action" className="w-full data-[size=default]:h-9" data-testid="audit-action">
              <SelectValue placeholder="All actions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All actions</SelectItem>
              {groupByCategory(actions ?? []).map(([category, items]) => (
                <SelectGroup key={category}>
                  <SelectLabel>{CATEGORY_LABEL[category] ?? category}</SelectLabel>
                  {items.map((item) => (
                    <SelectItem key={item.key} value={item.key}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <div className="flex items-center gap-3 sm:col-span-2 lg:col-span-1">
          {filtered && (
            <Button type="button" variant="ghost" className="h-9" onClick={clearFilters} data-testid="audit-clear">
              Clear filters
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-4">
          Dates are whole days in UTC; without dates, the last 30 days are shown. Times below are in your time zone.
        </p>
      </form>

      {error ? (
        <FormAlert>{error}</FormAlert>
      ) : !page ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading the audit log…
        </p>
      ) : page.items.length === 0 ? (
        <section
          data-testid="audit-empty"
          className="flex flex-col items-center rounded-2xl border border-dashed border-border bg-card/60 px-6 py-14 text-center"
        >
          <ScrollText aria-hidden="true" className="size-8 text-muted-foreground" />
          <h2 className="mt-4 text-base font-semibold">{filtered ? "No events match" : "No events yet"}</h2>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            {filtered
              ? "Nothing was recorded for these dates and this action. Try a wider range or another action."
              : "Sign-ins, invites, role changes, settings changes, shares and deletions are recorded here."}
          </p>
        </section>
      ) : (
        <div role="table" aria-label="Audit log" className="rounded-2xl border border-border bg-card" data-testid="audit-list">
          <div
            role="row"
            className={cn("hidden gap-4 border-b border-border px-4 py-3 text-xs font-medium text-muted-foreground md:grid", COLUMNS)}
          >
            <span role="columnheader">Time</span>
            <span role="columnheader">Who</span>
            <span role="columnheader">Action</span>
            <span role="columnheader">On</span>
            <span role="columnheader">IP address</span>
          </div>
          <div role="rowgroup" className="divide-y divide-border">
            {page.items.map((event) => (
              <AuditRow key={event.id} event={event} label={labels.get(event.action)} />
            ))}
          </div>
        </div>
      )}
      {page?.next && !error && (
        <div className="mt-3 grid justify-items-start gap-2">
          <Button
            type="button"
            variant="outline"
            size="lg"
            className="h-10 px-4"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            aria-busy={loadingMore || undefined}
            data-testid="load-more-audit"
          >
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
          {moreError && <FormAlert>{moreError}</FormAlert>}
        </div>
      )}
    </>
  );
}

function AuditRow({ event, label }: { event: AuditEvent; label: string | undefined }) {
  const actor = actorText(event);
  const target = targetText(event);
  return (
    <div
      role="row"
      data-testid="audit-row"
      data-action={event.action}
      data-event-id={event.id}
      className={cn("grid grid-cols-1 gap-x-4 gap-y-1.5 px-4 py-3 text-sm md:items-start", COLUMNS)}
    >
      <Cell label="Time">
        <time dateTime={event.at} className="tabular-nums">
          {zonedWhenText(event.at)}
        </time>
      </Cell>
      <Cell label="Who">
        <span className={cn("break-words", actor.muted && "text-muted-foreground")} data-testid="audit-actor">
          {actor.text}
        </span>
      </Cell>
      <Cell label="Action">
        <span className="block break-words" data-testid="audit-action-label">
          {label ?? event.action}
        </span>
        <span className="block font-mono text-xs text-muted-foreground">{event.action}</span>
      </Cell>
      <Cell label="On">
        <span className="block text-xs text-muted-foreground">{target.kind}</span>
        <span
          className={cn("block break-words", !target.named && "font-mono text-xs")}
          title={target.named ? undefined : `${event.target.type}:${event.target.id}`}
          data-testid="audit-target"
        >
          {target.text}
        </span>
      </Cell>
      <Cell label="IP">
        <span className={cn("break-all tabular-nums", !event.ip && "text-muted-foreground")}>{event.ip ?? "—"}</span>
      </Cell>
    </div>
  );
}

/** A labelled filter control. */
function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  );
}

/** A table cell; under `md` it is a labelled line of a stacked row. */
function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="cell" className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-2 md:block">
      <span className="text-xs font-medium text-muted-foreground md:sr-only">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
