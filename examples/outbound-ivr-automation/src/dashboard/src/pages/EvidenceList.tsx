import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { CaseRecord } from "@/lib/events"
import { getCases } from "@/lib/api"
import { LoadingRows } from "@/components/LoadingRows"

function fmtTime(iso?: string): string {
  if (!iso) return ""
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

const BADGE_CLASS: Record<string, string> = {
  complete: "border-success text-success bg-success/10",
  handed_off: "border-success text-success bg-success/10",
  escalated: "border-warning text-warning bg-warning/10",
  pending: "border-border text-muted-foreground bg-secondary",
}

export function EvidenceList() {
  const [cases, setCases] = useState<CaseRecord[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getCases()
      .then((data) => {
        if (cancelled) return
        // getCases() returns outbound-ivr-cases DynamoDB items (case_id, patient_name,
        // updated_at) — reshape minimally for this list; EvidenceDetail does the fuller
        // adaptation per case (see lib/api.ts's adaptAwsCase).
        const asRecords: CaseRecord[] = data.map(
          (c) =>
            ({
              id: c.case_id,
              auth_ref: c.auth_ref,
              patient: c.patient_name as string | undefined,
              started_at: c.updated_at,
              outcome: c.outcome,
            }) as CaseRecord,
        )
        setCases(asRecords)
      })
      .catch((err) => {
        if (!cancelled) setError(`Could not load cases: ${err instanceof Error ? err.message : String(err)}`)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="h-full overflow-auto p-5">
      <div className="mx-auto max-w-3xl">
        <h2 className="text-lg font-bold">Evidence</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Stored case records from the <code className="rounded bg-secondary px-1.5 py-0.5">outbound-ivr-cases</code> table,
          newest first.
        </p>

        {error && <p className="mt-4 text-sm text-destructive">{error}</p>}

        {cases === null && !error && <LoadingRows label="Loading evidence…" />}
        {cases && cases.length === 0 && (
          <p className="mt-6 text-sm text-muted-foreground">
            No cases yet. Start a call from the case queue to create one.
          </p>
        )}

        <ul className="mt-4 flex flex-col gap-2.5">
          {cases?.map((c) => {
            const kind = c.outcome?.type ?? "pending"
            return (
              <li key={c.id} className="rounded-md border border-border bg-card px-4 py-3">
                <Link to={`/evidence/${encodeURIComponent(c.id)}`} className="font-bold text-primary hover:underline">
                  {c.auth_ref} — {c.patient}
                </Link>
                <Badge variant="outline" className={cn("ml-2 align-middle text-[10px] uppercase", BADGE_CLASS[kind])}>
                  {kind}
                </Badge>
                <div className="mt-1 text-xs text-muted-foreground">started {fmtTime(c.started_at)}</div>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
