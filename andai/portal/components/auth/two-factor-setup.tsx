"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { RecoveryCodes, useForgetOnRestore } from "@/components/auth/recovery-codes";
import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { SubmitButton } from "@/components/forms/submit-button";
import { Button, buttonVariants } from "@/components/ui/button";
import { ApiError, api, destinationFor, sentence } from "@/lib/api";
import { signOut } from "@/lib/session";

// Two-factor enrollment, for both ways in: a signed-in person turning it on from their security
// settings, and a person whose organisation requires it (their session can reach nothing else).
// The secret, QR code and recovery codes come from andai-core once and stay in this component.

type Enrollment = { otpauth_uri: string; qr_svg: string };
type Step =
  | { kind: "loading" }
  | { kind: "scan"; enrollment: Enrollment }
  | { kind: "codes"; codes: string[] }
  | { kind: "already" }
  | { kind: "failed"; message: string };

function secretOf(uri: string): string {
  try {
    return new URL(uri).searchParams.get("secret") ?? "";
  } catch {
    return "";
  }
}

function grouped(secret: string): string {
  return secret.replace(/(.{4})/g, "$1 ").trim();
}

function qrSource(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export function TwoFactorSetup() {
  const fromProfile = useSearchParams().get("from") === "profile";
  const [step, setStep] = useState<Step>({ kind: "loading" });
  const [required, setRequired] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  const forget = useCallback(() => setStep({ kind: "loading" }), []);
  useForgetOnRestore(forget);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void (async () => {
      // A session the organisation has sent to enrollment can reach only /me/mfa*; its /me answers
      // with next_step "mfa_enroll". That is how this page knows enrollment is required. A full
      // session's /me says whether two-factor login is already on.
      try {
        const me = await api<{ mfa_enabled: boolean }>("/me", { redirect: false });
        if (me.mfa_enabled) return setStep({ kind: "already" });
      } catch (err) {
        if (err instanceof ApiError && err.nextStep === "mfa_enroll") setRequired(true);
      }
      try {
        const enrollment = await api<Enrollment>("/me/mfa", { method: "POST", redirect: false });
        setStep({ kind: "scan", enrollment });
      } catch (err) {
        if (!(err instanceof ApiError)) throw err;
        if (err.status === 409) return setStep({ kind: "already" });
        const destination = destinationFor(err, "GET");
        if (destination && destination !== "/403") return window.location.assign(destination);
        setStep({ kind: "failed", message: sentence(err.detail) });
      }
    })();
  }, []);

  async function onConfirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = String(new FormData(event.currentTarget).get("code") ?? "").replace(/\s+/g, "");
    setPending(true);
    setError(null);
    try {
      const result = await api<{ recovery_codes: string[] }>("/me/mfa/confirm", {
        method: "POST",
        body: { code },
        redirect: false,
      });
      setStep({ kind: "codes", codes: result.recovery_codes });
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 400) setError("That code didn't work. Check the time on your phone and try the newest code.");
      else if (err.status === 401) window.location.assign(destinationFor(err, "POST") ?? "/sign-in");
      else setError(sentence(err.detail));
    } finally {
      setPending(false);
    }
  }

  function finish() {
    setStep({ kind: "loading" });
    window.location.assign(fromProfile ? "/profile/security" : "/");
  }

  if (step.kind === "loading") {
    return (
      <AuthCard title="Set up two-factor login">
        <p className="text-sm text-muted-foreground" role="status">
          Preparing your setup…
        </p>
      </AuthCard>
    );
  }

  if (step.kind === "already") {
    return (
      <AuthCard title="Two-factor login is on" description="Your account already asks for a code when you sign in.">
        <Link href="/profile/security" className={buttonVariants({ size: "lg", className: "h-10 w-full" })}>
          Go to security settings
        </Link>
      </AuthCard>
    );
  }

  if (step.kind === "failed") {
    return (
      <AuthCard title="Set up two-factor login">
        <FormAlert>{step.message}</FormAlert>
      </AuthCard>
    );
  }

  if (step.kind === "codes") {
    return (
      <AuthCard title="Save your recovery codes" description="Two-factor login is now on for your account.">
        <RecoveryCodes codes={step.codes} onDone={finish} />
      </AuthCard>
    );
  }

  const secret = secretOf(step.enrollment.otpauth_uri);
  return (
    <AuthCard
      title="Set up two-factor login"
      description={
        required
          ? "Your organisation requires two-factor login. Set it up to continue."
          : "Use an authenticator app such as Google Authenticator, Microsoft Authenticator or Authy."
      }
    >
      <div className="grid gap-5">
        <ol className="grid gap-1 text-sm text-muted-foreground">
          <li>1. Scan this QR code with your authenticator app.</li>
          <li>2. Enter the 6-digit code the app shows.</li>
        </ol>
        <div className="mx-auto rounded-xl bg-white p-3 shadow-sm ring-1 ring-foreground/10">
          {/* eslint-disable-next-line @next/next/no-img-element -- a data: URL from the API response */}
          <img
            src={qrSource(step.enrollment.qr_svg)}
            alt="QR code for your authenticator app"
            width={176}
            height={176}
            className="size-44"
          />
        </div>
        {secret && (
          <details className="group rounded-lg border border-border px-3 py-2 text-sm">
            <summary className="cursor-pointer font-medium text-primary">Can&apos;t scan it? Enter a key instead</summary>
            <p className="mt-2 text-muted-foreground">Choose a time-based key in your app and enter:</p>
            <code data-testid="totp-secret" className="mt-1 block font-mono text-sm tracking-wider break-all">
              {grouped(secret)}
            </code>
          </details>
        )}
        <form onSubmit={onConfirm} className="grid gap-4">
          {error && <FormAlert>{error}</FormAlert>}
          <Field
            id="code"
            name="code"
            label="Code from the app"
            autoComplete="one-time-code"
            inputMode="numeric"
            pattern="[0-9 ]{6,7}"
            maxLength={7}
            inputClassName="font-mono tracking-wider"
            required
          />
          <SubmitButton pending={pending}>Turn on two-factor login</SubmitButton>
        </form>
        {required ? (
          <Button type="button" variant="link" className="h-auto justify-self-center text-muted-foreground" onClick={() => void signOut()}>
            Sign out
          </Button>
        ) : (
          <Link href="/profile/security" className="justify-self-center text-sm text-muted-foreground underline-offset-4 hover:underline">
            Not now
          </Link>
        )}
      </div>
    </AuthCard>
  );
}
