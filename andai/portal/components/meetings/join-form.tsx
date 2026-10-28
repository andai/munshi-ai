"use client";

import { Link2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { SubmitButton } from "@/components/forms/submit-button";
import { LANGUAGE_NAME } from "@/components/meetings/display";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, api, destinationFor, sentence } from "@/lib/api";
import { joinMeeting, type Language } from "@/lib/meetings";
import { isAdmin, useSession } from "@/lib/session";

// "Add notetaker": paste a meeting link, pick the language and (optionally) one of your teams, and
// andai-core sends the bot. On success the browser goes to the meeting's page; pasting a link the
// bot is already in lands on that same meeting. The link check here is only a hint — andai-core
// decides which links it accepts, and its refusal is shown as it says it.

type TeamOption = { id: string; name: string };
type TeamList = { teams: { id: string; name: string; members: { user_id: string }[] }[] };

const DEFAULT_LANGUAGE = "default";
const NO_TEAM = "none";
const MEETING_HOSTS = ["meet.google.com", "teams.microsoft.com", "teams.live.com", "teams.microsoft.us", "zoom.us", "zoomgov.com"];

/** Whether a pasted value looks like a Google Meet, Teams or Zoom link (a hint, not a rule). */
export function looksLikeMeetingLink(value: string): boolean {
  try {
    const url = new URL(value.trim());
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" && MEETING_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
  } catch {
    return false;
  }
}

export function JoinForm() {
  const router = useRouter();
  const { me } = useSession();
  const [link, setLink] = useState("");
  const [language, setLanguage] = useState<string>(DEFAULT_LANGUAGE);
  const [team, setTeam] = useState<string>(NO_TEAM);
  const [teams, setTeams] = useState<TeamOption[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api<TeamList>("/teams", { redirect: false })
      .then(({ teams: all }) => {
        if (!active) return;
        // Admins may name any team; everyone else the teams they belong to (andai-core checks it).
        const mine = isAdmin(me.role) ? all : all.filter((t) => t.members.some((m) => m.user_id === me.user.id));
        setTeams(mine.map(({ id, name }) => ({ id, name })));
      })
      .catch(() => {
        // Without the list the meeting is sent without a team.
      });
    return () => {
      active = false;
    };
  }, [me.role, me.user.id]);

  const trimmed = link.trim();
  const showHint = trimmed.length > 0 && !looksLikeMeetingLink(trimmed);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!trimmed) {
      setError("Paste a meeting link.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const meeting = await joinMeeting({
        join_url: trimmed,
        language: language === DEFAULT_LANGUAGE ? null : (language as Language),
        team_id: team === NO_TEAM ? null : team,
      });
      router.push(`/meetings/${meeting.id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        const destination = destinationFor(err, "POST");
        if (destination) {
          window.location.assign(destination);
          return;
        }
        setError(sentence(err.detail));
      } else {
        setError("Something went wrong. Try again.");
      }
      setPending(false);
    }
  }

  return (
    <section
      aria-labelledby="join-title"
      data-testid="join-form"
      className="relative overflow-hidden rounded-2xl border border-border bg-card p-5 shadow-xs sm:p-6"
    >
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-1 bg-saffron" />
      <h2 id="join-title" className="text-lg font-semibold">
        Add notetaker
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Paste a Google Meet, Microsoft Teams or Zoom link. The bot joins as a recorder and tells everyone it is recording.
      </p>
      <form onSubmit={onSubmit} noValidate className="mt-5 grid gap-4">
        <div className="grid gap-1.5">
          <Label htmlFor="join-link">Meeting link</Label>
          <div className="relative">
            <Link2 aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="join-link"
              name="join_url"
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://meet.google.com/abc-defg-hij"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              aria-describedby="join-link-hint"
              aria-invalid={showHint || undefined}
              className="h-10 pl-9 md:text-sm"
            />
          </div>
          <p id="join-link-hint" className="text-xs text-muted-foreground" data-testid="join-link-hint">
            {showHint
              ? "This doesn't look like a Google Meet, Teams or Zoom link."
              : "The bot joins with the link as you paste it, passcode included."}
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor="join-language">Language</Label>
            <Select value={language} onValueChange={setLanguage}>
              <SelectTrigger id="join-language" className="h-10 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_LANGUAGE}>Organisation default</SelectItem>
                {(Object.keys(LANGUAGE_NAME) as Language[]).map((code) => (
                  <SelectItem key={code} value={code}>
                    {LANGUAGE_NAME[code]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="join-team">Team</Label>
            <Select value={team} onValueChange={setTeam}>
              <SelectTrigger id="join-team" className="h-10 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_TEAM}>No team</SelectItem>
                {teams.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    {t.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        {error && <FormAlert>{error}</FormAlert>}
        <SubmitButton pending={pending} className="sm:w-auto sm:justify-self-start sm:px-6">
          {pending ? "Sending the bot…" : "Send the bot"}
        </SubmitButton>
      </form>
    </section>
  );
}
