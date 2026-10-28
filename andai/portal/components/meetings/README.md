# andai/portal/components/meetings

The join form and the meeting page.

- `join-form.tsx` — "Add notetaker" on the dashboard: link, language, team; goes to the meeting's
  page on success and shows andai-core's refusal otherwise.
- `meeting-view.tsx` — the meeting page: loads the meeting, follows it with `lib/meetings.ts`
  `subscribeLive` until it ends, and lays out the parts below.
- `status-badge.tsx`, `failure-panel.tsx`, `admit-instructions.tsx`, `consent-note.tsx`,
  `live-transcript.tsx` — the page's parts.
- `display.ts` — the words for andai-core's values (status, `JoinFailureReason`, end reason,
  platform, language), keyed by andai's enums.
