import type { Metadata } from "next";
import { Suspense } from "react";

import { ResetPasswordForm } from "@/components/auth/password-forms";

export const metadata: Metadata = { title: "Choose a new password", referrer: "no-referrer" };

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetPasswordForm />
    </Suspense>
  );
}
