"use client";

import { Download, FileArchive } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "@/components/app/page-header";
import { FormAlert } from "@/components/forms/form-alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, sentence } from "@/lib/api";
import {
  EXPORT_POLL_MS,
  dayText,
  downloadHref,
  failureText,
  isOpen,
  listMyExports,
  requestMyExport,
  sizeText,
  timeText,
  type DataExport,
  type ExportState,
} from "@/lib/exports";
import { useSession } from "@/lib/session";

// "Your data": what a copy of the person's data holds, a button to ask for one (off while one is
// being made), and their copies with state, expiry and a Download link (a plain link, so the browser
// streams the zip to disk). The list is read again every few seconds while a copy is being made.

const BADGE: Record<ExportState, { label: string; variant: "secondary" | "outline" | "destructive"; className?: string }> = {
  pending: { label: "Being prepared", variant: "secondary" },
  running: { label: "Being prepared", variant: "secondary" },
  ready: { label: "Ready", variant: "outline", className: "border-success/30 text-success" },
  failed: { label: "Failed", variant: "destructive" },
  expired: { label: "Expired", variant: "outline", className: "text-muted-foreground" },
};

export function ExportStateBadge({ state }: { state: ExportState }) {
  const badge = BADGE[state];
  return (
    <Badge variant={badge.variant} className={badge.className} data-testid="export-state">
      {badge.label}
    </Badge>
  );
}

/** The line under a copy: when it was asked for, and until when it can be downloaded or why not. */
export function ExportDetail({ item, mine }: { item: DataExport; mine?: boolean }) {
  const asked = `Asked ${timeText(item.createdAt)}${mine && item.source === "org_admin" ? " by an admin" : ""}`;
  let status: string | null = null;
  if (item.state === "ready" && item.expiresAt) {
    const size = sizeText(item.bytes);
    status = `Available until ${dayText(item.expiresAt)}${size ? ` · ${size}` : ""}`;
  } else if (item.state === "failed") {
    status = failureText(item.error);
  } else if (item.state === "expired") {
    status = "Deleted; ask for a new copy";
  }
  return (
    <span className="grid gap-0.5 text-xs text-muted-foreground">
      <span>{asked}</span>
      {status && <span data-testid="export-status">{status}</span>}
    </span>
  );
}

export function DownloadLink({ item }: { item: DataExport }) {
  if (item.state !== "ready" || !item.canDownload) return null;
  return (
    <a
      href={downloadHref(item.id)}
      download
      data-testid="export-download"
      className={buttonVariants({ variant: "outline", size: "sm", className: "h-8" })}
    >
      <Download aria-hidden="true" />
      Download
    </a>
  );
}

function useMyExports() {
  const [items, setItems] = useState<DataExport[] | null>(null);

  const reload = useCallback(async () => {
    setItems((await listMyExports()).items);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const open = items?.some(isOpen) ?? false;
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => void reload(), EXPORT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, reload]);

  return { items, reload };
}

export function DataExportPage() {
  const { me } = useSession();
  const { items, reload } = useMyExports();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = items?.some(isOpen) ?? false;

  async function ask() {
    setPending(true);
    setError(null);
    try {
      await requestMyExport();
      await reload();
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      setError(sentence(err.detail));
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <PageHeader title="Your data" description={`A copy of what munshi.ai holds about you in ${me.org.name}`} />
      <div className="grid gap-6">
        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <FileArchive aria-hidden="true" className="size-4 text-muted-foreground" />
              Download a copy of your data
            </CardTitle>
            <CardDescription>
              A zip file of machine-readable JSON and your recordings. It takes a few minutes to prepare, and stays
              available for a few days: the date shows once it is ready.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-5">
            <div className="grid gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-lg border border-border p-3">
                <p className="mb-1.5 font-medium">What it contains</p>
                <ul className="grid list-disc gap-1 pl-4 text-muted-foreground">
                  <li>Your account and your membership of {me.org.name}</li>
                  <li>Every meeting you sent the notetaker to: transcript, all versions of the minutes, who it was shared with, and how the room was told</li>
                  <li>The recordings of those meetings</li>
                </ul>
              </div>
              <div className="rounded-lg border border-border p-3">
                <p className="mb-1.5 font-medium">What it leaves out</p>
                <ul className="grid list-disc gap-1 pl-4 text-muted-foreground">
                  <li>Meetings other people own, even if they were shared with you</li>
                  <li>Other people&rsquo;s email addresses; people appear by name</li>
                  <li>Passwords, two-factor secrets and other keys</li>
                </ul>
              </div>
            </div>
            {error && <FormAlert>{error}</FormAlert>}
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={() => void ask()} disabled={pending || open || items === null} aria-busy={pending} className="h-9 px-4">
                Request a copy
              </Button>
              {open && (
                <span role="status" className="text-sm text-muted-foreground">
                  Your copy is being prepared. This page updates on its own.
                </span>
              )}
            </div>
          </CardContent>
        </Card>

        <section aria-labelledby="my-exports-title">
          <h2 id="my-exports-title" className="mb-3 text-sm font-semibold">
            Your copies
          </h2>
          {items === null ? (
            <p role="status" className="text-sm text-muted-foreground">
              Loading…
            </p>
          ) : items.length === 0 ? (
            <p className="text-sm text-muted-foreground">You haven&rsquo;t asked for a copy yet.</p>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
              {items.map((item) => (
                <li
                  key={item.id}
                  data-testid={`export-${item.id}`}
                  data-state={item.state}
                  className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3"
                >
                  <ExportDetail item={item} mine />
                  <span className="flex items-center gap-2">
                    <ExportStateBadge state={item.state} />
                    <DownloadLink item={item} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
