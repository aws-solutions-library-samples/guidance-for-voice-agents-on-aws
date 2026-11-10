// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, renderHook } from "@testing-library/react"
import { useMonitorFeed } from "@/lib/useMonitorFeed"
import { MonitorNotEnabledError } from "@/lib/api"

const getMonitorSegmentsMock = vi.fn()
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api")
  return { ...actual, getMonitorSegments: (...args: unknown[]) => getMonitorSegmentsMock(...args) }
})
vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }))

function renderFeed(callId: string | null, mode: "listen" | "levels", want: boolean) {
  const chunks: unknown[] = []
  const hook = renderHook(() => useMonitorFeed(callId, mode, want, (msg) => chunks.push(msg)))
  return { ...hook, chunks }
}

beforeEach(() => {
  vi.useFakeTimers()
  getMonitorSegmentsMock.mockReset().mockResolvedValue({ segments: [], after: -1 })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe("useMonitorFeed", () => {
  it("polls for the call and mode once acquired, and reports 'on'", async () => {
    const { result } = renderFeed("call-1", "listen", true)
    expect(result.current).toBe("connecting")

    await vi.waitFor(() => expect(getMonitorSegmentsMock).toHaveBeenCalledWith("call-1", "listen", -1))
    await vi.waitFor(() => expect(result.current).toBe("on"))
  })

  it("never polls while not wanted, or with no call id", () => {
    renderFeed(null, "listen", true)
    renderFeed("call-1", "listen", false)

    expect(getMonitorSegmentsMock).not.toHaveBeenCalled()
  })

  it("delivers chunks to subscribers in order, one callback per chunk, with the documented seq", async () => {
    getMonitorSegmentsMock.mockResolvedValue({
      segments: [
        { seq: 3, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0.1, agent_rms: 0, pcm: "AA==" }, { far_rms: 0.2, agent_rms: 0, pcm: "AQ==" }] },
      ],
      after: 3,
    })
    const { chunks } = renderFeed("call-1", "listen", true)

    await vi.waitFor(() => expect(chunks).toHaveLength(2))
    expect(chunks[0]).toEqual({ type: "audio", seq: 30, pcm: "AA==", far_rms: 0.1, agent_rms: 0 })
    expect(chunks[1]).toEqual({ type: "audio", seq: 31, pcm: "AQ==", far_rms: 0.2, agent_rms: 0 })
  })

  it("strips pcm in 'levels' mode, emitting a 'levels' message instead", async () => {
    getMonitorSegmentsMock.mockResolvedValue({
      segments: [{ seq: 0, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0.5, agent_rms: 0.1 }] }],
      after: 0,
    })
    const { chunks } = renderFeed("call-1", "levels", true)

    await vi.waitFor(() => expect(chunks).toHaveLength(1))
    expect(chunks[0]).toEqual({ type: "levels", seq: 0, far_rms: 0.5, agent_rms: 0.1 })
  })

  it("advances the cursor and never redelivers a chunk already seen across polls", async () => {
    getMonitorSegmentsMock
      .mockResolvedValueOnce({ segments: [{ seq: 0, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0, agent_rms: 0 }] }], after: 0 })
      .mockResolvedValueOnce({ segments: [{ seq: 1, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0, agent_rms: 0 }] }], after: 1 })

    const { chunks } = renderFeed("call-1", "levels", true)
    await vi.waitFor(() => expect(chunks).toHaveLength(1))

    await vi.advanceTimersByTimeAsync(2000)
    await vi.waitFor(() => expect(chunks).toHaveLength(2))

    expect(getMonitorSegmentsMock).toHaveBeenNthCalledWith(2, "call-1", "levels", 0)
    expect(chunks.map((c) => (c as { seq: number }).seq)).toEqual([0, 10])
  })

  it("shares a single poll loop across two consumers of the same call and mode", async () => {
    const a = renderFeed("call-1", "listen", true)
    const b = renderFeed("call-1", "listen", true)

    await vi.waitFor(() => expect(a.result.current).toBe("on"))
    await vi.waitFor(() => expect(b.result.current).toBe("on"))
    expect(getMonitorSegmentsMock).toHaveBeenCalledTimes(1)
  })

  it("keeps separate poll loops for 'listen' and 'levels' on the same call", async () => {
    renderFeed("call-1", "listen", true)
    renderFeed("call-1", "levels", true)

    await vi.waitFor(() => expect(getMonitorSegmentsMock).toHaveBeenCalledWith("call-1", "listen", -1))
    await vi.waitFor(() => expect(getMonitorSegmentsMock).toHaveBeenCalledWith("call-1", "levels", -1))
  })

  it("stops polling once every consumer has let go", async () => {
    const a = renderFeed("call-1", "listen", true)
    const b = renderFeed("call-1", "listen", true)
    await vi.waitFor(() => expect(a.result.current).toBe("on"))

    a.unmount()
    getMonitorSegmentsMock.mockClear()
    await vi.advanceTimersByTimeAsync(2000)
    expect(getMonitorSegmentsMock).toHaveBeenCalled() // b still holds it

    getMonitorSegmentsMock.mockClear()
    b.unmount()
    await vi.advanceTimersByTimeAsync(5000)
    expect(getMonitorSegmentsMock).not.toHaveBeenCalled()
  })

  it("goes 'off' (and stops polling) on a 404 -- monitor not enabled", async () => {
    getMonitorSegmentsMock.mockRejectedValue(new MonitorNotEnabledError("monitor not enabled"))
    const { result } = renderFeed("call-1", "listen", true)

    await vi.waitFor(() => expect(result.current).toBe("off"))

    getMonitorSegmentsMock.mockClear()
    await vi.advanceTimersByTimeAsync(5000)
    expect(getMonitorSegmentsMock).not.toHaveBeenCalled()
  })

  it("keeps polling with the same `after` on a transient error", async () => {
    getMonitorSegmentsMock
      .mockResolvedValueOnce({ segments: [{ seq: 0, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0, agent_rms: 0 }] }], after: 0 })
      .mockRejectedValueOnce(new Error("network blip"))
      .mockResolvedValueOnce({ segments: [{ seq: 1, t0: 1, chunk_ms: 200, chunks: [{ far_rms: 0, agent_rms: 0 }] }], after: 1 })

    const { result, chunks } = renderFeed("call-1", "listen", true)
    await vi.waitFor(() => expect(chunks).toHaveLength(1))

    await vi.advanceTimersByTimeAsync(2000)
    await vi.waitFor(() => expect(getMonitorSegmentsMock).toHaveBeenCalledTimes(2))
    expect(result.current).toBe("on") // a single failed poll doesn't flip status off

    await vi.advanceTimersByTimeAsync(2000)
    await vi.waitFor(() => expect(chunks).toHaveLength(2))
    expect(getMonitorSegmentsMock).toHaveBeenNthCalledWith(3, "call-1", "listen", 0)
  })

  it("reconnects to a new call once the call id changes", async () => {
    const { result, rerender } = renderHook(
      ({ callId }) => useMonitorFeed(callId, "listen", true, () => {}),
      { initialProps: { callId: "call-1" } },
    )
    await vi.waitFor(() => expect(result.current).toBe("on"))

    getMonitorSegmentsMock.mockClear()
    rerender({ callId: "call-2" })
    await vi.waitFor(() => expect(getMonitorSegmentsMock).toHaveBeenCalledWith("call-2", "listen", -1))
  })
})
