"use client";

import { Lock } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, api, sentence } from "@/lib/api";
import type { OrgRole } from "@/lib/session";
import { cn } from "@/lib/utils";

// Shapes andai-core returns for the admin screens (GET /api/v1/org/users and /api/v1/teams).

export type Member = {
  id: string;
  email: string;
  display_name: string | null;
  status: "active" | "disabled" | "invited";
  role: OrgRole;
  mfa_enabled: boolean;
  teams: { id: string; name: string; role: "lead" | "member" }[];
};

export type Invite = { id: string; email: string; role: OrgRole; expires_at: string };

export type Team = {
  id: string;
  name: string;
  members: { user_id: string; email: string; display_name: string | null; role: "lead" | "member" }[];
};

export const ROLE_LABEL: Record<OrgRole, string> = { owner: "Owner", admin: "Admin", member: "Member" };

export function personName(person: { email: string; display_name: string | null }): string {
  return person.display_name?.trim() || person.email;
}

export function errorText(err: unknown): string {
  if (err instanceof ApiError) return sentence(err.detail);
  throw err;
}

/** The org's members and pending invites; a 403 here sends the browser to /403 (admins only). */
export function useOrgMembers(options: { redirect?: boolean; enabled?: boolean } = {}) {
  const [data, setData] = useState<{ users: Member[]; invites: Invite[] } | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const redirect = options.redirect ?? true;
  const enabled = options.enabled ?? true;

  const reload = useCallback(async () => {
    try {
      setData(await api<{ users: Member[]; invites: Invite[] }>("/org/users", { redirect }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setForbidden(true);
      else throw err;
    }
  }, [redirect]);

  useEffect(() => {
    if (enabled) void reload();
  }, [reload, enabled]);

  return { data, forbidden, reload };
}

export function StatusBadge({ status }: { status: Member["status"] | "invited" }) {
  if (status === "active")
    return (
      <Badge variant="outline" className="border-success/30 text-success">
        Active
      </Badge>
    );
  if (status === "disabled") return <Badge variant="destructive">Disabled</Badge>;
  return <Badge variant="secondary">Invited</Badge>;
}

export function TwoFactorBadge({ enabled }: { enabled: boolean }) {
  return enabled ? (
    <Badge variant="outline" className="border-success/30 text-success">
      On
    </Badge>
  ) : (
    <Badge variant="outline" className="text-muted-foreground">
      Off
    </Badge>
  );
}

/** A small lock with the reason a control is unavailable (the value is locked, not hidden). */
export function LockedHint({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs text-muted-foreground", className)}>
      <Lock className="size-3" aria-hidden="true" />
      {children}
    </span>
  );
}

/** A confirmation that runs an async change and stays open to show its error. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  destructive,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  async function confirm() {
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        {error && <FormAlert>{error}</FormAlert>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <Button
            variant={destructive ? "destructive" : "default"}
            disabled={pending}
            aria-busy={pending}
            onClick={() => void confirm()}
          >
            {confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
