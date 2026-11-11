# /admin/audit

The organisation's audit trail, newest first (GET /api/v1/org/audit, 50 per page with Load more),
for owners and admins, filtered by a UTC day range and one action (GET /api/v1/org/audit/actions);
the filters live in the URL query. Members and leads are sent to /403.
