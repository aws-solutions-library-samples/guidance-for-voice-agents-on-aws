// Edit a case's info (and reset it) from the case queue. aws mode only -- PATCH
// /api/cases/{id} (lambdas/api/README.md). See CaseQueue.tsx for the row-level Edit
// button that opens this.
import { useEffect, useState } from "react"
import { Pencil } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { updateCase, type EditableCaseFields, type QueuedCase } from "@/lib/api"
import { showToast } from "@/lib/toast"

const STATUS_OPTIONS = ["pending", "queued", "calling", "complete", "escalated"] as const

const TEXT_FIELDS: { key: keyof EditableCaseFields; label: string }[] = [
  { key: "patient_name", label: "Patient name" },
  { key: "patient_dob", label: "Patient DOB" },
  { key: "member_id", label: "Member ID" },
  { key: "pa_reference", label: "Auth ref" },
  { key: "procedure", label: "Procedure" },
  { key: "payer", label: "Payer" },
  { key: "ordering_provider", label: "Ordering provider" },
]

const selectClass = cn(
  "h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none",
  "focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
  "md:text-sm dark:bg-input/30",
)

type Props = {
  caseItem: QueuedCase
  onSaved: (updated: QueuedCase) => void
}

export function EditCaseDialog({ caseItem, onSaved }: Props) {
  // The backend refuses to edit fields while a call is in flight (409) -- only Reset is
  // allowed then, so disable the rest of the form rather than let Save fail every time.
  const isCalling = caseItem.status === "calling"
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState(caseItem.status)
  const [fields, setFields] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!open) return
    setStatus(caseItem.status)
    setFields(
      TEXT_FIELDS.reduce<Record<string, string>>((acc, f) => {
        const value = caseItem[f.key]
        acc[f.key] = typeof value === "string" ? value : ""
        return acc
      }, {}),
    )
    setError(null)
    setConfirmReset(false)
  }, [open, caseItem])

  const close = () => {
    setOpen(false)
    setConfirmReset(false)
    setError(null)
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const editFields: EditableCaseFields = { status }
      for (const f of TEXT_FIELDS) {
        editFields[f.key] = fields[f.key] ?? ""
      }
      const updated = await updateCase(caseItem.case_id, { fields: editFields })
      onSaved(updated)
      showToast(`Case ${caseItem.case_id} updated`, "ok")
      close()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the case.")
    } finally {
      setSaving(false)
    }
  }

  const reset = async () => {
    if (!confirmReset) {
      setConfirmReset(true)
      return
    }
    setResetting(true)
    setError(null)
    try {
      const updated = await updateCase(caseItem.case_id, { reset: true })
      onSaved(updated)
      showToast(`Case ${caseItem.case_id} reset to pending`, "ok")
      close()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reset the case.")
    } finally {
      setResetting(false)
      setConfirmReset(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setConfirmReset(false)
      }}
    >
      <Button
        size="icon-sm"
        variant="outline"
        aria-label={`Edit case ${caseItem.case_id}`}
        onClick={() => setOpen(true)}
      >
        <Pencil />
      </Button>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit case</DialogTitle>
          <DialogDescription>{caseItem.case_id}</DialogDescription>
        </DialogHeader>

        {isCalling && (
          <p className="text-sm text-muted-foreground">
            This case is on a live call. Only Reset is available until it finishes.
          </p>
        )}

        <div className="grid gap-3">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Status</span>
            <select
              className={selectClass}
              value={status}
              disabled={isCalling}
              onChange={(e) => setStatus(e.target.value)}
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>

          {TEXT_FIELDS.map((f) => (
            <label key={f.key} className="grid gap-1 text-sm">
              <span className="font-medium">{f.label}</span>
              <Input
                value={fields[f.key] ?? ""}
                maxLength={200}
                disabled={isCalling}
                onChange={(e) =>
                  setFields((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
              />
            </label>
          ))}
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <DialogFooter className="sm:justify-between">
          <Button
            variant={confirmReset ? "destructive" : "outline"}
            onClick={reset}
            disabled={saving || resetting}
          >
            {resetting ? "Resetting…" : confirmReset ? "Confirm reset?" : "Reset case"}
          </Button>
          <Button onClick={save} disabled={saving || resetting || isCalling}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
