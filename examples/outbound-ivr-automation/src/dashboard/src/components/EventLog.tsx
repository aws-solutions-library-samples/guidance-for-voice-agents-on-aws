import { cn } from "@/lib/utils"
import type { LogEntry, LogKind } from "@/lib/useLiveCall"

// One label and one color per kind, so each card says what it is at a glance.
const KIND: Record<LogKind, { label: string; border: string; tag: string }> = {
  call: {
    label: "Call lifecycle",
    border: "border-l-muted-foreground",
    // bg-muted matches the card itself, so the box needs its own tint and border.
    tag: "bg-muted-foreground/15 text-muted-foreground border-muted-foreground/30",
  },
  status: { label: "Outbound IVR status", border: "border-l-warning", tag: "bg-warning/15 text-warning border-warning/30" },
  heard: { label: "Far end", border: "border-l-foreground/40", tag: "bg-foreground/10 text-foreground/80 border-foreground/25" },
  said: { label: "Outbound IVR", border: "border-l-primary", tag: "bg-primary/15 text-primary border-primary/30" },
  dtmf: {
    label: "Key press",
    border: "border-l-sky-500",
    tag: "bg-sky-500/15 text-sky-600 border-sky-500/30 dark:text-sky-400",
  },
  evidence: { label: "Evidence", border: "border-l-success", tag: "bg-success/15 text-success border-success/30" },
  outcome: {
    label: "Outcome",
    border: "border-l-violet-500",
    tag: "bg-violet-500/15 text-violet-600 border-violet-500/30 dark:text-violet-400",
  },
  err: { label: "Error", border: "border-l-destructive", tag: "bg-destructive/15 text-destructive border-destructive/30" },
}

export function EventLog({ entries }: { entries: LogEntry[] }) {
  return (
    <div className="flex min-h-[120px] flex-1 flex-col gap-1.5 overflow-y-auto">
      {entries.map((e) => {
        const k = KIND[e.kind] ?? KIND.status
        return (
          <div
            key={e.id}
            className={cn(
              "rounded-md border-l-2 bg-secondary px-2.5 py-1.5 text-xs",
              k.border,
              e.kind === "err" && "text-destructive",
            )}
          >
            <span className="mb-0.5 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              {new Date(e.ts).toLocaleTimeString()}
              <span className={cn("rounded border px-1.5 py-px font-sans text-[10px] font-semibold uppercase tracking-wide", k.tag)}>
                {k.label}
              </span>
            </span>
            {e.text}
          </div>
        )
      })}
    </div>
  )
}
