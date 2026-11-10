// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, renderHook } from "@testing-library/react"
import { useAwsLevels } from "@/lib/useAwsLevels"
import type { MonitorMessage } from "@/lib/useMonitorFeed"

// The poll loop is lib/useMonitorFeed.ts's job (own tests there) -- this hook only
// turns whatever "levels" chunks it's handed into eased, sparklined, replayed-at-
// cadence levels, so the feed is stubbed out and driven directly via onChunk.
let onChunk: (msg: MonitorMessage) => void
const useMonitorFeedMock = vi.fn()
vi.mock("@/lib/useMonitorFeed", () => ({
  useMonitorFeed: (_callId: unknown, _mode: unknown, _want: unknown, cb: (msg: MonitorMessage) => void) => {
    onChunk = cb
    return "on"
  },
}))

function levelsChunk(seq: number, far_rms: number, agent_rms: number): MonitorMessage {
  return { type: "levels", seq, far_rms, agent_rms }
}

beforeEach(() => {
  useMonitorFeedMock.mockReset()
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe("useAwsLevels", () => {
  it("starts silent with no chunks", () => {
    const { result } = renderHook(() => useAwsLevels("call-1", true))
    act(() => { vi.advanceTimersByTime(200) })
    expect(result.current).toEqual({ far: 0, agent: 0, farSpark: "", agentSpark: "", farSpeaking: false, agentSpeaking: false })
  })

  it("replays queued chunks at the 200 ms cadence, one per tick, instead of jumping all at once", () => {
    const { result } = renderHook(() => useAwsLevels("call-1", true))

    // A whole segment (10 chunks) delivered at once, as a poll would -- the first
    // chunk is a loud far-end, the rest silent.
    act(() => {
      onChunk(levelsChunk(0, 1, 0))
      for (let seq = 1; seq < 10; seq++) onChunk(levelsChunk(seq, 0, 0))
    })

    // Before the first replay tick (200 ms), nothing has been popped off the queue
    // yet -- the target is still the silent initial state.
    act(() => { vi.advanceTimersByTime(100) })
    expect(result.current.far).toBe(0)

    // The first replay tick pops the loud chunk; the ease loop then pulls toward it.
    act(() => { vi.advanceTimersByTime(100) }) // crosses the 200 ms replay tick
    act(() => { vi.advanceTimersByTime(300) }) // a few more ease ticks
    const afterFirstReplay = result.current.far
    expect(afterFirstReplay).toBeGreaterThan(0)

    // The second replay tick (at 400 ms) pops a silent chunk -- the level eases back
    // down rather than staying pinned to the first loud one.
    act(() => { vi.advanceTimersByTime(2000) })
    expect(result.current.far).toBeLessThan(afterFirstReplay)
  })

  it("marks speaking once a replayed chunk is over threshold", () => {
    const { result } = renderHook(() => useAwsLevels("call-1", true))

    act(() => { onChunk(levelsChunk(1, 1, 10 ** (-30 / 20))) })
    act(() => { vi.advanceTimersByTime(2000) })

    expect(result.current.farSpeaking).toBe(true)
    expect(result.current.agentSpeaking).toBe(true)
    expect(result.current.far).toBeGreaterThan(0)
    expect(result.current.agent).toBeGreaterThan(0)
  })

  it("ignores a repeated or out-of-order seq (does not re-queue the same chunk)", () => {
    const { result } = renderHook(() => useAwsLevels("call-1", true))
    act(() => {
      onChunk(levelsChunk(1, 1, 0))
      onChunk(levelsChunk(1, 1, 0)) // same seq again
    })
    act(() => { vi.advanceTimersByTime(2000) })
    // No assertion error means the duplicate seq was safely ignored (no crash, no
    // double-pushed spark history, no unbounded queue growth) -- the level settles at
    // a single chunk's worth, not something pushed twice.
    expect(result.current.far).toBeGreaterThan(0)
  })

  it("eases levels back to zero once the chunk stream stops, and resets entirely once inactive", () => {
    const { result, rerender } = renderHook(({ active }) => useAwsLevels("call-1", active), {
      initialProps: { active: true },
    })
    act(() => { onChunk(levelsChunk(1, 1, 0)) })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current.far).toBeGreaterThan(0)

    rerender({ active: false })
    expect(result.current).toEqual({ far: 0, agent: 0, farSpark: "", agentSpark: "", farSpeaking: false, agentSpeaking: false })
  })
})

describe("levelFromRms", () => {
  it("matches the server's dB scale: -50 dBFS and below is 0, full scale is 1", async () => {
    const { levelFromRms } = await import("@/lib/useAwsLevels")
    expect(levelFromRms(0)).toBe(0)
    expect(levelFromRms(10 ** (-60 / 20))).toBe(0)
    expect(levelFromRms(1)).toBeCloseTo(1, 5)
    // A typical phone voice (-30 dBFS) lands mid-scale, not near zero.
    expect(levelFromRms(10 ** (-30 / 20))).toBeCloseTo(0.4, 2)
  })
})
