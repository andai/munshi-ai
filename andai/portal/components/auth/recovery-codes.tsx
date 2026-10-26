"use client";

import { Check, Copy, Download } from "lucide-react";
import { useEffect, useState } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";

// Recovery codes are shown exactly once, from the response that made them. They live only in the
// state of the component showing them: nothing writes them to storage, and the parent drops them
// when the person continues. A page restored from the back/forward cache drops them too.

export function useForgetOnRestore(forget: () => void): void {
  useEffect(() => {
    const onShow = (event: PageTransitionEvent) => {
      if (event.persisted) forget();
    };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, [forget]);
}

export function RecoveryCodes({
  codes,
  onDone,
  doneLabel = "Continue",
}: {
  codes: string[];
  onDone: () => void;
  doneLabel?: string;
}) {
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const text = codes.join("\n") + "\n";

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      setCopyFailed(true);
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "munshi-recovery-codes.txt";
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="grid gap-4">
      <p className="text-sm text-muted-foreground">
        Each code works once, if you lose your authenticator. Keep them somewhere safe — you won&apos;t see
        them again.
      </p>
      <ol
        aria-label="Recovery codes"
        data-testid="recovery-codes"
        className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-lg border border-border bg-muted/50 p-4 font-mono text-sm tracking-wide"
      >
        {codes.map((code) => (
          <li key={code} className="tabular-nums">
            {code}
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={() => void copy()}>
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? "Copied" : "Copy"}
        </Button>
        <Button type="button" variant="outline" onClick={download}>
          <Download aria-hidden="true" />
          Download
        </Button>
      </div>
      {copyFailed && <FormAlert>Copying isn&apos;t allowed here. Select the codes, or download them.</FormAlert>}
      <div className="flex items-start gap-2.5">
        <Checkbox id="codes-saved" checked={saved} onCheckedChange={(value) => setSaved(value === true)} />
        <Label htmlFor="codes-saved" className="leading-snug font-normal">
          I&apos;ve saved my recovery codes
        </Label>
      </div>
      <Button type="button" size="lg" className="h-10 w-full" disabled={!saved} onClick={onDone}>
        {doneLabel}
      </Button>
    </div>
  );
}
