"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Field } from "@/components/forms/field";
import { FormAlert } from "@/components/forms/form-alert";
import { SubmitButton } from "@/components/forms/submit-button";
import { Button, buttonVariants } from "@/components/ui/button";
import { ApiError, api, retryText, sentence } from "@/lib/api";
import { signOut } from "@/lib/session";

type Mode = "totp" | "recovery";

export function TwoFactorForm() {
  const [mode, setMode] = useState<Mode>("totp");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The attempt has ended (too many wrong codes, or the pending sign-in expired): sign in again.
  const [ended, setEnded] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const typed = String(new FormData(event.currentTarget).get("code") ?? "").trim();
    const code = mode === "totp" ? typed.replace(/\s+/g, "") : typed;
    setPending(true);
    setError(null);
    try {
      await api("/auth/mfa/verify", { method: "POST", body: { code }, redirect: false });
      window.location.assign("/");
    } catch (err) {
      setPending(false);
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 401 && err.nextStep === "mfa_verify") {
        setError(
          mode === "totp"
            ? "That code didn't work. Check your authenticator app and try again."
            : "That recovery code didn't work. Check it and try again.",
        );
      } else if (err.status === 401 && err.nextStep === "login") {
        setEnded("Too many wrong codes, so this sign-in attempt has ended. Sign in again to continue.");
      } else if (err.status === 401) {
        setEnded("This sign-in attempt has expired. Sign in again to continue.");
      } else if (err.status === 429) {
        setError(`Too many wrong codes. ${retryText(err.retryAfter)}`);
      } else {
        setError(sentence(err.detail));
      }
    }
  }

  if (ended) {
    return (
      <div className="grid gap-4">
        <FormAlert>{ended}</FormAlert>
        <Link href="/sign-in" className={buttonVariants({ size: "lg", className: "h-10 w-full" })}>
          Sign in again
        </Link>
      </div>
    );
  }

  const totp = mode === "totp";
  return (
    <form onSubmit={onSubmit} className="grid gap-4">
      {error && <FormAlert>{error}</FormAlert>}
      <Field
        key={mode}
        id="code"
        name="code"
        label={totp ? "Authentication code" : "Recovery code"}
        hint={totp ? "The 6-digit code from your authenticator app." : "One of the codes you saved, like abcde-fghjk."}
        autoComplete={totp ? "one-time-code" : "off"}
        inputMode={totp ? "numeric" : "text"}
        pattern={totp ? "[0-9 ]{6,7}" : undefined}
        maxLength={totp ? 7 : 64}
        spellCheck={false}
        inputClassName="font-mono tracking-wider"
        required
        autoFocus
      />
      <SubmitButton pending={pending}>Verify</SubmitButton>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <Button
          type="button"
          variant="link"
          className="h-auto px-0"
          onClick={() => {
            setMode(totp ? "recovery" : "totp");
            setError(null);
          }}
        >
          {totp ? "Use a recovery code" : "Use your authenticator app"}
        </Button>
        <Button type="button" variant="link" className="h-auto px-0 text-muted-foreground" onClick={() => void signOut()}>
          Use a different account
        </Button>
      </div>
    </form>
  );
}
