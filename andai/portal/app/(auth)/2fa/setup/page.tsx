import type { Metadata } from "next";
import { Suspense } from "react";

import { TwoFactorSetup } from "@/components/auth/two-factor-setup";

export const metadata: Metadata = { title: "Set up two-factor login" };

export default function TwoFactorSetupPage() {
  return (
    <Suspense>
      <TwoFactorSetup />
    </Suspense>
  );
}
