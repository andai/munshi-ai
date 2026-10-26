"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { SubmitButton } from "@/components/forms/submit-button";
import { ApiError, api, retryText, sentence } from "@/lib/api";

// Where each login stage continues (andai-core decides the stage).
const STAGE_PAGES: Record<string, string> = {
  full: "/",
  mfa_pending: "/2fa",
  enroll_required: "/2fa/setup",
};

const NOTICES: Record<string, string> = {
  "signed-out": "You're signed out.",
  "password-changed": "Your password is changed. Sign in with the new one.",
  "account-ready": "Your account is ready. Sign in to continue.",
};

export function SignInForm() {
  const notice = NOTICES[useSearchParams().get("notice") ?? ""];
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError(null);
    try {
      const result = await api<{ stage: string }>("/auth/login", {
        method: "POST",
        body: { email: String(form.get("email") ?? ""), password: String(form.get("password") ?? "") },
        redirect: false,
      });
      window.location.assign(STAGE_PAGES[result.stage] ?? "/");
    } catch (err) {
      setPending(false);
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 401) setError("The email or password is not right.");
      else if (err.status === 429) setError(`Too many sign-in attempts. ${retryText(err.retryAfter)}`);
      else setError(sentence(err.detail));
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-4" noValidate={false}>
      {notice && !error && <FormAlert tone="success">{notice}</FormAlert>}
      {error && <FormAlert>{error}</FormAlert>}
      <Field
        id="email"
        name="email"
        label="Email"
        type="email"
        autoComplete="username"
        inputMode="email"
        required
        autoFocus
      />
      <Field
        id="password"
        name="password"
        label="Password"
        type="password"
        autoComplete="current-password"
        required
        action={
          <Link
            href="/forgot-password"
            className="text-xs font-medium text-primary underline-offset-4 hover:underline"
          >
            Forgot password?
          </Link>
        }
      />
      <SubmitButton pending={pending}>Sign in</SubmitButton>
    </form>
  );
}
