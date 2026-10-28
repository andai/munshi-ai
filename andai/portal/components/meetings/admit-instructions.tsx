import { DoorOpen } from "lucide-react";

// How to let the bot in, while it waits in the lobby. The words are andai-core's, per platform.

export function AdmitInstructions({ text }: { text: string }) {
  return (
    <section
      aria-labelledby="admit-title"
      data-testid="admit-instructions"
      className="relative flex gap-3 overflow-hidden rounded-xl border border-border bg-secondary/60 p-4 pl-5"
    >
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-1 bg-saffron" />
      <DoorOpen aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-primary" />
      <div className="grid min-w-0 gap-1">
        <h2 id="admit-title" className="text-sm font-semibold">
          Let the bot in
        </h2>
        <p className="text-sm [overflow-wrap:anywhere] text-foreground">{text}</p>
      </div>
    </section>
  );
}
