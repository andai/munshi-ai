"""The recap mail of a meeting's first minutes (MOM-05, D-48).

When a meeting's first minutes version is stored, the minutes job sets `recap_state = pending`
(`minutes_job.py`); a regenerated or edited version never does. andai-worker claims due recaps
through `andai_claim_recap()` (migration 0010), which leases each one with a fresh token for two
minutes, and calls `send_one` for it:

* **Audience** (`recipients`) — the meeting's requester and the members of the meeting's team,
  each only while the user is `active` and a member of the meeting's org, deduplicated, requester
  first. Nobody else: no attendee (andai holds no attendee addresses), no grantee outside the team.
  A meeting without a team goes to the requester alone. Until Phase 8 this audience is fixed (D-48).
* **Mail** (`body`, `subject`) — plain text in the minutes' output language with the template's
  headings: the summary, the decisions and the action items (owner, due date), "None recorded" for
  an empty section, and a link to the meeting page on the portal (`/meetings/<id>`). The recap is
  always version 1, the minutes as first generated. It is one message to all recipients, so the
  relay accepts or refuses it as a whole.
* **Send once** — `send_one` locks the meeting row and re-checks the lease, sends the mail, then
  marks the recap `sent` in the same transaction; the mail leaves before that transaction commits
  (as invites do). A crash between the relay accepting the mail and the commit can send it again
  (at-least-once); nothing else does. A relay error (`MailError`) counts an attempt and retries
  after the backoff (30 s, doubling, at most 30 min), as does any other error of the run; the
  fifth ends `failed`. A deleted meeting,
  a meeting without minutes or without any recipient ends `skipped`.

The function never raises for a meeting's own failure; it returns the recap's state afterwards, or
`lost` when the run no longer held the meeting and changed nothing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import anyio.from_thread
import anyio.to_thread
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Tx

from andai_core.auth.csrf import normalize_origin
from andai_core.db.models import Meeting, MomDocument, OrgMembership, TeamMembership, User
from andai_core.db.session import transaction
from andai_core.mail import Mail, Mailer, MailError
from andai_core.meetings.ingest import backoff
from andai_core.meetings.mom import Document
from andai_core.meetings.mom_writer import DEFAULT_TIMEZONE, local_date
from andai_core.minutes import templates
from andai_core.minutes.owners import UNASSIGNED

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RECAP_VERSION = 1

# The words of the mail around the template's headings, per output language.
WORDS: dict[str, dict[str, str]] = {
    "en": {
        "subject": "Minutes: {name}",
        "untitled": "meeting of {date}",
        "intro": "The minutes of {name} are ready.",
        "owner": "Owner",
        "due": "due",
        "link": "Read the full minutes and the transcript in munshi.ai:",
        "why": ("You get this mail because you sent munshi.ai to this meeting or are on its team."),
    },
    "hi": {
        "subject": "कार्यवृत्त: {name}",
        "untitled": "{date} की मीटिंग",
        "intro": "{name} का कार्यवृत्त तैयार है।",
        "owner": "ज़िम्मेदार",
        "due": "नियत तिथि",
        "link": "पूरा कार्यवृत्त और ट्रांसक्रिप्ट munshi.ai में देखें:",
        "why": (
            "यह मेल आपको इसलिए मिला क्योंकि आपने munshi.ai को इस मीटिंग में भेजा था या आप इसकी टीम में हैं।"
        ),
    },
    "hinglish": {
        "subject": "Minutes: {name}",
        "untitled": "{date} ki meeting",
        "intro": "{name} ke minutes taiyaar hain.",
        "owner": "Owner",
        "due": "due",
        "link": "Poore minutes aur transcript munshi.ai par dekhein:",
        "why": (
            "Yeh mail aapko isliye mila kyunki aapne munshi.ai ko is meeting mein bheja tha ya "
            "aap iski team mein hain."
        ),
    },
}


def utcnow() -> datetime:
    return datetime.now(UTC)


# ─── audience ─────────────────────────────────────────────────────────────────────────────────────


def recipients(tx: Tx, meeting: Meeting) -> list[str]:
    """The addresses the recap goes to: requester, then the team's members (see the module
    docstring), each once."""
    ids: list[uuid.UUID] = [meeting.requested_by]
    if meeting.team_id is not None:
        ids += tx.scalars(
            select(TeamMembership.user_id)
            .where(
                TeamMembership.team_id == meeting.team_id,
                TeamMembership.org_id == meeting.org_id,
            )
            .order_by(TeamMembership.created_at, TeamMembership.user_id)
        ).all()
    active = dict(
        tx.execute(
            select(User.id, User.email)
            .join(
                OrgMembership,
                (OrgMembership.user_id == User.id) & (OrgMembership.org_id == meeting.org_id),
            )
            .where(User.id.in_(ids), User.status == "active")
        ).all()
    )
    out: list[str] = []
    for user_id in ids:
        email = active.get(user_id)
        if email and email not in out:
            out.append(email)
    return out


# ─── the mail ─────────────────────────────────────────────────────────────────────────────────────


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _name(meeting: Meeting, words: dict[str, str], meeting_date: date) -> str:
    title = _one_line(meeting.title or "")
    return title or words["untitled"].format(date=meeting_date.isoformat())


def meeting_link(portal_origin: str, meeting_id: uuid.UUID) -> str:
    return f"{normalize_origin(portal_origin)}/meetings/{meeting_id}"


def subject(meeting: Meeting, doc: Document, meeting_date: date) -> str:
    words = WORDS[doc.output_language]
    return words["subject"].format(name=_name(meeting, words, meeting_date))


def body(meeting: Meeting, doc: Document, portal_link: str, meeting_date: date) -> str:
    """The plain-text recap of `doc` in its output language."""
    language = doc.output_language
    words = WORDS[language]
    try:
        template = templates.get(doc.template_id)
    except ValueError:
        template = templates.get(templates.DEFAULT_TEMPLATE)
    none = templates.NONE_RECORDED[language]

    def section(name: templates.Section, lines: list[str]) -> str:
        heading = template.heading(name, language)
        return f"{heading}\n{'-' * len(heading)}\n" + ("\n".join(lines) if lines else none)

    actions = []
    for item in doc.action_items:
        owner = templates.UNASSIGNED_LABEL[language] if item.owner == UNASSIGNED else item.owner
        detail = f"{words['owner']}: {owner}"
        if item.due is not None:
            detail += f"; {words['due']} {item.due.isoformat()}"
        actions.append(f"- {_one_line(item.text)} ({detail})")
    parts = [
        words["intro"].format(name=_name(meeting, words, meeting_date)),
        section("summary", [doc.summary.strip()] if doc.summary.strip() else []),
        section("decisions", [f"- {_one_line(d.text)}" for d in doc.decisions]),
        section("action_items", actions),
        f"{words['link']}\n{portal_link}",
        f"-- \n{words['why']}",
    ]
    return "\n\n".join(parts) + "\n"


# ─── send once ────────────────────────────────────────────────────────────────────────────────────


def _held(tx: Tx, meeting_id: uuid.UUID, lease: uuid.UUID) -> Meeting | None:
    """The meeting under its row lock while its recap is pending under `lease`."""
    meeting = tx.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
    if meeting is None or meeting.recap_state != "pending" or meeting.recap_lease != lease:
        return None
    return meeting


def _end(meeting: Meeting, state: str, now: datetime | None = None) -> str:
    meeting.recap_state = state
    meeting.recap_lease = None
    meeting.recap_next_at = None
    if state == "sent":
        meeting.recap_at = now
    return state


def _send(
    engine: Engine,
    mailer: Mailer,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    portal_origin: str,
    timezone: str,
    now: datetime,
) -> str:
    with transaction(engine, org_id=org_id) as tx:
        meeting = _held(tx, meeting_id, lease)
        if meeting is None:
            return "lost"
        if meeting.deleted_at is not None or meeting.deletion_state != "none":
            return _end(meeting, "skipped")
        row = tx.scalar(
            select(MomDocument).where(
                MomDocument.meeting_id == meeting.id, MomDocument.version == RECAP_VERSION
            )
        )
        to = recipients(tx, meeting)
        if row is None or not to:
            log.info("meeting %s: no minutes or no recipient, recap skipped", meeting_id)
            return _end(meeting, "skipped")
        doc = Document.model_validate(row.body)
        meeting_date = local_date(meeting, timezone)
        mail = Mail(
            to=", ".join(to),
            subject=subject(meeting, doc, meeting_date),
            text=body(meeting, doc, meeting_link(portal_origin, meeting.id), meeting_date),
        )
        anyio.from_thread.run(mailer.send, mail)  # before the commit, as invites do
        log.info("meeting %s: recap sent to %d recipient(s)", meeting_id, len(to))
        return _end(meeting, "sent", now)


def _retry(engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, lease: uuid.UUID, now) -> str:
    with transaction(engine, org_id=org_id) as tx:
        meeting = _held(tx, meeting_id, lease)
        if meeting is None:
            return "lost"
        attempts = meeting.recap_attempts + 1
        meeting.recap_attempts = attempts
        if attempts >= MAX_ATTEMPTS:
            return _end(meeting, "failed")
        meeting.recap_lease = None
        meeting.recap_next_at = now + backoff(attempts)
        return "pending"


async def send_one(
    engine: Engine,
    mailer: Mailer,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    *,
    portal_origin: str,
    timezone: str = DEFAULT_TIMEZONE,
    clock: Callable[[], datetime] = utcnow,
) -> str:
    """Send one claimed meeting's recap; returns `sent`, `pending` (retry), `failed`, `skipped`
    or `lost`."""
    now = clock()
    try:
        return await anyio.to_thread.run_sync(
            lambda: _send(engine, mailer, org_id, meeting_id, lease, portal_origin, timezone, now)
        )
    except MailError:
        log.warning("meeting %s: the relay did not accept the recap", meeting_id)
    except Exception:  # noqa: BLE001 - a meeting's failure is counted, never raised
        log.exception("meeting %s: recap failed", meeting_id)
    return await anyio.to_thread.run_sync(
        lambda: _retry(engine, org_id, meeting_id, lease, clock())
    )


__all__ = ["MAX_ATTEMPTS", "body", "meeting_link", "recipients", "send_one", "subject"]
