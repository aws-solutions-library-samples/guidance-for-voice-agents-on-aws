import { useEffect, useRef } from "react"
import { getCallEvents } from "@/lib/api"
import type { AgentEvent } from "@/lib/events"

/**
 * Polls `/api/calls/{call}/events?after=<seq>` once a second -- there is no live push
 * stream from the deployed backend. Feeds useLiveCall's reducer the same event shape a
 * push stream would.
 */
// Polls (about a second each) to wait for an out-of-order event before moving past it.
const GAP_POLLS_BEFORE_SKIP = 5

export function usePolledEvents(
  call: string | null,
  handlers: {
    onMessage: (data: AgentEvent) => void
    onSwitch?: () => void
    onConnectionChange?: (connected: boolean) => void
  },
  intervalMs = 1000,
) {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers
  const afterRef = useRef(-1)
  const seenRef = useRef(new Set<number>())
  const gapPollsRef = useRef(0)
  const callRef = useRef<string | null>(null)

  useEffect(() => {
    if (!call) return
    if (callRef.current !== call) {
      callRef.current = call
      afterRef.current = -1
      seenRef.current = new Set()
      gapPollsRef.current = 0
      handlersRef.current.onSwitch?.()
    }

    let cancelled = false
    const tick = async () => {
      try {
        const events = await getCallEvents(call, afterRef.current)
        if (cancelled) return
        handlersRef.current.onConnectionChange?.(true)
        for (const event of events) {
          if (seenRef.current.has(event.seq)) continue
          seenRef.current.add(event.seq)
          handlersRef.current.onMessage(event)
        }
        // Advance the cursor only through seqs with no gap: Outbound IVR's writes can land
        // out of order, and jumping past a seq that isn't visible yet would skip that
        // event for good (a missing key press on the dial pad). A gap that never fills
        // is skipped after a few polls.
        let next = afterRef.current
        while (seenRef.current.has(next + 1)) next += 1
        const highest = Math.max(afterRef.current, ...seenRef.current)
        if (next < highest) {
          gapPollsRef.current += 1
          if (gapPollsRef.current >= GAP_POLLS_BEFORE_SKIP) {
            next = highest
            gapPollsRef.current = 0
          }
        } else {
          gapPollsRef.current = 0
        }
        afterRef.current = next
      } catch {
        if (!cancelled) handlersRef.current.onConnectionChange?.(false)
      }
    }

    void tick()
    const id = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [call, intervalMs])
}
