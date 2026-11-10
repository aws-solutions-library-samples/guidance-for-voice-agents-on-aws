// @vitest-environment jsdom
// This is the regression test for the bug ConnectSoftphoneProvider fixes: amazon-connect-
// streams' initCCP has no "destroy" and may only run once per container, so when
// ConnectCcpCard owned its own hidden iframe, navigating Live call -> Case queue ->
// Live call unmounted and remounted the card, Streams never re-initialized (same guard
// that stops a double StrictMode mount from re-initializing also stopped this), and the
// card was left showing a stale "Available" state with no CCP underneath it at all.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, waitFor } from "@testing-library/react"
import {
  ConnectSoftphoneProvider,
  useConnectSoftphoneContext,
} from "@/lib/connectSoftphoneContext"

vi.mock("amazon-connect-streams", () => ({}))

const CONFIG = {
  apiUrl: "x",
  cognitoDomain: "x",
  cognitoClientId: "x",
  cognitoRegion: "us-east-1",
  region: "us-east-1",
  ccpUrl: "https://a.my.connect.aws/ccp-v2/",
  connectRegion: "us-east-1",
}

const useRuntimeConfigMock = vi.fn()
vi.mock("@/lib/useRuntimeConfig", () => ({ useRuntimeConfig: () => useRuntimeConfigMock() }))
vi.mock("@/lib/backend", () => ({ isAws: true, isLocal: false }))

class FakeAgent {
  getState() {
    return { name: "Available" }
  }
  getAgentStates() {
    return [{ agentStateARN: "arn:available", type: "routable", name: "Available" }]
  }
  onStateChange() {
    return { unsubscribe: () => {} }
  }
  onMuteToggle() {
    return { unsubscribe: () => {} }
  }
  onSoftphoneError() {
    return { unsubscribe: () => {} }
  }
}

let agentCallback: ((agent: FakeAgent) => void) | null = null
const initCCP = vi.fn()

beforeEach(() => {
  agentCallback = null
  initCCP.mockReset()
  ;(globalThis as unknown as { connect: unknown }).connect = {
    core: { initCCP },
    agent: (cb: (agent: FakeAgent) => void) => {
      agentCallback = cb
      return { unsubscribe: () => {} }
    },
    contact: () => {
      return { unsubscribe: () => {} }
    },
    AgentStateType: { ROUTABLE: "routable", NOT_ROUTABLE: "not_routable", OFFLINE: "offline" },
    ContactType: { VOICE: "voice", CHAT: "chat" },
  }
  useRuntimeConfigMock.mockReset().mockReturnValue(CONFIG)
})

afterEach(() => {
  cleanup()
  delete (globalThis as { connect?: unknown }).connect
})

function Consumer({ label }: { label: string }) {
  const { phase } = useConnectSoftphoneContext()
  return <div data-testid={label}>{phase}</div>
}

describe("ConnectSoftphoneProvider", () => {
  it("initializes Streams exactly once and keeps running when a consumer unmounts and remounts (simulated navigation)", async () => {
    function Harness({ showConsumer }: { showConsumer: boolean }) {
      return (
        <ConnectSoftphoneProvider>
          {showConsumer && <Consumer label="consumer" />}
        </ConnectSoftphoneProvider>
      )
    }

    const { rerender } = render(<Harness showConsumer={true} />)
    await waitFor(() => expect(initCCP).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(agentCallback).not.toBeNull())

    // Simulate navigating away from the Live call page: the card (consumer) unmounts,
    // but the provider -- mounted once at the app root in AppShell.tsx -- does not.
    rerender(<Harness showConsumer={false} />)
    expect(initCCP).toHaveBeenCalledTimes(1)

    // Fire an incoming contact while no consumer is mounted, exactly as it would while
    // the agent is looking at another page.
    act(() => agentCallback?.(new FakeAgent()))

    // Simulate navigating back to Live call: the card remounts.
    rerender(<Harness showConsumer={true} />)

    // Streams must not be re-initialized -- a second initCCP call for the same
    // container throws in the real package.
    expect(initCCP).toHaveBeenCalledTimes(1)
  })

  it("exposes enabled: false and never calls initCCP when config.json has no ccpUrl (local mode shape)", async () => {
    useRuntimeConfigMock.mockReturnValue(null)

    function Harness() {
      const { enabled } = useConnectSoftphoneContext()
      return <div data-testid="enabled">{String(enabled)}</div>
    }

    const { getByTestId } = render(
      <ConnectSoftphoneProvider>
        <Harness />
      </ConnectSoftphoneProvider>,
    )

    expect(getByTestId("enabled").textContent).toBe("false")
    expect(initCCP).not.toHaveBeenCalled()
  })

  it("throws when used without a ConnectSoftphoneProvider above it", () => {
    function Lonely() {
      useConnectSoftphoneContext()
      return null
    }
    expect(() => render(<Lonely />)).toThrow(/ConnectSoftphoneProvider/)
  })
})
