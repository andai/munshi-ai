/** @type {import('next').NextConfig} */
const nextConfig = {
  // Self-contained server bundle for the container image (andai/portal/Dockerfile).
  output: "standalone",
  // Pin the tracing root to this app. Without it Next infers the Munshi monorepo root (its
  // lockfiles sit above andai/portal) and nests server.js under .next/standalone/andai/portal/.
  outputFileTracingRoot: import.meta.dirname,
  turbopack: { root: import.meta.dirname },
  poweredByHeader: false,
};

export default nextConfig;
