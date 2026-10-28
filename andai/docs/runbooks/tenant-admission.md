# Letting the munshi.ai bot into meetings: a guide for tenant administrators (draft)

The munshi.ai bot joins a meeting as an **anonymous guest** in a browser. It always shows it is
recording: its name is **munshi.ai (recording)**, or **munshi.ai - recording** on Teams, which
does not accept brackets in a guest name. After it is admitted, it posts a recording notice with a
privacy link in the meeting chat. Each platform decides whether such a guest may join and who must
let it in. This page lists the settings an administrator of the customer's Microsoft 365, Google
Workspace or Zoom account should review, and what people in the meeting see.

**Draft.** These settings come from the platforms' public documentation and announcements as of
October 2026, read during munshi.ai's research. They have not been tried in a customer tenant.
Names and defaults change: check the admin console before changing anything. Live evidence so far:
Google Meet works end to end; Teams `/meet/` links work when someone admits the bot; Zoom showed an
anti-bot wall on one account.

When a bot cannot get in, the user's meeting page in the munshi.ai portal tells them so, with the
reason:

| munshi.ai status / reason | What it usually means |
|---|---|
| `awaiting_admission` | The bot is in the lobby or waiting room; someone in the meeting must admit it. The page shows how. |
| `admission_denied` | Someone in the meeting refused it, or the platform auto-denied it. |
| `admission_timeout` | Nobody admitted it within the lobby timeout (`ANDAI_LOBBY_TIMEOUT_S`, 10 minutes by default). |
| `blocked_by_policy` | The platform refused guests or bots outright: a tenant policy, Teams' disabled "Join now", Zoom's bot wall, a captcha. |
| `auth_required` | The meeting accepts only signed-in users. |

The bot never retries on its own. A participant who removes it keeps it out of that meeting link
for 12 hours.

## Microsoft Teams

**External-bot detection.** Teams detects third-party meeting bots that join as guests. It labels
them in the lobby, and by default the organiser must approve them before they get in. This is on
by default and is rolling out worldwide through the end of October 2026 (Microsoft 365 message
center MC1251206; a later notice, MC1459141, describes the policy control). munshi.ai's bot is a
self-hosted browser guest, so it carries no Microsoft bot-identification marker. Expect it to be
labelled and held **every time** unless the policy says otherwise. The policy options reported are:

- **require approval when a bot is detected** (default): the bot waits in the lobby with a bot
  label, and the organiser admits it. munshi.ai shows this as `awaiting_admission`, with
  instructions;
- **do not detect bots / allow all bots**: the bot is treated like any other guest, and the
  lobby settings below decide.

The setting is reported as a meetings policy (PowerShell `ExternalBotAccessMode`) that cannot be
scoped to an Entra group. Review it in the Teams admin center under **Meetings → Meeting
policies**, and confirm the exact name in your tenant first.

**Anonymous join.** The bot joins without signing in, so the organiser's meeting policy must allow
**Anonymous users can join a meeting** (the org-wide switch is being retired in favour of the
per-policy one). With it off, the bot sees a sign-in page or a disabled **Join now** button.
munshi.ai reports this as `auth_required` or `blocked_by_policy` (`teams_join_disabled`). The CIS
Microsoft 365 benchmark (level 2) recommends blocking anonymous join, so security-hardened tenants
will refuse the bot. Such a tenant needs a policy exception for the organisers who use munshi.ai,
or cannot use it yet.

**Lobby.** Meeting policy **Who can bypass the lobby** (organisers can change it per meeting in
**Meeting options**). With anything narrower than **Everyone**, the bot waits in the lobby. A
presenter admits it from the **People** pane → **Waiting in lobby** → **Admit**. When anonymous
users may not start a meeting, the bot also waits until a signed-in participant joins.

**What admins and participants see.** The bot appears in the lobby as **munshi.ai - recording**,
possibly with a bot label. Once admitted, it posts the recording notice in the meeting chat. If
guests cannot chat, it says so through its name only, and munshi.ai records `bot_name_only` on the
meeting's consent record.

**Links.** Teams `/meet/<id>?p=<passcode>` links have been proven live; the classic
`l/meetup-join/...` links have not been run live yet.

## Google Meet

**Who may join.** Meet admits an anonymous guest only if the meeting lets people outside the
organisation ask to join. If the Workspace admin restricts meetings to in-domain users
(**Admin console → Apps → Google Workspace → Google Meet → Meet safety settings**, the domain and
access settings), the bot sees "You can't join this meeting". munshi.ai reports this as
`blocked_by_policy` or `meeting_not_found`.

**Admission.** With host management on, the host chooses **Meeting access**: *Open* (no one has
to ask), *Trusted* or *Restricted*. Unless access is *Open*, the bot asks to join and a host must
admit it.

**Risky-lobby screening.** Since 2026, Meet sorts join requests into confirmed and suspicious
("With potential risks") groups. Bots and unverified guests land in the second group, which the
host may need to expand. Requests left there can be denied automatically unless a host acts. The
host admits the bot from the **⋮** menu next to it → **Admit**. munshi.ai's admit instructions say
so on the meeting page. Google may also reject some display names.

**What participants see.** A guest named **munshi.ai (recording)** asking to join. After
admission, the bot posts a chat message with the notice and the privacy link. If chat is off for
participants, the notice relies on the bot's name (`bot_name_only`).

## Zoom

**Waiting room.** With the waiting room on (the account, group or meeting setting **Waiting
Room**), the bot waits until the host or a co-host admits it from **Participants → Waiting room →
Admit**. munshi.ai shows `awaiting_admission` and, if nobody admits it, `admission_timeout`.

**Authentication.** **Only authenticated users can join** (meeting security, or the account's
authentication profiles) refuses an anonymous guest. munshi.ai reports this as `auth_required`.
Keep the passcode in the link (`?pwd=`), or the bot is asked for it.

**Bots.** Zoom showed the bot a wall saying automated bots are not allowed and pointing to its
Realtime Media Streams (RTMS) API. This happened on munshi.ai's own Zoom test account in October 2026. munshi.ai
reports this as `blocked_by_policy`. It is a platform-side refusal that no meeting setting known
to us lifts. Treat Zoom joins as unproven until a canary on the deployment's Zoom test meeting
succeeds.

**Join before host.** A meeting whose host has not started it holds every guest. The bot keeps
trying for up to 10 minutes and then fails.

## Checking a tenant

1. Create a test meeting under the customer's normal settings and send the bot to it from the
   portal ("Add notetaker").
2. Watch the meeting page. It shows `awaiting_admission` with the platform's admit instructions,
   then `recording` once admitted, or a failure reason from the table above.
3. Change one setting at a time, and repeat.

For munshi.ai's own canaries (standing meetings that admit the bot with nobody present), see
`canaries.md`.
