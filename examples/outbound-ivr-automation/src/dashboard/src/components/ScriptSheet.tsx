import { ScrollText } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import type { CaseSummary } from "@/lib/events"
import { useCaseScript } from "@/lib/useCaseScript"

export type ScriptSheetProps = {
  /** The live call's current case summary (state.caseSummary from useLiveCall), used
   * in aws mode to look up and show that case's own script. Undefined (no call yet, or
   * local mode) falls back to the fixed demo script. */
  caseSummary?: CaseSummary
}

/**
 * Non-modal: left on screen while Answer, Mute and Hang up still work underneath, since
 * whoever is reading these lines also needs to run the front desk card at the same time.
 */
export function ScriptSheet({ caseSummary }: ScriptSheetProps = {}) {
  const script = useCaseScript(caseSummary)
  return (
    <Sheet modal={false}>
      <SheetTrigger asChild>
        <Button size="sm" variant="outline">
          <ScrollText /> Script
        </Button>
      </SheetTrigger>
      <SheetContent showOverlay={false} className="flex w-full flex-col sm:max-w-sm">
        <SheetHeader className="flex-none">
          <SheetTitle>Front desk script</SheetTitle>
          <SheetDescription>
            What to say if you're playing Sam. The phone menu is already in the transcript.
          </SheetDescription>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-4 px-4 pb-4">
            {script.map((section) => (
              <div key={section.title} className="flex flex-col gap-2">
                <h3 className="text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
                  {section.title}
                </h3>
                <ul className="flex flex-col gap-1.5">
                  {section.lines.map((line) => (
                    <li key={line.label} className="rounded-md border bg-secondary px-3 py-2 text-sm">
                      <span className="block text-[11px] font-mono tracking-wide text-muted-foreground uppercase">
                        {line.label}
                      </span>
                      <span className="text-foreground">{line.text}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
