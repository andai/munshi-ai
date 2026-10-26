# /2fa/setup

TOTP enrollment (POST /api/v1/me/mfa, then /me/mfa/confirm): the QR code and key from the API,
then the 10 recovery codes, shown once. Used for voluntary enrollment (`?from=profile`) and for
sessions the organisation sent to enrollment (`enroll_required`).
