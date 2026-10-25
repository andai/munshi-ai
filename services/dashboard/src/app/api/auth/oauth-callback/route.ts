import { NextRequest, NextResponse } from "next/server";
import { getServerSession } from "next-auth/next";
import { authOptions } from "../[...nextauth]/route";

/**
 * OAuth callback endpoint - syncs user info after Google OAuth
 * Called by the frontend after successful OAuth to get user info
 */
export async function GET(request: NextRequest) {
  try {
    const session = await getServerSession(authOptions);

    if (!session || !(session as any).munshiUser) {
      return NextResponse.json(
        { error: "Not authenticated via OAuth" },
        { status: 401 }
      );
    }

    const munshiUser = (session as any).munshiUser;
    const token = (session as any).munshiToken;

    // Return user info in the same format as the login endpoint
    return NextResponse.json({
      user: {
        id: munshiUser.id,
        email: munshiUser.email,
        name: munshiUser.name,
        max_concurrent_bots: munshiUser.max_concurrent_bots,
        created_at: munshiUser.created_at,
      },
      token,
      isNewUser: (session as any).isNewUser || false,
    });
  } catch (error) {
    console.error("OAuth callback error:", error);
    return NextResponse.json(
      { error: "Failed to sync OAuth session" },
      { status: 500 }
    );
  }
}

