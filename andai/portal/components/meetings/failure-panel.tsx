import { CircleAlert } from "lucide-react";

import { failureCopy } from "@/components/meetings/display";

// Why the bot could not join (or stay), in plain words for the meeting's JoinFailureReason.

export function FailurePanel({ reason }: { reason: string | null }) {
  const copy = failureCopy(reason);
  return (
    <section
      aria-labelledby="failure-title"
      data-testid="failure-panel"
      data-reason={reason ?? ""}
      className="flex gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4"
    >
      <CircleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-destructive" />
      <div className="grid min-w-0 gap-1">
        <h2 id="failure-title" className="text-sm font-semibold text-destructive">
          {copy.title}
        </h2>
        <p className="text-sm text-foreground">{copy.text}</p>
      </div>
    </section>
  );
}
