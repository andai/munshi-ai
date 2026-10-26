# andai/services/core/andai_core/org

Org administration for andai-core, mounted under `/api/v1/org`:

- `security.py` — `GET/PUT /security` (`require2fa`: read by any member, changed by an admin or
  the owner; turning it on ends the sessions in the org of every member without confirmed TOTP)
  and `DELETE /users/{id}/mfa` (an admin or the owner clears a member's TOTP and recovery codes
  and ends that member's sessions; an admin cannot reset the owner, nobody resets themselves
  here).

Role checks use `andai_core.auth.roles.require_role`.
