// Phase 1 skeleton: health only. Same JSON shape as the andai Python services' /healthz.
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({
    service: "andai-portal",
    status: "ok",
    version: process.env.ANDAI_VERSION ?? "dev",
  });
}
