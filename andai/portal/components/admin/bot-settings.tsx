"use client";

import { useEffect, useMemo, useState } from "react";

import { errorText, type Team } from "@/components/admin/shared";
import { PageHeader } from "@/components/app/page-header";
import { FormAlert } from "@/components/forms/form-alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";
import { AUTO_JOIN_VALUES, getBotSettings, saveBotSettings, type AutoJoin, type BotSettings } from "@/lib/admin";
import { useSession } from "@/lib/session";

// The bot's auto-join setting (owners and admins; a 403 on load shows /403): one value for the
// organisation, and per team either the organisation's value or an override of its own. Saving
// sends the whole form (PUT /api/v1/org/bot-settings) and shows it as saved at once; when andai-core
// refuses, the form goes back to the last saved values and says why.

export const AUTO_JOIN_LABEL: Record<AutoJoin, string> = {
  only_when_invited: "When the notetaker is invited",
  all_with_link: "Every invitation with a link",
  off: "Off",
};

const AUTO_JOIN_HELP: Record<AutoJoin, string> = {
  only_when_invited:
    "Meetings whose invitation is sent to the notetaker are scheduled. Invitations a mailbox rule forwards on its own are not.",
  all_with_link: "Invitations a mailbox rule forwards on its own are scheduled too, when they carry a meeting link.",
  off: "Invitations are not scheduled. Changes and cancellations of meetings already scheduled still apply.",
};

const INHERIT = "inherit";

function sameSettings(a: BotSettings, b: BotSettings): boolean {
  if (a.autoJoin !== b.autoJoin) return false;
  const values = new Map(b.teams.map((t) => [t.teamId, t.autoJoin]));
  return a.teams.length === b.teams.length && a.teams.every((t) => values.get(t.teamId) === t.autoJoin);
}

export function BotSettingsPage() {
  const { me } = useSession();
  const [saved, setSaved] = useState<BotSettings | null>(null);
  const [draft, setDraft] = useState<BotSettings | null>(null);
  const [names, setNames] = useState<Map<string, string>>(new Map());
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([getBotSettings(), api<{ teams: Team[] }>("/teams")])
      .then(([settings, { teams }]) => {
        if (!live) return;
        setSaved(settings);
        setDraft(settings);
        setNames(new Map(teams.map((team) => [team.id, team.name])));
      })
      .catch((err) => {
        if (live) setNotice({ tone: "error", text: errorText(err) });
      });
    return () => {
      live = false;
    };
  }, []);

  const teams = useMemo(
    () =>
      [...(draft?.teams ?? [])].sort((a, b) =>
        (names.get(a.teamId) ?? "").localeCompare(names.get(b.teamId) ?? ""),
      ),
    [draft, names],
  );

  if (!saved || !draft) {
    return (
      <>
        <PageHeader title="Bot" />
        {notice ? (
          <FormAlert>{notice.text}</FormAlert>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">
            Loading bot settings…
          </p>
        )}
      </>
    );
  }

  const dirty = !sameSettings(draft, saved);

  async function save() {
    if (!draft || !saved) return;
    const before = saved;
    const sent = draft;
    setPending(true);
    setNotice(null);
    setSaved(sent); // shown as saved at once; put back if andai-core refuses
    try {
      const answer = await saveBotSettings(sent);
      setSaved(answer);
      setDraft(answer);
      setNotice({ tone: "success", text: "Auto-join settings are saved." });
    } catch (err) {
      setSaved(before);
      setDraft(before);
      setNotice({ tone: "error", text: errorText(err) });
    } finally {
      setPending(false);
    }
  }

  function setTeam(teamId: string, value: string) {
    setDraft((current) =>
      current
        ? {
            ...current,
            teams: current.teams.map((t) => (t.teamId === teamId ? { ...t, autoJoin: value === INHERIT ? null : (value as AutoJoin) } : t)),
          }
        : current,
    );
  }

  return (
    <>
      <PageHeader title="Bot" description={`When the bot joins meetings it is invited to, for ${me.org.name}`} />
      <div className="grid gap-6" data-testid="bot-settings">
        {notice && <FormAlert tone={notice.tone}>{notice.text}</FormAlert>}
        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="text-base">Auto-join from invitations</CardTitle>
            <CardDescription className="max-w-prose">
              People in your organisation add the notetaker to a calendar invitation, and the bot joins shortly before the
              start. This is the setting for everyone; a team can have its own below.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            <Label htmlFor="org-auto-join">Organisation</Label>
            <Select value={draft.autoJoin} onValueChange={(value) => setDraft({ ...draft, autoJoin: value as AutoJoin })}>
              <SelectTrigger id="org-auto-join" className="h-10 w-full sm:w-80" data-testid="org-auto-join">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AUTO_JOIN_VALUES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {AUTO_JOIN_LABEL[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="max-w-prose text-sm text-muted-foreground" data-testid="org-auto-join-help">
              {AUTO_JOIN_HELP[draft.autoJoin]}
            </p>
          </CardContent>
        </Card>

        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="text-base">Teams</CardTitle>
            <CardDescription>A team&apos;s own setting applies to invitations from its members.</CardDescription>
          </CardHeader>
          <CardContent>
            {teams.length === 0 ? (
              <p className="text-sm text-muted-foreground" data-testid="bot-teams-empty">
                No teams yet. Every invitation follows the organisation&apos;s setting.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {teams.map((team) => {
                  const name = names.get(team.teamId) ?? "Team";
                  return (
                    <li
                      key={team.teamId}
                      data-testid={`bot-team-${name}`}
                      className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3"
                    >
                      <span className="min-w-0 flex-1 basis-40 truncate text-sm font-medium">{name}</span>
                      <Select value={team.autoJoin ?? INHERIT} onValueChange={(value) => setTeam(team.teamId, value)}>
                        <SelectTrigger aria-label={`Auto-join for ${name}`} className="h-10 w-full sm:w-80">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={INHERIT}>Organisation setting ({AUTO_JOIN_LABEL[draft.autoJoin].toLowerCase()})</SelectItem>
                          {AUTO_JOIN_VALUES.map((value) => (
                            <SelectItem key={value} value={value}>
                              {AUTO_JOIN_LABEL[value]}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>

        <div className="flex flex-wrap gap-2">
          <Button type="button" size="lg" onClick={() => void save()} disabled={!dirty || pending} aria-busy={pending}>
            {pending ? "Saving…" : "Save changes"}
          </Button>
          <Button type="button" size="lg" variant="outline" onClick={() => setDraft(saved)} disabled={!dirty || pending}>
            Discard
          </Button>
        </div>
      </div>
    </>
  );
}
