// @vitest-environment jsdom
// The call-id-driven branches of useLiveCall: polling events for the real call id. The
// reducer itself is covered in useLiveCall.test.ts.
import { act } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { renderHook } from "@testing-library/react"
import type { AgentEvent } from "@/lib/events"

vi.mock("@/lib/usePolledEvents", () => ({ usePolledEvents: vi.fn() }))

import { usePolledEvents } from "@/lib/usePolledEvents"
import { useLiveCall } from "@/lib/useLiveCall"

beforeEach(() => {
  vi.mocked(usePolledEvents).mockReset()
})

describe("useLiveCall", () => {
  it("polls events for the given call id", () => {
    renderHook(() => useLiveCall("real-call-uuid"))

    expect(usePolledEvents).toHaveBeenCalledWith("real-call-uuid", expect.anything())
  })

  it("passes null to the poller when there is no call id yet (nothing to poll)", () => {
    renderHook(() => useLiveCall(null))

    expect(usePolledEvents).toHaveBeenCalledWith(null, expect.anything())
  })
})

describe("useLiveCall: a poll batch with several dtmf presses", () => {
  // getCallEvents (lib/api.ts) returns AgentEvent & { seq: number } -- usePolledEvents
  // feeds every event from one poll to onMessage, in whatever order the response had
  // them in. Build the real dial-pad call: digits 3, 3, 2 (outbound-ivr-events,
  // call 5cd34a1a-deef-4e0f-b0eb-dee5211b4a7b has these at seq 6, 36, 50), but delivered
  // out of order within a single batch -- a retried page, or events written out of
  // order, lands the way the task describes.
  const dtmf = (digit: string, seq: number): AgentEvent & { seq: number } =>
    ({ type: "dtmf", call: "c1", ts: "2026-10-19T14:02:00Z", digit, seq }) as never

  function fireBatch(order: (AgentEvent & { seq: number })[]) {
    const { result } = renderHook(() => useLiveCall("c1"))
    const onMessage = vi.mocked(usePolledEvents).mock.calls[0]![1].onMessage
    act(() => {
      for (const event of order) onMessage(event)
    })
    return result
  }

  it("counts every press when the batch arrives in seq order", () => {
    const result = fireBatch([dtmf("3", 6), dtmf("3", 36), dtmf("2", 50)])
    expect(result.current.state.pressCounts).toEqual({ "3": 2, "2": 1 })
    expect(result.current.state.presses).toBe(3)
    expect(result.current.state.lastPressedKey).toBe("2")
  })

  it("still counts every press, and keeps the true last key lit, when the batch arrives out of seq order", () => {
    const result = fireBatch([dtmf("2", 50), dtmf("3", 6), dtmf("3", 36)])
    expect(result.current.state.pressCounts).toEqual({ "3": 2, "2": 1 })
    expect(result.current.state.presses).toBe(3)
    // seq 50 ("2") is the real last press, even though it was dispatched first.
    expect(result.current.state.lastPressedKey).toBe("2")
  })
})
