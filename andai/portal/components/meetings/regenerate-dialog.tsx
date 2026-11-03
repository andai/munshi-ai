"use client";

import { useEffect, useState } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { OUTPUT_LANGUAGE_NAME, TEMPLATE_NAME } from "@/components/meetings/display";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, sentence } from "@/lib/api";
import { regenerateMinutes, type OutputLanguage } from "@/lib/meetings";

// Generate the minutes again, as a new version, in a template and output language picked here (the
// shown version's are preselected). andai-core queues it and answers at once; the page then shows
// the minutes as being prepared until the new version is stored. A refusal (another regenerate is
// still waiting, the transcript is not stored) stays in the dialog in andai-core's words.

const LANGUAGES = Object.keys(OUTPUT_LANGUAGE_NAME) as OutputLanguage[];

export function RegenerateDialog({
  meetingId,
  templateId,
  outputLanguage,
  open,
  onOpenChange,
  onRequested,
}: {
  meetingId: string;
  templateId: string;
  outputLanguage: OutputLanguage;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRequested: () => void;
}) {
  const [template, setTemplate] = useState(templateId);
  const [language, setLanguage] = useState<OutputLanguage>(outputLanguage);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setTemplate(templateId in TEMPLATE_NAME ? templateId : "standard");
    setLanguage(outputLanguage);
    setError(null);
  }, [open, templateId, outputLanguage]);

  async function submit() {
    setPending(true);
    setError(null);
    try {
      await regenerateMinutes(meetingId, { templateId: template, outputLanguage: language });
      onOpenChange(false);
      onRequested();
    } catch (err) {
      setError(err instanceof ApiError ? sentence(err.detail) : "Could not ask for new minutes. Try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent data-testid="regenerate-dialog">
        <DialogHeader>
          <DialogTitle>Regenerate the minutes</DialogTitle>
          <DialogDescription>
            The minutes are written again from the transcript as a new version. Earlier versions stay in the version menu.
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="regenerate-template">Template</Label>
            <Select value={template} onValueChange={setTemplate}>
              <SelectTrigger id="regenerate-template" className="h-10 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(TEMPLATE_NAME).map(([id, name]) => (
                  <SelectItem key={id} value={id}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="regenerate-language">Output language</Label>
            <Select value={language} onValueChange={(value) => setLanguage(value as OutputLanguage)}>
              <SelectTrigger id="regenerate-language" className="h-10 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {LANGUAGES.map((code) => (
                  <SelectItem key={code} value={code}>
                    {OUTPUT_LANGUAGE_NAME[code]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {error && <FormAlert>{error}</FormAlert>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={pending} onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending} aria-busy={pending}>
              {pending ? "Asking…" : "Regenerate"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
