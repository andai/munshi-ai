import type { NextConfig } from "next";
import path from "path";
import fs from "fs";

const normalizeBasePath = (value?: string) => {
  if (!value) return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
};

const basePath = normalizeBasePath(process.env.NEXT_PUBLIC_BASE_PATH);

// Read version from munshi monorepo root VERSION file
function getVersion(): string {
  const candidates = [
    path.resolve(__dirname, "../../VERSION"),       // services/dashboard -> munshi root
    path.resolve(__dirname, "VERSION"),              // local fallback
  ];
  for (const p of candidates) {
    try {
      return fs.readFileSync(p, "utf-8").trim();
    } catch {}
  }
  return "dev";
}

const MUNSHI_API_URL = process.env.MUNSHI_API_URL;
if (!MUNSHI_API_URL) {
  throw new Error("MUNSHI_API_URL is required: dashboard rewrites must use the release/deploy SSOT, not a baked fallback");
}

const nextConfig: NextConfig = {
  // Only use standalone output for production builds
  ...(process.env.NODE_ENV === 'production' ? { output: 'standalone' } : {}),
  ...(basePath ? { basePath, assetPrefix: basePath } : {}),
  // Ensure Turbopack uses this project as root
  // (avoids picking a parent lockfile and serving nothing)
  turbopack: {
    root: path.resolve(__dirname),
  },
  // Expose app version from munshi VERSION file at build time
  env: {
    NEXT_PUBLIC_APP_VERSION: getVersion(),
  },
  // Allow dev access from nginx-proxied domains
  allowedDevOrigins: [""],
  // Proxy /b/ routes to the agentic gateway for VNC/CDP (supports WebSocket upgrade)
  async rewrites() {
    return [
      {
        source: "/b/:path*",
        destination: `${MUNSHI_API_URL}/b/:path*`,
      },
      {
        source: "/ws",
        destination: `${MUNSHI_API_URL}/ws`,
      },
    ];
  },
  // v0.10.5.3 Pack D-2: redirect dashboard's internal /docs/* to canonical
  // example.invalid. Anyone landing on example.invalid/docs (typed in URL bar
  // or external link) goes to the unified docs site. The internal docs pages
  // under src/app/docs/ remain in the codebase for now (decoupling them is
  // a larger cleanup — out of scope for this surgical patch); they are no
  // longer reachable at runtime via standard navigation.
  async redirects() {
    const docsBase = process.env.NEXT_PUBLIC_DOCS_URL || "";
    // No hosted docs site by default: without NEXT_PUBLIC_DOCS_URL there is nothing to redirect to
    // (an empty destination is an invalid redirect and fails `next build`).
    if (!docsBase) return [];
    return [
      {
        source: "/docs",
        destination: docsBase,
        permanent: true,
      },
      {
        source: "/docs/:path*",
        destination: `${docsBase}/:path*`,
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
