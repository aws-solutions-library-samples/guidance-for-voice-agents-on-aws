// Event shapes the voice agent writes per call, polled by the dashboard from
// `/api/calls/{call}/events` (lambdas/api/handler.py, lib/usePolledEvents.ts) and fed
// into useLiveCall's reducer.

export type CaseSummary = {
  payer?: string
  provider_office?: string
  patient_name?: string
  patient_dob?: string
  member_id?: string
  auth_ref?: string
  procedure?: string
  ordering_provider?: string
  [key: string]: string | undefined
}

export type ChecklistItem = {
  key: string
  label: string
  /** aws mode only: the synthetic front-desk answer from the cases table
   * (scripts/seed_cases.py), for the dashboard's Script panel. Never present on the
   * local `ready` event's checklist, and never read by anything Outbound IVR sees. */
  answer?: string
}

export type ReadyEvent = {
  type: "ready"
  ts: string
  call: string
  mode: string
  model: string
  case: CaseSummary
  checklist: ChecklistItem[]
}

export type PhaseEvent = {
  type: "phase"
  ts: string
  call: string
  phase: "navigate" | "intake"
}

export type HeardEvent = {
  type: "heard"
  ts: string
  call: string
  text: string
}

export type SaidEvent = {
  type: "said"
  ts: string
  call: string
  text: string
}

export type DtmfEvent = {
  type: "dtmf"
  ts: string
  call: string
  digit: string
  option?: string
}

export type StatusEvent = {
  type: "status"
  ts: string
  call: string
  value: "wait" | "completed" | "stuck"
}

export type GotEvent = {
  type: "got"
  ts: string
  call: string
  key: string
  value?: string
  confirmed?: boolean
}

export type EscalateEvent = {
  type: "escalate"
  ts: string
  call: string
  reason?: string
  follow_up?: string
}

export type HandoffEvent = {
  type: "handoff"
  ts: string
  call: string
  // Three outcomes. `handed_off` was missing here, which is the root of the bug where a
  // successful warm transfer displayed as ESCALATED: the reducer was written for the two
  // the type admitted, so `handed_off` fell into the not-complete branch. The agent has
  // always emitted it (src/outbound_ivr/voice/agent.py's transfer_to_team).
  outcome: "complete" | "escalated" | "handed_off"
  summary?: string
  reason?: string
  follow_up?: string
}

// Warm handoff to a human (docs/escalation-handoff.md): written by the agent when
// `transfer_to_team` is called, while it dials, when a "team-" participant joins, and
// if nobody does within the wait.
export type HandoffRequestedEvent = {
  type: "handoff_requested"
  ts: string
  call: string
  reason?: string
  summary?: string
}

export type HandoffRingingEvent = {
  type: "handoff_ringing"
  ts: string
  call: string
}

export type HandoffJoinedEvent = {
  type: "handoff_joined"
  ts: string
  call: string
  identity?: string
}

export type HandoffFailedEvent = {
  type: "handoff_failed"
  ts: string
  call: string
  reason?: string
}

export type EndedEvent = {
  type: "ended"
  ts: string
  call: string
  reason?: string
}

export type ErrorEvent = {
  type: "error"
  ts: string
  call: string
  text?: string
}

export type AgentEvent =
  | ReadyEvent
  | PhaseEvent
  | HeardEvent
  | SaidEvent
  | DtmfEvent
  | StatusEvent
  | GotEvent
  | EscalateEvent
  | HandoffEvent
  | HandoffRequestedEvent
  | HandoffRingingEvent
  | HandoffJoinedEvent
  | HandoffFailedEvent
  | EndedEvent
  | ErrorEvent

export type LevelSample = {
  t: number
  far?: number
  agent?: number
}

export type CaseRecord = {
  id: string
  auth_ref?: string
  patient?: string
  started_at?: string
  items?: Record<string, { value?: string; confirmed?: boolean; at?: string }>
  outcome?: {
    type: "complete" | "escalated" | "handed_off"
    summary?: string
    reason?: string
    follow_up?: string
    missing?: string[]
    model_summary?: string
    /** handed_off only (docs/escalation-handoff.md part 6): who joined, and when. */
    joined_identity?: string
    joined_at?: string
  } | null
  transcript?: { speaker?: string; who?: string; text: string; ts?: string }[]
  /** Every tool call Outbound IVR made, written by the agent's ToolAudit hook. */
  tool_calls?: {
    t: number
    phase: string
    tool: string
    input?: Record<string, unknown>
    status?: string
    result?: string
    ms?: number
    error?: string
  }[]
  case_summary?: CaseSummary
  checklist?: ChecklistItem[]
  recording?: string
}
