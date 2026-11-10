import { useEffect, useRef } from "react"
import { cn } from "@/lib/utils"
import type { TranscriptTurn } from "@/lib/useLiveCall"

export function Transcript({ turns }: { turns: TranscriptTurn[] }) {
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const box = boxRef.current
    if (box) box.scrollTop = box.scrollHeight
  }, [turns.length])

  return (
    <div ref={boxRef} className="flex min-h-[120px] flex-1 flex-col gap-1.5 overflow-y-auto pt-2.5">
      {turns.map((t) => (
        <div
          key={t.id}
          className={cn(
            "max-w-[92%] rounded-md border-l-2 px-2.5 py-1.5 text-sm",
            t.cls === "heard"
              ? "self-start border-l-foreground/40 bg-secondary"
              : "self-end border-l-primary bg-primary/10 text-right",
          )}
        >
          <span className="block text-[10px] font-medium tracking-wider text-muted-foreground uppercase">
            {t.who}
          </span>
          {t.text}
        </div>
      ))}
    </div>
  )
}
