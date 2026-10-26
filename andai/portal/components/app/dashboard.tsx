"use client";

import { LogoMark } from "@/components/brand/logo-mark";
import { PageHeader } from "@/components/app/page-header";
import { displayName, useSession } from "@/lib/session";

export function Dashboard() {
  const { me } = useSession();
  return (
    <>
      <PageHeader title="Meetings" description={`Welcome, ${displayName(me.user)} · ${me.org.name}`} />
      <section
        aria-labelledby="empty-title"
        data-testid="dashboard-empty"
        className="flex flex-col items-center rounded-2xl border border-dashed border-border bg-card/60 px-6 py-16 text-center"
      >
        <LogoMark size={44} className="opacity-90" />
        <h2 id="empty-title" className="mt-5 text-lg font-semibold">
          No meetings yet
        </h2>
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          Meetings arrive in a later release. Your notes, transcripts and minutes will appear here.
        </p>
      </section>
    </>
  );
}
