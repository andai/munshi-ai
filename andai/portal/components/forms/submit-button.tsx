import { LoaderCircle } from "lucide-react";
import type { ComponentProps } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** A full-width form submit button that shows progress and blocks double submits. */
export function SubmitButton({
  pending,
  children,
  className,
  disabled,
  ...props
}: ComponentProps<typeof Button> & { pending: boolean }) {
  return (
    <Button
      type="submit"
      size="lg"
      {...props}
      disabled={pending || disabled}
      aria-busy={pending}
      className={cn("h-10 w-full", className)}
    >
      {pending && <LoaderCircle className="animate-spin" aria-hidden="true" />}
      {children}
    </Button>
  );
}
