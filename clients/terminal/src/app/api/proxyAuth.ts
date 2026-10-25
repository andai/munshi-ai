/** Per-user API key for the upstream proxies.
 *
 *  The X-API-Key the terminal forwards to the gateway / agent-api is the logged-in user's APIToken,
 *  read from the httpOnly `munshi-token` cookie (set by /api/auth/login). When there's no cookie (e.g.
 *  before login, or a self-host deploy with a baked key) we fall back to MUNSHI_API_KEY, then to the
 *  legacy MUNSHI_BOT_API_KEY so existing single-key deploys keep working. */
import { cookies } from "next/headers";
import { AUTH_COOKIE } from "./auth/adminApi";

/** Resolve the API key to send upstream: cookie token → MUNSHI_API_KEY → MUNSHI_BOT_API_KEY → "".
 *  cookies() can throw outside a request scope (e.g. in tests / edge cases); degrade to the env keys. */
export async function resolveApiKey(): Promise<string> {
  let cookieToken: string | undefined;
  try {
    cookieToken = (await cookies()).get(AUTH_COOKIE)?.value;
  } catch {
    cookieToken = undefined;
  }
  return cookieToken || process.env.MUNSHI_API_KEY || process.env.MUNSHI_BOT_API_KEY || "";
}
