import { cn } from "@/lib/utils"
import type { VerdictKind } from "@/lib/useLiveCall"

const KIND_STYLES: Record<VerdictKind, { border: string; big: string }> = {
  "": { border: "border-border", big: "text-foreground" },
  idle: { border: "border-border", big: "text-foreground" },
  wait: { border: "border-warning", big: "text-warning" },
  done: { border: "border-success", big: "text-success" },
  err: { border: "border-destructive", big: "text-destructive" },
}

export function VerdictBox({
  kind,
  big,
  cap,
  compact,
}: {
  kind: VerdictKind
  big: string
  cap: string
  compact?: boolean
}) {
  const style = KIND_STYLES[kind] ?? KIND_STYLES[""]

  if (compact) {
    return (
      <div
        className={cn(
          "flex min-w-0 max-w-xs items-center gap-2 rounded-full border bg-secondary px-3 py-1.5",
          style.border,
        )}
      >
        {big && (
          <span className={cn("flex-none font-mono text-xs font-bold tracking-wide", style.big)}>
            {big}
          </span>
        )}
        <span className="min-w-0 truncate text-[11px] tracking-wider text-muted-foreground uppercase">
          {cap}
        </span>
      </div>
    )
  }

  return (
    <div
      className={cn(
        "mx-auto grid min-h-[72px] place-items-center rounded-md border bg-secondary px-4 py-3",
        style.border,
      )}
    >
      <div className="text-center">
        {big && (
          <div className={cn("font-mono text-xl font-bold tracking-wide", style.big)}>{big}</div>
        )}
        <div className="mt-1 text-[11px] tracking-wider text-muted-foreground uppercase">{cap}</div>
      </div>
    </div>
  )
}
