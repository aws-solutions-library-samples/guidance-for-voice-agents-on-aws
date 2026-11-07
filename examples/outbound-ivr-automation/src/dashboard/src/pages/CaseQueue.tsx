// The case queue from the cases DynamoDB table (lambdas/api GET /api/cases), with a
// Start call action per row that mints a call and has Outbound IVR dial the office right away
// (POST /api/calls {"case_id"} -> {"call": "<uuid>"}, which invokes the Outbound IVR Runtime
// directly -- no queue, no dispatcher).
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { getCases, type QueuedCase } from "@/lib/api"
import { startCallForCase } from "@/lib/startCallAction"
import { EditCaseDialog } from "@/components/EditCaseDialog"
import { LoadingRows } from "@/components/LoadingRows"

function fmtTime(iso?: string): string {
  if (!iso) return ""
  try {
    // Short ("Oct 19, 10:05 AM") so it fits its column; the full time is the tooltip.
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
  } catch {
    return iso
  }
}

const STATUS_CLASS: Record<string, string> = {
  pending: "border-border text-muted-foreground bg-secondary",
  queued: "border-primary/50 text-primary bg-primary/10",
  calling: "border-primary/50 text-primary bg-primary/10",
  complete: "border-success text-success bg-success/10",
  escalated: "border-warning text-warning bg-warning/10",
}

export function CaseQueue() {
  const [cases, setCases] = useState<QueuedCase[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState<string | null>(null)
  const navigate = useNavigate()

  const refresh = useCallback(() => {
    getCases()
      .then((data) => {
        setCases(data)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load cases."))
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  const start = async (caseId: string) => {
    setStarting(caseId)
    const callId = await startCallForCase(caseId)
    setStarting(null)
    if (callId) {
      refresh()
      navigate(`/?call=${encodeURIComponent(callId)}`)
    }
  }

  const applyUpdate = (updated: QueuedCase) => {
    setCases((prev) => (prev ? prev.map((c) => (c.case_id === updated.case_id ? updated : c)) : prev))
  }

  return (
    <div className="h-full overflow-auto p-5">
      <div className="mx-auto w-full max-w-6xl">
        <h2 className="text-lg font-bold">Case queue</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Cases from the cases table. Start call has Outbound IVR dial the office right away.
        </p>

        {error && <p className="mt-4 text-sm text-destructive">{error}</p>}

        {cases === null && !error && <LoadingRows label="Loading cases…" />}
        {cases && cases.length === 0 && (
          <p className="mt-6 text-sm text-muted-foreground">No cases yet.</p>
        )}

        {cases && cases.length > 0 && (
          <Table className="mt-4 w-full table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="w-40">Call</TableHead>
                <TableHead>Patient</TableHead>
                <TableHead className="hidden w-32 md:table-cell">Auth ref</TableHead>
                <TableHead>Procedure</TableHead>
                <TableHead className="w-24">Status</TableHead>
                <TableHead className="hidden w-20 lg:table-cell">Attempts</TableHead>
                <TableHead className="hidden w-28 lg:table-cell">Outcome</TableHead>
                <TableHead className="hidden w-44 xl:table-cell">Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {cases.map((c) => {
                const canStart = c.status !== "queued" && c.status !== "calling"
                return (
                  <TableRow key={c.case_id} className="even:bg-muted/40">
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          disabled={!canStart || starting === c.case_id}
                          onClick={() => start(c.case_id)}
                        >
                          {starting === c.case_id ? "Starting…" : "Start call"}
                        </Button>
                        <EditCaseDialog caseItem={c} onSaved={applyUpdate} />
                      </div>
                    </TableCell>
                    <TableCell className="max-w-0 truncate font-medium" title={c.patient_name ?? c.case_id}>
                      {c.patient_name ?? c.case_id}
                    </TableCell>
                    <TableCell className="hidden max-w-0 truncate md:table-cell" title={c.auth_ref ?? "—"}>
                      {c.auth_ref ?? "—"}
                    </TableCell>
                    <TableCell className="max-w-0 truncate" title={c.procedure ?? "—"}>
                      {c.procedure ?? "—"}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className={cn("text-[10px] uppercase", STATUS_CLASS[c.status])}>
                        {c.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="hidden lg:table-cell">{c.attempts}</TableCell>
                    <TableCell className="hidden max-w-0 truncate lg:table-cell" title={c.outcome?.type ?? "—"}>
                      {c.outcome?.type ?? "—"}
                    </TableCell>
                    <TableCell
                      className="hidden max-w-0 truncate xl:table-cell"
                      title={c.updated_at ? new Date(c.updated_at).toLocaleString() : undefined}
                    >
                      {fmtTime(c.updated_at)}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  )
}
