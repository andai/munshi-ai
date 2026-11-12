"use client";

import { MoreHorizontal, UserPlus } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  ConfirmDialog,
  LockedHint,
  ROLE_LABEL,
  StatusBadge,
  TwoFactorBadge,
  errorText,
  personName,
  useOrgMembers,
  type Member,
} from "@/components/admin/shared";
import { DownloadLink, ExportDetail, ExportStateBadge } from "@/components/app/data-export";
import { PageHeader } from "@/components/app/page-header";
import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { listErasures, requestErasure, type ErasureRequest } from "@/lib/admin";
import { api } from "@/lib/api";
import { EXPORT_POLL_MS, isOpen, listOrgExports, requestMemberExport, type DataExport } from "@/lib/exports";
import { useSession, type OrgRole } from "@/lib/session";

// Members: everyone in the organisation with their role, account status, two-factor state and
// teams, plus pending invites. Admins invite people, change roles, disable or re-enable accounts,
// reset a member's two-factor login, make a copy of a member's data for a DPDP access request and
// erase a member's data (OPS-02; never the owner's or their own). andai-core decides every change; a
// control the server would refuse for this admin (their own row, the owner's row for an admin) is
// shown locked.
//
// "Data exports" lists the copies owners and admins asked for, with a Download link on the ones this
// admin asked for (andai-core's `canDownload`); it is read again every few seconds while one is
// being made.
//
// An erased member leaves the list at once (andai-core ends their membership in the request); the
// "Data erasure" section shows each request's state from GET /org/erasures, which carries ids only.
// The name of someone erased in this visit is remembered in memory for that list, nowhere else.

type Pending =
  | { kind: "role"; member: Member; role: OrgRole }
  | { kind: "status"; member: Member }
  | { kind: "reset-2fa"; member: Member }
  | null;

function InviteDialog({ open, onOpenChange, onInvited }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onInvited: (email: string) => Promise<void>;
}) {
  const [role, setRole] = useState<"member" | "admin">("member");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    setPending(true);
    setError(null);
    try {
      await api("/org/users", { method: "POST", body: { email, role }, redirect: false });
      await onInvited(email);
      onOpenChange(false);
      setRole("member");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setError(null);
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Invite people</DialogTitle>
          <DialogDescription>They get an email with a link to set a password. The link works for 7 days.</DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="grid gap-4" id="invite-form">
          {error && <FormAlert>{error}</FormAlert>}
          <Field id="invite-email" name="email" label="Email" type="email" inputMode="email" autoComplete="off" required autoFocus />
          <div className="grid gap-1.5">
            <Label htmlFor="invite-role">Role</Label>
            <Select value={role} onValueChange={(value) => setRole(value as "member" | "admin")}>
              <SelectTrigger id="invite-role" className="h-10 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="member">Member</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">Admins manage members, teams and security settings.</p>
          </div>
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" form="invite-form" disabled={pending} aria-busy={pending}>
            Send invite
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const ERASURE_POLL_MS = 5000;

function EraseDialog({ member, orgName, onClose, onErased }: {
  member: Member | null;
  orgName: string;
  onClose: () => void;
  onErased: (member: Member) => Promise<void>;
}) {
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const matches = member !== null && typed.trim().toLowerCase() === member.email.toLowerCase();

  function close() {
    setTyped("");
    setError(null);
    onClose();
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!member || !matches) return;
    setPending(true);
    setError(null);
    try {
      await requestErasure(member.id, typed.trim());
      await onErased(member);
      close();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  const name = member ? personName(member) : "";
  return (
    <Dialog open={member !== null} onOpenChange={(open) => !open && !pending && close()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Erase {name}&rsquo;s data?</DialogTitle>
          <DialogDescription>
            This can&rsquo;t be undone. {name} loses access to {orgName} now, and munshi.ai removes their data within
            24 hours, usually within minutes.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 text-sm sm:grid-cols-2">
          <div className="rounded-lg border border-border p-3">
            <p className="mb-1.5 font-medium">What goes</p>
            <ul className="grid list-disc gap-1 pl-4 text-muted-foreground">
              <li>Meetings they sent the notetaker to here, with recordings, transcripts and minutes</li>
              <li>Their teams, shares, invites and the notetaker mail they sent here</li>
              <li>Their account, if they belong to no other organisation</li>
            </ul>
          </div>
          <div className="rounded-lg border border-border p-3">
            <p className="mb-1.5 font-medium">What stays</p>
            <ul className="grid list-disc gap-1 pl-4 text-muted-foreground">
              <li>What they said in other people&rsquo;s meetings</li>
              <li>The audit log, which keeps their user ID and shows them as &ldquo;Erased user&rdquo;</li>
            </ul>
          </div>
        </div>
        <form id="erase-form" onSubmit={onSubmit} className="grid gap-3">
          {error && <FormAlert>{error}</FormAlert>}
          <Field
            id="erase-confirm"
            name="confirm"
            label="Type their email address to confirm"
            hint={member?.email}
            type="email"
            inputMode="email"
            autoComplete="off"
            spellCheck={false}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoFocus
          />
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" form="erase-form" variant="destructive" disabled={!matches || pending} aria-busy={pending}>
            Erase data
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const ERASURE_BADGE: Record<ErasureRequest["state"], { label: string; variant: "secondary" | "outline" | "destructive"; className?: string }> = {
  pending: { label: "Pending", variant: "secondary" },
  running: { label: "In progress", variant: "secondary" },
  done: { label: "Erased", variant: "outline", className: "border-success/30 text-success" },
  failed: { label: "Failed", variant: "destructive" },
};

function shortDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** The organisation's erasure requests, refreshed every few seconds while one is still open. */
function useErasures() {
  const [items, setItems] = useState<ErasureRequest[] | null>(null);

  const reload = useCallback(async () => {
    setItems((await listErasures()).items);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const open = items?.some((item) => item.state === "pending" || item.state === "running") ?? false;
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => void reload(), ERASURE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, reload]);

  return { items, reload };
}

function ErasureList({ items, names }: { items: ErasureRequest[]; names: Record<string, string> }) {
  if (!items.length) return null;
  return (
    <section aria-labelledby="erasures-title" className="mt-8">
      <h2 id="erasures-title" className="mb-1 text-sm font-semibold">
        Data erasure
      </h2>
      <p className="mb-3 text-xs text-muted-foreground">Requests to erase a member&rsquo;s data. Each finishes within 24 hours.</p>
      <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
        {items.map((item) => {
          const badge = ERASURE_BADGE[item.state];
          return (
            <li
              key={item.id}
              data-testid={`erasure-${item.subjectId}`}
              data-state={item.state}
              className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-3"
            >
              <span className="min-w-0 truncate text-sm">{names[item.subjectId] ?? "A former member"}</span>
              <span className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>
                  {item.finishedAt ? `Finished ${shortDate(item.finishedAt)}` : `Asked ${shortDate(item.createdAt)}`}
                </span>
                <Badge variant={badge.variant} className={badge.className}>
                  {badge.label}
                </Badge>
              </span>
            </li>
          );
        })}
      </ul>
      {items.some((item) => item.state === "failed") && (
        <p className="mt-2 text-xs text-muted-foreground">
          A failed erasure needs your munshi.ai operator; nothing more happens on its own.
        </p>
      )}
    </section>
  );
}

/** The copies owners and admins asked for, refreshed every few seconds while one is being made. */
function useOrgExports() {
  const [items, setItems] = useState<DataExport[] | null>(null);

  const reload = useCallback(async () => {
    setItems((await listOrgExports()).items);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const open = items?.some(isOpen) ?? false;
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => void reload(), EXPORT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, reload]);

  return { items, reload };
}

function ExportList({ items }: { items: DataExport[] }) {
  if (!items.length) return null;
  return (
    <section aria-labelledby="exports-title" className="mt-8">
      <h2 id="exports-title" className="mb-1 text-sm font-semibold">
        Data exports
      </h2>
      <p className="mb-3 text-xs text-muted-foreground">
        Copies of a member&rsquo;s data for an access request. Only the admin who asked, and the member, can download one.
      </p>
      <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
        {items.map((item) => (
          <li
            key={item.id}
            data-testid={`export-${item.id}`}
            data-subject={item.subjectId}
            data-state={item.state}
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3"
          >
            <span className="grid min-w-0 gap-0.5">
              <span className="truncate text-sm">{item.subjectName ?? "A former member"}</span>
              <ExportDetail item={item} />
            </span>
            <span className="flex items-center gap-2">
              <ExportStateBadge state={item.state} />
              <DownloadLink item={item} />
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RoleControl({ member, callerRole, isSelf, onChange }: {
  member: Member;
  callerRole: OrgRole;
  isSelf: boolean;
  onChange: (role: OrgRole) => void;
}) {
  const label = `Role for ${member.email}`;
  if (isSelf) {
    return (
      <div className="grid gap-1">
        <Badge variant="secondary">{ROLE_LABEL[member.role]}</Badge>
        <LockedHint>Your own role</LockedHint>
      </div>
    );
  }
  const locked = member.role === "owner" && callerRole !== "owner";
  return (
    <div className="grid gap-1">
      <Select value={member.role} onValueChange={(value) => onChange(value as OrgRole)} disabled={locked}>
        <SelectTrigger aria-label={label} size="sm" className="w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(callerRole === "owner" || member.role === "owner") && <SelectItem value="owner">Owner</SelectItem>}
          <SelectItem value="admin">Admin</SelectItem>
          <SelectItem value="member">Member</SelectItem>
        </SelectContent>
      </Select>
      {locked && <LockedHint>Only the owner can change this</LockedHint>}
    </div>
  );
}

export function Members() {
  const { me, refresh } = useSession();
  const { data, reload } = useOrgMembers();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [erasing, setErasing] = useState<Member | null>(null);
  const [erasedNames, setErasedNames] = useState<Record<string, string>>({});
  const erasures = useErasures();
  const exports = useOrgExports();
  const setNotice = (text: string) => setMessage({ tone: "success", text });

  if (!data) {
    return (
      <>
        <PageHeader title="Members" />
        <p role="status" className="text-sm text-muted-foreground">
          Loading members…
        </p>
      </>
    );
  }

  async function patch(member: Member, body: { role?: OrgRole; status?: "active" | "disabled" }, done: string) {
    await api(`/org/users/${member.id}`, { method: "PATCH", body, redirect: false });
    setNotice(done);
    await reload();
    await refresh();
  }

  function confirmTitle(p: NonNullable<Pending>): string {
    if (p.kind === "role") return `Make ${personName(p.member)} the owner?`;
    if (p.kind === "status") return p.member.status === "disabled" ? `Enable ${personName(p.member)}?` : `Disable ${personName(p.member)}?`;
    return `Reset two-factor login for ${personName(p.member)}?`;
  }

  function confirmText(p: NonNullable<Pending>): string {
    if (p.kind === "role") return "There is one owner. You become an admin, and they take over owner rights.";
    if (p.kind === "status")
      return p.member.status === "disabled"
        ? "They can sign in again with their password."
        : "They are signed out everywhere now and can't sign in until an admin enables the account.";
    return "Their authenticator and recovery codes stop working, and they are signed out everywhere. They set it up again at their next sign-in if your organisation requires it.";
  }

  async function runPending(p: NonNullable<Pending>) {
    const who = personName(p.member);
    if (p.kind === "role") return patch(p.member, { role: p.role }, `${who} is now the owner.`);
    if (p.kind === "status") {
      const enable = p.member.status === "disabled";
      return patch(p.member, { status: enable ? "active" : "disabled" }, enable ? `${who} is enabled.` : `${who} is disabled.`);
    }
    await api(`/org/users/${p.member.id}/mfa`, { method: "DELETE", redirect: false });
    setNotice(`Two-factor login is reset for ${who}.`);
    await reload();
  }

  async function exportData(member: Member) {
    setMessage(null);
    try {
      await requestMemberExport(member.id);
      setNotice(`A copy of ${personName(member)}'s data is being prepared. It appears under Data exports below.`);
      await exports.reload();
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err) });
    }
  }

  async function changeRole(member: Member, role: OrgRole) {
    if (role === member.role) return;
    if (role === "owner") return setPending({ kind: "role", member, role });
    setMessage(null);
    try {
      await patch(member, { role }, `${personName(member)} is now ${role === "admin" ? "an admin" : "a member"}.`);
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err) });
    }
  }

  const rows = [...data.users].sort((a, b) => personName(a).localeCompare(personName(b)));
  return (
    <>
      <PageHeader
        title="Members"
        description={`${data.users.length} ${data.users.length === 1 ? "person" : "people"} in ${me.org.name}`}
        actions={
          <Button onClick={() => setInviteOpen(true)} className="h-9 px-3.5">
            <UserPlus aria-hidden="true" />
            Invite people
          </Button>
        }
      />
      <div className="mb-4 grid gap-2 empty:hidden" aria-live="polite">
        {message && <FormAlert tone={message.tone}>{message.text}</FormAlert>}
      </div>

      <section aria-label="Members" className="overflow-hidden rounded-xl border border-border bg-card">
        <div
          aria-hidden="true"
          className="hidden grid-cols-[minmax(0,2.2fr)_9rem_6rem_6rem_minmax(0,1.3fr)_2.5rem] gap-4 border-b border-border bg-muted/40 px-4 py-2.5 text-xs font-medium text-muted-foreground lg:grid"
        >
          <span>Person</span>
          <span>Role</span>
          <span>Status</span>
          <span>Two-factor</span>
          <span>Teams</span>
          <span />
        </div>
        <ul className="divide-y divide-border">
          {rows.map((member) => {
            const isSelf = member.id === me.user.id;
            const ownerLocked = member.role === "owner" && me.role !== "owner";
            const actionsLocked = isSelf || ownerLocked;
            const erasable = !isSelf && member.role !== "owner";
            return (
              <li
                key={member.id}
                data-testid={`member-${member.email}`}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-3 px-4 py-3.5 lg:grid-cols-[minmax(0,2.2fr)_9rem_6rem_6rem_minmax(0,1.3fr)_2.5rem]"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {personName(member)}
                    {isSelf && <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span>}
                  </p>
                  {member.display_name && <p className="truncate text-xs text-muted-foreground">{member.email}</p>}
                </div>
                <div className="col-start-1 row-start-2 lg:col-start-auto lg:row-start-auto">
                  <RoleControl member={member} callerRole={me.role} isSelf={isSelf} onChange={(role) => void changeRole(member, role)} />
                </div>
                <div className="col-start-1 row-start-3 flex flex-wrap items-center gap-2 lg:contents">
                  <span className="lg:block">
                    <span className="sr-only">Status: </span>
                    <StatusBadge status={member.status} />
                  </span>
                  <span className="flex items-center gap-1.5 lg:block">
                    <span className="text-xs text-muted-foreground lg:sr-only">Two-factor</span>
                    <TwoFactorBadge enabled={member.mfa_enabled} />
                  </span>
                  <span className="min-w-0 truncate text-xs text-muted-foreground lg:text-sm">
                    {member.teams.length ? member.teams.map((team) => team.name).join(", ") : <span className="lg:text-muted-foreground/70">No team</span>}
                  </span>
                </div>
                <div className="col-start-2 row-start-1 justify-self-end lg:col-start-auto lg:row-start-auto">
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon" aria-label={`Actions for ${member.email}`} disabled={actionsLocked}>
                        <MoreHorizontal aria-hidden="true" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-56">
                      <DropdownMenuItem onSelect={() => setPending({ kind: "status", member })}>
                        {member.status === "disabled" ? "Enable account" : "Disable account"}
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={!member.mfa_enabled}
                        onSelect={() => setPending({ kind: "reset-2fa", member })}
                      >
                        Reset two-factor login
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => void exportData(member)}>Export data</DropdownMenuItem>
                      {erasable && (
                        <DropdownMenuItem variant="destructive" onSelect={() => setErasing(member)}>
                          Erase data…
                        </DropdownMenuItem>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      {data.invites.length > 0 && (
        <section aria-labelledby="invites-title" className="mt-8">
          <h2 id="invites-title" className="mb-3 text-sm font-semibold">
            Pending invites
          </h2>
          <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
            {data.invites.map((invite) => (
              <li
                key={invite.id}
                data-testid={`invite-${invite.email}`}
                className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-3"
              >
                <span className="min-w-0 truncate text-sm">{invite.email}</span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="secondary">{ROLE_LABEL[invite.role]}</Badge>
                  Invited · link expires {new Date(invite.expires_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <ExportList items={exports.items ?? []} />
      <ErasureList items={erasures.items ?? []} names={erasedNames} />

      <EraseDialog
        member={erasing}
        orgName={me.org.name}
        onClose={() => setErasing(null)}
        onErased={async (member) => {
          const who = personName(member);
          setErasedNames((names) => ({ ...names, [member.id]: who }));
          setNotice(`${who} no longer has access. Their data is erased within 24 hours.`);
          await Promise.all([reload(), erasures.reload()]);
        }}
      />
      <InviteDialog
        open={inviteOpen}
        onOpenChange={setInviteOpen}
        onInvited={async (email) => {
          setNotice(`Invite sent to ${email}.`);
          await reload();
        }}
      />
      {pending && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setPending(null)}
          title={confirmTitle(pending)}
          description={confirmText(pending)}
          confirmLabel={
            pending.kind === "role"
              ? "Transfer ownership"
              : pending.kind === "status"
                ? pending.member.status === "disabled"
                  ? "Enable"
                  : "Disable"
                : "Reset"
          }
          destructive={pending.kind !== "status" || pending.member.status !== "disabled"}
          onConfirm={() => runPending(pending)}
        />
      )}
    </>
  );
}
