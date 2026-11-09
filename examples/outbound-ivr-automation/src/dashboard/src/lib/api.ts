// Data layer: fetches lambdas/api/ behind API Gateway, with a Cognito bearer token
// attached. See lambdas/api/README.md.
import { loadConfig } from "@/lib/config"
import { getAccessToken } from "@/lib/auth"
import type { AgentEvent, CaseRecord } from "@/lib/events"

export type QueuedCase = {
  case_id: string
  status: string
  attempts: number
  outcome: CaseRecord["outcome"] | null
  updated_at: string
  patient_name?: string
  pa_reference?: string
  procedure?: string
  [key: string]: unknown
}

async function awsFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const config = await loadConfig()
  const token = await getAccessToken()
  const headers = new Headers(init.headers)
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  return fetch(`${config.apiUrl}${path}`, { ...init, headers })
}

/** Thrown by `getMonitorSegments` when the monitor route 404s -- either the monitor
 *  bucket isn't deployed, or (not expected in practice) the call id looks malformed.
 *  Kept distinct from a generic `Error` so `useMonitorFeed.ts` can tell "not enabled,
 *  stop polling and tell the user" apart from a transient network/server error, which
 *  it should just retry. */
export class MonitorNotEnabledError extends Error {}

async function parseOrThrow(res: Response): Promise<unknown> {
  if (!res.ok) {
    let detail = `request failed (${res.status})`
    try {
      const body = await res.json()
      // Most routes' error bodies carry `detail` (lambdas/api/handler.py's `_response`
      // helper); the monitor route's "not enabled" body carries `error` instead -- read
      // both so neither message falls back to the generic one above.
      if (body?.detail) detail = body.detail
      else if (body?.error) detail = body.error
    } catch {
      // non-JSON error body
    }
    throw new Error(detail)
  }
  return res.json()
}

// --------------------------------------------------------------------------
// Start call
// --------------------------------------------------------------------------

export type StartCallResult = { call: string }

/** `{"case_id": string}` mints a call id, kicks off the Outbound IVR Runtime invoke, and
 *  returns `{"call": "<uuid>"}` right away — that id (not the case id) is what every
 *  other route (events, recording) is keyed on, so callers must use the response's
 *  `call`, not the `case_id` they sent. */
export async function startCall(caseId: string): Promise<StartCallResult> {
  const res = await awsFetch("/api/calls", { method: "POST", body: JSON.stringify({ case_id: caseId }) })
  return (await parseOrThrow(res)) as StartCallResult
}

// --------------------------------------------------------------------------
// Case queue / stored evidence
// --------------------------------------------------------------------------

export async function getCases(): Promise<QueuedCase[]> {
  const res = await awsFetch("/api/cases")
  return (await parseOrThrow(res)) as QueuedCase[]
}

// --------------------------------------------------------------------------
// Edit / reset a case (lambdas/api PATCH /api/cases/{id}).
// --------------------------------------------------------------------------

export type EditableCaseFields = Partial<{
  status: string
  patient_name: string
  patient_dob: string
  member_id: string
  pa_reference: string
  procedure: string
  payer: string
  ordering_provider: string
}>

export type UpdateCaseRequest = { fields?: EditableCaseFields; reset?: boolean }

/** PATCH /api/cases/{id}: edit allowlisted fields and/or reset a stuck case back to
 * `pending` (attempts 0, outcome/current_call cleared). Returns the updated item in the
 * same shape `getCases` returns per item. */
export async function updateCase(caseId: string, body: UpdateCaseRequest): Promise<QueuedCase> {
  const res = await awsFetch(`/api/cases/${encodeURIComponent(caseId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
  return (await parseOrThrow(res)) as QueuedCase
}

type AwsCaseItem = {
  case_id: string
  payer?: string
  provider_office?: string
  patient_name?: string
  patient_dob?: string
  member_id?: string
  pa_reference?: string
  procedure?: string
  ordering_provider?: string
  missing_items?: { key: string; label?: string; question?: string; answer?: string }[]
  results?: Record<string, { value?: string; confirmed?: boolean; at?: string; call?: string }>
  outcome?: CaseRecord["outcome"]
  updated_at?: string
  records?: Partial<CaseRecord>[]
}

/** Reshapes a DynamoDB `outbound-ivr-cases` item (+ its S3 case records) into the `CaseRecord`
 * shape EvidenceDetail renders. */
async function adaptAwsCase(item: AwsCaseItem): Promise<CaseRecord> {
  const records = item.records ?? []
  // Newest-started record (handler.py returns them oldest-first). Outbound IVR's in-process
  // tools never write `results` on the case item -- only this S3 record carries what got
  // collected -- so fall back to its `items` when `results` is empty. handler.py already
  // does this same fallback server-side; keeping it here too means this still works
  // against an API response that didn't.
  const latest = records[records.length - 1]
  const results: NonNullable<AwsCaseItem["results"]> =
    item.results && Object.keys(item.results).length ? item.results : (latest?.items ?? {})
  // Best-effort: the most recently touched result tells us which call last worked this
  // case, so the recording for that call can be resolved into a presigned URL.
  let lastCall: string | undefined
  let lastAt = ""
  for (const r of Object.values(results)) {
    if (r.call && (r.at ?? "") >= lastAt) {
      lastCall = r.call
      lastAt = r.at ?? ""
    }
  }
  let recording: string | undefined
  if (lastCall) {
    try {
      recording = await getRecordingUrl(lastCall)
    } catch {
      recording = undefined
    }
  }
  return {
    id: item.case_id,
    pa_reference: item.pa_reference,
    patient: item.patient_name,
    // The record's own `started_at` (when this call attempt began) beats the case
    // item's `updated_at` (last time anything touched the case row) for "started".
    started_at: latest?.started_at ?? item.updated_at,
    items: results,
    outcome: item.outcome ?? latest?.outcome ?? null,
    transcript: latest?.transcript ?? [],
    tool_calls: latest?.tool_calls ?? [],
    case_summary: {
      payer: item.payer,
      provider_office: item.provider_office,
      patient_name: item.patient_name,
      patient_dob: item.patient_dob,
      member_id: item.member_id,
      pa_reference: item.pa_reference,
      procedure: item.procedure,
      ordering_provider: item.ordering_provider,
    },
    checklist: (item.missing_items ?? []).map((m) => ({ key: m.key, label: m.label ?? m.key, answer: m.answer })),
    recording,
  }
}

export async function getCase(id: string): Promise<CaseRecord> {
  const res = await awsFetch(`/api/cases/${encodeURIComponent(id)}`)
  return adaptAwsCase((await parseOrThrow(res)) as AwsCaseItem)
}

// --------------------------------------------------------------------------
// Events (polled -- see lib/usePolledEvents.ts)
// --------------------------------------------------------------------------

export async function getCallEvents(call: string, after: number): Promise<(AgentEvent & { seq: number })[]> {
  const res = await awsFetch(`/api/calls/${encodeURIComponent(call)}/events?after=${after}`)
  return (await parseOrThrow(res)) as (AgentEvent & { seq: number })[]
}

// --------------------------------------------------------------------------
// Monitor: polled segments of audio/levels the Runtime wrote to S3 while someone was
// watching, read back through this same Cognito-protected API -- no presigned URLs, no
// bucket access from the browser, no WebSocket (see lib/useMonitorFeed.ts).
// --------------------------------------------------------------------------

export type MonitorChunk = { far_rms: number; agent_rms: number; pcm?: string }
export type MonitorSegment = { seq: number; t0: number; chunk_ms: number; chunks: MonitorChunk[] }
export type MonitorSegmentsResult = { segments: MonitorSegment[]; after: number }

/** `after` is the highest chunk seq the caller has already consumed (-1 for none).
 *  404 means the monitor bucket isn't deployed; callers should stop polling on it,
 *  not retry. */
export async function getMonitorSegments(
  call: string,
  mode: "listen" | "levels",
  after: number,
): Promise<MonitorSegmentsResult> {
  const res = await awsFetch(`/api/calls/${encodeURIComponent(call)}/monitor?mode=${mode}&after=${after}`)
  if (res.status === 404) {
    let detail = "monitor not enabled"
    try {
      const body = await res.json()
      if (body?.error) detail = body.error
    } catch {
      // non-JSON error body
    }
    throw new MonitorNotEnabledError(detail)
  }
  return (await parseOrThrow(res)) as MonitorSegmentsResult
}

// --------------------------------------------------------------------------
// Recordings
// --------------------------------------------------------------------------

/** A presigned S3 URL, valid 5 minutes. */
export async function getRecordingUrl(call: string): Promise<string> {
  const res = await awsFetch(`/api/recordings/${encodeURIComponent(call)}`)
  const body = (await parseOrThrow(res)) as { url: string }
  return body.url
}
