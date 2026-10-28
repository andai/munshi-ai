import type { Metadata } from "next";

import { MeetingView } from "@/components/meetings/meeting-view";

export const metadata: Metadata = { title: "Meeting" };

export default async function MeetingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <MeetingView id={id} />;
}
