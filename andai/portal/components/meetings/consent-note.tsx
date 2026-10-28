import { Megaphone } from "lucide-react";

import { clockTime } from "@/components/meetings/display";
import type { Consent } from "@/lib/meetings";

// How the room was told that munshi.ai records: the bot's name, where the notice went (meeting chat,
// or the bot's name alone when chat was not possible), and a removal by a participant.

function safeLink(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function channelText(consent: Consent): string {
  if (consent.disclosure_channel === "chat") {
    const time = clockTime(consent.disclosure_posted_at);
    return time ? `The recording notice was posted in the meeting chat at ${time}.` : "The recording notice was posted in the meeting chat.";
  }
  if (consent.disclosure_channel === "tts") return "The bot announced the recording out loud.";
  if (consent.disclosure_detail === "pending") return "The bot is posting the recording notice in the meeting chat.";
  return "Chat was not available, so the bot's name told participants about the recording.";
}

export function ConsentNote({ botName, consent }: { botName: string; consent: Consent | null }) {
  const name = consent?.bot_display_name || botName;
  const privacy = consent ? safeLink(consent.privacy_notice_url) : null;
  return (
    <section aria-labelledby="consent-title" data-testid="consent-note" className="flex gap-3 rounded-xl border border-border bg-card p-4">
      <Megaphone aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
      <div className="grid min-w-0 gap-1 text-sm">
        <h2 id="consent-title" className="font-semibold">
          Recording notice
        </h2>
        <p className="[overflow-wrap:anywhere] text-muted-foreground">
          The bot joins as <span className="font-medium text-foreground">“{name}”</span> and tells everyone it is recording.
        </p>
        {consent && (
          <p className="text-muted-foreground" data-testid="consent-channel" data-channel={consent.disclosure_channel}>
            {channelText(consent)}
          </p>
        )}
        {consent?.removed_at && (
          <p className="font-medium text-foreground" data-testid="consent-removed">
            A participant removed the bot at {clockTime(consent.removed_at)}.
          </p>
        )}
        {privacy && (
          <a href={privacy} target="_blank" rel="noreferrer noopener" className="w-fit text-primary underline-offset-4 hover:underline">
            Privacy notice
          </a>
        )}
      </div>
    </section>
  );
}
