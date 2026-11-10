import { useState } from "react"
import { Phone, PhoneOff, Mic, MicOff, LogIn } from "lucide-react"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { useConnectSoftphoneContext } from "@/lib/connectSoftphoneContext"

function formatTimer(seconds: number) {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}:${s.toString().padStart(2, "0")}`
}

/**
 * The "Front desk" card: the left-column slot on the Live call page (see
 * pages/LiveCall.tsx), backed by the real Amazon Connect agent workspace.
 *
 * This is a pure consumer of ConnectSoftphoneProvider (lib/connectSoftphoneContext.tsx):
 * the hidden CCP iframe itself, and the amazon-connect-streams wiring that drives it, are
 * owned by that provider above the router so they survive this card unmounting whenever
 * the user navigates to another page (see the provider's own doc comment for why that
 * used to break incoming calls).
 */
export function ConnectCcpCard() {
  const connectSoftphone = useConnectSoftphoneContext()
  const {
    phase,
    agentStateName,
    agentStates,
    settingState,
    callerNumber,
    muted,
    callSeconds,
    error,
    openLogin,
    toggleAvailability,
    answer,
    decline,
    toggleMute,
    hangup,
    clearError,
  } = connectSoftphone

  const [loginClicked, setLoginClicked] = useState(false)

  const prominent = phase === "incoming" || phase === "connected"
  const isAvailable = agentStates.find((s) => s.name === agentStateName)?.routable ?? false
  const hasAvailabilityToggle = agentStates.some((s) => s.routable) && agentStates.some((s) => !s.routable)

  return (
    <Card
      className={cn(
        "flex-none overflow-hidden transition-shadow",
        prominent && "border-primary/50 shadow-md",
      )}
    >
      <CardHeader
        className={cn(
          "flex-none flex-row items-center justify-between gap-2 border-b",
          prominent ? "pb-3" : "pb-2",
        )}
      >
        <div className="min-w-0">
          <h2 className="text-xs font-medium tracking-wider text-muted-foreground uppercase">
            Office front desk · Amazon Connect
          </h2>
          <p className="mt-0.5 text-[11px] text-muted-foreground/80">
            Live call transferred by the office's Amazon Connect IVR
          </p>
        </div>
        {phase !== "signing-in" && (
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              isAvailable ? "bg-success" : "bg-muted-foreground/40",
            )}
            title={agentStateName ?? undefined}
          />
        )}
      </CardHeader>
      <CardContent className={cn("flex flex-col gap-3", prominent ? "p-4" : "p-3")}>
        {phase === "signing-in" && (
          <div className="flex flex-col gap-3">
            <p className="text-xs text-muted-foreground">Sign in to Connect to answer calls here.</p>
            <Button
              size="sm"
              onClick={() => {
                setLoginClicked(true)
                openLogin()
              }}
            >
              <LogIn /> Sign in to Connect
            </Button>
            <p className="text-xs text-muted-foreground">
              Chrome needs third-party cookies allowed for this site for the sign-in popup to work.
              {loginClicked && " If no popup appeared, check that it wasn't blocked."}
            </p>
          </div>
        )}

        {phase === "ready" && (
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">{agentStateName ?? "Unknown"}</span>
            {hasAvailabilityToggle && (
              <Button size="sm" variant="outline" disabled={settingState} onClick={toggleAvailability}>
                {isAvailable ? "Go offline" : "Go available"}
              </Button>
            )}
          </div>
        )}

        {phase === "incoming" && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3">
              <Phone className="h-6 w-6 animate-pulse text-primary" />
              <div>
                <div className="text-sm font-semibold">Incoming call</div>
                {callerNumber && <div className="text-xs text-muted-foreground">{callerNumber}</div>}
              </div>
            </div>
            <div className="flex gap-2">
              <Button size="lg" className="flex-1" onClick={answer}>
                <Phone /> Answer
              </Button>
              <Button size="lg" variant="outline" onClick={decline}>
                <PhoneOff /> Decline
              </Button>
            </div>
          </div>
        )}

        {phase === "connected" && (
          <div className="flex flex-col gap-3">
            <span className="font-mono text-sm">{formatTimer(callSeconds)}</span>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant={muted ? "secondary" : "outline"}
                className="flex-1"
                onClick={toggleMute}
              >
                {muted ? <MicOff /> : <Mic />}
                {muted ? "Unmute" : "Mute"}
              </Button>
              <Button size="sm" variant="destructive" className="flex-1" onClick={hangup}>
                <PhoneOff /> Hang up
              </Button>
            </div>
          </div>
        )}

        {error && (
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs text-destructive">{error}</p>
            <Button size="xs" variant="ghost" onClick={clearError}>
              Dismiss
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
