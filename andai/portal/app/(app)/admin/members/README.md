# /admin/members

Members and pending invites: invite, change roles, disable or enable accounts, reset two-factor
login (GET/POST /api/v1/org/users, PATCH /org/users/{id}, DELETE /org/users/{id}/mfa).
"Export data" asks for a copy of a member's data (POST /org/users/{id}/exports); the "Data exports"
section lists the copies admins asked for, with a Download link for the admin who asked
(GET /org/exports, GET /exports/{id}/download). "Erase data…" and the "Data erasure" section erase
a member's data (OPS-02).
