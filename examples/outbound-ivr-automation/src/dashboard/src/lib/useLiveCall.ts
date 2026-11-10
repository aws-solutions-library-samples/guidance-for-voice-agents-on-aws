import { useCallback, useReducer } from "react"
import type { AgentEvent, CaseSummary, ChecklistItem } from "@/lib/events"
import { usePolledEvents } from "@/lib/usePolledEvents"

export type VerdictKind = "idle" | "wait" | "done" | "err" | ""

export type LogKind = "call" | "status" | "heard" | "said" | "dtmf" | "evidence" | "outcome" | "err"

export type LogEntry = {
  id: number
  cls: "heard" | "said" | "dtmf" | "status" | "err"
  /** What the entry is about, shown as a label on the card. */
  kind: LogKind
  text: string
  ts: number
}

export type TranscriptTurn = {
  id: number
  cls: "heard" | "said"
  who: string
  text: string
}

export type ChecklistState = {
  got: boolean
  value?: string
  confirmed?: boolean
}

export type LiveCallState = {
  connected: boolean
  mode?: string
  model?: string
  caseSummary?: CaseSummary
  checklist: ChecklistItem[]
  checklistState: Record<string, ChecklistState>
  phase?: "navigate" | "intake"
  verdict: { kind: VerdictKind; big: string; cap: string }
  pressCounts: Record<string, number>
  pressSeq: number
  lastPressedKey?: string
  said: string
  transcript: TranscriptTurn[]
  log: LogEntry[]
  presses: number
  started: boolean
  ended: boolean
  /** The call has actually hung up ("ended" event), as opposed to the outcome being recorded. */
  hungUp: boolean
  /** The call the events belong to, and one the user cleared with End call (its late events are ignored). */
  callId?: string
  ignoredCall?: string
  /**
   * aws mode only: the highest event `seq` applied so far (see usePolledEvents.ts), used
   * to tell a late-arriving-but-older event apart from the actual most recent one. A
   * poll batch can carry several dtmf presses, or (pagination, retries) the same batch
   * out of order; counts (presses, pressCounts) are fine either way since addition
   * doesn't care about order, but "which key was pressed last" does -- see the "dtmf"
   * case below.
   */
  lastSeq?: number
}

export const initialState: LiveCallState = {
  connected: false,
  checklist: [],
  checklistState: {},
  verdict: { kind: "", big: "", cap: "waiting for a call…" },
  pressCounts: {},
  pressSeq: 0,
  said: "",
  transcript: [],
  log: [],
  presses: 0,
  started: false,
  ended: false,
  hungUp: false,
}

let logSeq = 0
let turnSeq = 0

/**
 * Which human-readable reason to show for an "ended" event, from the backend's `reason`
 * (see src/outbound_ivr/voice/agent.py and livekit_calls.ensure_ended_event) and the phase the
 * call was in when it ended. End call and the office hanging up look the same to the
 * agent (the far side goes away), so the label says where the call was, not who ended it.
 */
export function endedReasonLabel(reason: string | undefined, phase: LiveCallState["phase"]): string {
  if (reason === "agent hung up") return "Outbound IVR hung up"
  if (phase === "intake") return "the front desk side hung up"
  return "ended before the front desk"
}

export type Action =
  | { type: "connection"; connected: boolean }
  | { type: "switch" }
  | { type: "clear" }
  | { type: "event"; event: AgentEvent }

function pushLog(log: LogEntry[], entry: Omit<LogEntry, "id">): LogEntry[] {
  const next = [{ id: ++logSeq, ...entry }, ...log]
  return next.length > 300 ? next.slice(0, 300) : next
}

export function reducer(state: LiveCallState, action: Action): LiveCallState {
  if (action.type === "connection") {
    return { ...state, connected: action.connected }
  }
  if (action.type === "switch") {
    return { ...initialState, connected: state.connected }
  }
  if (action.type === "clear") {
    // End call: wipe the page (keypad presses, transcript, events) and drop the ended
    // call's stragglers, so it looks like no call happened until the next Start call.
    return { ...initialState, connected: state.connected, ignoredCall: state.callId ?? state.ignoredCall }
  }

  const msg = action.event
  const msgCall = (msg as { call?: string }).call
  if (msgCall && msgCall === state.ignoredCall) return state
  const priorCallId = state.callId
  const priorStarted = state.started
  if (msgCall && msgCall !== state.callId) state = { ...state, callId: msgCall }
  const ts = msg.ts ? new Date(msg.ts).getTime() : Date.now()

  // aws mode: `/api/calls/{call}/events` carries a `seq` on every event (see
  // usePolledEvents.ts). A single poll's batch can land several dtmf presses at once,
  // or -- out-of-order delivery, a retried page -- the same batch's events arrive in
  // something other than seq order. `isNewestEvent` says whether this is the most
  // recent event seen for this call so far, by seq rather than by dispatch order, so
  // "which key did they press last" (lastPressedKey, pressSeq below) tracks the real
  // last press instead of whichever event happened to be dispatched last. Events with
  // no `seq` (local mode's SSE stream) always count as newest -- there's nothing to
  // compare against.
  const msgSeq = (msg as { seq?: number }).seq
  const isNewestEvent = msgSeq === undefined || state.lastSeq === undefined || msgSeq >= state.lastSeq
  if (msgSeq !== undefined) state = { ...state, lastSeq: Math.max(state.lastSeq ?? msgSeq, msgSeq) }

  switch (msg.type) {
    case "ready": {
      // A second "ready" for the same call (a duplicate backend message, or a
      // reconnect that replays it) must not add a second "call started" line to the
      // log, or reset presses back to 0 mid-call. Compare against the callId from
      // *before* this reducer call's generic callId update above, since that update
      // already adopted msg.call by this point.
      const alreadyStartedThisCall = priorStarted && priorCallId === msg.call
      return {
        ...state,
        started: true,
        mode: msg.mode,
        model: msg.model,
        caseSummary: msg.case,
        checklist: msg.checklist,
        presses: alreadyStartedThisCall ? state.presses : 0,
        log: alreadyStartedThisCall
          ? state.log
          : pushLog(state.log, { cls: "heard", kind: "call", text: "call started", ts }),
      }
    }
    case "phase": {
      const verdict =
        msg.phase === "navigate"
          ? { kind: "" as VerdictKind, big: "MENU", cap: "navigating the phone menu" }
          : { kind: "" as VerdictKind, big: "TALKING", cap: "talking to the front desk — collecting evidence" }
      return {
        ...state,
        phase: msg.phase,
        verdict,
        log: pushLog(state.log, {
          cls: "status",
          kind: "call",
          text: `phase: ${msg.phase === "navigate" ? "navigating menu" : "talking to the front desk"}`,
          ts,
        }),
      }
    }
    case "heard": {
      return {
        ...state,
        log: pushLog(state.log, { cls: "heard", kind: "heard", text: msg.text, ts }),
        transcript: [...state.transcript, { id: ++turnSeq, cls: "heard", who: "caller", text: msg.text }],
      }
    }
    case "said": {
      return {
        ...state,
        said: msg.text,
        log: pushLog(state.log, { cls: "said", kind: "said", text: msg.text, ts }),
        transcript: [...state.transcript, { id: ++turnSeq, cls: "said", who: "agent", text: msg.text }],
      }
    }
    case "dtmf": {
      return {
        ...state,
        // Every press counts, regardless of arrival order.
        presses: state.presses + 1,
        pressCounts: { ...state.pressCounts, [msg.digit]: (state.pressCounts[msg.digit] ?? 0) + 1 },
        // But "which key lit up last" must track the real last press (by seq), not
        // whichever dtmf happened to be dispatched last -- an older, out-of-order event
        // must not stomp a newer press's highlight.
        pressSeq: isNewestEvent ? state.pressSeq + 1 : state.pressSeq,
        lastPressedKey: isNewestEvent ? msg.digit : state.lastPressedKey,
        log: pushLog(state.log, {
          cls: "dtmf",
          kind: "dtmf",
          text: `PRESS ${msg.digit}${msg.option ? ` — ${msg.option}` : ""}`,
          ts,
        }),
      }
    }
    case "status": {
      const map: Record<string, { kind: VerdictKind; big: string; cap: string }> = {
        completed: { kind: "", big: "MENU DONE", cap: "a human is coming on the line" },
        wait: { kind: "wait", big: "WAIT", cap: "nothing matched — holding" },
        stuck: { kind: "err", big: "STUCK", cap: "no way forward" },
      }
      const verdict = map[msg.value] ?? state.verdict
      return {
        ...state,
        verdict,
        log: pushLog(state.log, { cls: "status", kind: "status", text: `status: ${msg.value.toUpperCase()}`, ts }),
      }
    }
    case "got": {
      const prev = state.checklistState[msg.key] ?? { got: false }
      return {
        ...state,
        checklistState: {
          ...state.checklistState,
          [msg.key]: { got: true, value: msg.value ?? prev.value, confirmed: msg.confirmed },
        },
        log: pushLog(state.log, {
          cls: "dtmf",
          kind: "evidence",
          text: `captured ${msg.key}${msg.confirmed ? " (confirmed)" : ""}`,
          ts,
        }),
      }
    }
    case "escalate": {
      return {
        ...state,
        verdict: { kind: "wait", big: "ESCALATING", cap: (msg.reason ?? "").slice(0, 70) },
        log: pushLog(state.log, {
          cls: "status",
          kind: "outcome",
          text: `ESCALATE — ${msg.reason ?? ""}${msg.follow_up ? ` · follow up: ${msg.follow_up}` : ""}`,
          ts,
        }),
      }
    }
    case "handoff_requested": {
      return {
        ...state,
        verdict: { kind: "wait", big: "BRINGING IN A COLLEAGUE", cap: (msg.reason ?? "").slice(0, 70) },
        log: pushLog(state.log, {
          cls: "status",
          kind: "outcome",
          text: `HANDOFF REQUESTED — ${msg.reason ?? ""}`,
          ts,
        }),
      }
    }
    case "handoff_ringing": {
      return {
        ...state,
        log: pushLog(state.log, { cls: "status", kind: "outcome", text: "HANDOFF — ringing the Evidence team", ts }),
      }
    }
    case "handoff_joined": {
      return {
        ...state,
        ended: true,
        verdict: { kind: "done", big: "HANDED OFF", cap: "a colleague is on the line" },
        log: pushLog(state.log, {
          cls: "status",
          kind: "outcome",
          text: `HANDOFF — ${msg.identity ?? "a colleague"} joined`,
          ts,
        }),
      }
    }
    case "handoff_failed": {
      return {
        ...state,
        verdict: { kind: "err", big: "NO ANSWER", cap: (msg.reason ?? "no answer").slice(0, 70) },
        log: pushLog(state.log, {
          cls: "status",
          kind: "outcome",
          text: `HANDOFF — no answer (${msg.reason ?? "no answer"})`,
          ts,
        }),
      }
    }
    case "handoff": {
      // Three outcomes, not two. `handed_off` used to fall into the ESCALATED bucket
      // simply because it was not "complete", and since the agent writes
      // `handoff_joined` and then `handoff` about 8ms apart, the correct "HANDED OFF"
      // verdict set just above was immediately overwritten with "ESCALATED" on every
      // successful warm transfer.
      const handedOff = msg.outcome === "handed_off"
      const ok = msg.outcome === "complete"
      const reason =
        msg.reason ??
        msg.follow_up ??
        (ok ? "all three items collected" : handedOff ? "a colleague is on the line" : "handed to a human")
      return {
        ...state,
        ended: true,
        verdict: {
          kind: ok || handedOff ? "done" : "wait",
          big: ok ? "COMPLETE" : handedOff ? "HANDED OFF" : "ESCALATED",
          cap: reason.slice(0, 90),
        },
        log: pushLog(state.log, { cls: "status", kind: "outcome", text: `HANDOFF — ${msg.outcome.toUpperCase()}`, ts }),
      }
    }
    case "ended": {
      const reasonLabel = endedReasonLabel(msg.reason, state.phase)
      return {
        ...state,
        ended: true,
        hungUp: true,
        // Stop showing the menu/front-desk chips as current -- the call is over. Keep
        // the outcome (complete, escalated) if one was recorded; otherwise say it ended.
        phase: undefined,
        verdict: state.ended ? state.verdict : { kind: "idle", big: "CALL ENDED", cap: reasonLabel },
        log: pushLog(state.log, { cls: "status", kind: "call", text: `call ended — ${reasonLabel}`, ts }),
      }
    }
    case "error": {
      return {
        ...state,
        verdict: { kind: "err", big: "ERROR", cap: "see events" },
        log: pushLog(state.log, { cls: "err", kind: "err", text: msg.text ?? "", ts }),
      }
    }
    default:
      return state
  }
}

/**
 * `callId` is the real call id `POST /api/calls` returned when the call was started
 * (not the case id), and selects which call's events `/api/calls/{call}/events` is
 * polled for -- see lib/usePolledEvents.ts.
 */
export function useLiveCall(callId: string | null = null) {
  const [state, dispatch] = useReducer(reducer, initialState)

  const onMessage = useCallback((event: AgentEvent) => dispatch({ type: "event", event }), [])
  const onSwitch = useCallback(() => dispatch({ type: "switch" }), [])
  const clear = useCallback(() => dispatch({ type: "clear" }), [])
  const onConnectionChange = useCallback((connected: boolean) => dispatch({ type: "connection", connected }), [])

  usePolledEvents(callId, { onMessage, onSwitch, onConnectionChange })

  return { state, clear }
}
