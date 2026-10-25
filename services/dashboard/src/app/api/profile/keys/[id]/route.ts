import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUserId } from "@/lib/auth-utils";

/**
 * DELETE /api/profile/keys/:id — revoke one of the signed-in user's API keys
 * via the admin API. Keys belonging to anyone else are reported as not found.
 */
export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const MUNSHI_ADMIN_API_URL = process.env.MUNSHI_ADMIN_API_URL || "";
  const MUNSHI_ADMIN_API_KEY = process.env.MUNSHI_ADMIN_API_KEY || "";

  if (!MUNSHI_ADMIN_API_URL || !MUNSHI_ADMIN_API_KEY) {
    return NextResponse.json({ error: "Admin API URL/key not configured" }, { status: 503 });
  }

  const userId = await getAuthenticatedUserId();
  if (!userId) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  const { id } = await params;
  if (!/^\d+$/.test(id)) {
    return NextResponse.json({ error: "API key not found" }, { status: 404 });
  }

  try {
    // Only revoke a key the signed-in user owns.
    const tokensRes = await fetch(
      `${MUNSHI_ADMIN_API_URL}/admin/users/${encodeURIComponent(userId)}/tokens`,
      {
        headers: { "X-Admin-API-Key": MUNSHI_ADMIN_API_KEY },
        cache: "no-store",
      }
    );
    if (!tokensRes.ok) {
      return NextResponse.json({ error: "Failed to revoke API key" }, { status: 502 });
    }
    const tokens = (await tokensRes.json()) as unknown;
    const owned =
      Array.isArray(tokens) &&
      tokens.some((t) => typeof t === "object" && t !== null && String((t as { id?: unknown }).id) === id);
    if (!owned) {
      return NextResponse.json({ error: "API key not found" }, { status: 404 });
    }

    const response = await fetch(`${MUNSHI_ADMIN_API_URL}/admin/tokens/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: {
        "X-Admin-API-Key": MUNSHI_ADMIN_API_KEY,
      },
    });

    if (!response.ok) {
      return NextResponse.json(
        { error: "Failed to revoke API key" },
        { status: response.status }
      );
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    return NextResponse.json(
      { error: (error as Error).message },
      { status: 500 }
    );
  }
}
