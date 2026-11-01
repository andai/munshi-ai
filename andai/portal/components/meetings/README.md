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
- `minutes-view.tsx` — the latest minutes, read-only: summary, decisions, action items (unassigned
  ones grouped apart), topics and risks, "None recorded" for an empty section, and evidence times
  that show the cited line.
- `attendees.tsx` — who attended (invitees, then speakers), initials and names.
- `share-dialog.tsx` — share with a person or team from the org directory (names only, no email
  entry) and remove shares. `delete-dialog.tsx` — confirm, delete, then the not-found page.
- `status-badge.tsx` (and the list's `StatusPill`), `failure-panel.tsx`, `admit-instructions.tsx`,
  `consent-note.tsx`, `live-transcript.tsx` — the live page's parts.
- `display.ts` — the words for andai-core's values (status, `JoinFailureReason`, end reason,
  platform, language, list sections, the "<Platform> meeting · <date>" title fallback, initials,
  meeting times and due dates), keyed by andai's enums.
