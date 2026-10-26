"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { SubmitButton } from "@/components/forms/submit-button";
import { buttonVariants } from "@/components/ui/button";
import { ApiError, api, sentence } from "@/lib/api";

// Forgot password, reset password (from the emailed link) and invite acceptance (set a first
// password from the invite link). andai-core judges every password; the portal only checks that
// the two fields match.

const PASSWORD_HINT = "At least 12 characters. A few unrelated words work well.";

function BackToSignIn({ label = "Back to sign in" }: { label?: string }) {
  return (
    <Link href="/sign-in" className="justify-self-center text-sm text-muted-foreground underline-offset-4 hover:underline">
      {label}
    </Link>
  );
}

function SignInButton({ notice }: { notice: string }) {
  return (
    <Link
      href={`/sign-in?notice=${notice}`}
      className={buttonVariants({ size: "lg", className: "h-10 w-full" })}
    >
      Sign in
    </Link>
  );
}

function passwordsFrom(event: FormEvent<HTMLFormElement>): { password: string; confirm: string; form: FormData } {
  const form = new FormData(event.currentTarget);
  return {
    form,
    password: String(form.get("password") ?? ""),
    confirm: String(form.get("confirm") ?? ""),
  };
}

function NewPasswordFields() {
  return (
    <>
      <Field
        id="password"
        name="password"
        label="New password"
        type="password"
        autoComplete="new-password"
        hint={PASSWORD_HINT}
        required
      />
      <Field id="confirm" name="confirm" label="Confirm new password" type="password" autoComplete="new-password" required />
    </>
  );
}

export function ForgotPasswordForm() {
  const [pending, setPending] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    setPending(true);
    setError(null);
    try {
      await api("/auth/password-reset", { method: "POST", body: { email }, redirect: false });
      setSentTo(email);
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      setError(sentence(err.detail));
    } finally {
      setPending(false);
    }
  }

  if (sentTo) {
    return (
      <AuthCard title="Check your email">
        <div className="grid gap-4">
          <FormAlert tone="success">
            If <strong className="font-medium break-all">{sentTo}</strong> has an account, a link to reset the password is
            on its way. The link works for 30 minutes.
          </FormAlert>
          <BackToSignIn />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Reset your password" description="Enter the email you sign in with and we'll send you a link.">
      <form onSubmit={onSubmit} className="grid gap-4">
        {error && <FormAlert>{error}</FormAlert>}
        <Field id="email" name="email" label="Email" type="email" autoComplete="username" inputMode="email" required autoFocus />
        <SubmitButton pending={pending}>Send reset link</SubmitButton>
        <BackToSignIn />
      </form>
    </AuthCard>
  );
}

export function ResetPasswordForm() {
  const token = useSearchParams().get("token") ?? "";
  const [pending, setPending] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [badLink, setBadLink] = useState(!token);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const { password, confirm } = passwordsFrom(event);
    if (password !== confirm) return setError("The two passwords don't match.");
    setPending(true);
    setError(null);
    try {
      await api("/auth/password-reset/confirm", { method: "POST", body: { token, password }, redirect: false });
      setDone(true);
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 400) setBadLink(true);
      else setError(sentence(err.detail));
    } finally {
      setPending(false);
    }
  }

  if (badLink) {
    return (
      <AuthCard title="This link doesn't work">
        <div className="grid gap-4">
          <FormAlert>The reset link is invalid, already used, or older than 30 minutes.</FormAlert>
          <Link href="/forgot-password" className={buttonVariants({ size: "lg", className: "h-10 w-full" })}>
            Send a new link
          </Link>
          <BackToSignIn />
        </div>
      </AuthCard>
    );
  }

  if (done) {
    return (
      <AuthCard title="Password changed">
        <div className="grid gap-4">
          <FormAlert tone="success">Your password is changed, and you&apos;re signed out on every device.</FormAlert>
          <SignInButton notice="password-changed" />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Choose a new password">
      <form onSubmit={onSubmit} className="grid gap-4">
        {error && <FormAlert>{error}</FormAlert>}
        <NewPasswordFields />
        <SubmitButton pending={pending}>Change password</SubmitButton>
      </form>
    </AuthCard>
  );
}

export function InviteForm() {
  const token = useSearchParams().get("token") ?? "";
  const [pending, setPending] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problem, setProblem] = useState<"bad-link" | "active" | null>(token ? null : "bad-link");

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const { password, confirm, form } = passwordsFrom(event);
    if (password !== confirm) return setError("The two passwords don't match.");
    const name = String(form.get("display_name") ?? "").trim();
    setPending(true);
    setError(null);
    try {
      await api("/auth/invite/accept", {
        method: "POST",
        body: { token, password, display_name: name || null },
        redirect: false,
      });
      setDone(true);
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 400) setProblem("bad-link");
      else if (err.status === 409) setProblem("active");
      else setError(sentence(err.detail));
    } finally {
      setPending(false);
    }
  }

  if (problem === "bad-link") {
    return (
      <AuthCard title="This invite doesn't work">
        <div className="grid gap-4">
          <FormAlert>The invite link is invalid, already used, or expired. Ask an admin of your organisation for a new one.</FormAlert>
          <BackToSignIn label="Go to sign in" />
        </div>
      </AuthCard>
    );
  }

  if (problem === "active") {
    return (
      <AuthCard title="You already have an account">
        <div className="grid gap-4">
          <FormAlert>This account is already active. Sign in with your password instead.</FormAlert>
          <SignInButton notice="account-ready" />
        </div>
      </AuthCard>
    );
  }

  if (done) {
    return (
      <AuthCard title="You're all set">
        <div className="grid gap-4">
          <FormAlert tone="success">Your account is ready.</FormAlert>
          <SignInButton notice="account-ready" />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Join your organisation on munshi.ai" description="Choose a password to activate your account.">
      <form onSubmit={onSubmit} className="grid gap-4">
        {error && <FormAlert>{error}</FormAlert>}
        <Field id="display_name" name="display_name" label="Your name" autoComplete="name" maxLength={200} hint="Optional. Shown to your teammates." />
        <NewPasswordFields />
        <SubmitButton pending={pending}>Activate account</SubmitButton>
      </form>
    </AuthCard>
  );
}
