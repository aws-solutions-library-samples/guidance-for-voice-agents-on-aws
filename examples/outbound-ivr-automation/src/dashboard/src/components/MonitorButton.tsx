import { Headphones, HeadphoneOff } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { ensureAudioUnlocked } from "@/lib/audioUnlock"
import type { useAwsMonitor } from "@/lib/useAwsMonitor"

type Props = Pick<ReturnType<typeof useAwsMonitor>, "listen" | "toggleListen"> & {
  wsConnected: boolean
  callActive: boolean
}

/** Silent monitoring: hear the whole call live (the menu, Outbound IVR, the front desk) without
 * being heard. Contact centres call this "monitor"; "barge-in" would mean joining the call. */
export function MonitorButton({ listen, toggleListen, wsConnected, callActive }: Props) {
  const on = listen !== "off"
  const click = () => {
    ensureAudioUnlocked() // this click unlocks sound in the browser
    toggleListen()
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          size="sm"
          variant={on ? "outline" : "secondary"}
          className={cn(on && "border-primary text-primary")}
          disabled={!wsConnected || (!callActive && !on)}
          onClick={click}
        >
          {on ? <HeadphoneOff /> : <Headphones />}
          {listen === "connecting" ? "Connecting…" : on ? "Stop monitoring" : "Monitor call"}
          {listen === "on" && <span className="ml-1 h-2 w-2 animate-pulse rounded-full bg-primary" />}
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        {on ? "Stop listening. The call carries on." : "Listen to the call live, silently. Nobody on the call hears you."}
      </TooltipContent>
    </Tooltip>
  )
}
