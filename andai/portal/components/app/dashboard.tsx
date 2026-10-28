"use client";

import { LogoMark } from "@/components/brand/logo-mark";
import { PageHeader } from "@/components/app/page-header";
import { JoinForm } from "@/components/meetings/join-form";
import { displayName, useSession } from "@/lib/session";

export function Dashboard() {
  const { me } = useSession();
  return (
    <>
      <PageHeader title="Meetings" description={`Welcome, ${displayName(me.user)} · ${me.org.name}`} />
      <JoinForm />
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
          Your list of meetings arrives in a later release. Send the bot above to follow a meeting live.
        </p>
      </section>
    </>
  );
}
