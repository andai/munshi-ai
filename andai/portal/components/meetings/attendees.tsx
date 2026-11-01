import { initials } from "@/components/meetings/display";
import type { Attendee } from "@/lib/meetings";

// Who attended, as the platform listed them when andai stored the meeting: calendar invitees first,
// then the people who spoke (D-43). Each person once, with their initials.

export function Attendees({ attendees }: { attendees: Attendee[] }) {
  return (
    <section aria-labelledby="attendees-title" className="grid gap-2" data-testid="attendees">
      <h2 id="attendees-title" className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        Attendees{attendees.length > 0 && ` · ${attendees.length}`}
      </h2>
      {attendees.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="attendees-empty">
          No attendees recorded.
        </p>
      ) : (
        <ul className="flex flex-wrap gap-2">
          {attendees.map((person) => (
            <li
              key={person.name}
              data-testid="attendee"
              data-source={person.source}
              className="inline-flex max-w-full min-w-0 items-center gap-2 rounded-full border border-border bg-card py-1 pr-3 pl-1 text-sm"
            >
              <span
                aria-hidden="true"
                className="flex size-6 shrink-0 items-center justify-center rounded-full bg-secondary text-[10px] font-bold text-secondary-foreground"
              >
                {initials(person.name)}
              </span>
              <span className="truncate">{person.name}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
