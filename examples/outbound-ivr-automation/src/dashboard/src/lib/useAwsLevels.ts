import { useCallback, useEffect, useRef, useState } from "react"
import { useMonitorFeed, type MonitorMessage } from "@/lib/useMonitorFeed"

export type LevelsState = {
  far: number
  agent: number
  farSpark: string
  agentSpark: string
  farSpeaking: boolean
  agentSpeaking: boolean
}

const SPARK_CHARS = "▁▂▃▄▅▆▇█"
const SPARK_LEN = 40
const SPEAKING_THRESHOLD = 0.08
const DECAY_EASE = 0.25
// The server sends one RMS pair per chunk, about every 200 ms (the Runtime's chunk
// cadence, src/outbound_ivr/voice/monitor.py) -- a dB scale where -50 dBFS is 0. A whole
// segment of them (lib/useMonitorFeed.ts) lands at once every poll, so they're queued
// and replayed at that same per-chunk cadence rather than jumping straight to the
// newest value, which would make the meters update in visible 2 s steps instead of
// animating smoothly.
const FLOOR_DB = -50
const CHUNK_MS = 200

/** RMS of one chunk as the server's `_level`: -50 dBFS and below is 0, full scale is 1. */
export function levelFromRms(rms: number): number {
  const db = 20 * Math.log10(rms + 1e-9)
  return Math.min(1, Math.max(0, (db - FLOOR_DB) / -FLOOR_DB))
}

const initialState: LevelsState = {
  far: 0,
  agent: 0,
  farSpark: "",
  agentSpark: "",
  farSpeaking: false,
  agentSpeaking: false,
}

/**
 * Speaking indicators: polled audio-levels segments from the Runtime
 * (lib/useMonitorFeed.ts, mode "levels" -- no `pcm`, just `far_rms`/`agent_rms` per
 * chunk), eased and sparklined the same way the old LiveKit-tap implementation was. No
 * local audio track to analyze anymore -- the RMS values come pre-computed from the
 * server, from the recorder's own per-leg buffers before mixing.
 */
export function useAwsLevels(callId: string | null, active: boolean): LevelsState {
  const [state, setState] = useState<LevelsState>(initialState)

  const queue = useRef<{ far: number; agent: number }[]>([])
  const sparkHistory = useRef<{ far: number[]; agent: number[] }>({ far: [], agent: [] })
  const levelShown = useRef<{ far: number; agent: number }>({ far: 0, agent: 0 })
  const easeTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const replayTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const target = useRef<{ far: number; agent: number }>({ far: 0, agent: 0 })
  const lastSeq = useRef(-1)

  const reset = () => {
    queue.current = []
    sparkHistory.current = { far: [], agent: [] }
    levelShown.current = { far: 0, agent: 0 }
    target.current = { far: 0, agent: 0 }
    lastSeq.current = -1
    setState(initialState)
  }

  useEffect(() => {
    if (!active) reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // Each chunk the feed hands us, in order, is queued rather than applied immediately --
  // see the replay-cadence effect below.
  const onChunk = useCallback((msg: MonitorMessage) => {
    if (msg.type !== "levels" || msg.seq <= lastSeq.current) return
    lastSeq.current = msg.seq
    queue.current.push({ far: levelFromRms(msg.far_rms), agent: levelFromRms(msg.agent_rms) })
  }, [])

  useMonitorFeed(callId, "levels", active, onChunk)

  const pushSpark = (who: "far" | "agent", value: number) => {
    const hist = sparkHistory.current[who]
    hist.push(Math.max(0, Math.min(1, value)))
    while (hist.length > SPARK_LEN) hist.shift()
  }

  // Replay one queued chunk per tick, at the chunk cadence, so a whole segment arriving
  // at once (every poll) still animates smoothly instead of jumping.
  useEffect(() => {
    if (!active) return
    const tick = () => {
      const next = queue.current.shift()
      if (!next) return
      target.current = next
      pushSpark("far", next.far)
      pushSpark("agent", next.agent)
    }
    replayTimer.current = setInterval(tick, CHUNK_MS)
    return () => {
      if (replayTimer.current) clearInterval(replayTimer.current)
      replayTimer.current = null
    }
  }, [active])

  // A steady ease-toward-target loop, independent of replay cadence, so the displayed
  // level decays smoothly even if the next chunk is a little late.
  useEffect(() => {
    if (!active) return
    const tick = () => {
      const next: LevelsState = { ...initialState }
      for (const who of ["far", "agent"] as const) {
        levelShown.current[who] += (target.current[who] - levelShown.current[who]) * DECAY_EASE
        if (Math.abs(levelShown.current[who]) < 0.002) levelShown.current[who] = 0
        const level = levelShown.current[who]
        const spark = sparkHistory.current[who]
          .map((v) => SPARK_CHARS[Math.min(SPARK_CHARS.length - 1, Math.floor(v * (SPARK_CHARS.length - 1)))])
          .join("")
        if (who === "far") {
          next.far = level
          next.farSpark = spark
          next.farSpeaking = level > SPEAKING_THRESHOLD
        } else {
          next.agent = level
          next.agentSpark = spark
          next.agentSpeaking = level > SPEAKING_THRESHOLD
        }
      }
      setState(next)
    }
    easeTimer.current = setInterval(tick, 100)
    return () => {
      if (easeTimer.current) clearInterval(easeTimer.current)
      easeTimer.current = null
    }
  }, [active])

  return state
}
