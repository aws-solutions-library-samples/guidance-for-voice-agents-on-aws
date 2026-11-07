import { useEffect, useState } from "react"
import { useParams } from "react-router-dom"
import { Check, Printer, X } from "lucide-react"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { getCase } from "@/lib/api"
import type { CaseRecord } from "@/lib/events"

function fmtTime(iso?: string): string {
  if (!iso) return ""
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

const HEADER_FIELDS: [string, string][] = [
  ["payer", "Payer"],
  ["provider_office", "Provider office"],
  ["patient_name", "Patient"],
  ["patient_dob", "Date of birth"],
  ["member_id", "Member ID"],
  ["auth_ref", "Auth reference"],
  ["procedure", "Procedure"],
  ["ordering_provider", "Ordering provider"],
]

export function EvidenceDetail() {
  const { id } = useParams<{ id: string }>()
  const [record, setRecord] = useState<CaseRecord | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setRecord(null)
    setError(null)
    getCase(id)
      .then((data) => {
        if (!cancelled) setRecord(data)
      })
      .catch((err) => {
        if (!cancelled) setError(`Could not load case "${id}": ${err instanceof Error ? err.message : String(err)}`)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  if (error) {
    return (
      <div className="h-full overflow-auto p-5">
        <p className="text-sm text-destructive">{error}</p>
      </div>
    )
  }
  if (!record) {
    return <div className="h-full overflow-auto p-5 text-sm text-muted-foreground">Loading…</div>
  }

  const summary = record.case_summary ?? {}
  const checklist = record.checklist ?? []
  const items = record.items ?? {}
  const outcome = record.outcome
  const transcript = record.transcript ?? []

  return (
    <div className="h-full overflow-auto p-5 print:p-0">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <Card>
          <CardHeader>
            <h1 className="text-lg font-bold">
              {summary.auth_ref ?? record.auth_ref} — {summary.patient_name ?? record.patient}
            </h1>
            <p className="text-sm text-muted-foreground">started {fmtTime(record.started_at)}</p>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-3">
              {HEADER_FIELDS.map(([key, label]) => (
                <div key={key}>
                  <span className="block text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
                    {label}
                  </span>
                  <span className="text-sm font-bold">
                    {(summary as Record<string, string | undefined>)[key] ??
                      (record as unknown as Record<string, string | undefined>)[key] ??
                      "—"}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="text-base font-bold">Evidence collected</h2>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-2.5">
              {(checklist.length ? checklist : Object.keys(items).map((k) => ({ key: k, label: k }))).map((def) => {
                const item = items[def.key]
                const confirmed = item?.confirmed
                return (
                  <li
                    key={def.key}
                    className={cn(
                      "rounded-md border border-border bg-secondary px-3.5 py-2.5",
                      !confirmed && "border-destructive bg-destructive/5",
                    )}
                  >
                    <div className="flex items-center gap-2 font-bold">
                      <span
                        className={cn(
                          "grid h-4 w-4 flex-none place-items-center rounded border border-border",
                          confirmed
                            ? "border-success bg-success text-background"
                            : "border-destructive",
                        )}
                      >
                        {confirmed ? <Check className="h-3 w-3" strokeWidth={3} /> : <X className="h-3 w-3" strokeWidth={3} />}
                      </span>
                      {def.label}
                    </div>
                    {confirmed ? (
                      <>
                        <div className="mt-1 text-sm">{item?.value}</div>
                        <div className="mt-0.5 text-xs text-muted-foreground">confirmed {fmtTime(item?.at)}</div>
                      </>
                    ) : (
                      <div className="mt-1 text-sm text-destructive">not captured</div>
                    )}
                  </li>
                )
              })}
            </ul>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="text-base font-bold">Outcome</h2>
          </CardHeader>
          <CardContent>
            {!outcome ? (
              <div className="rounded-md border border-border bg-secondary p-3.5">
                <div className="font-mono text-lg font-bold text-muted-foreground">PENDING</div>
                <p className="mt-1.5 text-sm text-muted-foreground">Call has not finished.</p>
              </div>
            ) : outcome.type === "complete" ? (
              <div className="rounded-md border border-success bg-success/10 p-3.5">
                <div className="font-mono text-lg font-bold text-success">COMPLETE</div>
                <p className="mt-1.5 text-sm">{outcome.summary}</p>
              </div>
            ) : outcome.type === "handed_off" ? (
              <div className="rounded-md border border-success bg-success/10 p-3.5">
                <div className="font-mono text-lg font-bold text-success">HANDED OFF</div>
                <p className="mt-1.5 text-sm">{outcome.summary}</p>
                <dl className="mt-2.5 grid grid-cols-[max-content_1fr] gap-x-2.5 gap-y-1 text-sm">
                  <dt className="text-muted-foreground">Reason</dt>
                  <dd>{outcome.reason}</dd>
                  {outcome.joined_identity && (
                    <>
                      <dt className="text-muted-foreground">Joined</dt>
                      <dd>
                        {outcome.joined_identity}
                        {outcome.joined_at ? ` at ${outcome.joined_at}` : ""}
                      </dd>
                    </>
                  )}
                </dl>
              </div>
            ) : (
              <div className="rounded-md border border-warning bg-warning/10 p-3.5">
                <div className="font-mono text-lg font-bold text-warning">ESCALATED</div>
                <dl className="mt-2.5 grid grid-cols-[max-content_1fr] gap-x-2.5 gap-y-1 text-sm">
                  <dt className="text-muted-foreground">Reason</dt>
                  <dd>{outcome.reason}</dd>
                  {outcome.follow_up && (
                    <>
                      <dt className="text-muted-foreground">Follow up</dt>
                      <dd>{outcome.follow_up}</dd>
                    </>
                  )}
                  {outcome.missing && (
                    <>
                      <dt className="text-muted-foreground">Still missing</dt>
                      <dd>{outcome.missing.join(", ")}</dd>
                    </>
                  )}
                  {outcome.model_summary && (
                    <>
                      <dt className="text-muted-foreground">Agent's summary</dt>
                      <dd>{outcome.model_summary}</dd>
                    </>
                  )}
                </dl>
              </div>
            )}
          </CardContent>
        </Card>

        {record.recording && (
          <Card className="print:hidden">
            <CardHeader>
              <h2 className="text-base font-bold">Call recording</h2>
            </CardHeader>
            <CardContent>
              {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
              <audio controls preload="metadata" src={record.recording} className="w-full" />
              <p className="mt-2 text-xs text-muted-foreground">
                Both sides of the call, mixed: the office's menu, Outbound IVR, and the front desk.
              </p>
            </CardContent>
          </Card>
        )}

        {!!record.tool_calls?.length && (
          <Card>
            <CardHeader>
              <h2 className="text-base font-bold">Agent actions</h2>
              <p className="text-xs text-muted-foreground">
                Every tool call Outbound IVR made, in order: key presses, menu decisions, recorded items, and the outcome.
              </p>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full text-xs tabular-nums">
                <thead className="text-muted-foreground">
                  <tr className="border-b text-left uppercase tracking-wider">
                    <th className="py-1.5 pr-3 font-medium">Time</th>
                    <th className="py-1.5 pr-3 font-medium">Phase</th>
                    <th className="py-1.5 pr-3 font-medium">Tool</th>
                    <th className="py-1.5 pr-3 font-medium">Input</th>
                    <th className="py-1.5 pr-3 font-medium">Result</th>
                    <th className="py-1.5 text-right font-medium">ms</th>
                  </tr>
                </thead>
                <tbody>
                  {record.tool_calls.map((c, i) => (
                    <tr key={i} className="border-b border-border/60 align-top">
                      <td className="py-1.5 pr-3 whitespace-nowrap">{new Date(c.t * 1000).toLocaleTimeString()}</td>
                      <td className="py-1.5 pr-3">{c.phase}</td>
                      <td className="py-1.5 pr-3 font-bold">{c.tool}</td>
                      <td className="py-1.5 pr-3 break-all">
                        {c.input ? Object.entries(c.input).map(([k, v]) => `${k}: ${String(v)}`).join(" · ") : ""}
                      </td>
                      <td className={c.status === "error" || c.error ? "py-1.5 pr-3 text-destructive" : "py-1.5 pr-3"}>
                        {c.error ?? c.result ?? c.status}
                      </td>
                      <td className="py-1.5 text-right">{c.ms}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <h2 className="text-base font-bold">Transcript</h2>
          </CardHeader>
          <CardContent>
            {transcript.length ? (
              <div className="flex flex-col gap-2">
                {transcript.map((t, i) => {
                  const speaker = t.speaker ?? t.who ?? "agent"
                  const isCaller = speaker === "caller"
                  return (
                    <div
                      key={i}
                      className={cn(
                        "max-w-[92%] rounded-md border-l-2 px-3 py-2 text-sm",
                        isCaller
                          ? "self-start border-l-foreground/40 bg-secondary"
                          : "self-end border-l-primary bg-primary/10 text-right",
                      )}
                    >
                      <span className="block text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
                        {speaker}
                      </span>
                      {t.text}
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No transcript recorded.</p>
            )}
          </CardContent>
        </Card>

        <Button variant="outline" size="sm" className="w-fit print:hidden" onClick={() => window.print()}>
          <Printer className="h-3.5 w-3.5" /> print / save as PDF
        </Button>
      </div>
    </div>
  )
}
