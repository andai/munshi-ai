"use client";

import { useEffect, useState } from "react";

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
import { Button } from "@/components/ui/button";
import { ApiError, sentence } from "@/lib/api";
import { deleteMeeting } from "@/lib/meetings";

// Delete a meeting after a confirmation. andai-core hides it from everyone at once and purges its
// audio, transcript and minutes within 15 minutes; a refusal (the bot is still in the meeting, or
// the meeting is already gone) stays in the dialog in andai-core's words.

export function DeleteDialog({
  meetingId,
  open,
  onOpenChange,
  onDeleted,
}: {
  meetingId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleted: () => void;
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
      await deleteMeeting(meetingId);
      onOpenChange(false);
      onDeleted();
    } catch (err) {
      setError(err instanceof ApiError ? sentence(err.detail) : "Could not delete the meeting. Try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent data-testid="delete-dialog">
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this meeting?</AlertDialogTitle>
          <AlertDialogDescription>
            The audio, transcript and every version of the minutes are removed for everyone within 15 minutes. This can&apos;t be
            undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && <FormAlert>{error}</FormAlert>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Keep it</AlertDialogCancel>
          <Button variant="destructive" disabled={pending} aria-busy={pending} onClick={() => void confirm()}>
            {pending ? "Deleting…" : "Delete meeting"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
