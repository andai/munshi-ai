import type { Metadata } from "next";

import { OrgSecurity } from "@/components/admin/security";

export const metadata: Metadata = { title: "Security" };

export default function OrgSecurityPage() {
  return <OrgSecurity />;
}
