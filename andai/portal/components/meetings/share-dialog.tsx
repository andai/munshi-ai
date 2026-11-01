"use client";

import { Search, UsersRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { initials } from "@/components/meetings/display";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, sentence } from "@/lib/api";
import { getDirectory, grantShare, listShares, revokeShare, type Directory, type Share } from "@/lib/meetings";

// Share a meeting with a person or a team of the organisation, and stop sharing it. The people and
// teams to pick from are the org directory (`GET /org/directory`: names and ids, never email
// addresses); the box only narrows that list, so there is no way to type in someone from outside.
// andai-core decides who may share (the page shows the button only to them) and checks each grant.

const MAX_OPTIONS = 8;

type Option = { type: "user" | "team"; id: string; name: string; detail: string };

function errorText(err: unknown): string {
  return err instanceof ApiError ? sentence(err.detail) : "Something went wrong. Try again.";
}

function options(directory: Directory, excluded: Set<string>): Option[] {
  const teamName = new Map(directory.teams.map((t) => [t.id, t.name]));
  const size = new Map<string, number>();
  for (const user of directory.users) for (const team of user.team_ids) size.set(team, (size.get(team) ?? 0) + 1);
  const teams: Option[] = directory.teams.map((team) => {
    const n = size.get(team.id) ?? 0;
    return { type: "team", id: team.id, name: team.name, detail: `team, ${n} ${n === 1 ? "person" : "people"}` };
  });
  const people: Option[] = directory.users.map((user) => ({
    type: "user",
    id: user.id,
    name: user.name?.trim() || "Unnamed member",
    detail: user.team_ids.map((id) => teamName.get(id)).filter(Boolean).join(", "),
  }));
  return [...teams, ...people].filter((o) => !excluded.has(`${o.type}:${o.id}`));
}

export function ShareDialog({
  meetingId,
  orgName,
  hidden,
  open,
  onOpenChange,
  onChanged,
}: {
  meetingId: string;
  orgName: string;
  /** People not to offer (they can open the meeting already: the requester, the caller). */
  hidden: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** After a share is added or removed. */
  onChanged: () => void;
}) {
  const [directory, setDirectory] = useState<Directory | null>(null);
  const [shares, setShares] = useState<Share[] | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [people, current] = await Promise.all([getDirectory(), listShares(meetingId)]);
      setDirectory(people);
      setShares(current);
    } catch (err) {
      setError(errorText(err));
    }
  }, [meetingId]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setError(null);
    void load();
  }, [open, load]);

  const choices = useMemo(() => {
    if (!directory || !shares) return [];
    const excluded = new Set([...shares.map((s) => `${s.grantee_type}:${s.grantee_id}`), ...hidden.map((id) => `user:${id}`)]);
    const needle = query.trim().toLocaleLowerCase();
    return options(directory, excluded).filter((o) => !needle || o.name.toLocaleLowerCase().includes(needle));
  }, [directory, shares, hidden, query]);

  async function change(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await action();
      setShares(await listShares(meetingId));
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  const add = (option: Option) =>
    change(`add:${option.type}:${option.id}`, () => grantShare(meetingId, { grantee_type: option.type, grantee_id: option.id }));
  const remove = (share: Share) => change(`remove:${share.id}`, () => revokeShare(meetingId, share.id));

  const loading = !directory || !shares;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="share-dialog">
        <DialogHeader>
          <DialogTitle>Share this meeting</DialogTitle>
          <DialogDescription>Meetings can only be shared inside your organisation.</DialogDescription>
        </DialogHeader>
        {error && <FormAlert>{error}</FormAlert>}

        <div className="grid min-w-0 gap-1.5">
          <Label htmlFor="share-find">Add a person or team in {orgName}</Label>
          <div className="relative">
            <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="share-find"
              type="search"
              autoComplete="off"
              placeholder="Name or team"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="h-10 pl-9"
              data-testid="share-find"
            />
          </div>
          {loading ? (
            !error && (
              <p className="py-2 text-sm text-muted-foreground" aria-busy="true">
                Loading people and teams…
              </p>
            )
          ) : choices.length === 0 ? (
            <p className="py-2 text-sm text-muted-foreground" data-testid="share-no-match">
              {query.trim() ? "No one in your organisation matches." : "Everyone in your organisation can already open it."}
            </p>
          ) : (
            <ul className="grid max-h-56 gap-0.5 overflow-y-auto rounded-xl border border-border p-1" aria-label="People and teams" data-testid="share-options">
              {choices.slice(0, MAX_OPTIONS).map((option) => (
                <li
                  key={`${option.type}:${option.id}`}
                  data-testid="share-option"
                  data-type={option.type}
                  data-id={option.id}
                  className="flex min-w-0 items-center gap-2.5 rounded-lg px-2 py-1.5"
                >
                  <Avatar option={option} />
                  <span className="grid min-w-0 flex-1">
                    <span className="truncate text-sm font-medium">{option.name}</span>
                    {option.detail && <span className="truncate text-xs text-muted-foreground">{option.detail}</span>}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy !== null}
                    aria-busy={busy === `add:${option.type}:${option.id}`}
                    aria-label={`Share with ${option.name}`}
                    onClick={() => void add(option)}
                  >
                    Add
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {choices.length > MAX_OPTIONS && (
            <p className="text-xs text-muted-foreground">{choices.length - MAX_OPTIONS} more — type to narrow the list.</p>
          )}
        </div>

        <div className="grid min-w-0 gap-1.5">
          <h3 className="text-sm font-medium">Shared with</h3>
          {shares && shares.length === 0 && <p className="text-sm text-muted-foreground">Nobody yet.</p>}
          {shares && shares.length > 0 && (
            <ul className="grid gap-0.5" data-testid="share-list">
              {shares.map((share) => {
                const name = share.name ?? (share.grantee_type === "team" ? "A removed team" : "A former member");
                return (
                  <li key={share.id} data-testid="share-grant" data-type={share.grantee_type} className="flex min-w-0 items-center gap-2.5 py-1">
                    <Avatar option={{ type: share.grantee_type, id: share.grantee_id, name, detail: "" }} />
                    <span className="grid min-w-0 flex-1">
                      <span className="truncate text-sm font-medium">{name}</span>
                      <span className="text-xs text-muted-foreground">{share.grantee_type === "team" ? "Team" : "Person"}</span>
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={busy !== null}
                      aria-busy={busy === `remove:${share.id}`}
                      aria-label={`Stop sharing with ${name}`}
                      onClick={() => void remove(share)}
                    >
                      Remove
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <DialogFooter>
          <Button type="button" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Avatar({ option }: { option: Option }) {
  return (
    <span
      aria-hidden="true"
      className="flex size-8 shrink-0 items-center justify-center rounded-full bg-secondary text-[11px] font-bold text-secondary-foreground"
    >
      {option.type === "team" ? <UsersRound className="size-4" /> : initials(option.name)}
    </span>
  );
}
