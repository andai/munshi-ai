# andai/services/core/andai_core/auth

Human authentication for andai-core, mounted under `/api/v1`:

- `routes.py` — login, logout, `GET /me`, password reset, invite acceptance, first-run
  bootstrap, the second login step (`/auth/mfa/verify`) and the user's own two-factor routes
  (`/me/mfa`: start, confirm, disable, regenerate recovery codes).
- `mfa.py` — TOTP two-factor login: sealed secrets, ±1 step skew, replay rejection by time step,
  10 argon2id-hashed single-use recovery codes, 5 wrong codes end an `mfa_pending` session and
  10 per user per 15 minutes are throttled.
- `sessions.py` — opaque session tokens in the `andai_session` cookie (only the SHA-256 is stored),
  idle and absolute expiry, `revoke_all`.
- `passwords.py` — argon2id hashing, rehash on login, a dummy verification for unknown emails.
- `ratelimit.py` — sliding-window limiter (Valkey in the stack, in memory for tests): 5 failed logins
  per (email, client address) per 15 minutes.
- `stage_gate.py` — middleware: an `mfa_pending` session reaches only `/auth/mfa/verify` and
  logout, an `enroll_required` one only `/me/mfa*` and logout; anything else is 401 with
  `next_step`.
- `roles.py` — `require_role(...)`: a `full` session whose org role is one of the given ones (or,
  with `team_lead=True`, who leads the team in the path),
  else 403.
- `csrf.py` — refuses state-changing requests whose `Origin` is not `ANDAI_PORTAL_ORIGIN`.
- `clientip.py` — the client address; `X-Forwarded-For` only from `ANDAI_TRUSTED_PROXIES`.
- `deps.py` — the app's services and the current-session dependencies.
- `resets.py` — 30-minute single-use reset links; confirming one ends every session.
- `invites.py` — invite tokens and mails; accepting sets the password, activates the user and adds
  the membership.
- `bootstrap.py` — the first org and owner, only while no user exists.
- `links.py` — address validation and portal links; `tokens.py` — random tokens and their stored
  hashes; `users.py` — disabling a user.
