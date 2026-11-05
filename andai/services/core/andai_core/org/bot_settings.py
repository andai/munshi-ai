"""When the bot joins on its own (`bot.autoJoin`), for the org and per team (INV-05, D-57).

`auto_join` is one of:

* `only_when_invited` (the default) — schedule from mail when the notetaker address is an attendee
  or a member forwarded the invite by hand;
* `all_with_link` — invites a mail rule forwarded are scheduled too;
* `off` — nothing is scheduled from mail.

A team's effective value is its own override when it has one, else the org's value, else
`only_when_invited` (`effective_auto_join`, which the intake processor calls).

Routes, for org owners and admins only (anyone else in the org gets 403):

* `GET /api/v1/org/bot-settings` → `{autoJoin, teams: [{teamId, autoJoin}]}`: the org's value and
  every team of the org with its override, or `null` when it has none.
* `PUT /api/v1/org/bot-settings` with `{autoJoin, teams?: [{teamId, autoJoin}]}` sets the org's
  value and, for each listed team, its override (`null` removes it); unlisted teams keep theirs.
  A team id that is not a team of the caller's org is 404 and nothing changes; the same team twice
  is 422. The answer is the new state, as `GET` gives it. Each scope whose value changes writes a
  `bot.auto_join.update` audit event with target `org:<id>` or `team:<id>`.

Changes of one org are serialised by a transaction-scoped advisory lock, so two concurrent writes
cannot both see the old value.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth.deps import Services, client_ip, get_services, org_transaction
from andai_core.auth.roles import NO_SUCH_TEAM, Caller, require_role
from andai_core.db.models import DEFAULT_AUTO_JOIN, BotSetting, Team

router = APIRouter(prefix="/api/v1/org")

AUDIT_ACTION = "bot.auto_join.update"
MAX_TEAMS = 1000

AutoJoin = Literal["only_when_invited", "all_with_link", "off"]


class TeamSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teamId: uuid.UUID
    autoJoin: AutoJoin | None


class BotSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    autoJoin: AutoJoin
    teams: list[TeamSetting] = Field(default_factory=list, max_length=MAX_TEAMS)


def effective_auto_join(tx: Tx, org_id: uuid.UUID, team_id: uuid.UUID | None) -> str:
    """The `bot.autoJoin` that applies to a meeting of `team_id` (or of no team) in `org_id`.

    `tx` must be in `org_id`'s row-level-security context. A team that is not a team of the org
    has no override here, so the org's value applies."""
    scope = BotSetting.team_id.is_(None)
    if team_id is not None:
        scope = or_(scope, BotSetting.team_id == team_id)
    values = dict(
        tx.execute(
            select(BotSetting.team_id, BotSetting.auto_join).where(
                BotSetting.org_id == org_id, scope
            )
        ).all()
    )
    if team_id is not None and team_id in values:
        return values[team_id]
    return values.get(None, DEFAULT_AUTO_JOIN)


def _stored(tx: Tx, org_id: uuid.UUID) -> dict[uuid.UUID | None, str]:
    return dict(
        tx.execute(
            select(BotSetting.team_id, BotSetting.auto_join).where(BotSetting.org_id == org_id)
        ).all()
    )


def read(tx: Tx, org_id: uuid.UUID) -> dict:
    stored = _stored(tx, org_id)
    teams = tx.scalars(
        select(Team.id).where(Team.org_id == org_id).order_by(Team.name, Team.id)
    ).all()
    return {
        "autoJoin": stored.get(None, DEFAULT_AUTO_JOIN),
        "teams": [{"teamId": str(team), "autoJoin": stored.get(team)} for team in teams],
    }


def _put(
    tx: Tx, org_id: uuid.UUID, team_id: uuid.UUID | None, value: str, user_id: uuid.UUID
) -> None:
    statement = insert(BotSetting).values(
        id=uuid.uuid4(), org_id=org_id, team_id=team_id, auto_join=value, updated_by=user_id
    )
    tx.execute(
        statement.on_conflict_do_update(
            index_elements=[BotSetting.org_id, BotSetting.team_id],
            set_={"auto_join": value, "updated_by": user_id, "updated_at": func.now()},
        )
    )


def update(
    tx: Tx, org_id: uuid.UUID, user_id: uuid.UUID, body: BotSettingsBody, ip: str | None
) -> None:
    wanted = {team.teamId: team.autoJoin for team in body.teams}
    if len(wanted) != len(body.teams):
        raise HTTPException(status_code=422, detail="a team is listed more than once")
    tx.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"andai_bot_settings:{org_id}", 0)))
    )
    if wanted:
        known = set(
            tx.scalars(select(Team.id).where(Team.org_id == org_id, Team.id.in_(wanted))).all()
        )
        if known != set(wanted):
            raise NO_SUCH_TEAM
    stored = _stored(tx, org_id)

    if body.autoJoin != stored.get(None, DEFAULT_AUTO_JOIN):
        _put(tx, org_id, None, body.autoJoin, user_id)
        audit.record(tx, org_id, user_id, AUDIT_ACTION, f"org:{org_id}", ip)
    for team_id, value in wanted.items():
        if value == stored.get(team_id):
            continue
        if value is None:
            tx.execute(
                delete(BotSetting).where(
                    BotSetting.org_id == org_id, BotSetting.team_id == team_id
                )
            )
        else:
            _put(tx, org_id, team_id, value, user_id)
        audit.record(tx, org_id, user_id, AUDIT_ACTION, f"team:{team_id}", ip)


@router.get("/bot-settings")
def get_bot_settings(
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        return read(tx, caller.session.org_id)


@router.put("/bot-settings")
def put_bot_settings(
    body: BotSettingsBody,
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    org_id = caller.session.org_id
    with org_transaction(services, caller.session) as tx:
        update(tx, org_id, caller.session.user_id, body, ip)
        return read(tx, org_id)
