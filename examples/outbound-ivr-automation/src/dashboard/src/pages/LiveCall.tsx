import { useEffect } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { ClipboardCheck } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { CaseStrip } from "@/components/CaseStrip"
import { ScriptSheet } from "@/components/ScriptSheet"
import { SpeakingIndicator } from "@/components/SpeakingIndicator"
import { VerdictBox } from "@/components/VerdictBox"
import { Keypad } from "@/components/Keypad"
import { Transcript } from "@/components/Transcript"
import { Checklist } from "@/components/Checklist"
import { EventLog } from "@/components/EventLog"
import { ConnectCcpCard } from "@/components/ConnectCcpCard"
import { MonitorButton } from "@/components/MonitorButton"
import { cn } from "@/lib/utils"
import { getActiveCall, setActiveCall } from "@/lib/activeCall"
import { useLiveCall } from "@/lib/useLiveCall"
import { useAwsLevels } from "@/lib/useAwsLevels"
import { useRuntimeConfig } from "@/lib/useRuntimeConfig"
import { useAwsMonitor } from "@/lib/useAwsMonitor"
import { useConnectSoftphoneContext } from "@/lib/connectSoftphoneContext"

export function LiveCall() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  // The real call id POST /api/calls returned when the case queue page's "Start call"
  // started this call (?call=<uuid> on the live-call route) -- not the case id, which is
  // a different value (see lib/api.ts's startCall). Falls back to the call persisted in
  // lib/activeCall.ts when the URL doesn't carry one -- e.g. a plain nav click back to
  // "Live call" (AppShell.tsx's link itself carries ?call=<id> while one is active, but a
  // bare "/" -- a reload, or a stale link -- must not reset the page).
  const callId = params.get("call") ?? getActiveCall()

  useEffect(() => {
    if (callId) setActiveCall(callId)
  }, [callId])

  const { state, clear } = useLiveCall(callId)
  const config = useRuntimeConfig()
  const callActive = state.started && !state.hungUp
  const awsMonitor = useAwsMonitor(callId, callActive)
  // Whoever answers as the front desk is now on the call itself: stop Monitor so they
  // don't hear the conversation twice.
  const { phase: softphonePhase } = useConnectSoftphoneContext()
  const stopMonitor = awsMonitor.stop
  useEffect(() => {
    if (softphonePhase === "connected") stopMonitor()
  }, [softphonePhase, stopMonitor])
  // Speaking indicators: a silent, polled "levels" feed (lib/useAwsLevels.ts) -- no
  // config gate needed, since the dashboard API itself 404s (with a toast) if the
  // monitor bucket isn't deployed.
  const awsLevels = useAwsLevels(callId, callActive)

  // "End after-call work": the case queue page starts calls (see CaseQueue.tsx), so
  // this page needs its own way to drop the finished call once the user is done
  // reviewing it, clearing both the persisted active call and the page's own state, and
  // the URL's ?call= along with them.
  const endAfterCallWork = () => {
    setActiveCall(null)
    clear()
    navigate("/", { replace: true })
  }

  const navActive = state.phase === "navigate"
  const talkActive = state.phase === "intake"

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-4 lg:overflow-hidden">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1">
          <CaseStrip c={state.caseSummary} />
        </div>
        {/* Monitor: always rendered -- if the monitor bucket isn't deployed, toggling
            it just 404s and toasts (see lib/useAwsMonitor.ts, lib/useMonitorFeed.ts). */}
        <MonitorButton
          listen={awsMonitor.listen}
          toggleListen={awsMonitor.toggleListen}
          wsConnected={true}
          callActive={callActive}
        />
        <ScriptSheet caseSummary={state.caseSummary} />
        {state.hungUp && (
          <Button size="sm" variant="secondary" onClick={endAfterCallWork}>
            <ClipboardCheck /> End after-call work
          </Button>
        )}
      </div>

      <div
        className={cn(
          "grid min-h-0 flex-1 grid-cols-1 gap-3 lg:overflow-hidden",
          config?.ccpUrl
            ? "lg:grid-cols-[minmax(260px,0.9fr)_minmax(0,1.6fr)_minmax(280px,1fr)]"
            : "lg:grid-cols-[minmax(0,1.6fr)_minmax(280px,1fr)]",
        )}
      >
        {/* There is no browser front desk leg -- the handoff is SIP/Connect -- but this
            left column reappears once config.json names a Connect CCP to embed (see
            ConnectCcpCard.tsx). */}
        {config?.ccpUrl && (
          <div className="flex min-h-0 flex-col gap-3 overflow-hidden lg:overflow-auto">
            <ConnectCcpCard />
          </div>
        )}

        <Card className="flex min-h-0 flex-col overflow-hidden lg:overflow-auto">
          <CardContent className="flex min-h-0 flex-1 flex-col gap-3 p-4">
            {!state.started ? (
              <div className="grid flex-1 place-items-center">
                <div className="max-w-xs text-center">
                  <h2 className="text-base font-bold">Waiting for a call</h2>
                  <p className="mt-2 text-sm text-muted-foreground">
                    Click Start call to have Outbound IVR call the office.
                  </p>
                </div>
              </div>
            ) : (
              <>
                <div className="flex flex-none items-center gap-2.5">
                  <span
                    className={cn(
                      "rounded-full border px-3 py-1.5 font-mono text-xs",
                      navActive
                        ? "border-primary/50 bg-primary/10 text-primary"
                        : "border-border bg-secondary text-muted-foreground",
                    )}
                  >
                    navigating menu
                  </span>
                  <span className="text-muted-foreground">→</span>
                  <span
                    className={cn(
                      "rounded-full border px-3 py-1.5 font-mono text-xs",
                      talkActive
                        ? "border-primary/50 bg-primary/10 text-primary"
                        : "border-border bg-secondary text-muted-foreground",
                    )}
                  >
                    talking to the front desk
                  </span>
                </div>

                {/* There is no server-side levels source (OUTBOUND_IVR_LEVELS=0) -- levels are
                    computed client-side instead, from a silent polled feed
                    (lib/useAwsLevels.ts); no config gate needed (see awsLevels above). */}
                <div className="flex-none grid grid-cols-2 gap-3">
                  <SpeakingIndicator
                    label="Far end"
                    level={awsLevels.far}
                    spark={awsLevels.farSpark}
                    speaking={awsLevels.farSpeaking}
                  />
                  <SpeakingIndicator
                    label="Outbound IVR"
                    level={awsLevels.agent}
                    spark={awsLevels.agentSpark}
                    speaking={awsLevels.agentSpeaking}
                    accent
                  />
                </div>

                <div className="flex-none">
                  <VerdictBox kind={state.verdict.kind} big={state.verdict.big} cap={state.verdict.cap} />
                </div>

                <div className="flex-none">
                  <Keypad
                    pressCounts={state.pressCounts}
                    lastPressedKey={state.lastPressedKey}
                    pressSeq={state.pressSeq}
                    dimmed={talkActive}
                  />
                </div>

                <Transcript turns={state.transcript} />
              </>
            )}
          </CardContent>
        </Card>

        <Card className="flex min-h-0 flex-col overflow-hidden lg:overflow-auto">
          <CardContent className="flex min-h-0 flex-1 flex-col gap-3 p-4">
            {state.checklist.length > 0 && (
              <div className="flex-none">
                <h3 className="mb-2 text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
                  Evidence collected
                </h3>
                <Checklist items={state.checklist} state={state.checklistState} />
              </div>
            )}
            <div className="flex min-h-0 flex-1 flex-col border-t pt-3">
              <h3 className="mb-2 flex-none text-[11px] font-medium tracking-wider text-muted-foreground uppercase">
                Live events
              </h3>
              <EventLog entries={state.log} />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
