import type { Metadata } from "next";

import { Members } from "@/components/admin/members";

export const metadata: Metadata = { title: "Members" };

export default function MembersPage() {
  return <Members />;
}
