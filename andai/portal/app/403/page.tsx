import type { Metadata } from "next";
import Link from "next/link";

import { Wordmark } from "@/components/brand/wordmark";
import { buttonVariants } from "@/components/ui/button";

export const metadata: Metadata = { title: "No access" };

// Where the portal sends a page load that andai-core answered with 403.
export default function ForbiddenPage() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-4 py-12 text-center">
      <Wordmark size="md" />
      <p className="mt-10 font-mono text-sm font-medium text-primary">403</p>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight">You don&apos;t have access to this page</h1>
      <p className="mt-2 max-w-md text-sm text-muted-foreground">
        This page is for your organisation&apos;s admins. If you need it, ask an admin to change your role.
      </p>
      <Link href="/" className={buttonVariants({ size: "lg", className: "mt-8 h-10 px-5" })}>
        Back to meetings
      </Link>
    </main>
  );
}
