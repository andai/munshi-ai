"use client";

import { useEffect, useState } from "react";

import {
  ConfirmDialog,
  LockedHint,
  TwoFactorBadge,
  personName,
  useOrgMembers,
  type Member,
} from "@/components/admin/shared";
import { PageHeader } from "@/components/app/page-header";
import { FormAlert } from "@/components/forms/form-alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

// Organisation security: the require-two-factor switch, and resetting a member's two-factor login
// when they lose their device. The page reads the member list, which only admins may (a 403 sends
// the browser to /403). Turning the requirement on ends the sessions of members without two-factor
// login — the admin's own included, when they have not set it up — so the confirmation says who.

export function OrgSecurity() {
  const { me, refresh } = useSession();
  const { data, reload } = useOrgMembers();
  const [required, setRequired] = useState<boolean | null>(null);
  const [confirming, setConfirming] = useState<boolean | null>(null);
  const [resetting, setResetting] = useState<Member | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api<{ require2fa: boolean }>("/org/security")
      .then((result) => setRequired(result.require2fa))
      .catch(() => setNotice(null)); // the page stays on its loading state; a 401/403 has already navigated
  }, []);

  if (!data || required === null) {
    return (
      <>
        <PageHeader title="Security" />
        <p role="status" className="text-sm text-muted-foreground">
          Loading security settings…
        </p>
      </>
    );
  }

  const withoutTwoFactor = data.users.filter((member) => member.status === "active" && !member.mfa_enabled);
  const selfWithout = withoutTwoFactor.some((member) => member.id === me.user.id);

  async function setRequirement(next: boolean) {
    await api<{ require2fa: boolean }>("/org/security", { method: "PUT", body: { require2fa: next }, redirect: false });
    setRequired(next);
    setNotice(next ? "Two-factor login is now required." : "Two-factor login is no longer required.");
    // When the requirement just ended this admin's own session, /me answers 401 and the shell
    // follows it to sign-in.
    await refresh();
    await reload();
  }

  const rows = [...data.users].sort((a, b) => personName(a).localeCompare(personName(b)));
  return (
    <>
      <PageHeader title="Security" description={`Sign-in rules for ${me.org.name}`} />
      <div className="grid gap-6">
        {notice && <FormAlert tone="success">{notice}</FormAlert>}
        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="text-base">Require two-factor login</CardTitle>
            <CardDescription className="max-w-prose">
              Everyone signs in with a password and a code from an authenticator app. Members who haven&apos;t set it up
              are signed out when you turn this on, and must set it up at their next sign-in.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            <Switch
              id="require-2fa"
              checked={required}
              onCheckedChange={(checked) => setConfirming(checked)}
            />
            <Label htmlFor="require-2fa" className="font-normal">
              {required ? "Required for everyone" : "Not required"}
            </Label>
            {!required && withoutTwoFactor.length > 0 && (
              <span className="text-xs text-muted-foreground" data-testid="without-2fa-count">
                {withoutTwoFactor.length} of {data.users.length} {data.users.length === 1 ? "person has" : "people have"} not set it up
              </span>
            )}
          </CardContent>
        </Card>

        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="text-base">Members&apos; two-factor login</CardTitle>
            <CardDescription>Reset it for someone who lost their phone. They are signed out everywhere.</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-border">
              {rows.map((member) => {
                const isSelf = member.id === me.user.id;
                const ownerLocked = member.role === "owner" && me.role !== "owner";
                return (
                  <li
                    key={member.id}
                    data-testid={`security-${member.email}`}
                    className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3"
                  >
                    <span className="min-w-0 flex-1 basis-48">
                      <span className="block truncate text-sm font-medium">{personName(member)}</span>
                      {member.display_name && <span className="block truncate text-xs text-muted-foreground">{member.email}</span>}
                    </span>
                    <TwoFactorBadge enabled={member.mfa_enabled} />
                    <span className="flex w-full justify-end sm:w-44">
                      {isSelf ? (
                        <LockedHint>Change yours in security settings</LockedHint>
                      ) : ownerLocked && member.mfa_enabled ? (
                        <LockedHint>Only the owner can reset this</LockedHint>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={!member.mfa_enabled}
                          onClick={() => setResetting(member)}
                          aria-label={`Reset two-factor login for ${member.email}`}
                        >
                          Reset
                        </Button>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          </CardContent>
        </Card>
      </div>

      <ConfirmDialog
        open={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={confirming ? "Require two-factor login?" : "Stop requiring two-factor login?"}
        description={
          confirming
            ? withoutTwoFactor.length === 0
              ? "Everyone has set it up already, so nobody is signed out."
              : `${withoutTwoFactor.length} ${withoutTwoFactor.length === 1 ? "person is" : "people are"} signed out now and must set it up at their next sign-in${selfWithout ? " — including you" : ""}.`
            : "People can turn their own two-factor login off again. Nobody is signed out."
        }
        confirmLabel={confirming ? "Require it" : "Stop requiring it"}
        destructive={false}
        onConfirm={() => setRequirement(confirming === true)}
      />
      {resetting && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setResetting(null)}
          title={`Reset two-factor login for ${personName(resetting)}?`}
          description="Their authenticator and recovery codes stop working, and they are signed out everywhere."
          confirmLabel="Reset"
          destructive
          onConfirm={async () => {
            await api(`/org/users/${resetting.id}/mfa`, { method: "DELETE", redirect: false });
            setNotice(`Two-factor login is reset for ${personName(resetting)}.`);
            await reload();
          }}
        />
      )}
    </>
  );
}
