import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]

export function Keypad({
  pressCounts,
  lastPressedKey,
  pressSeq,
  dimmed,
  compact,
}: {
  pressCounts: Record<string, number>
  lastPressedKey?: string
  pressSeq: number
  dimmed?: boolean
  compact?: boolean
}) {
  return (
    <div
      className={cn(
        "mx-auto grid w-fit grid-cols-[repeat(3,auto)] gap-1.5 transition-opacity",
        dimmed && "opacity-35",
      )}
    >
      {KEYS.map((k) => {
        const count = pressCounts[k]
        // Remounting with a seq-keyed animation class restarts the animation on repeats.
        const animKey = k === lastPressedKey ? pressSeq : 0
        return (
          <div
            key={k}
            className={cn(
              "relative grid place-items-center rounded-md border border-border bg-secondary font-mono font-bold text-muted-foreground",
              compact ? "h-6 w-8 text-xs" : "h-9 w-12 text-base",
            )}
          >
            <span key={animKey} className={cn("absolute inset-0 grid place-items-center rounded-md", animKey && "key-hit")}>
              {k}
            </span>
            {!!count && (
              <Badge className="absolute -top-1.5 -right-1.5 h-4 min-w-4 justify-center rounded-full px-1 text-[10px]">
                {count}
              </Badge>
            )}
          </div>
        )
      })}
    </div>
  )
}
