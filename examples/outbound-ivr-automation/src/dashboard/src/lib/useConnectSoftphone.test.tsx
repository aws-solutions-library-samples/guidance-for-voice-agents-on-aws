// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, renderHook, waitFor } from "@testing-library/react"
import { useConnectSoftphone } from "@/lib/useConnectSoftphone"

// amazon-connect-streams exports no module value -- it sets the ambient global `connect`
// as a side effect of being imported (see useConnectSoftphone.ts's doc comment). The
// mock stays an empty module so the hook never touches the real package under jsdom,
// and the fake `connect` global is built fresh in `beforeEach` (a factory-set global
// wouldn't survive `afterEach` deleting it between tests, since a dynamic `import()`
// only runs a module's body once).

vi.mock("amazon-connect-streams", () => ({}))

type Sub = { unsubscribe: () => void }
type FakeAgentState = { agentStateARN: string; type: string; name: string }

const AVAILABLE: FakeAgentState = { agentStateARN: "arn:available", type: "routable", name: "Available" }
const OFFLINE: FakeAgentState = { agentStateARN: "arn:offline", type: "offline", name: "Offline" }

class FakeAgent {
  stateName = AVAILABLE.name
  stateChangeCb: ((c: { agent: FakeAgent; oldState: string; newState: string }) => void) | null = null
  muteCb: ((s: { muted: boolean }) => void) | null = null
  errorCb: ((e: { errorType: string; errorMessage: string }) => void) | null = null

  getState() {
    return { name: this.stateName }
  }
  getAgentStates() {
    return [AVAILABLE, OFFLINE]
  }
  onStateChange(cb: typeof this.stateChangeCb): Sub {
    this.stateChangeCb = cb
    return { unsubscribe: () => {} }
  }
  onMuteToggle(cb: typeof this.muteCb): Sub {
    this.muteCb = cb
    return { unsubscribe: () => {} }
  }
  onSoftphoneError(cb: typeof this.errorCb): Sub {
    this.errorCb = cb
    return { unsubscribe: () => {} }
  }
  setState(
    state: FakeAgentState,
    callbacks: { success?: () => void; failure?: () => void },
  ) {
    this.setStateCalls.push(state)
    if (this.nextSetStateFails) {
      callbacks.failure?.()
      return
    }
    const oldState = this.stateName
    this.stateName = state.name
    callbacks.success?.()
    this.stateChangeCb?.({ agent: this, oldState, newState: state.name })
  }
  mute() {
    this.muteCb?.({ muted: true })
  }
  unmute() {
    this.muteCb?.({ muted: false })
  }

  setStateCalls: FakeAgentState[] = []
  nextSetStateFails = false
}

class FakeContact {
  incomingCb: (() => void) | null = null
  connectingCb: (() => void) | null = null
  connectedCb: (() => void) | null = null
  endedCb: (() => void) | null = null
  cleared = false
  rejected = false
  agentConnectionDestroyed = false
  type = "voice"
  phoneNumber: string | null = "+15551234567"

  getType() {
    return this.type
  }
  getInitialConnection() {
    return { getEndpoint: () => (this.phoneNumber ? { phoneNumber: this.phoneNumber } : null) }
  }
  onIncoming(cb: () => void): Sub {
    this.incomingCb = cb
    return { unsubscribe: () => {} }
  }
  onConnecting(cb: () => void): Sub {
    this.connectingCb = cb
    return { unsubscribe: () => {} }
  }
  onConnected(cb: () => void): Sub {
    this.connectedCb = cb
    return { unsubscribe: () => {} }
  }
  onEnded(cb: () => void): Sub {
    this.endedCb = cb
    return { unsubscribe: () => {} }
  }
  accept(callbacks: { success?: () => void; failure?: () => void }) {
    this.connectedCb?.()
    callbacks.success?.()
  }
  reject(callbacks: { success?: () => void; failure?: () => void }) {
    this.rejected = true
    callbacks.success?.()
  }
  clear(callbacks: { success?: () => void; failure?: () => void }) {
    this.cleared = true
    callbacks.success?.()
  }
  getAgentConnection() {
    return {
      destroy: (callbacks: { success?: () => void; failure?: () => void }) => {
        this.agentConnectionDestroyed = true
        callbacks.success?.()
      },
    }
  }
}

let fakeAgent: FakeAgent
let agentCallback: ((agent: FakeAgent) => void) | null = null
let contactCallback: ((contact: FakeContact) => void) | null = null
const initCCP = vi.fn()

// The agent/contact subscription effect lazy-imports "amazon-connect-streams" (even
// though the mock resolves it instantly, that's still a microtask) before it calls
// `connect.agent(...)` / `connect.contact(...)`, so a test must wait for those
// registrations before firing them -- see useConnectSoftphone.ts's doc comment.
async function waitForAgentRegistered() {
  await waitFor(() => expect(agentCallback).not.toBeNull())
}
async function waitForContactRegistered() {
  await waitFor(() => expect(contactCallback).not.toBeNull())
}
function fireAgentReady() {
  agentCallback?.(fakeAgent)
}
function fireContact(contact: FakeContact) {
  contactCallback?.(contact)
}

beforeEach(() => {
  fakeAgent = new FakeAgent()
  agentCallback = null
  contactCallback = null
  initCCP.mockReset()
  ;(globalThis as unknown as { connect: unknown }).connect = {
    core: { initCCP },
    agent: (cb: (agent: FakeAgent) => void) => {
      agentCallback = cb
      return { unsubscribe: () => {} }
    },
    contact: (cb: (contact: FakeContact) => void) => {
      contactCallback = cb
      return { unsubscribe: () => {} }
    },
    AgentStateType: { ROUTABLE: "routable", NOT_ROUTABLE: "not_routable", OFFLINE: "offline" },
    ContactType: { VOICE: "voice", CHAT: "chat" },
  }
})

afterEach(() => {
  delete (globalThis as { connect?: unknown }).connect
})

describe("useConnectSoftphone", () => {
  it("goes through sign-in -> available -> incoming -> answer -> connected -> mute -> hang up -> ended", async () => {
    const { result } = renderHook(() => useConnectSoftphone({ ccpUrl: "https://a.my.connect.aws/ccp-v2/", connectRegion: "us-east-1" }))

    expect(result.current.phase).toBe("signing-in")

    await waitForAgentRegistered()
    await act(async () => fireAgentReady())
    expect(result.current.phase).toBe("ready")
    expect(result.current.agentStateName).toBe("Available")
    expect(result.current.agentStates).toEqual([
      { name: "Available", routable: true },
      { name: "Offline", routable: false },
    ])

    const contact = new FakeContact()
    await waitForContactRegistered()
    await act(async () => fireContact(contact))
    await act(async () => contact.incomingCb?.())
    expect(result.current.phase).toBe("incoming")
    expect(result.current.callerNumber).toBe("+15551234567")

    await act(async () => result.current.answer())
    expect(result.current.phase).toBe("connected")

    expect(result.current.muted).toBe(false)
    await act(async () => result.current.toggleMute())
    expect(result.current.muted).toBe(true)
    await act(async () => result.current.toggleMute())
    expect(result.current.muted).toBe(false)

    await act(async () => result.current.hangup())
    expect(contact.agentConnectionDestroyed).toBe(true)

    await act(async () => contact.endedCb?.())
    expect(contact.cleared).toBe(true)
    expect(result.current.phase).toBe("ready")
    expect(result.current.callerNumber).toBe(null)
  })

  it("declines an incoming call via contact.reject", async () => {
    const { result } = renderHook(() => useConnectSoftphone({ ccpUrl: "https://a.my.connect.aws/ccp-v2/", connectRegion: "us-east-1" }))
    await waitForAgentRegistered()
    await act(async () => fireAgentReady())

    const contact = new FakeContact()
    await waitForContactRegistered()
    await act(async () => fireContact(contact))
    await act(async () => contact.incomingCb?.())
    expect(result.current.phase).toBe("incoming")

    await act(async () => result.current.decline())
    expect(contact.rejected).toBe(true)
  })

  it("surfaces a setState failure as an inline error and leaves the agent state unchanged", async () => {
    const { result } = renderHook(() => useConnectSoftphone({ ccpUrl: "https://a.my.connect.aws/ccp-v2/", connectRegion: "us-east-1" }))
    await waitForAgentRegistered()
    await act(async () => fireAgentReady())

    fakeAgent.nextSetStateFails = true
    await act(async () => result.current.toggleAvailability())

    expect(result.current.error).toMatch(/Offline/)
    expect(result.current.agentStateName).toBe("Available")
    expect(result.current.settingState).toBe(false)
  })

  it("toggles availability between Available and Offline", async () => {
    const { result } = renderHook(() => useConnectSoftphone({ ccpUrl: "https://a.my.connect.aws/ccp-v2/", connectRegion: "us-east-1" }))
    await waitForAgentRegistered()
    await act(async () => fireAgentReady())

    await act(async () => result.current.toggleAvailability())
    expect(result.current.agentStateName).toBe("Offline")

    await act(async () => result.current.toggleAvailability())
    expect(result.current.agentStateName).toBe("Available")
  })

  it("surfaces a softphone error from the agent inline", async () => {
    const { result } = renderHook(() => useConnectSoftphone({ ccpUrl: "https://a.my.connect.aws/ccp-v2/", connectRegion: "us-east-1" }))
    await waitForAgentRegistered()
    await act(async () => fireAgentReady())

    await act(async () => fakeAgent.errorCb?.({ errorType: "mic_error", errorMessage: "Microphone unavailable." }))
    expect(result.current.error).toBe("Microphone unavailable.")
  })

  it("initializes the CCP exactly once per mount even under a StrictMode-like double effect run", async () => {
    // The init effect only runs once `containerRef` is attached to a real DOM node, so
    // this uses a small harness component instead of a bare renderHook.
    function Harness(props: { ccpUrl: string; connectRegion: string }) {
      const { containerRef } = useConnectSoftphone(props)
      return <div ref={containerRef} />
    }

    const { unmount, rerender } = render(
      <Harness ccpUrl="https://a.my.connect.aws/ccp-v2/" connectRegion="us-east-1" />,
    )
    await waitFor(() => expect(initCCP).toHaveBeenCalledTimes(1))

    // Re-rendering with the same props must not re-init.
    rerender(<Harness ccpUrl="https://a.my.connect.aws/ccp-v2/" connectRegion="us-east-1" />)
    await waitFor(() => expect(initCCP).toHaveBeenCalledTimes(1))

    unmount()
  })
})

describe("softphoneErrorText", () => {
  it("explains a second softphone for the same agent in plain words", async () => {
    const { softphoneErrorText } = await import("@/lib/useConnectSoftphone")
    expect(softphoneErrorText({ errorMessage: "Signalling Handshake Failure" })).toMatch(/Another tab/)
    expect(softphoneErrorText({ errorType: "IdempotencyException" })).toMatch(/Another tab/)
    expect(softphoneErrorText({ errorMessage: "microphone not found" })).toBe("microphone not found")
  })
})
