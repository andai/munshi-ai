// Same-origin proxy: the browser talks only to the portal, and every /api/* request (except the
// portal's own /api/health, a more specific route) is forwarded to andai-core at request time.
// ANDAI_CORE_URL is read per request, so one image serves every deployment.
//
// Forwarded upstream: method, query, body, Cookie, Content-Type, Accept, Cache-Control,
// Last-Event-ID, Origin (andai-core
// refuses a state-changing request whose Origin is not the portal's), User-Agent and the client
// address in X-Forwarded-For (resolved by lib/client-ip.cjs before Next sees the request;
// andai-core believes it only from the portal). Forwarded back: status, body, every Set-Cookie,
// and the headers the portal pages read (Content-Type, Cache-Control, Retry-After).
//
// Server-sent events (a `text/event-stream` answer, e.g. a meeting's live stream) pass through as
// a stream: each chunk is written to the browser as andai-core sends it, marked
// `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` so neither the portal's own
// compression nor a reverse proxy holds events back. The 30-second limit covers only the wait for
// andai-core's headers on a stream (the whole exchange otherwise), and a browser that goes away
// aborts the upstream request, which ends the stream in andai-core too.

export const dynamic = "force-dynamic";

const DEFAULT_CORE_URL = "http://andai-core:8000";
const UPSTREAM_TIMEOUT_MS = 30_000;
const REQUEST_HEADERS = [
  "accept",
  "cache-control",
  "content-type",
  "cookie",
  "last-event-id",
  "origin",
  "user-agent",
  "x-forwarded-for",
];
const RESPONSE_HEADERS = ["content-type", "cache-control", "retry-after", "content-disposition"];

type Context = { params: Promise<{ path: string[] }> };

function coreUrl(): string {
  return (process.env.ANDAI_CORE_URL || DEFAULT_CORE_URL).replace(/\/+$/, "");
}

function unavailable(): Response {
  return Response.json(
    { detail: "the service is unavailable; try again shortly" },
    { status: 502, headers: { "cache-control": "no-store" } },
  );
}

async function forward(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  if (path.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return Response.json({ detail: "not found" }, { status: 404 });
  }
  const search = new URL(request.url).search;
  const target = `${coreUrl()}/api/${path.map(encodeURIComponent).join("/")}${search}`;

  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }

  const method = request.method.toUpperCase();
  const body = method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer();

  // One abort for the upstream request: the browser went away, or andai-core took too long.
  const abort = new AbortController();
  const onClientGone = () => abort.abort();
  request.signal.addEventListener("abort", onClientGone, { once: true });
  const timer = setTimeout(() => abort.abort(), UPSTREAM_TIMEOUT_MS);

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body: body && body.byteLength > 0 ? body : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: abort.signal,
    });
  } catch {
    clearTimeout(timer);
    request.signal.removeEventListener("abort", onClientGone);
    return unavailable();
  }

  const responseHeaders = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) responseHeaders.set(name, value);
  }
  for (const cookie of upstream.headers.getSetCookie()) {
    responseHeaders.append("set-cookie", cookie);
  }
  const empty = method === "HEAD" || upstream.status === 204 || upstream.status === 304;
  if (empty || !upstream.body) {
    clearTimeout(timer);
    request.signal.removeEventListener("abort", onClientGone);
    return new Response(null, { status: upstream.status, headers: responseHeaders });
  }
  if (!isEventStream(upstream)) {
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  }

  // A stream: no time limit once andai-core answered; pass every chunk on as it arrives.
  clearTimeout(timer);
  // no-transform: the Next.js server compresses what it serves, and a compressed event stream is
  // held back in the encoder; compression skips a no-transform response.
  responseHeaders.set("cache-control", "no-cache, no-transform");
  responseHeaders.set("x-accel-buffering", "no");
  return new Response(passThrough(upstream.body, abort), {
    status: upstream.status,
    headers: responseHeaders,
  });
}

function isEventStream(response: Response): boolean {
  return (response.headers.get("content-type") ?? "").toLowerCase().startsWith("text/event-stream");
}

/** The upstream body chunk by chunk; cancelling it (the browser left) aborts the upstream request. */
function passThrough(body: ReadableStream<Uint8Array>, abort: AbortController): ReadableStream<Uint8Array> {
  const reader = body.getReader();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) controller.close();
        else controller.enqueue(value);
      } catch {
        // andai-core went away mid-stream: end the browser's stream; EventSource reports it.
        controller.close();
      }
    },
    cancel() {
      abort.abort();
      void reader.cancel().catch(() => {});
    },
  });
}

export const GET = forward;
export const HEAD = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
