import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";

/** The card every sign-in screen sits in: a page heading, a short description, the form. */
export function AuthCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="gap-5 py-6 shadow-sm shadow-ink/5 [--card-spacing:--spacing(6)]">
      <CardHeader className="gap-1.5">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <CardDescription className="text-sm leading-relaxed">{description}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}
