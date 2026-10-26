import { CircleAlert, CircleCheck } from "lucide-react";
import type { ReactNode } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";

/** An error or confirmation shown above or below a form; announced to screen readers. */
export function FormAlert({
  tone = "error",
  children,
}: {
  tone?: "error" | "success";
  children: ReactNode;
}) {
  if (tone === "success") {
    return (
      <Alert className="border-success/30 bg-success/5 text-foreground" data-testid="form-success">
        <CircleCheck className="text-success" aria-hidden="true" />
        <AlertDescription className="text-foreground">{children}</AlertDescription>
      </Alert>
    );
  }
  return (
    <Alert variant="destructive" className="border-destructive/30 bg-destructive/5" data-testid="form-error">
      <CircleAlert aria-hidden="true" />
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  );
}
