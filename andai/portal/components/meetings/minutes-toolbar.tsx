"use client";

import { Download, FileText, FileType, Pencil, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { minutesExportUrl } from "@/lib/meetings";

// What a reader can do with the minutes on screen. Edit and Regenerate appear only when the page
// says the caller may change them (andai-core's `permissions.can_share`; andai-core still decides
// on every request), and are offered only when they can start now. Download gives the shown version
// as Markdown or PDF: links to andai-core's own files through the portal's same-origin proxy.

export function MinutesToolbar({
  meetingId,
  version,
  canEdit,
  canRegenerate,
  onEdit,
  onRegenerate,
}: {
  meetingId: string;
  /** The shown version; downloads give this one. Null: nothing to download yet. */
  version: number | null;
  /** Show Edit (permitted, and the shown version can be edited now). */
  canEdit: boolean;
  /** Show Regenerate (permitted, and no generation is under way). */
  canRegenerate: boolean;
  onEdit: () => void;
  onRegenerate: () => void;
}) {
  if (!canEdit && !canRegenerate && version === null) return null;
  return (
    <div className="flex flex-wrap gap-2" data-testid="minutes-toolbar">
      {canEdit && (
        <Button type="button" variant="outline" onClick={onEdit}>
          <Pencil aria-hidden="true" />
          Edit
        </Button>
      )}
      {canRegenerate && (
        <Button type="button" variant="outline" onClick={onRegenerate}>
          <RefreshCw aria-hidden="true" />
          Regenerate
        </Button>
      )}
      {version !== null && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline">
              <Download aria-hidden="true" />
              Download
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-44">
            <DropdownMenuItem asChild>
              <a href={minutesExportUrl(meetingId, "md", version)} download data-testid="download-markdown">
                <FileText aria-hidden="true" />
                Markdown (.md)
              </a>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <a href={minutesExportUrl(meetingId, "pdf", version)} download data-testid="download-pdf">
                <FileType aria-hidden="true" />
                PDF (.pdf)
              </a>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
