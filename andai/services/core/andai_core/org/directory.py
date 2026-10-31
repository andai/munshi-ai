"""`GET /api/v1/org/directory` — the org's people and teams, for picking who to share a meeting
with.

Any member of the org may read it. It holds names and ids only:

* `users`: active members (`{id, name, team_ids}`), by name;
* `teams`: every team (`{id, name}`), by name.

No email addresses, org or team roles, or members who are disabled or have not joined yet. The
admin's member list (`GET /org/users`) is where those live. Rows come through this org's
memberships under row-level security, so another org's people never appear.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select

from andai_core.auth.deps import Services, get_services, org_transaction
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import OrgMembership, Team, TeamMembership, User

router = APIRouter()

EVERYONE = ("owner", "admin", "member")


@router.get("/org/directory")
def directory(
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    org_id = caller.session.org_id
    with org_transaction(services, caller.session) as tx:
        people = tx.execute(
            select(User.id, User.display_name)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .where(OrgMembership.org_id == org_id, User.status == "active")
            .order_by(User.display_name, User.id)
        ).all()
        teams = tx.execute(
            select(Team.id, Team.name).where(Team.org_id == org_id).order_by(Team.name, Team.id)
        ).all()
        memberships = tx.execute(
            select(TeamMembership.user_id, TeamMembership.team_id).where(
                TeamMembership.org_id == org_id
            )
        ).all()
    team_ids = defaultdict(list)
    for user_id, team_id in memberships:
        team_ids[user_id].append(str(team_id))
    return {
        "users": [
            {"id": str(user_id), "name": name, "team_ids": sorted(team_ids[user_id])}
            for user_id, name in people
        ],
        "teams": [{"id": str(team_id), "name": name} for team_id, name in teams],
    }
