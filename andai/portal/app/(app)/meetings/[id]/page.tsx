import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { MeetingView } from "@/components/meetings/meeting-view";

export const metadata: Metadata = { title: "Meeting" };

const MEETING_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function MeetingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!MEETING_ID.test(id)) notFound();
  return <MeetingView id={id} />;
}
