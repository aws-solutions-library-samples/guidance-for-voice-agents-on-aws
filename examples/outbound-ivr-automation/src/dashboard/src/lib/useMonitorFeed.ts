import { useEffect, useRef, useState } from "react"
import { MonitorNotEnabledError, getMonitorSegments } from "@/lib/api"
import { showToast } from "@/lib/toast"

export type FeedStatus = "off" | "connecting" | "on"
export type MonitorMode = "listen" | "levels"
export type MonitorMessage =
  | { type: "audio"; seq: number; pcm: string; far_rms: number; agent_rms: number }
  | { type: "levels"; seq: number; far_rms: number; agent_rms: number }

type ChunkListener = (msg: MonitorMessage) => void
type StatusListener = (status: FeedStatus) => void

const POLL_MS = 2000

/**
 * One poll loop per (call, mode) pair, shared by every hook instance that wants it --
 * Monitor's audio playback (useAwsMonitor.ts, mode "listen") and the speaking-indicator
 * levels (useAwsLevels.ts, mode "levels"). Each poll calls
 * `GET /api/calls/{call}/monitor?mode=...&after=<seq>` (lib/api.ts's
 * `getMonitorSegments`), the same Cognito-protected API every other route goes through
 * -- no WebSocket, no ticket, no bucket access from the browser.
 *
 * Module-level, reference-counted singleton per mode, same shape as the WebSocket
 * version it replaces: the first consumer for a given call starts the poll loop, later
 * consumers for the same call just attach, and it only stops once the last consumer
 * lets go. Chunks are handed to subscribers one at a time, in order, via a direct
 * callback -- never through React state, which would batch a whole segment's worth of
 * chunks into a single render and drop the ones in between.
 */
class SharedFeed {
  private readonly mode: MonitorMode
  private callId: string | null = null
  private refCount = 0
  private status: FeedStatus = "off"
  private statusListeners = new Set<StatusListener>()
  private chunkListeners = new Set<ChunkListener>()
  private after = -1
  private timer: ReturnType<typeof setTimeout> | null = null
  // Bumped on every swapTo (new call, or release to null) so a poll already in flight
  // when that happens can tell it's stale and must not touch state or schedule again.
  private generation = 0

  constructor(mode: MonitorMode) {
    this.mode = mode
  }

  private setStatus(status: FeedStatus) {
    this.status = status
    for (const listener of this.statusListeners) listener(status)
  }

  subscribeStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener)
    listener(this.status)
    return () => this.statusListeners.delete(listener)
  }

  subscribeChunks(listener: ChunkListener): () => void {
    this.chunkListeners.add(listener)
    return () => this.chunkListeners.delete(listener)
  }

  acquire(callId: string) {
    this.refCount++
    if (this.callId === callId) return // already polling, or about to
    this.swapTo(callId)
  }

  release() {
    this.refCount = Math.max(0, this.refCount - 1)
    if (this.refCount === 0) this.swapTo(null)
  }

  private swapTo(callId: string | null) {
    this.generation++
    this.callId = callId
    this.after = -1
    this.clearTimer()
    this.setStatus(callId ? "connecting" : "off")
    if (callId) this.schedule(0)
  }

  private clearTimer() {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private schedule(delayMs: number) {
    this.clearTimer()
    const generation = this.generation
    this.timer = setTimeout(() => void this.poll(generation), delayMs)
  }

  private async poll(generation: number) {
    if (generation !== this.generation || !this.callId) return
    const callId = this.callId
    try {
      const { segments, after } = await getMonitorSegments(callId, this.mode, this.after)
      if (generation !== this.generation) return // superseded while the request was in flight
      for (const segment of segments) {
        segment.chunks.forEach((chunk, index) => {
          const seq = segment.seq * 10 + index
          const msg: MonitorMessage =
            this.mode === "listen"
              ? { type: "audio", seq, pcm: chunk.pcm ?? "", far_rms: chunk.far_rms, agent_rms: chunk.agent_rms }
              : { type: "levels", seq, far_rms: chunk.far_rms, agent_rms: chunk.agent_rms }
          for (const listener of this.chunkListeners) listener(msg)
        })
      }
      this.after = after
      this.setStatus("on")
      this.schedule(POLL_MS)
    } catch (err) {
      if (generation !== this.generation) return
      if (err instanceof MonitorNotEnabledError) {
        showToast(err.message, "err")
        this.setStatus("off")
        return // monitor isn't deployed -- no point polling again
      }
      // Transient error (network blip, 5xx): keep polling with the same `after`,
      // nothing to show the user over a single failed poll.
      this.schedule(POLL_MS)
    }
  }
}

const sharedFeeds: Record<MonitorMode, SharedFeed> = {
  listen: new SharedFeed("listen"),
  levels: new SharedFeed("levels"),
}

/**
 * React binding for `SharedFeed`: acquires it while `want` is true (and `callId` is
 * set), releasing the moment either goes away -- a call ending, the owning component
 * unmounting, or this particular consumer no longer wanting the feed (e.g. Monitor
 * being switched off). Each chunk is delivered to `onChunk` synchronously, in order, as
 * soon as the shared poll sees it; the returned status is the only thing driven through
 * React state. See useAwsMonitor.ts and useAwsLevels.ts.
 */
export function useMonitorFeed(
  callId: string | null,
  mode: MonitorMode,
  want: boolean,
  onChunk: ChunkListener,
): FeedStatus {
  const [status, setStatus] = useState<FeedStatus>("off")
  const onChunkRef = useRef(onChunk)
  onChunkRef.current = onChunk

  useEffect(() => {
    if (!callId || !want) {
      setStatus("off")
      return
    }
    const shared = sharedFeeds[mode]
    const unsubscribeStatus = shared.subscribeStatus(setStatus)
    const unsubscribeChunks = shared.subscribeChunks((msg) => onChunkRef.current(msg))
    shared.acquire(callId)
    return () => {
      unsubscribeStatus()
      unsubscribeChunks()
      shared.release()
    }
  }, [callId, mode, want])

  return status
}
