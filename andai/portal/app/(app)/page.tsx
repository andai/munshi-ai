import type { Metadata } from "next";

import { Dashboard } from "@/components/app/dashboard";

export const metadata: Metadata = { title: "Meetings" };

export default function DashboardPage() {
  return <Dashboard />;
}
