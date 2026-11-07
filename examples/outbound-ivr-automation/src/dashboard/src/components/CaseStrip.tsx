import type { CaseSummary } from "@/lib/events"

const FIELDS: [keyof CaseSummary, string][] = [
  ["payer", "Calling as"],
  ["provider_office", "Calling"],
  ["patient_name", "Patient"],
  ["member_id", "Member ID"],
  ["auth_ref", "Auth ref"],
  ["procedure", "Requested"],
]

export function CaseStrip({ c }: { c?: CaseSummary }) {
  if (!c) return null
  return (
    <div className="grid grid-cols-2 gap-2.5 rounded-md border border-border bg-card p-3 sm:grid-cols-3 md:grid-cols-6">
      {FIELDS.map(([key, label]) => (
        <div key={key} className="min-w-0">
          <span className="block text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
            {label}
          </span>
          <span className="block truncate text-[13px] font-bold" title={c[key] || "—"}>
            {c[key] || "—"}
          </span>
        </div>
      ))}
    </div>
  )
}
