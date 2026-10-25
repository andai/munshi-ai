import { afterEach, describe, expect, it, vi } from "vitest";
import { munshiAPI } from "@/lib/api";

describe("munshiAPI.getRecordingMasterStreamUrl", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns null while the canonical master is not ready", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 404,
        ok: false,
      })
    );

    await expect(munshiAPI.getRecordingMasterStreamUrl(42, "audio")).resolves.toBeNull();
    expect(fetch).toHaveBeenCalledWith("/api/munshi/recordings/42/master?type=audio");
  });

  it("returns the dashboard media proxy URL for a resolved local master", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        json: async () => ({
          url: "/recordings/42/media/7/raw",
          duration_seconds: 12.5,
        }),
      })
    );

    await expect(munshiAPI.getRecordingMasterStreamUrl(42, "audio")).resolves.toEqual({
      url: "/api/munshi/recordings/42/media/7/raw",
      duration_seconds: 12.5,
    });
  });

  it("prefers the dashboard media proxy when the master response includes a raw route", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        json: async () => ({
          url: "http://localhost:42268/munshi-recordings/master.wav?X-Amz-Signature=abc",
          raw_url: "/recordings/42/media/7/raw",
          duration_seconds: 12.5,
        }),
      })
    );

    await expect(munshiAPI.getRecordingMasterStreamUrl(42, "audio")).resolves.toEqual({
      url: "/api/munshi/recordings/42/media/7/raw",
      duration_seconds: 12.5,
    });
  });

  it("falls back to the presigned media URL when no raw route is returned", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        json: async () => ({
          url: "http://localhost:42268/munshi-recordings/master.wav?X-Amz-Signature=abc",
          duration_seconds: 12.5,
        }),
      })
    );

    await expect(munshiAPI.getRecordingMasterStreamUrl(42, "audio")).resolves.toEqual({
      url: "http://localhost:42268/munshi-recordings/master.wav?X-Amz-Signature=abc",
      duration_seconds: 12.5,
    });
  });
});
