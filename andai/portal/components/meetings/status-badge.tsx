import { STATUS, clockTime, statusLabel, type Tone } from "@/components/meetings/display";
import type { MeetingStatus } from "@/lib/meetings";
import { cn } from "@/lib/utils";

// The meeting's status as a pill (a pulsing dot while it records) and when it last changed.

const TONE: Record<Tone, { pill: string; dot: string }> = {
  neutral: { pill: "bg-muted text-muted-foreground", dot: "bg-muted-foreground" },
  waiting: { pill: "bg-secondary text-secondary-foreground", dot: "bg-saffron" },
  live: { pill: "bg-primary text-primary-foreground", dot: "bg-saffron" },
  success: { pill: "bg-success/10 text-success", dot: "bg-success" },
  danger: { pill: "bg-destructive/10 text-destructive", dot: "bg-destructive" },
};

export function StatusBadge({ status, changedAt }: { status: MeetingStatus; changedAt: string | null }) {
  const tone = TONE[STATUS[status]?.tone ?? "neutral"];
  const time = clockTime(changedAt);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span
        role="status"
        aria-live="polite"
        data-testid="meeting-status"
        data-status={status}
        className={cn("inline-flex h-7 items-center gap-2 rounded-full px-3 text-sm font-medium", tone.pill)}
      >
        <span aria-hidden="true" className="relative flex size-2">
          {status === "recording" && (
            <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-75", tone.dot)} />
          )}
          <span className={cn("relative inline-flex size-2 rounded-full", tone.dot)} />
        </span>
        {statusLabel(status)}
      </span>
      {time && (
        <span className="text-xs text-muted-foreground" data-testid="meeting-status-time">
          since {time}
        </span>
      )}
    </div>
  );
}
