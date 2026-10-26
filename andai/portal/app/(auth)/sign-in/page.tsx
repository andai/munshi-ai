import type { Metadata } from "next";
import { Suspense } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { SignInForm } from "@/components/auth/sign-in-form";

export const metadata: Metadata = { title: "Sign in" };

export default function SignInPage() {
  return (
    <AuthCard title="Sign in" description="Welcome back. Sign in with your work email.">
      <Suspense>
        <SignInForm />
      </Suspense>
    </AuthCard>
  );
}
