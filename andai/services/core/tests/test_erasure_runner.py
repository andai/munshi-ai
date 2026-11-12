"""The erasure step machine (`andai_core.erasure.runner`) on Postgres with the stub platform: a
member's live meeting is stopped first, their meetings go through the meeting purge (platform
first, once each), references in the org's other rows point at the tombstone user, what they
received or sent is deleted, their account is anonymised only when no membership remains, the audit
trail keeps their id, failures retry and end `failed`, and a finished request is never run again.

Every test runs on a brand-new database, so nothing else's rows are in the way.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, text

from andai_core.db.models import (
    TOMBSTONE_USER_ID,
    BotSetting,
    ConsentRecord,
    InviteEvent,
    MeetingShare,
    PasswordReset,
    RecoveryCode,
    UserTotp,
)
from andai_core.db.session import transaction
from andai_core.erasure import runner
from andai_core.erasure.registry import ERASURE_RULES
from andai_core.erasure.requests import create_request, remove_from_org
from andai_core.meetings import deletion, mom, scheduled
from andai_core.meetings.service import Meetings
from conftest import (
    Harness,
    Member,
    give_principal,
    make_harness,
    mom_body,
    new_member,
    seed_meeting,
    seed_segments,
    sign_in,
    staged_session,
)


def _platform_meeting(harness: Harness, key: str, status: str = "completed") -> tuple[int, str]:
    """A meeting of the org whose platform key is `key`, in the stub platform, with a transcript
    and stored objects; returns (platform meeting id, native id)."""
    gateway = harness.gateway
    [platform_user] = [
        t["user_id"] for t in harness.admin_api.tokens.values() if t["token"] == key
    ]
    native = f"{uuid.uuid4().int % 10**11:011d}"
    mid = next(gateway._ids)
    now = datetime.now(UTC).isoformat()
    gateway.meetings[mid] = {
        "id": mid, "user_id": platform_user, "platform": "zoom", "native_meeting_id": native,
        "constructed_meeting_url": f"https://zoom.us/j/{native}", "status": status,
        "bot_container_id": None, "start_time": now, "end_time": now,
        "completion_reason": None, "failure_stage": None, "data": {},
        "created_at": now, "updated_at": now,
    }
    gateway.owners[mid] = platform_user
    gateway.transcripts[mid] = [
        {"segment_id": "s:1", "speaker": "Asha", "text": "budget final", "start": 0.0,
         "end": 1.0, "completed": True}
    ]
    gateway.objects[mid] = [f"recordings/{mid}/chunk-0001.webm", f"recordings/{mid}/master.webm"]
    return mid, native


@dataclass
class World:
    harness: Harness
    engine: Engine
    org_id: uuid.UUID
    key: str
    admin: Member
    subject: Member
    other: Member

    @property
    def meetings(self) -> Meetings:
        s = self.harness.services
        return Meetings(self.engine, s.keyring, s.gateway, s.settings, s.clock)

    def meeting(
        self, requester: Member, *, status: str = "done", platform_status: str = "completed",
        full: bool = True,
    ) -> tuple[uuid.UUID, int]:
        platform_id, native = _platform_meeting(self.harness, self.key, platform_status)
        meeting_id = seed_meeting(self.engine, self.org_id, requester.user_id, None, status=status)
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                text(
                    "UPDATE meetings SET platform_meeting_id = :p, native_meeting_id = :n,"
                    " attendees = CAST(:a AS jsonb) WHERE id = :m"
                ),
                {"p": platform_id, "n": native, "m": meeting_id,
                 "a": '[{"name": "Asha", "source": "speaker"}]'},
            )
        if full:
            ids = seed_segments(self.engine, self.org_id, meeting_id, ["kal ki meeting", "budget"])
            with transaction(self.engine, org_id=self.org_id) as tx:
                mom.seed_version(tx, self.org_id, meeting_id, mom_body(ids + ids))
                tx.add(
                    ConsentRecord(
                        meeting_id=meeting_id, org_id=self.org_id,
                        bot_display_name="munshi.ai (recording)", disclosure_text="notice",
                        privacy_notice_url="http://portal.test/privacy",
                        disclosure_channel="chat", admitted_at=self.harness.clock(),
                    )
                )
        return meeting_id, platform_id

    def share(self, meeting_id: uuid.UUID, grantee: Member, granted_by: Member) -> None:
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                insert(MeetingShare).values(
                    org_id=self.org_id, meeting_id=meeting_id, grantee_type="user",
                    grantee_id=grantee.user_id, granted_by=granted_by.user_id,
                )
            )

    def request(self, *, account: bool = True) -> uuid.UUID:
        """What the admin route does: remove the subject from the org and insert the request."""
        with transaction(self.engine, org_id=self.org_id, user_id=self.admin.user_id) as tx:
            removed = remove_from_org(tx, self.org_id, self.subject.user_id)
            return create_request(
                tx, subject=self.subject.user_id, org_id=self.org_id, audit_org_id=self.org_id,
                source="org_admin", requested_by=self.admin.user_id, account=account,
                now=self.harness.clock(), counts=removed,
            ).id

    def run(self, request_id: uuid.UUID, audit_org: uuid.UUID | None = None) -> str | None:
        s = self.harness.services
        return asyncio.run(
            runner.run(self.engine, self.meetings, audit_org or self.org_id, request_id,
                       clock=s.clock)
        )

    def purge_pending(self, superuser: Engine) -> None:
        """What andai-worker's deletion runner does for every pending deletion."""
        with superuser.connect() as conn:
            pending = conn.execute(
                text("SELECT org_id, id FROM meetings WHERE deletion_state = 'pending'")
            ).all()
        s = self.harness.services
        for org_id, meeting_id in pending:
            asyncio.run(
                deletion.purge(self.engine, s.keyring, s.gateway, org_id, meeting_id,
                               clock=s.clock)
            )

    def drive(self, superuser: Engine, request_id: uuid.UUID, rounds: int = 40,
              settle=None) -> str | None:
        state = None
        for _ in range(rounds):
            state = self.run(request_id)
            if state in ("done", "failed", None):
                return state
            if settle is not None:
                settle()
            self.purge_pending(superuser)
            self.harness.clock.advance(seconds=15)
        return state


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    engine = fresh_app_engine
    harness = make_harness(engine)
    admin = new_member(engine)
    key = give_principal(harness, engine, admin.org_id)
    subject = new_member(engine, role="member", org_id=admin.org_id)
    other = new_member(engine, role="member", org_id=admin.org_id)
    return World(harness, engine, admin.org_id, key, admin, subject, other)


def _superuser(world: World, superuser_engine: Engine) -> Engine:
    """A superuser engine on the test's own database."""
    from sqlalchemy import create_engine

    url = superuser_engine.url.set(database=world.engine.url.database)
    return create_engine(url)


def _quarantine(superuser: Engine, org_id: uuid.UUID | None, sender: str,
                resolved: uuid.UUID | None = None, status: str = "quarantined") -> None:
    raw = f"From: {sender}\r\nSubject: invite {uuid.uuid4().hex}\r\n\r\n".encode()
    quarantined = status == "quarantined"
    outcome, reason = ("quarantined", "unknown_user") if quarantined else ("scheduled", None)
    with superuser.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inbound_mail (raw, raw_sha256, mail_from, rcpt_to, status, outcome,"
                " quarantine_reason, org_id, resolved_user_id, sender, processed_at)"
                " VALUES (:raw, :sha, :sender, ARRAY['notetaker@munshi.test'], :status, :outcome,"
                " :reason, :org, :resolved, :sender, now())"
            ),
            {"raw": raw, "sha": hashlib.sha256(raw).hexdigest(), "sender": sender,
             "org": org_id, "resolved": resolved, "status": status, "outcome": outcome,
             "reason": reason},
        )


def _count(conn, sql: str, **params) -> int:
    return conn.execute(text(sql), params).scalar_one()


def test_a_member_is_erased_from_the_org_and_their_account_anonymised(
    world: World, superuser_engine: Engine
) -> None:
    su = _superuser(world, superuser_engine)
    w, s, gateway = world, world.subject, world.harness.gateway
    # Their meetings: one finished with everything, one with the bot in the room, one scheduled.
    done_id, done_pid = w.meeting(s)
    live_id, live_pid = w.meeting(s, status="recording", platform_status="active", full=False)
    event_id = uuid.uuid4()
    now = w.harness.clock()
    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.add(InviteEvent(id=event_id, org_id=w.org_id, uid_norm=f"{event_id}@example.test",
                           sequence=0, state="active", requested_by=s.user_id))
        tx.flush()
        sched = scheduled.insert_scheduled(
            tx, meeting_id=uuid.uuid4(), org_id=w.org_id, invite_event_id=event_id,
            occurrence_key=now + timedelta(days=1), scheduled_start=now + timedelta(days=1),
            requested_by=s.user_id, team_id=None, join_url="https://zoom.us/j/12345678901",
            platform="zoom", title="Planning", language="en", now=now,
        ).id
    # Other people's records that name them.
    others_id, _ = w.meeting(w.other)
    w.share(others_id, w.admin, granted_by=s)        # a share they granted: tombstoned
    w.share(others_id, s, granted_by=w.other)        # a share they received: deleted
    w.share(done_id, w.other, granted_by=s)          # a share on their own meeting: purged
    skipped = seed_meeting(w.engine, w.org_id, w.other.user_id, None, status="scheduled")
    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.execute(text("UPDATE meetings SET skipped_at = now(), skipped_by = :s WHERE id = :m"),
                   {"s": s.user_id, "m": skipped})
        tx.execute(
            text("INSERT INTO invites (org_id, email, role, token_hash, invited_by, expires_at)"
                 " VALUES (:o, :e, 'member', :t, :s, now() + interval '1 day')"),
            {"o": w.org_id, "e": f"fresh-{uuid.uuid4().hex[:8]}@example.test",
             "t": uuid.uuid4().hex, "s": s.user_id},
        )
        tx.execute(
            text("INSERT INTO invites (org_id, email, role, token_hash, invited_by, expires_at)"
                 " VALUES (:o, :e, 'member', :t, :a, now() + interval '1 day')"),
            {"o": w.org_id, "e": s.email, "t": uuid.uuid4().hex, "a": w.admin.user_id},
        )
        tx.add(BotSetting(org_id=w.org_id, team_id=None, auto_join="off", updated_by=s.user_id))
    _quarantine(su, w.org_id, s.email)
    _quarantine(su, w.org_id, f"forwarder-{uuid.uuid4().hex[:6]}@example.test",
                resolved=s.user_id, status="done")
    _quarantine(su, None, s.email)
    with transaction(w.engine) as tx:
        tx.add(UserTotp(user_id=s.user_id, secret_ciphertext="x", key_id="k"))
        tx.add(RecoveryCode(user_id=s.user_id, code_hash="h"))
        tx.add(PasswordReset(user_id=s.user_id, token_hash=uuid.uuid4().hex,
                             expires_at=now + timedelta(hours=1)))
    sign_in(w.harness, s)  # a session and a login.success naming them
    with su.connect() as conn:
        actor_rows = _count(conn, "SELECT count(*) FROM audit_events WHERE actor_user_id = :u",
                            u=s.user_id)
    assert actor_rows >= 1

    request_id = w.request()

    def settle() -> None:
        """What the reconciler does once the platform says the stopped bot has left."""
        if any(native == gateway.meetings[live_pid]["native_meeting_id"]
               for _, native in gateway.stops):
            gateway.meetings[live_pid]["status"] = "completed"
            with su.begin() as conn:
                conn.execute(text("UPDATE meetings SET status = 'done', ended_at = now()"
                                  " WHERE id = :m AND status = 'recording'"), {"m": live_id})

    assert w.drive(su, request_id, settle=settle) == "done"

    # The live meeting was stopped once, each meeting was deleted on the platform once.
    assert [n for _, n in gateway.stops] == [gateway.meetings[live_pid]["native_meeting_id"]]
    assert sorted(gateway.deletions) == sorted([done_pid, live_pid])
    with su.connect() as conn:
        for meeting_id in (done_id, live_id, sched):
            assert _count(conn, "SELECT count(*) FROM meetings WHERE id = :m", m=meeting_id) == 0
        for table in ("transcript_segments", "mom_documents", "consent_records"):
            assert _count(conn, f"SELECT count(*) FROM {table} WHERE meeting_id = :m",
                          m=done_id) == 0
        # No org row names them any more, per the registry.
        email = s.email
        for rule in ERASURE_RULES:
            if rule.scope != "org" or rule.action == "keep":
                continue
            value = email if rule.column in ("email", "sender", "mail_from") else s.user_id
            n = _count(conn, f"SELECT count(*) FROM {rule.table} WHERE {rule.column} = :v",
                       v=value)
            assert n == 0, rule
        # Others' records stay, pointing at the tombstone.
        assert _count(conn, "SELECT count(*) FROM meeting_shares WHERE meeting_id = :m"
                      " AND granted_by = :t", m=others_id, t=TOMBSTONE_USER_ID) == 1
        assert _count(conn, "SELECT count(*) FROM meetings WHERE id = :m AND skipped_by = :t",
                      m=skipped, t=TOMBSTONE_USER_ID) == 1
        assert _count(conn, "SELECT count(*) FROM invites WHERE invited_by = :t",
                      t=TOMBSTONE_USER_ID) == 1
        assert _count(conn, "SELECT count(*) FROM bot_settings WHERE updated_by = :t",
                      t=TOMBSTONE_USER_ID) == 1
        assert _count(conn, "SELECT count(*) FROM meetings WHERE id = :m", m=others_id) == 1
        # Their account: anonymised, auth rows and org-less mail gone.
        user = conn.execute(text("SELECT email, display_name, password_hash, status FROM users"
                                 " WHERE id = :u"), {"u": s.user_id}).one()
        assert tuple(user) == (f"erased-{request_id}@munshi.invalid", "Erased user", None,
                               "erased")
        for table in ("sessions", "user_totp", "recovery_codes", "password_resets",
                      "org_memberships", "team_memberships"):
            assert _count(conn, f"SELECT count(*) FROM {table} WHERE user_id = :u",
                          u=s.user_id) == 0, table
        assert _count(conn, "SELECT count(*) FROM inbound_mail WHERE lower(sender) = :e"
                      " OR resolved_user_id = :u", e=email, u=s.user_id) == 0
        # The trail keeps their id.
        assert _count(conn, "SELECT count(*) FROM audit_events WHERE actor_user_id = :u",
                      u=s.user_id) == actor_rows
        request = conn.execute(text("SELECT state, step, error, counts, finished_at"
                                    " FROM erasure_requests WHERE id = :r"),
                               {"r": request_id}).mappings().one()
    assert (request["state"], request["step"], request["error"]) == ("done", "done", None)
    assert request["finished_at"] is not None
    assert request["counts"]["scrub"]["meeting_shares.grantee_id"] == 1
    assert request["counts"]["account"]["users.email"] == 1
    assert request["counts"]["account"]["inbound_mail"] == 1
    assert request["counts"]["scheduled_cancelled"] == 1
    # `user.erase.complete` is in the org's trail, through the definer the audit page reads.
    with transaction(w.engine, org_id=w.org_id) as tx:
        rows = tx.execute(
            text("SELECT actor_user_id, action, target FROM andai_audit_page(:f, :t,"
                 " ARRAY['user.erase.request', 'user.erase.complete', 'meeting.delete'],"
                 " NULL, NULL, 100)"),
            {"f": now - timedelta(days=1), "t": w.harness.clock() + timedelta(days=1)},
        ).all()
    actions = sorted((r.action, r.target) for r in rows)
    assert ("user.erase.complete", f"user:{s.user_id}") in actions
    assert ("user.erase.request", f"user:{s.user_id}") in actions
    assert {t for a, t in actions if a == "meeting.delete"} == {
        f"meeting:{done_id}", f"meeting:{live_id}", f"meeting:{sched}"
    }
    # A finished request is never run again.
    assert w.run(request_id) is None
    assert sorted(gateway.deletions) == sorted([done_pid, live_pid])
    su.dispose()


def test_with_another_membership_left_the_account_is_untouched(
    world: World, superuser_engine: Engine
) -> None:
    su = _superuser(world, superuser_engine)
    w, s = world, world.subject
    elsewhere = new_member(w.engine)
    from andai_core.db.models import OrgMembership

    with transaction(w.engine, org_id=elsewhere.org_id) as tx:
        tx.add(OrgMembership(org_id=elsewhere.org_id, user_id=s.user_id, role="member"))
    w.meeting(s)
    request_id = w.request(account=False)

    assert w.drive(su, request_id) == "done"

    with su.connect() as conn:
        user = conn.execute(text("SELECT email, display_name, status FROM users WHERE id = :u"),
                            {"u": s.user_id}).one()
        assert tuple(user) == (s.email, "Test Member", "active")
        assert _count(conn, "SELECT count(*) FROM org_memberships WHERE user_id = :u",
                      u=s.user_id) == 1
        assert _count(conn, "SELECT count(*) FROM meetings WHERE requested_by = :u",
                      u=s.user_id) == 0
    su.dispose()


def test_an_account_step_waits_for_the_persons_other_requests(
    world: World, superuser_engine: Engine
) -> None:
    su = _superuser(world, superuser_engine)
    w, s = world, world.subject
    elsewhere = new_member(w.engine)
    from andai_core.db.models import OrgMembership

    with transaction(w.engine, org_id=elsewhere.org_id) as tx:
        tx.add(OrgMembership(org_id=elsewhere.org_id, user_id=s.user_id, role="member"))
    first = w.request(account=False)
    with transaction(w.engine, org_id=elsewhere.org_id) as tx:
        remove_from_org(tx, elsewhere.org_id, s.user_id)
        second = create_request(
            tx, subject=s.user_id, org_id=elsewhere.org_id, audit_org_id=elsewhere.org_id,
            source="operator", requested_by=None, account=True, now=w.harness.clock(),
        ).id

    # The second (account) request reaches its account step while the first is unfinished.
    assert w.run(second, elsewhere.org_id) == "running"
    with su.connect() as conn:
        step = conn.execute(text("SELECT step FROM erasure_requests WHERE id = :r"),
                            {"r": second}).scalar_one()
        assert step == "account"
        assert conn.execute(text("SELECT status FROM users WHERE id = :u"),
                            {"u": s.user_id}).scalar_one() == "active"
    assert w.drive(su, first) == "done"
    assert w.run(second, elsewhere.org_id) == "done"
    with su.connect() as conn:
        assert conn.execute(text("SELECT status FROM users WHERE id = :u"),
                            {"u": s.user_id}).scalar_one() == "erased"
        # Each request's completion is in its own org's trail.
        rows = conn.execute(text("SELECT org_id FROM audit_events WHERE target = :t"
                                 " AND action = 'user.erase.complete'"),
                            {"t": f"user:{s.user_id}"}).scalars().all()
    assert sorted(map(str, rows)) == sorted([str(w.org_id), str(elsewhere.org_id)])
    su.dispose()


def test_a_purge_that_spends_its_attempts_fails_the_request(
    world: World, superuser_engine: Engine
) -> None:
    su = _superuser(world, superuser_engine)
    w, s = world, world.subject
    meeting_id, _ = w.meeting(s)
    w.harness.gateway.fail["delete meeting"] = 500
    request_id = w.request()

    state = None
    for _ in range(deletion.MAX_ATTEMPTS + 5):
        state = w.run(request_id)
        if state != "running":
            break
        w.purge_pending(su)
    assert state == "failed"

    with su.connect() as conn:
        row = conn.execute(text("SELECT state, error, step FROM erasure_requests WHERE id = :r"),
                           {"r": request_id}).one()
        assert tuple(row) == ("failed", "purge_failed", "wait_purge")
        assert conn.execute(text("SELECT deletion_state FROM meetings WHERE id = :m"),
                            {"m": meeting_id}).scalar_one() == "failed"
        assert _count(conn, "SELECT count(*) FROM audit_events WHERE target = :t"
                      " AND action = 'user.erase.failed'", t=f"user:{s.user_id}") == 1
        # The account was not touched: the person's data is not all gone.
        assert conn.execute(text("SELECT status FROM users WHERE id = :u"),
                            {"u": s.user_id}).scalar_one() == "active"
    su.dispose()


def test_errors_retry_with_backoff_and_fail_after_the_cap(
    world: World, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    su = _superuser(world, superuser_engine)
    w = world
    request_id = w.request()

    def boom(engine, request):  # noqa: ARG001
        raise RuntimeError("scrub failed")

    monkeypatch.setattr(runner, "_scrub", boom)
    seen = []
    for attempt in range(1, runner.MAX_ATTEMPTS + 1):
        state = w.run(request_id)
        with su.connect() as conn:
            row = conn.execute(text("SELECT state, attempts, next_at, error FROM erasure_requests"
                                    " WHERE id = :r"), {"r": request_id}).one()
        seen.append(row)
        if attempt < runner.MAX_ATTEMPTS:
            assert state == "running" and row.attempts == attempt
            assert row.next_at - w.harness.clock() == runner.backoff(attempt)
        else:
            assert state == "failed"
            assert (row.state, row.error) == ("failed", "internal_error")
    assert runner.backoff(1) == timedelta(seconds=10)
    assert runner.backoff(20) == timedelta(seconds=runner.BACKOFF_CAP_S)
    with su.connect() as conn:
        assert _count(conn, "SELECT count(*) FROM audit_events WHERE action = 'user.erase.failed'"
                      " AND target = :t", t=f"user:{w.subject.user_id}") == 1
    su.dispose()


def test_erase_inbound_acts_only_on_the_transactions_org(
    world: World, superuser_engine: Engine
) -> None:
    su = _superuser(world, superuser_engine)
    w = world
    stranger = new_member(w.engine)
    _quarantine(su, stranger.org_id, w.subject.email)
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError, match="transaction's org"):
        with transaction(w.engine, org_id=w.org_id) as tx:
            tx.execute(text("SELECT andai_erase_inbound(:o, :u, :e)"),
                       {"o": stranger.org_id, "u": w.subject.user_id, "e": w.subject.email})
    with transaction(w.engine, org_id=stranger.org_id) as tx:
        n = tx.execute(text("SELECT andai_erase_inbound(:o, :u, :e)"),
                       {"o": stranger.org_id, "u": w.subject.user_id,
                        "e": w.subject.email.upper()}).scalar_one()
        # The flag the scan role's delete policy reads is off again once the function returns.
        flag = tx.execute(text("SELECT current_setting('andai.erasing', true)")).scalar()
    assert n == 1 and flag == "off"
    su.dispose()


def test_the_subject_cannot_come_back_while_the_request_runs(world: World) -> None:
    w = world
    token = staged_session(w.engine, w.subject, "full", w.harness.clock)
    w.request()
    client = w.harness.client()
    from andai_core.auth.sessions import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/v1/me").status_code == 401
