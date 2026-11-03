# andai/portal/components/meetings

The dashboard's meeting list and search, the join form, and the meeting page.

- `meeting-list.tsx` — the dashboard's one list (D-42): Live now, Upcoming and Past, one page of
  each from `lib/meetings.ts` `listMeetings`, "Load more" per section by cursor; Past is a table
  that becomes stacked rows under the `md` breakpoint. Also `PlatformMark`.
- `meeting-filters.tsx` — All I can see / Requested by me / Shared with me, or one team (andai-core
  takes one filter at a time).
- `search-results.tsx` — keyword search results from `searchMeetings`: total, title, time and up to
  three snippets with the matched words marked (original script first, Roman Hinglish beside it).
- `join-form.tsx` — "Add notetaker" on the dashboard: link, language, team; goes to the meeting's
  page on success and shows andai-core's refusal otherwise.
- `meeting-view.tsx` — the meeting page: loads the meeting, follows it with `lib/meetings.ts`
  `subscribeLive` until it ends, then shows andai's stored copy (re-reading the meeting until
  `transcript_state` is `stored`), and lays out the parts below. Share, Delete and Stop follow the
  meeting's `permissions` from andai-core.
- `transcript-view.tsx` — a finished meeting's transcript: every stored line with an `id="seg-<id>"`
  anchor, "Original script | Roman Hinglish" (original first, D-41), and the line named by
  `#seg-<id>` or an evidence link scrolled into view and highlighted.
- `minutes-view.tsx` — the minutes (`MinutesPanel`): where generation stands from the meeting's
  `mom_state` ("Preparing minutes…", re-reading the meeting only while it is pending; a typed
  failure with Regenerate; "No minutes: nothing was said"), the shown version with its template,
  language and editor and a version menu for earlier ones, and the document laid out by the
  template's `sections`/`headings`/`labels` from andai-core (unassigned action items grouped apart,
  evidence times that show the cited line). Asks before unsaved edits are dropped.
- `minutes-toolbar.tsx` — Edit and Regenerate (for `permissions.can_share`), Download as Markdown or
  PDF (the shown version, andai-core's file through the `/api/*` proxy).
- `minutes-editor.tsx` — edit the summary and every item (owner from the attendees or unassigned,
  due date), remove and add items, cite transcript lines from a checklist of the stored lines; saves
  as the next version, offered only when every item has text and a cited line.
- `regenerate-dialog.tsx` — generate the minutes again in another template and output language.
- `attendees.tsx` — who attended (invitees, then speakers), initials and names.
- `share-dialog.tsx` — share with a person or team from the org directory (names only, no email
  entry) and remove shares. `delete-dialog.tsx` — confirm, delete, then the not-found page.
- `status-badge.tsx` (and the list's `StatusPill`), `failure-panel.tsx`, `admit-instructions.tsx`,
  `consent-note.tsx`, `live-transcript.tsx` — the live page's parts.
- `display.ts` — the words for andai-core's values (status, `JoinFailureReason`, end reason,
  platform, language, list sections, the "<Platform> meeting · <date>" title fallback, initials,
  meeting times and due dates, minutes templates, output languages and minutes failure codes), keyed
  by andai's enums.
