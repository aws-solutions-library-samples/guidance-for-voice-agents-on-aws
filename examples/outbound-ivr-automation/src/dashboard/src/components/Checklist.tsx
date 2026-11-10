import { Check } from "lucide-react"
import { cn } from "@/lib/utils"
import type { ChecklistItem } from "@/lib/events"
import type { ChecklistState } from "@/lib/useLiveCall"

export function Checklist({
  items,
  state,
}: {
  items: ChecklistItem[]
  state: Record<string, ChecklistState>
}) {
  if (!items.length) return null
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => {
        const s = state[item.key]
        return (
          <li
            key={item.key}
            className={cn(
              "flex items-start gap-2.5 rounded-md border border-border bg-secondary px-3 py-2 text-sm",
              s?.got && "border-success bg-success/10",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex h-4 w-4 flex-none items-center justify-center rounded border border-border",
                s?.got && "border-success bg-success text-background",
              )}
            >
              {s?.got && <Check className="h-3 w-3" strokeWidth={3} />}
            </span>
            <span className="flex flex-col">
              <span className="font-bold">{item.label}</span>
              {s?.value && <span className="mt-0.5 text-xs text-success">{s.value}</span>}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
