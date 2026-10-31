# andai/services/core/andai_core/org

Org administration for andai-core, under `/api/v1`:

- `routes.py` — `GET/POST /org/users` (the member list with roles, teams and 2FA state plus
  pending invites; invite an address as `admin` or `member`), `PATCH /org/users/{id}` (change a
  role, disable or re-enable an account; disabling ends every session), `GET/POST /teams` and
  `PUT /teams/{id}/members` (replace a team's members; a team lead may change who is in their own
  team but not who leads it).
- `directory.py` — `GET /org/directory`: the org's active members (id, name, team ids) and teams
  (id, name) for any member, for the meeting share picker; no emails, no roles.
- `service.py` — the rules behind those routes: nobody changes themselves, the owner is out of an
  admin's reach, only the owner hands on ownership, team members belong to the org; every change
  is audited.
- `security.py` — `GET/PUT /org/security` (`require2fa`: read by any member, changed by an admin or
  the owner; turning it on ends the sessions in the org of every member without confirmed TOTP)
  and `DELETE /org/users/{id}/mfa` (an admin or the owner clears a member's TOTP and recovery codes
  and ends that member's sessions; an admin cannot reset the owner, nobody resets themselves
  here).

Role checks use `andai_core.auth.roles.require_role` (org roles, and team leads for a team route).
Queries reach people through this org's memberships: `users` is a global table.
