import Link from "next/link";

import { Button } from "@/components/ui/button";

// The meeting page's answer for a meeting that does not exist for this account: never created, in
// another organisation, deleted, or not (or no longer) shared with them. andai-core answers all of
// these with the same 404, so the page says the same thing for each.

export default function MeetingNotFound() {
  return (
    <section data-testid="meeting-not-found" className="mx-auto grid max-w-md justify-items-center gap-2 py-16 text-center">
      <p className="text-sm font-medium text-muted-foreground">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">Meeting not found</h1>
      <p className="text-sm text-muted-foreground">This meeting does not exist, or it is not shared with you.</p>
      <Button asChild variant="secondary" className="mt-4">
        <Link href="/">Back to meetings</Link>
      </Button>
    </section>
  );
}
