# /api/* proxy

`route.ts` forwards every /api/* request (except the more specific /api/health) to andai-core at
`${ANDAI_CORE_URL}/api/…` (default http://andai-core:8000, read per request), passing cookies,
Set-Cookie, Origin and the client address in X-Forwarded-For. A `text/event-stream` answer (a
meeting's live stream) is passed through chunk by chunk, uncompressed, with no time limit after
the headers; the browser leaving aborts it upstream. The browser only ever talks to the portal's
own origin.
