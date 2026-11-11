import type { Metadata } from "next";
import { Suspense } from "react";

import { AuditLog } from "@/components/admin/audit-log";

export const metadata: Metadata = { title: "Audit log" };

export default function AuditPage() {
  return (
    <Suspense>
      <AuditLog />
    </Suspense>
  );
}
