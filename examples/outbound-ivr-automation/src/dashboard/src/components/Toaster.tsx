import { useEffect, useState } from "react"
import { subscribeToasts, type Toast } from "@/lib/toast"
import { cn } from "@/lib/utils"

export function Toaster() {
  const [toasts, setToasts] = useState<Toast[]>([])

  useEffect(() => subscribeToasts(setToasts), [])

  if (toasts.length === 0) return null

  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2 print:hidden">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto rounded-md border px-3 py-2 text-xs shadow-md",
            t.kind === "err"
              ? "border-destructive/50 bg-destructive/10 text-destructive"
              : "border-primary/50 bg-primary/10 text-primary",
          )}
        >
          {t.text}
        </div>
      ))}
    </div>
  )
}
