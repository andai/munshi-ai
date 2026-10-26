import type { Metadata } from "next";

import { AuthCard } from "@/components/auth/auth-card";
import { TwoFactorForm } from "@/components/auth/two-factor-form";

export const metadata: Metadata = { title: "Two-factor login" };

export default function TwoFactorPage() {
  return (
    <AuthCard title="Two-factor login" description="Enter the code from your authenticator app to finish signing in.">
      <TwoFactorForm />
    </AuthCard>
  );
}
