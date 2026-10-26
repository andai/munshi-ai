"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { PageHeader } from "@/components/app/page-header";
import { RecoveryCodes, useForgetOnRestore } from "@/components/auth/recovery-codes";
import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api, retryText, sentence } from "@/lib/api";
import { useSession } from "@/lib/session";

// The person's own sign-in security: turn two-factor login on (→ /2fa/setup), replace recovery
// codes, or turn it off. Both changes need a current code; turning it off is not offered while the
// organisation requires two-factor login (andai-core refuses it then, too).

function codeError(err: unknown): string {
  if (!(err instanceof ApiError)) throw err;
  if (err.status === 400) return "That code didn't work. Use the newest code from your app, or a recovery code.";
  if (err.status === 429) return `Too many wrong codes. ${retryText(err.retryAfter)}`;
  return sentence(err.detail);
}

function CodeForm({
  id,
  submitLabel,
  destructive,
  onCode,
}: {
  id: string;
  submitLabel: string;
  destructive?: boolean;
  onCode: (code: string) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const typed = String(new FormData(event.currentTarget).get("code") ?? "").trim();
    const code = /^[0-9\s]+$/.test(typed) ? typed.replace(/\s+/g, "") : typed;
    setPending(true);
    setError(null);
    try {
      await onCode(code);
    } catch (err) {
      setError(codeError(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-3 sm:max-w-sm">
      {error && <FormAlert>{error}</FormAlert>}
      <Field
        id={id}
        name="code"
        label="Current code"
        hint="From your authenticator app, or one of your recovery codes."
        autoComplete="one-time-code"
        maxLength={64}
        inputClassName="font-mono tracking-wider"
        required
      />
      <Button
        type="submit"
        variant={destructive ? "destructive" : "default"}
        disabled={pending}
        aria-busy={pending}
        className="h-9 justify-self-start"
      >
        {submitLabel}
      </Button>
    </form>
  );
}

export function ProfileSecurity() {
  const { me, refresh } = useSession();
  const [required, setRequired] = useState<boolean | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [panel, setPanel] = useState<"none" | "regenerate" | "disable">("none");
  const [notice, setNotice] = useState<string | null>(null);

  const forget = useCallback(() => setCodes(null), []);
  useForgetOnRestore(forget);

  useEffect(() => {
    api<{ require2fa: boolean }>("/org/security")
      .then((result) => setRequired(result.require2fa))
      .catch(() => setRequired(null)); // unknown: neither the switch-off nor the note is shown
  }, []);

  async function regenerate(code: string) {
    const result = await api<{ recovery_codes: string[] }>("/me/mfa/recovery-codes", {
      method: "POST",
      body: { code },
      redirect: false,
    });
    setPanel("none");
    setNotice(null);
    setCodes(result.recovery_codes);
  }

  async function disable(code: string) {
    await api("/me/mfa", { method: "DELETE", body: { code }, redirect: false });
    setPanel("none");
    setNotice("Two-factor login is off.");
    await refresh();
  }

  const enabled = me.mfa_enabled;
  return (
    <>
      <PageHeader title="Security settings" description={me.user.email} />
      <div className="grid gap-6">
        {notice && <FormAlert tone="success">{notice}</FormAlert>}
        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              Two-factor login
              <Badge variant={enabled ? "default" : "secondary"} data-testid="mfa-status">
                {enabled ? "On" : "Off"}
              </Badge>
            </CardTitle>
            <CardDescription>
              {enabled
                ? "Signing in asks for a code from your authenticator app."
                : "Add a second step to sign-in with a code from an authenticator app."}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-5">
            {!enabled && (
              <Link
                href="/2fa/setup?from=profile"
                className={buttonVariants({ className: "h-9 justify-self-start px-4" })}
              >
                Set up two-factor login
              </Link>
            )}

            {enabled && codes && (
              <div className="grid gap-3 sm:max-w-md">
                <h3 className="text-sm font-semibold">Your new recovery codes</h3>
                <RecoveryCodes codes={codes} onDone={() => setCodes(null)} doneLabel="Done" />
              </div>
            )}

            {enabled && !codes && (
              <div className="grid gap-4">
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant={panel === "regenerate" ? "secondary" : "outline"}
                    aria-expanded={panel === "regenerate"}
                    onClick={() => setPanel(panel === "regenerate" ? "none" : "regenerate")}
                  >
                    New recovery codes
                  </Button>
                  {required === false && (
                    <Button
                      variant={panel === "disable" ? "secondary" : "outline"}
                      aria-expanded={panel === "disable"}
                      onClick={() => setPanel(panel === "disable" ? "none" : "disable")}
                    >
                      Turn off two-factor login
                    </Button>
                  )}
                </div>
                {required && (
                  <p className="text-sm text-muted-foreground" data-testid="mfa-required-note">
                    Your organisation requires two-factor login, so it can&apos;t be turned off.
                  </p>
                )}
                {panel === "regenerate" && (
                  <div className="grid gap-2 rounded-lg border border-border p-4">
                    <p className="text-sm text-muted-foreground">Your old recovery codes stop working.</p>
                    <CodeForm id="regenerate-code" submitLabel="Make new codes" onCode={regenerate} />
                  </div>
                )}
                {panel === "disable" && (
                  <div className="grid gap-2 rounded-lg border border-destructive/30 p-4">
                    <p className="text-sm text-muted-foreground">
                      Sign-in will ask only for your password. Your recovery codes stop working.
                    </p>
                    <CodeForm id="disable-code" submitLabel="Turn off" destructive onCode={disable} />
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="[--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle className="text-base">Password</CardTitle>
            <CardDescription>
              To change your password, request a reset link. Changing it signs you out on every device.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/forgot-password" className={buttonVariants({ variant: "outline", className: "h-9 px-4" })}>
              Send a reset link
            </Link>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
