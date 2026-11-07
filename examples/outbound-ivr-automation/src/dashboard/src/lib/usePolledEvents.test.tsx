// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, renderHook, waitFor } from "@testing-library/react"
import { usePolledEvents } from "@/lib/usePolledEvents"
import { getCallEvents } from "@/lib/api"

vi.mock("@/lib/api", () => ({ getCallEvents: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.mocked(getCallEvents).mockReset()
})

describe("usePolledEvents", () => {
  it("does not skip an event that becomes visible after a later one", async () => {
    const poll = vi.mocked(getCallEvents)
    // First poll sees seq 0 and 2 (1 not yet visible), the next poll sees 1.
    poll.mockResolvedValueOnce([
      { seq: 0, type: "dtmf", digit: "3" },
      { seq: 2, type: "dtmf", digit: "2" },
    ] as never)
    poll.mockResolvedValue([{ seq: 1, type: "dtmf", digit: "3" }] as never)
    const onMessage = vi.fn()
    renderHook(() => usePolledEvents("call-1", { onMessage }, 10))
    await waitFor(() => expect(onMessage).toHaveBeenCalledTimes(3))
    // The second poll asked from seq 0 (the last gap-free seq), not 2.
    expect(poll.mock.calls[1]).toEqual(["call-1", 0])
    expect(onMessage.mock.calls.map((c) => c[0].seq).sort()).toEqual([0, 1, 2])
  })

  it("never delivers the same seq twice", async () => {
    const poll = vi.mocked(getCallEvents)
    poll.mockResolvedValue([{ seq: 0, type: "phase" }] as never)
    const onMessage = vi.fn()
    renderHook(() => usePolledEvents("call-2", { onMessage }, 10))
    await waitFor(() => expect(poll.mock.calls.length).toBeGreaterThan(2))
    expect(onMessage).toHaveBeenCalledTimes(1)
  })
})
