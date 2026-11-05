import type { Metadata } from "next";

import { BotSettingsPage } from "@/components/admin/bot-settings";

export const metadata: Metadata = { title: "Bot" };

export default function BotPage() {
  return <BotSettingsPage />;
}
