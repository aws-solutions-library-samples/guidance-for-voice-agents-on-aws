// @vitest-environment jsdom
// Change (1): persisting the active call across navigation (lib/activeCall.ts). This
// mocks everything else the page renders/uses so the test stays about that logic, not
// about re-testing Transcript/Checklist/etc, which already have their own tests.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { LiveCall } from "@/pages/LiveCall"
import { getActiveCall, resetActiveCallForTests, setActiveCall } from "@/lib/activeCall"

vi.mock("@/components/CaseStrip", () => ({ CaseStrip: () => null }))
vi.mock("@/components/ScriptSheet", () => ({ ScriptSheet: () => null }))
vi.mock("@/components/SpeakingIndicator", () => ({
  SpeakingIndicator: ({ label }: { label: string }) => <div data-testid="speaking-indicator">{label}</div>,
}))
vi.mock("@/components/VerdictBox", () => ({ VerdictBox: () => null }))
vi.mock("@/components/Keypad", () => ({ Keypad: () => null }))
vi.mock("@/components/Transcript", () => ({ Transcript: () => null }))
vi.mock("@/components/Checklist", () => ({ Checklist: () => null }))
vi.mock("@/components/EventLog", () => ({ EventLog: () => null }))
vi.mock("@/components/ConnectCcpCard", () => ({ ConnectCcpCard: () => <div data-testid="ccp-card" /> }))
vi.mock("@/components/MonitorButton", () => ({ MonitorButton: () => <div data-testid="monitor-button" /> }))
const monitorStop = vi.fn()
vi.mock("@/lib/useAwsMonitor", () => ({ useAwsMonitor: () => ({ listen: "on", toggleListen: vi.fn(), stop: monitorStop }) }))
const softphone = { phase: "ready" }
vi.mock("@/lib/connectSoftphoneContext", () => ({ useConnectSoftphoneContext: () => softphone }))
vi.mock("@/lib/useAwsLevels", () => ({
  useAwsLevels: () => ({ far: 0, agent: 0, farSpark: "", agentSpark: "", farSpeaking: false, agentSpeaking: false }),
}))

const useRuntimeConfigMock = vi.fn()
vi.mock("@/lib/useRuntimeConfig", () => ({ useRuntimeConfig: () => useRuntimeConfigMock() }))

const useLiveCallMock = vi.fn()
vi.mock("@/lib/useLiveCall", () => ({ useLiveCall: (callId: string | null) => useLiveCallMock(callId) }))

function stubLiveCall(_callId: string | null, overrides: Record<string, unknown> = {}) {
  const clear = vi.fn()
  const state = {
    started: false,
    hungUp: false,
    phase: undefined,
    verdict: { kind: "", big: "", cap: "" },
    pressCounts: {},
    pressSeq: 0,
    transcript: [],
    log: [],
    checklist: [],
    checklistState: {},
    caseSummary: undefined,
    ...overrides,
  }
  useLiveCallMock.mockReturnValue({ state, clear })
  return { clear, state }
}

function LocationCapture() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search}</div>
}

function renderLiveCall(initialEntry = "/") {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/" element={<LiveCall />} />
      </Routes>
      <LocationCapture />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  useLiveCallMock.mockReset()
  useRuntimeConfigMock.mockReset().mockReturnValue(null)
  stubLiveCall(null)
})

afterEach(() => {
  cleanup()
  resetActiveCallForTests()
})

describe("LiveCall (aws mode): persisting the active call", () => {
  it("passes the ?call= id straight through, and persists it as the active call", () => {
    stubLiveCall(null)
    renderLiveCall("/?call=real-call-uuid")

    expect(useLiveCallMock).toHaveBeenCalledWith("real-call-uuid")
    expect(getActiveCall()).toBe("real-call-uuid")
  })

  it("falls back to the persisted active call when the URL carries none", () => {
    setActiveCall("stored-call-uuid")
    stubLiveCall(null)
    renderLiveCall("/")

    expect(useLiveCallMock).toHaveBeenCalledWith("stored-call-uuid")
  })

  it("has nothing to show (and nothing to persist) with no url call and no stored one", () => {
    stubLiveCall(null)
    renderLiveCall("/")

    expect(useLiveCallMock).toHaveBeenCalledWith(null)
    expect(getActiveCall()).toBeNull()
  })

  it("prefers the URL's call id over a stale stored one (starting a fresh call)", () => {
    setActiveCall("stale-call-uuid")
    stubLiveCall(null)
    renderLiveCall("/?call=fresh-call-uuid")

    expect(useLiveCallMock).toHaveBeenCalledWith("fresh-call-uuid")
    expect(getActiveCall()).toBe("fresh-call-uuid")
  })
})

describe("LiveCall (aws mode): End after-call work", () => {
  it("is offered once the call has ended", () => {
    stubLiveCall("real-call-uuid", { started: true, hungUp: true })
    renderLiveCall("/?call=real-call-uuid")

    expect(screen.getByText(/End after-call work/)).toBeTruthy()
  })

  it("is not offered while the call is still live, or before one has started", () => {
    stubLiveCall("real-call-uuid", { started: true, hungUp: false })
    renderLiveCall("/?call=real-call-uuid")

    expect(screen.queryByText(/End after-call work/)).toBeNull()
  })

  it("clears the persisted active call, clears the page state, and drops ?call= from the url", () => {
    setActiveCall("real-call-uuid")
    const { clear } = stubLiveCall("real-call-uuid", { started: true, hungUp: true })
    renderLiveCall("/?call=real-call-uuid")

    fireEvent.click(screen.getByText(/End after-call work/))

    expect(getActiveCall()).toBeNull()
    expect(clear).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("location").textContent).toBe("/")
  })
})

describe("LiveCall (aws mode): Monitor and Connect CCP", () => {
  it("always shows Monitor -- there's no config gate (the API itself 404s if undeployed)", () => {
    useRuntimeConfigMock.mockReturnValue({ apiUrl: "x", cognitoDomain: "x", cognitoClientId: "x", cognitoRegion: "us-east-1", region: "us-east-1" })
    stubLiveCall(null)
    renderLiveCall("/")

    expect(screen.getByTestId("monitor-button")).toBeTruthy()
    expect(screen.queryByTestId("ccp-card")).toBeNull()
  })

  it("shows the Connect CCP card once config.json's ccpUrl is set", () => {
    useRuntimeConfigMock.mockReturnValue({
      apiUrl: "x",
      cognitoDomain: "x",
      cognitoClientId: "x",
      cognitoRegion: "us-east-1",
      region: "us-east-1",
      ccpUrl: "https://example.my.connect.aws/ccp-v2/",
      connectRegion: "us-east-1",
    })
    stubLiveCall(null)
    renderLiveCall("/")

    expect(screen.getByTestId("ccp-card")).toBeTruthy()
    expect(screen.getByTestId("monitor-button")).toBeTruthy()
  })
})

describe("LiveCall (aws mode): speaking indicators", () => {
  it("are hidden before a call starts", () => {
    stubLiveCall(null)
    renderLiveCall("/")

    expect(screen.queryAllByTestId("speaking-indicator")).toHaveLength(0)
  })

  it("show once the call is live -- no config gate", () => {
    stubLiveCall("real-call-uuid", { started: true, hungUp: false })
    renderLiveCall("/?call=real-call-uuid")

    expect(screen.queryAllByTestId("speaking-indicator")).toHaveLength(2)
  })
})

describe("LiveCall (aws mode): Monitor stops when the front desk answers", () => {
  it("stops Monitor once the Connect softphone is connected", () => {
    useRuntimeConfigMock.mockReturnValue({ apiUrl: "x", cognitoDomain: "x", cognitoClientId: "x", cognitoRegion: "us-east-1", region: "us-east-1" })
    stubLiveCall("call-1", { started: true })
    monitorStop.mockClear()
    softphone.phase = "ready"
    const page = () => (
      <MemoryRouter initialEntries={["/?call=call-1"]}>
        <Routes>
          <Route path="/" element={<LiveCall />} />
        </Routes>
      </MemoryRouter>
    )
    const { rerender } = render(page())
    expect(monitorStop).not.toHaveBeenCalled()
    softphone.phase = "connected"
    rerender(page())
    expect(monitorStop).toHaveBeenCalled()
  })
})
