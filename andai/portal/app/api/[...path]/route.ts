// Same-origin proxy: the browser talks only to the portal, and every /api/* request (except the
// portal's own /api/health, a more specific route) is forwarded to andai-core at request time.
// ANDAI_CORE_URL is read per request, so one image serves every deployment.
//
// Forwarded upstream: method, query, body, Cookie, Content-Type, Accept, Origin (andai-core
// refuses a state-changing request whose Origin is not the portal's), User-Agent and the client
// address in X-Forwarded-For (resolved by lib/client-ip.cjs before Next sees the request;
// andai-core believes it only from the portal). Forwarded back: status, body, every Set-Cookie,
// and the headers the portal pages read (Content-Type, Cache-Control, Retry-After).

export const dynamic = "force-dynamic";

const DEFAULT_CORE_URL = "http://andai-core:8000";
const UPSTREAM_TIMEOUT_MS = 30_000;
const REQUEST_HEADERS = ["accept", "content-type", "cookie", "origin", "user-agent", "x-forwarded-for"];
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

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body: body && body.byteLength > 0 ? body : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch {
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
  return new Response(empty ? null : upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = forward;
export const HEAD = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
