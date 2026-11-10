// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  getActiveCall,
  liveCallNavTarget,
  resetActiveCallForTests,
  setActiveCall,
  subscribeActiveCall,
} from "@/lib/activeCall"

afterEach(() => {
  resetActiveCallForTests()
})

describe("getActiveCall / setActiveCall", () => {
  it("starts with no active call", () => {
    expect(getActiveCall()).toBeNull()
  })

  it("persists a call id so it survives a remount (sessionStorage, not component state)", () => {
    setActiveCall("real-call-uuid")
    expect(getActiveCall()).toBe("real-call-uuid")
    expect(sessionStorage.getItem("outbound_ivr.activeCall")).toBe("real-call-uuid")
  })

  it("clears the stored call when set back to null (End after-call work)", () => {
    setActiveCall("real-call-uuid")
    setActiveCall(null)
    expect(getActiveCall()).toBeNull()
    expect(sessionStorage.getItem("outbound_ivr.activeCall")).toBeNull()
  })
})

describe("subscribeActiveCall", () => {
  it("notifies subscribers whenever the active call changes", () => {
    const listener = vi.fn()
    const unsubscribe = subscribeActiveCall(listener)

    setActiveCall("call-1")
    setActiveCall(null)
    expect(listener).toHaveBeenCalledTimes(2)

    unsubscribe()
    setActiveCall("call-2")
    expect(listener).toHaveBeenCalledTimes(2) // not called again after unsubscribing
  })
})

describe("liveCallNavTarget", () => {
  it("points at the bare route when no call is active", () => {
    expect(liveCallNavTarget(null)).toBe("/")
  })

  it("carries the active call id as a query param", () => {
    expect(liveCallNavTarget("real-call-uuid")).toBe("/?call=real-call-uuid")
  })

  it("encodes the call id", () => {
    expect(liveCallNavTarget("has space")).toBe("/?call=has%20space")
  })
})
