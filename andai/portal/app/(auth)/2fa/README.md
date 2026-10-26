# /2fa

The second sign-in step for an `mfa_pending` session: a TOTP code or a recovery code
(POST /api/v1/auth/mfa/verify). Too many wrong codes end the attempt.
