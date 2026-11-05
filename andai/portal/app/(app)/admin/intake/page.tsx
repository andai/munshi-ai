import type { Metadata } from "next";

import { Quarantine } from "@/components/admin/quarantine";

export const metadata: Metadata = { title: "Mail intake" };

export default function IntakePage() {
  return <Quarantine />;
}
