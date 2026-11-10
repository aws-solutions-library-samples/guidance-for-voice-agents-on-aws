// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { useAwsMonitor } from "@/lib/useAwsMonitor"
import type { FeedStatus, MonitorMessage } from "@/lib/useMonitorFeed"

// useAwsMonitor only decides what to do with chunks the feed hands it -- the poll loop
// itself is lib/useMonitorFeed.ts's job (and has its own tests), so this stubs it out
// and drives it directly via the onChunk callback it's given.
let feedStatus: FeedStatus
let onChunk: (msg: MonitorMessage) => void
const useMonitorFeedMock = vi.fn()
vi.mock("@/lib/useMonitorFeed", () => ({
  useMonitorFeed: (...args: unknown[]) => useMonitorFeedMock(...args),
}))

// jsdom has no Web Audio API -- a minimal fake standing in for AudioContext and its
// buffer/source nodes.
class FakeBufferSource {
  buffer: unknown = null
  startCalls: number[] = []
  connect() {}
  start(when: number) {
    this.startCalls.push(when)
  }
}
class FakeAudioContext {
  currentTime = 0
  closed = false
  destination = {}
  sources: FakeBufferSource[] = []
  constructor(_opts?: { sampleRate?: number }) {
    audioContextInstances.push(this)
  }
  createBuffer(_channels: number, length: number, sampleRate: number) {
    return { duration: length / sampleRate, copyToChannel: vi.fn() }
  }
  createBufferSource() {
    const source = new FakeBufferSource()
    this.sources.push(source)
    return source
  }
  async close() {
    this.closed = true
  }
}

const audioContextInstances: FakeAudioContext[] = []

function b64Pcm16(samples: number[]): string {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  samples.forEach((s, i) => view.setInt16(i * 2, s, true))
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

function audioChunk(seq: number): MonitorMessage {
  return { type: "audio", seq, pcm: b64Pcm16([0, 100]), far_rms: 0.1, agent_rms: 0 }
}

beforeEach(() => {
  audioContextInstances.length = 0
  feedStatus = "off"
  useMonitorFeedMock.mockReset().mockImplementation((_callId: unknown, _mode: unknown, _want: unknown, cb: (msg: MonitorMessage) => void) => {
    onChunk = cb
    return feedStatus
  })
  vi.stubGlobal("AudioContext", FakeAudioContext)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe("useAwsMonitor", () => {
  it("starts off, and does nothing without a call id", async () => {
    const { result } = renderHook(() => useAwsMonitor(null, true))
    expect(result.current.listen).toBe("off")

    await act(async () => {
      await result.current.toggleListen()
    })
    expect(result.current.listen).toBe("off")
  })

  it("goes connecting on toggle, then on once the feed reports 'on'", async () => {
    const { result, rerender } = renderHook(() => useAwsMonitor("call-1", true))

    await act(async () => {
      await result.current.toggleListen()
    })
    expect(result.current.listen).toBe("connecting")

    feedStatus = "on"
    rerender()
    await waitFor(() => expect(result.current.listen).toBe("on"))
  })

  it("delays the first chunk's playback by one segment's worth of buffering", async () => {
    const { result } = renderHook(() => useAwsMonitor("call-1", true))
    await act(async () => {
      await result.current.toggleListen()
    })

    onChunk(audioChunk(0))
    expect(audioContextInstances).toHaveLength(1)
    expect(audioContextInstances[0].sources).toHaveLength(1)
    // 10 chunks * 200 ms -- one segment -- not scheduled at currentTime (0).
    expect(audioContextInstances[0].sources[0].startCalls[0]).toBeCloseTo(2, 5)
  })

  it("schedules each subsequent chunk back-to-back", async () => {
    const { result } = renderHook(() => useAwsMonitor("call-1", true))
    await act(async () => {
      await result.current.toggleListen()
    })
    onChunk(audioChunk(0))
    onChunk(audioChunk(1))

    expect(audioContextInstances[0].sources).toHaveLength(2)
    const [first, second] = audioContextInstances[0].sources
    expect(second.startCalls[0]).toBeGreaterThan(first.startCalls[0])
  })

  it("ignores a repeated or out-of-order seq (no duplicate playback)", async () => {
    const { result } = renderHook(() => useAwsMonitor("call-1", true))
    await act(async () => {
      await result.current.toggleListen()
    })
    onChunk(audioChunk(1))
    expect(audioContextInstances[0].sources).toHaveLength(1)

    onChunk(audioChunk(1)) // same seq again
    onChunk(audioChunk(0)) // an older seq
    expect(audioContextInstances[0].sources).toHaveLength(1)
  })

  it("stops and tears down the AudioContext when toggled off", async () => {
    const { result } = renderHook(() => useAwsMonitor("call-1", true))
    await act(async () => {
      await result.current.toggleListen()
    })
    onChunk(audioChunk(0))
    expect(audioContextInstances).toHaveLength(1)

    await act(async () => {
      await result.current.toggleListen()
    })
    expect(result.current.listen).toBe("off")
    expect(audioContextInstances[0].closed).toBe(true)
  })

  it("leaves the moment the call ends", async () => {
    const { result, rerender } = renderHook(({ callActive }) => useAwsMonitor("call-1", callActive), {
      initialProps: { callActive: true },
    })

    await act(async () => {
      await result.current.toggleListen()
    })
    feedStatus = "on"
    rerender({ callActive: true })
    expect(result.current.listen).toBe("on")

    rerender({ callActive: false })
    await waitFor(() => expect(result.current.listen).toBe("off"))
  })

  it("bounces back to off if the feed fails to connect", async () => {
    const { result, rerender } = renderHook(() => useAwsMonitor("call-1", true))

    await act(async () => {
      await result.current.toggleListen()
    })
    expect(result.current.listen).toBe("connecting")

    feedStatus = "connecting"
    rerender()
    expect(result.current.listen).toBe("connecting")

    feedStatus = "off"
    rerender()
    await waitFor(() => expect(result.current.listen).toBe("off"))
  })
})
