// The portal's only way to reach andai-core: same-origin requests to /api/v1/* (proxied by
// app/api/[...path]/route.ts). Every decision comes from andai-core's answer:
//
// * 401 with `next_step` — the session is in a two-factor stage (or ended): go where it says.
// * 401 without it — not signed in: go to /sign-in.
// * 403 on a page load — the page is not for this account: show /403.
//
// Callers on the sign-in and two-factor screens pass `redirect: false` and read the ApiError
// themselves, because there a 401 is an answer to show, not a reason to leave.

export type NextStep = "mfa_verify" | "mfa_enroll" | "login";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly nextStep: NextStep | null;
  readonly retryAfter: number | null;

  constructor(status: number, detail: string, nextStep: NextStep | null, retryAfter: number | null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.nextStep = nextStep;
    this.retryAfter = retryAfter;
  }
}

type Options = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Follow 401/403 answers to the page they call for (default true). */
  redirect?: boolean;
};

const STEP_PAGES: Record<NextStep, string> = {
  mfa_verify: "/2fa",
  mfa_enroll: "/2fa/setup",
  login: "/sign-in",
};

/** The page an error sends the browser to, or null when the caller should show it in place. */
export function destinationFor(error: ApiError, method: string): string | null {
  if (error.status === 401) return error.nextStep ? STEP_PAGES[error.nextStep] : "/sign-in";
  if (error.status === 403 && method === "GET") return "/403";
  return null;
}

function detailOf(payload: unknown, status: number): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return "some of the values are not valid";
  }
  if (status >= 500) return "the service is unavailable; try again shortly";
  return `request failed (${status})`;
}

function nextStepOf(payload: unknown): NextStep | null {
  if (payload && typeof payload === "object" && "next_step" in payload) {
    const step = (payload as { next_step: unknown }).next_step;
    if (step === "mfa_verify" || step === "mfa_enroll" || step === "login") return step;
  }
  return null;
}

export async function api<T = unknown>(path: string, options: Options = {}): Promise<T> {
  const method = options.method ?? "GET";
  const init: RequestInit = { method, credentials: "same-origin", cache: "no-store", headers: {} };
  if (options.body !== undefined) {
    init.headers = { "content-type": "application/json" };
    init.body = JSON.stringify(options.body);
  }
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, init);
  } catch {
    throw new ApiError(0, "the network is unavailable; check your connection", null, null);
  }
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (response.ok) return payload as T;

  const retry = Number(response.headers.get("retry-after"));
  const error = new ApiError(
    response.status,
    detailOf(payload, response.status),
    nextStepOf(payload),
    Number.isFinite(retry) && retry > 0 ? retry : null,
  );
  if (options.redirect !== false) {
    const destination = destinationFor(error, method);
    if (destination && typeof window !== "undefined") {
      window.location.assign(destination);
      // The page is leaving; never settle, so nothing renders the error on the way out.
      return new Promise<T>(() => {});
    }
  }
  throw error;
}

/** "Try again in 15 minutes" from a Retry-After value in seconds. */
export function retryText(seconds: number | null): string {
  if (!seconds) return "Try again later.";
  const minutes = Math.ceil(seconds / 60);
  if (minutes <= 1) return "Try again in a minute.";
  return `Try again in ${minutes} minutes.`;
}

/** andai-core's lowercase `detail` phrases as a sentence for the screen. */
export function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return trimmed;
  const capitalised = trimmed[0].toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capitalised) ? capitalised : `${capitalised}.`;
}
