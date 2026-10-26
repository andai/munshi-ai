import type { Metadata } from "next";
import { Suspense } from "react";

import { InviteForm } from "@/components/auth/password-forms";

export const metadata: Metadata = { title: "Accept your invite", referrer: "no-referrer" };

export default function InvitePage() {
  return (
    <Suspense>
      <InviteForm />
    </Suspense>
  );
}
