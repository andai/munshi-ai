import type { ComponentProps, ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/** A labelled input with an optional hint; the hint is announced with the input. */
export function Field({
  id,
  label,
  hint,
  action,
  className,
  inputClassName,
  ...input
}: ComponentProps<"input"> & {
  id: string;
  label: string;
  hint?: ReactNode;
  action?: ReactNode;
  inputClassName?: string;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  return (
    <div className={cn("grid gap-1.5", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={id}>{label}</Label>
        {action}
      </div>
      <Input id={id} aria-describedby={hintId} className={cn("h-10 md:text-sm", inputClassName)} {...input} />
      {hint && (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}
