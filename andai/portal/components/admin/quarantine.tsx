"use client";

import { MailWarning } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { errorText } from "@/components/admin/shared";
import { PageHeader } from "@/components/app/page-header";
import { FormAlert } from "@/components/forms/form-alert";
import { zonedWhenText } from "@/components/meetings/display";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { listQuarantine, type QuarantineItem, type QuarantinePage, type QuarantineReason } from "@/lib/admin";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

// Mail intake (owners and admins; a 403 on load shows /403): the mail sent to the notetaker that
// andai-intake did not turn into a meeting for this organisation, newest first, 25 at a time with
// "Load more" (andai-core's keyset cursor). Each row says when it came, who it claims to be from,
// its subject, why it was held back in plain words, and what the sender check found. andai-core
// returns only this organisation's rows and never a message's body; rows go after 30 days.

export const REASON: Record<QuarantineReason, { label: string; text: string }> = {
  sender_unverified: {
    label: "Sender not verified",
    text: "The mail could not be proven to come from the address it names.",
  },
  unknown_user: {
    label: "Not a member",
    text: "The sender is not an active member of this organisation.",
  },
  no_meeting_link: {
    label: "No meeting link",
    text: "The invitation has no Google Meet, Microsoft Teams or Zoom link.",
  },
  parse_error: {
    label: "Not readable",
    text: "The mail holds no calendar invitation that could be read.",
  },
  rate_limited: {
    label: "Too many invitations",
    text: "More invitations came in an hour than allowed for this sender or organisation.",
  },
};

function reasonCopy(reason: string): { label: string; text: string } {
  return REASON[reason as QuarantineReason] ?? { label: reason, text: "" };
}

function checkText(item: QuarantineItem): string {
  if (item.check) return item.check;
  return item.senderAuth === "fail" ? "The sender could not be verified." : "The sender was verified.";
}

const COLUMNS = "md:grid-cols-[minmax(0,1.1fr)_minmax(0,1.4fr)_minmax(0,1.6fr)_minmax(0,1.9fr)]";

export function Quarantine() {
  const { me } = useSession();
  const [page, setPage] = useState<QuarantinePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listQuarantine({ signal: controller.signal })
      .then(setPage)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, []);

  const loadMore = useCallback(async () => {
    if (!page?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const next = await listQuarantine({ cursor: page.next_cursor });
      setPage((before) => (before ? { items: [...before.items, ...next.items], next_cursor: next.next_cursor } : before));
    } catch (err) {
      setMoreError(errorText(err));
    } finally {
      setLoadingMore(false);
    }
  }, [page, loadingMore]);

  const header = (
    <PageHeader
      title="Mail intake"
      description={`Invitations to the notetaker that did not schedule a meeting for ${me.org.name}, kept for 30 days`}
    />
  );

  if (error) {
    return (
      <>
        {header}
        <FormAlert>{error}</FormAlert>
      </>
    );
  }
  if (!page) {
    return (
      <>
        {header}
        <p role="status" className="text-sm text-muted-foreground">
          Loading quarantined mail…
        </p>
      </>
    );
  }

  return (
    <>
      {header}
      {page.items.length === 0 ? (
        <section
          data-testid="quarantine-empty"
          className="flex flex-col items-center rounded-2xl border border-dashed border-border bg-card/60 px-6 py-14 text-center"
        >
          <MailWarning aria-hidden="true" className="size-8 text-muted-foreground" />
          <h2 className="mt-4 text-base font-semibold">No quarantined mail</h2>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            Invitations the notetaker could not accept, for example from someone outside your organisation, are listed
            here.
          </p>
        </section>
      ) : (
        <div role="table" aria-label="Quarantined mail" className="rounded-2xl border border-border bg-card" data-testid="quarantine-list">
          <div
            role="row"
            className={cn("hidden gap-4 border-b border-border px-4 py-3 text-xs font-medium text-muted-foreground md:grid", COLUMNS)}
          >
            <span role="columnheader">Received</span>
            <span role="columnheader">From</span>
            <span role="columnheader">Subject</span>
            <span role="columnheader">Why it was held</span>
          </div>
          <div role="rowgroup" className="divide-y divide-border">
            {page.items.map((item, index) => (
              <QuarantineRow key={`${item.receivedAt}-${index}`} item={item} />
            ))}
          </div>
        </div>
      )}
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
            data-testid="load-more-quarantine"
          >
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
          {moreError && <FormAlert>{moreError}</FormAlert>}
        </div>
      )}
    </>
  );
}

function QuarantineRow({ item }: { item: QuarantineItem }) {
  const reason = reasonCopy(item.reason);
  return (
    <div
      role="row"
      data-testid="quarantine-row"
      data-reason={item.reason}
      className={cn("grid grid-cols-1 gap-x-4 gap-y-1.5 px-4 py-3 text-sm md:items-start", COLUMNS)}
    >
      <Cell label="Received">
        <span className="tabular-nums">{zonedWhenText(item.receivedAt)}</span>
      </Cell>
      <Cell label="From">
        <span className="break-all" data-testid="quarantine-sender">
          {item.sender ?? "No single sender address"}
        </span>
      </Cell>
      <Cell label="Subject">
        <span className="break-words" data-testid="quarantine-subject">
          {item.subject?.trim() || "(no subject)"}
        </span>
      </Cell>
      <Cell label="Why">
        <Badge
          variant="outline"
          className={cn(item.reason === "sender_unverified" ? "border-destructive/30 text-destructive" : "text-foreground")}
          data-testid="quarantine-reason"
        >
          {reason.label}
        </Badge>
        {reason.text && <span className="mt-1 block text-xs text-muted-foreground">{reason.text}</span>}
        <span className="mt-1 block text-xs text-muted-foreground" data-testid="quarantine-check">
          {checkText(item)}
        </span>
      </Cell>
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
