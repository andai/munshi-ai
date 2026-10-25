# andai/services/core/andai_core/auth

Human authentication for andai-core, mounted under `/api/v1`:

- `routes.py` — login, logout and `GET /me`.
- `sessions.py` — opaque session tokens in the `andai_session` cookie (only the SHA-256 is stored),
  idle and absolute expiry, `revoke_all`.
- `passwords.py` — argon2id hashing, rehash on login, a dummy verification for unknown emails.
- `ratelimit.py` — sliding-window limiter (Valkey in the stack, in memory for tests): 5 failed logins
  per (email, client address) per 15 minutes.
- `csrf.py` — refuses state-changing requests whose `Origin` is not `ANDAI_PORTAL_ORIGIN`.
- `clientip.py` — the client address; `X-Forwarded-For` only from `ANDAI_TRUSTED_PROXIES`.
- `deps.py` — the app's services and the current-session dependencies.
- `tokens.py` — random tokens and their stored hashes; `users.py` — disabling a user.
