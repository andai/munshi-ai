import type { Metadata } from "next";

import { DataExportPage } from "@/components/app/data-export";

export const metadata: Metadata = { title: "Your data" };

export default function ProfileDataPage() {
  return <DataExportPage />;
}
