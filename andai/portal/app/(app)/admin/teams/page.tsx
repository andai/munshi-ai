import type { Metadata } from "next";

import { Teams } from "@/components/admin/teams";

export const metadata: Metadata = { title: "Teams" };

export default function TeamsPage() {
  return <Teams />;
}
