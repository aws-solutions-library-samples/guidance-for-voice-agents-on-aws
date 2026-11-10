import { useCallback, useEffect, useRef, useState } from "react"
import { useMonitorFeed, type MonitorMessage } from "@/lib/useMonitorFeed"

export type AwsMonitorStatus = "off" | "connecting" | "on"

// The Runtime writes 16-bit mono PCM at 8 kHz, in ~200 ms chunks, 10 to a segment (see
// src/outbound_ivr/voice/monitor.py). A whole segment (~2 s of audio) lands at once on every
// poll (lib/useMonitorFeed.ts, every 2 s), so delaying the very first chunk's playback
// by one segment's worth of time gives the next poll a full cycle to land before
// playback catches up to it -- a startup buffer sized to match the natural delivery
// rhythm, rather than a tighter per-chunk jitter buffer.
const SAMPLE_RATE = 8000
const SEGMENT_CHUNKS = 10
const CHUNK_MS = 200
const STARTUP_BUFFER_SECONDS = (SEGMENT_CHUNKS * CHUNK_MS) / 1000

function decodePcm16(base64: string): Float32Array<ArrayBuffer> {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  const view = new DataView(bytes.buffer)
  const sampleCount = Math.floor(bytes.length / 2)
  const samples = new Float32Array(sampleCount)
  for (let i = 0; i < sampleCount; i++) samples[i] = view.getInt16(i * 2, true) / 32768
  return samples
}

/**
 * Monitor: polled audio segments from the Runtime (lib/useMonitorFeed.ts, mode
 * "listen"), decoded and scheduled back-to-back on a shared `AudioContext` -- no
 * LiveKit, no `<audio>` elements, no WebSocket, just raw PCM chunks. Every byte comes
 * through the same Cognito-protected dashboard API every other route uses; there is
 * about a 3-6 second delay from the poll interval plus the startup buffer below, which
 * the user has accepted in exchange for the smaller moving parts.
 *
 * The feed is shared with the speaking-indicator levels (lib/useAwsLevels.ts) only in
 * the sense that both go through lib/useMonitorFeed.ts's per-mode singleton; "listen"
 * and "levels" always poll separately, since one strips `pcm` server-side and the
 * other doesn't.
 */
export function useAwsMonitor(callId: string | null, callActive: boolean) {
  const [listen, setListen] = useState<AwsMonitorStatus>("off")
  const wantFeed = listen !== "off"
  const audioCtx = useRef<AudioContext | null>(null)
  const nextPlayTime = useRef(0)
  const lastSeq = useRef(-1)
  const connectedOnce = useRef(false)

  const stopPlayback = useCallback(() => {
    if (audioCtx.current) {
      void audioCtx.current.close()
      audioCtx.current = null
    }
    nextPlayTime.current = 0
    lastSeq.current = -1
  }, [])

  const leave = useCallback(() => {
    connectedOnce.current = false
    stopPlayback()
    setListen("off")
  }, [stopPlayback])

  // The click that turns Monitor on is also the user gesture browsers require before an
  // AudioContext is allowed to make sound -- see MonitorButton.tsx's own unlock call.
  // Async to match the shape MonitorButton expects for `toggleListen`.
  const toggleListen = useCallback(async () => {
    if (!callId) return
    if (listen === "off") setListen("connecting")
    else leave()
  }, [callId, listen, leave])

  // Decode and schedule each chunk the feed hands us, in order, one at a time -- never
  // through React state, which would batch a whole segment's worth into one render.
  const onChunk = useCallback((msg: MonitorMessage) => {
    if (msg.type !== "audio" || msg.seq <= lastSeq.current) return
    lastSeq.current = msg.seq

    if (!audioCtx.current) {
      audioCtx.current = new AudioContext({ sampleRate: SAMPLE_RATE })
      nextPlayTime.current = audioCtx.current.currentTime + STARTUP_BUFFER_SECONDS
    }
    const ctx = audioCtx.current
    const samples = decodePcm16(msg.pcm)
    const buffer = ctx.createBuffer(1, samples.length, SAMPLE_RATE)
    buffer.copyToChannel(samples, 0)
    const source = ctx.createBufferSource()
    source.buffer = buffer
    source.connect(ctx.destination)

    // Underrun: playback caught up to (or past) real time because a poll was late --
    // re-anchor with a fresh startup buffer instead of overlapping audio or rushing to
    // catch up.
    if (nextPlayTime.current < ctx.currentTime) {
      nextPlayTime.current = ctx.currentTime + STARTUP_BUFFER_SECONDS
    }
    source.start(nextPlayTime.current)
    nextPlayTime.current += buffer.duration
  }, [])

  const status = useMonitorFeed(callId, "listen", wantFeed, onChunk)

  // Mirror the shared feed's status onto our own, but only once it has actually
  // reached "connecting" or "on" for *this* request -- otherwise the very first render
  // after clicking (feed not yet acquired) would read as an immediate failure and
  // bounce straight back to "off".
  useEffect(() => {
    if (!wantFeed) {
      connectedOnce.current = false
      return
    }
    if (status !== "off") connectedOnce.current = true
    if (status === "on") setListen("on")
    else if (status === "off" && connectedOnce.current) leave()
  }, [wantFeed, status, leave])

  // Leave the moment the call ends, and on unmount -- Monitor must never outlive the call
  // or the page.
  useEffect(() => {
    if (!callActive) leave()
  }, [callActive, leave])
  useEffect(() => () => leave(), [leave])

  return { listen, toggleListen, stop: leave }
}
