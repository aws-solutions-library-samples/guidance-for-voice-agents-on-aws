import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/backend", () => ({ isAws: true, isLocal: false }))
vi.mock("@/lib/config", () => ({
  loadConfig: vi.fn().mockResolvedValue({
    apiUrl: "https://api.example.com",
    cognitoDomain: "d",
    cognitoClientId: "c",
    cognitoRegion: "us-east-1",
    region: "us-east-1",
  }),
}))
vi.mock("@/lib/auth", () => ({ getAccessToken: vi.fn().mockResolvedValue("tok") }))

// Imported after the mocks above so `isAws` is already resolved to true when api.ts
// reads it at module scope.
import { MonitorNotEnabledError, getCase, getMonitorSegments, startCall } from "@/lib/api"

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal("fetch", fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("startCall (aws mode)", () => {
  it("returns the backend's own call id, not the case id it sent", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ call: "real-call-uuid" }), { status: 200 }),
    )

    const result = await startCall("case-1")

    expect(result).toEqual({ call: "real-call-uuid" })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.example.com/api/calls")
    expect(JSON.parse(init.body as string)).toEqual({ case_id: "case-1" })
  })

  it("throws with the server's detail message on failure", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "case not found" }), { status: 404 }),
    )

    await expect(startCall("missing-case")).rejects.toThrow("case not found")
  })
})

describe("getMonitorSegments (aws mode)", () => {
  it("fetches segments for the given call and mode, with the bearer token attached", async () => {
    const body = { segments: [{ seq: 0, t0: 1, chunk_ms: 200, chunks: [] }], after: 0 }
    fetchMock.mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }))

    const result = await getMonitorSegments("real-call-uuid", "listen", -1)

    expect(result).toEqual(body)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.example.com/api/calls/real-call-uuid/monitor?mode=listen&after=-1")
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok")
  })

  it("passes the 'levels' mode and `after` cursor through as query params", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ segments: [], after: 7 }), { status: 200 }))

    await getMonitorSegments("real-call-uuid", "levels", 7)

    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.example.com/api/calls/real-call-uuid/monitor?mode=levels&after=7")
  })

  it("throws a MonitorNotEnabledError with the server's message on a 404", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: "monitor not enabled" }), { status: 404 }),
    )

    await expect(getMonitorSegments("real-call-uuid", "listen", -1)).rejects.toThrow(MonitorNotEnabledError)
    await expect(getMonitorSegments("real-call-uuid", "listen", -1)).rejects.toThrow("monitor not enabled")
  })

  it("throws a generic error on a non-404 failure", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "boom" }), { status: 500 }))

    const err = await getMonitorSegments("real-call-uuid", "listen", -1).catch((e) => e)
    expect(err).not.toBeInstanceOf(MonitorNotEnabledError)
    expect(err.message).toBe("boom")
  })
})

describe("getCase (aws mode)", () => {
  // Belt-and-suspenders: handler.py already falls back to the latest S3 record's
  // `items`/`outcome` server-side when `results` is empty (Outbound IVR's in-process tools,
  // OUTBOUND_IVR_TOOLS=local, never write `results`), but this client-side fallback keeps
  // working even against an API response that didn't do that merge.
  it("falls back to the latest S3 record's items/outcome/started_at when results is empty", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          case_id: "case-1",
          updated_at: "2026-01-05T00:00:00Z",
          results: {},
          records: [
            {
              started_at: "2026-01-01T00:00:00Z",
              items: { physical_therapy: { value: "6 visits", confirmed: true, at: "2026-01-01T00:01:00Z" } },
              outcome: { type: "complete", summary: "done" },
              transcript: [{ speaker: "agent", text: "hi" }],
            },
          ],
        }),
        { status: 200 },
      ),
    )

    const result = await getCase("case-1")

    expect(result.items?.physical_therapy?.confirmed).toBe(true)
    expect(result.outcome?.type).toBe("complete")
    expect(result.started_at).toBe("2026-01-01T00:00:00Z")
  })

  it("prefers the case item's own results/outcome over the S3 fallback when present", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          case_id: "case-1",
          updated_at: "2026-01-05T00:00:00Z",
          results: { physical_therapy: { value: "4 visits", confirmed: true, at: "2026-01-02T00:00:00Z" } },
          outcome: { type: "escalated", reason: "no answer" },
          records: [
            {
              started_at: "2026-01-01T00:00:00Z",
              items: { physical_therapy: { value: "stale", confirmed: true, at: "2026-01-01T00:01:00Z" } },
              outcome: { type: "complete", summary: "stale" },
            },
          ],
        }),
        { status: 200 },
      ),
    )

    const result = await getCase("case-1")

    expect(result.items?.physical_therapy?.value).toBe("4 visits")
    expect(result.outcome?.type).toBe("escalated")
  })

  it("passes each missing item's synthetic front-desk answer through to the checklist", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          case_id: "case-1",
          updated_at: "2026-01-05T00:00:00Z",
          missing_items: [
            { key: "physical_therapy", label: "PT", answer: "Eight weeks, twice a week." },
            { key: "progress_note", label: "Progress note" },
          ],
        }),
        { status: 200 },
      ),
    )

    const result = await getCase("case-1")

    expect(result.checklist).toEqual([
      { key: "physical_therapy", label: "PT", answer: "Eight weeks, twice a week." },
      { key: "progress_note", label: "Progress note", answer: undefined },
    ])
  })
})
