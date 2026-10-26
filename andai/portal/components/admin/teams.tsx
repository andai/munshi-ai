"use client";

import { Plus, Trash2, Users } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { LockedHint, errorText, personName, useOrgMembers, type Member, type Team } from "@/components/admin/shared";
import { PageHeader } from "@/components/app/page-header";
import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";
import { isAdmin, useSession } from "@/lib/session";

// Teams: every member can see the organisation's teams. Admins create teams and set who is in each
// one and who leads it; a team's lead can change its members but not who leads it, so for a lead
// the lead/member values are shown locked. Saving replaces the team's member list
// (PUT /api/v1/teams/{id}/members); andai-core enforces who may do what.

type Draft = { user_id: string; email: string; display_name: string | null; role: "lead" | "member" };

function CreateTeamDialog({ open, onOpenChange, onCreated }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (name: string) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("name") ?? "").trim();
    setPending(true);
    setError(null);
    try {
      await api("/teams", { method: "POST", body: { name }, redirect: false });
      await onCreated(name);
      onOpenChange(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { onOpenChange(next); if (!next) setError(null); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Create a team</DialogTitle>
          <DialogDescription>Add people and choose a lead after it is created.</DialogDescription>
        </DialogHeader>
        <form id="create-team-form" onSubmit={onSubmit} className="grid gap-4">
          {error && <FormAlert>{error}</FormAlert>}
          <Field id="team-name" name="name" label="Team name" maxLength={100} autoComplete="off" required autoFocus />
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" form="create-team-form" disabled={pending} aria-busy={pending}>
            Create team
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ManageTeamDialog({ team, people, leadsLocked, onClose, onSaved }: {
  team: Team;
  /** The organisation's members (admins only); without it, people can be removed but not added. */
  people: Member[] | null;
  leadsLocked: boolean;
  onClose: () => void;
  onSaved: (name: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Draft[]>(team.members.map((m) => ({ ...m })));
  const [adding, setAdding] = useState<string>("");
  const [addRole, setAddRole] = useState<"lead" | "member">("member");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const candidates = (people ?? []).filter(
    (person) => person.status === "active" && !draft.some((d) => d.user_id === person.id),
  );

  function add() {
    const person = candidates.find((p) => p.id === adding);
    if (!person) return;
    setDraft([...draft, { user_id: person.id, email: person.email, display_name: person.display_name, role: addRole }]);
    setAdding("");
    setAddRole("member");
  }

  async function save() {
    setPending(true);
    setError(null);
    try {
      await api(`/teams/${team.id}/members`, {
        method: "PUT",
        body: { members: draft.map(({ user_id, role }) => ({ user_id, role })) },
        redirect: false,
      });
      await onSaved(team.name);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{team.name}</DialogTitle>
          <DialogDescription>Choose who is in this team and who leads it.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          {error && <FormAlert>{error}</FormAlert>}
          {draft.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
              Nobody is in this team yet.
            </p>
          ) : (
            <ul className="grid max-h-72 gap-1 overflow-y-auto" aria-label="Team members">
              {draft.map((member) => {
                const locked = leadsLocked && member.role === "lead";
                return (
                  <li key={member.user_id} className="flex items-center gap-2 rounded-lg px-1 py-1.5" data-testid={`team-member-${member.email}`}>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm">{personName(member)}</span>
                      {member.display_name && <span className="block truncate text-xs text-muted-foreground">{member.email}</span>}
                    </span>
                    <Select
                      value={member.role}
                      disabled={leadsLocked}
                      onValueChange={(value) =>
                        setDraft(draft.map((d) => (d.user_id === member.user_id ? { ...d, role: value as "lead" | "member" } : d)))
                      }
                    >
                      <SelectTrigger size="sm" className="w-28" aria-label={`Team role for ${member.email}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="lead">Lead</SelectItem>
                        <SelectItem value="member">Member</SelectItem>
                      </SelectContent>
                    </Select>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove ${member.email}`}
                      disabled={locked}
                      onClick={() => setDraft(draft.filter((d) => d.user_id !== member.user_id))}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
          {leadsLocked && <LockedHint>Only admins change who leads a team.</LockedHint>}

          {people ? (
            <div className="grid gap-2 border-t border-border pt-4">
              <Label htmlFor="add-person">Add a person</Label>
              <div className="flex flex-wrap gap-2">
                <Select value={adding} onValueChange={setAdding}>
                  <SelectTrigger id="add-person" className="h-9 min-w-0 flex-1 basis-48" aria-label="Person to add">
                    <SelectValue placeholder={candidates.length ? "Choose a person" : "Everyone is in this team"} />
                  </SelectTrigger>
                  <SelectContent>
                    {candidates.map((person) => (
                      <SelectItem key={person.id} value={person.id}>
                        {personName(person)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Select value={addRole} onValueChange={(value) => setAddRole(value as "lead" | "member")} disabled={leadsLocked}>
                  <SelectTrigger className="h-9 w-28" aria-label="Team role for the new person">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="member">Member</SelectItem>
                    <SelectItem value="lead">Lead</SelectItem>
                  </SelectContent>
                </Select>
                <Button type="button" variant="outline" className="h-9" onClick={add} disabled={!adding}>
                  <Plus aria-hidden="true" />
                  Add
                </Button>
              </div>
            </div>
          ) : (
            <p className="border-t border-border pt-4 text-xs text-muted-foreground">To add people, ask an admin of your organisation.</p>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" onClick={() => void save()} disabled={pending} aria-busy={pending}>
            Save changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function Teams() {
  const { me, refresh } = useSession();
  const admin = isAdmin(me.role);
  // Only admins may read the member list (needed to add people); nobody else asks for it.
  const members = useOrgMembers({ redirect: false, enabled: admin });
  const [teams, setTeams] = useState<Team[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Team | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setTeams((await api<{ teams: Team[] }>("/teams")).teams);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (!teams) {
    return (
      <>
        <PageHeader title="Teams" />
        <p role="status" className="text-sm text-muted-foreground">
          Loading teams…
        </p>
      </>
    );
  }

  const leads = (team: Team) => team.members.some((m) => m.user_id === me.user.id && m.role === "lead");

  return (
    <>
      <PageHeader
        title="Teams"
        description="Group people who meet together. Leads manage their team's members."
        actions={
          admin && (
            <Button onClick={() => setCreating(true)} className="h-9 px-3.5">
              <Plus aria-hidden="true" />
              Create team
            </Button>
          )
        }
      />
      {notice && (
        <div className="mb-4">
          <FormAlert tone="success">{notice}</FormAlert>
        </div>
      )}
      {teams.length === 0 ? (
        <div className="flex flex-col items-center rounded-2xl border border-dashed border-border px-6 py-14 text-center">
          <Users className="size-8 text-muted-foreground" aria-hidden="true" />
          <h2 className="mt-4 font-semibold">No teams yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {admin ? "Create a team to group people who meet together." : "An admin hasn't created any teams yet."}
          </p>
        </div>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2">
          {teams.map((team) => {
            const canManage = admin || leads(team);
            const teamLeads = team.members.filter((m) => m.role === "lead");
            return (
              <li key={team.id} data-testid={`team-${team.name}`}>
                <Card className="h-full [--card-spacing:--spacing(5)]">
                  <CardHeader className="flex flex-row items-start justify-between gap-3">
                    <div className="min-w-0">
                      <CardTitle className="truncate text-base">{team.name}</CardTitle>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {team.members.length} {team.members.length === 1 ? "member" : "members"}
                        {teamLeads.length > 0 && ` · led by ${teamLeads.map(personName).join(", ")}`}
                      </p>
                    </div>
                    {canManage && (
                      <Button variant="outline" size="sm" onClick={() => setEditing(team)} aria-label={`Manage ${team.name}`}>
                        Manage
                      </Button>
                    )}
                  </CardHeader>
                  <CardContent>
                    {team.members.length === 0 ? (
                      <p className="text-sm text-muted-foreground">Nobody yet.</p>
                    ) : (
                      <ul className="flex flex-wrap gap-1.5">
                        {team.members.map((member) => (
                          <li key={member.user_id}>
                            <Badge variant={member.role === "lead" ? "default" : "secondary"} className="max-w-56">
                              <span className="truncate">{personName(member)}</span>
                              {member.role === "lead" && <span className="opacity-80">· lead</span>}
                            </Badge>
                          </li>
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>
              </li>
            );
          })}
        </ul>
      )}

      <CreateTeamDialog
        open={creating}
        onOpenChange={setCreating}
        onCreated={async (name) => {
          setNotice(`Team ${name} is created.`);
          await reload();
        }}
      />
      {editing && (
        <ManageTeamDialog
          key={editing.id}
          team={editing}
          people={admin ? members.data?.users ?? null : null}
          leadsLocked={!admin}
          onClose={() => setEditing(null)}
          onSaved={async (name) => {
            setNotice(`Team ${name} is saved.`);
            await reload();
            await refresh();
          }}
        />
      )}
    </>
  );
}
