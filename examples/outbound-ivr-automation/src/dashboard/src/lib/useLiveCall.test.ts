import { describe, expect, it } from "vitest"
import type { AgentEvent } from "@/lib/events"
import { endedReasonLabel, initialState, reducer, type LiveCallState } from "@/lib/useLiveCall"

function event(e: AgentEvent): { type: "event"; event: AgentEvent } {
  return { type: "event", event: e }
}

function startedState(phase?: "navigate" | "intake"): LiveCallState {
  let state = reducer(initialState, event({ type: "ready", ts: "", call: "c1", mode: "full_call", model: "m", case: {}, checklist: [] }))
  if (phase) {
    state = reducer(state, event({ type: "phase", ts: "", call: "c1", phase }))
  }
  return state
}

describe("endedReasonLabel", () => {
  it("is 'ended before the front desk' for a far-end hangup while still at the menu", () => {
    expect(endedReasonLabel("far end hung up", "navigate")).toBe("ended before the front desk")
    expect(endedReasonLabel("far end hung up", undefined)).toBe("ended before the front desk")
  })

  it("is 'ended before the front desk' for a torn-down call (End call) while still at the menu", () => {
    expect(endedReasonLabel("call torn down", "navigate")).toBe("ended before the front desk")
  })

  it("is 'Outbound IVR hung up' once the front desk phase has started, even on a far-end hangup", () => {
    expect(endedReasonLabel("far end hung up", "intake")).toBe("the front desk side hung up")
  })

  it("is 'Outbound IVR hung up' when the agent itself hung up, regardless of phase", () => {
    expect(endedReasonLabel("agent hung up", "navigate")).toBe("Outbound IVR hung up")
    expect(endedReasonLabel("agent hung up", "intake")).toBe("Outbound IVR hung up")
  })
})

describe("useLiveCall reducer: ended event", () => {
  it("marks the call ended and hung up, and shows a 'call ended' verdict", () => {
    const state = startedState("navigate")
    const next = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "far end hung up" }))

    expect(next.ended).toBe(true)
    expect(next.hungUp).toBe(true)
    expect(next.verdict.big).toBe("CALL ENDED")
    expect(next.verdict.cap).toBe("ended before the front desk")
  })

  it("clears the current phase so neither menu chip stays marked as current", () => {
    const state = startedState("navigate")
    expect(state.phase).toBe("navigate")

    const next = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "far end hung up" }))
    expect(next.phase).toBeUndefined()
  })

  it("ending while talking to the front desk reports Outbound IVR hanging up", () => {
    const state = startedState("intake")
    const next = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "agent hung up" }))

    expect(next.verdict.cap).toBe("Outbound IVR hung up")
  })

  it("overrides a stuck 'waiting' status left over from before the hangup", () => {
    let state = startedState("navigate")
    state = reducer(state, event({ type: "status", ts: "", call: "c1", value: "wait" }))
    expect(state.verdict.kind).toBe("wait")

    const next = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "far end hung up" }))
    expect(next.verdict.big).toBe("CALL ENDED")
    expect(next.verdict.kind).not.toBe("wait")
  })

  it("does not require a reason to still report ended-by-you at the menu", () => {
    const state = startedState("navigate")
    const next = reducer(state, event({ type: "ended", ts: "", call: "c1" }))
    expect(next.verdict.cap).toBe("ended before the front desk")
  })
})

describe("useLiveCall reducer: switch (new call)", () => {
  it("resets fully back to the initial state, keeping only the connection flag", () => {
    let state = startedState("navigate")
    state = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "agent hung up" }))
    state = reducer(state, { type: "connection", connected: true })
    expect(state.hungUp).toBe(true)

    const next = reducer(state, { type: "switch" })
    expect(next).toEqual({ ...initialState, connected: true })
  })
})

describe("useLiveCall reducer: handoff to a human (docs/escalation-handoff.md)", () => {
  it("handoff_requested shows a waiting verdict with the reason, and logs it", () => {
    const state = startedState("intake")
    const next = reducer(
      state,
      event({ type: "handoff_requested", ts: "", call: "c1", reason: "front desk cannot authorize this" }),
    )
    expect(next.verdict.big).toBe("BRINGING IN A COLLEAGUE")
    expect(next.verdict.cap).toBe("front desk cannot authorize this")
    expect(next.verdict.kind).toBe("wait")
    expect(next.log[0].text).toContain("HANDOFF REQUESTED")
    expect(next.ended).toBe(false)
  })

  it("handoff_ringing just logs — it doesn't change the verdict", () => {
    const state = reducer(startedState("intake"), event({ type: "handoff_requested", ts: "", call: "c1", reason: "r" }))
    const next = reducer(state, event({ type: "handoff_ringing", ts: "", call: "c1" }))
    expect(next.verdict).toEqual(state.verdict)
    expect(next.log[0].text).toContain("ringing")
  })

  it("handoff_joined marks the call ended (outcome decided) with a HANDED OFF verdict", () => {
    const state = startedState("intake")
    const next = reducer(state, event({ type: "handoff_joined", ts: "", call: "c1", identity: "team-abc" }))
    expect(next.verdict.big).toBe("HANDED OFF")
    expect(next.verdict.kind).toBe("done")
    expect(next.ended).toBe(true)
    expect(next.log[0].text).toContain("team-abc")
  })

  it("a later 'ended' event does not overwrite the HANDED OFF verdict", () => {
    let state = startedState("intake")
    state = reducer(state, event({ type: "handoff_joined", ts: "", call: "c1", identity: "team-abc" }))
    const next = reducer(state, event({ type: "ended", ts: "", call: "c1", reason: "far end hung up" }))
    expect(next.verdict.big).toBe("HANDED OFF")
    expect(next.hungUp).toBe(true)
  })

  it("handoff_failed shows a NO ANSWER verdict and logs the reason", () => {
    const state = startedState("intake")
    const next = reducer(state, event({ type: "handoff_failed", ts: "", call: "c1", reason: "no answer" }))
    expect(next.verdict.big).toBe("NO ANSWER")
    expect(next.verdict.kind).toBe("err")
    expect(next.ended).toBe(false)
  })
})

describe("ended after an outcome", () => {
  it("keeps the recorded outcome instead of replacing it with CALL ENDED", () => {
    const withOutcome: LiveCallState = {
      ...initialState,
      ended: true,
      phase: "intake",
      verdict: { kind: "done", big: "COMPLETE", cap: "all items collected" },
    }
    const next = reducer(withOutcome, {
      type: "event",
      event: { type: "ended", reason: "agent hung up", ts: "2026-10-17T00:00:00Z", call: "c" } as never,
    })
    expect(next.verdict.big).toBe("COMPLETE")
    expect(next.hungUp).toBe(true)
  })
})

describe("clear (End call)", () => {
  const ev = (type: string, extra: Record<string, unknown> = {}) =>
    ({ type, call: "c1", ts: "2026-10-17T00:00:00Z", ...extra }) as never

  it("wipes presses, transcript and events, and ignores the ended call's late events", () => {
    let s = reducer(initialState, { type: "event", event: ev("dtmf", { digit: "2", option: "providers" }) })
    s = reducer(s, { type: "event", event: ev("heard", { text: "for providers press two" }) })
    expect(s.presses).toBeGreaterThan(0)
    s = reducer(s, { type: "clear" })
    expect(s.presses).toBe(0)
    expect(s.pressCounts).toEqual({})
    expect(s.transcript).toEqual([])
    expect(s.log).toEqual([])
    s = reducer(s, { type: "event", event: ev("ended", { reason: "far end hung up" }) })
    expect(s.log).toEqual([])
    expect(s.hungUp).toBe(false)
  })

  it("shows the next call's events normally", () => {
    let s = reducer(initialState, { type: "event", event: ev("heard", { text: "hello" }) })
    s = reducer(s, { type: "clear" })
    s = reducer(s, { type: "event", event: { type: "heard", call: "c2", text: "new call", ts: "2026-10-17T00:01:00Z" } as never })
    expect(s.transcript.length).toBe(1)
  })
})

describe("useLiveCall reducer: duplicate ready event", () => {
  it("does not log 'call started' twice for a repeated ready on the same call", () => {
    const readyEvent = event({ type: "ready", ts: "", call: "c1", mode: "full_call", model: "m", case: {}, checklist: [] })
    let s = reducer(initialState, readyEvent)
    expect(s.log.filter((entry) => entry.text === "call started").length).toBe(1)
    s = reducer(s, readyEvent)
    expect(s.log.filter((entry) => entry.text === "call started").length).toBe(1)
  })

  it("does not reset presses back to 0 on a repeated ready for the same call", () => {
    let s = startedState("intake")
    s = reducer(s, {
      type: "event",
      event: { type: "dtmf", call: "c1", ts: "", digit: "2", option: "providers" } as never,
    })
    expect(s.presses).toBeGreaterThan(0)
    const pressesBefore = s.presses
    s = reducer(s, { type: "event", event: { type: "ready", ts: "", call: "c1", mode: "full_call", model: "m", case: {}, checklist: [] } })
    expect(s.presses).toBe(pressesBefore)
  })

  it("still logs 'call started' once for a genuinely new call", () => {
    let s = reducer(initialState, event({ type: "ready", ts: "", call: "c1", mode: "full_call", model: "m", case: {}, checklist: [] }))
    s = reducer(s, event({ type: "ready", ts: "", call: "c2", mode: "full_call", model: "m", case: {}, checklist: [] }))
    expect(s.log.filter((entry) => entry.text === "call started").length).toBe(2)
  })
})

describe("the handoff outcome has three cases, not two", () => {
  // The agent writes `handoff_joined` and then `handoff` about 8 milliseconds apart.
  // `handoff` used to compute `outcome === "complete" ? "COMPLETE" : "ESCALATED"`, so
  // the correct "HANDED OFF" verdict from `handoff_joined` was immediately overwritten
  // with "ESCALATED" on every successful warm transfer. Confirmed against the real
  // event stream of a live call.
  function handedOffState(): LiveCallState {
    let state = startedState("intake")
    state = reducer(
      state,
      event({ type: "handoff_joined", ts: "", call: "c1", identity: "evidence-1" }),
    )
    return state
  }

  it("keeps HANDED OFF when a colleague took the call", () => {
    const before = handedOffState()
    expect(before.verdict?.big).toBe("HANDED OFF")

    const after = reducer(
      before,
      event({ type: "handoff", ts: "", call: "c1", outcome: "handed_off" }),
    )
    expect(after.verdict?.big).toBe("HANDED OFF")
    expect(after.verdict?.kind).toBe("done")
  })

  it("still says COMPLETE when the call completed", () => {
    const after = reducer(
      startedState("intake"),
      event({ type: "handoff", ts: "", call: "c1", outcome: "complete" }),
    )
    expect(after.verdict?.big).toBe("COMPLETE")
  })

  it("still says ESCALATED for a real escalation", () => {
    const after = reducer(
      startedState("intake"),
      event({
        type: "handoff",
        ts: "",
        call: "c1",
        outcome: "escalated",
        reason: "front desk refused",
      }),
    )
    expect(after.verdict?.big).toBe("ESCALATED")
  })
})
