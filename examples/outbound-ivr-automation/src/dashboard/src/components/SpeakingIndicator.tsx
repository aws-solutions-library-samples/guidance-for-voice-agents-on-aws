import { cn } from "@/lib/utils"

export function SpeakingIndicator({
  label,
  level,
  spark,
  speaking,
  accent,
  compact,
}: {
  label: string
  level: number
  spark: string
  speaking: boolean
  accent?: boolean
  compact?: boolean
}) {
  const bases = [10, 16, 22]
  const baseOpacity = [0.9, 0.55, 0.3]
  const stroke = accent ? "var(--primary)" : "var(--muted-foreground)"

  if (compact) {
    return (
      <div className="flex items-center gap-1.5 rounded-full border border-border bg-secondary px-2.5 py-1">
        <span
          className={cn(
            "h-2 w-2 flex-none rounded-full transition-opacity",
            accent ? "bg-primary" : "bg-foreground/60",
            speaking ? "opacity-100" : "opacity-30",
          )}
          style={{ transform: `scale(${1 + level * 0.6})` }}
        />
        <span className="text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
          {label}
        </span>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center gap-1.5 rounded-md border border-border bg-secondary px-3 py-2.5">
      <svg viewBox="0 0 64 64" className="h-16 w-16">
        {bases.map((base, i) => (
          <circle
            key={i}
            cx={32}
            cy={32}
            r={base + level * 5}
            fill="none"
            stroke={stroke}
            strokeWidth={1}
            style={{ opacity: Math.min(1, baseOpacity[i] + level * 0.4) }}
          />
        ))}
      </svg>
      <div className="h-3.5 w-full overflow-hidden text-center text-xs leading-none tracking-wide text-muted-foreground">
        {spark}
      </div>
      <div className="text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
        {label}{" "}
        <span
          className={cn(speaking && (accent ? "text-primary" : "text-foreground/70"))}
        >
          {speaking ? "speaking" : ""}
        </span>
      </div>
    </div>
  )
}
