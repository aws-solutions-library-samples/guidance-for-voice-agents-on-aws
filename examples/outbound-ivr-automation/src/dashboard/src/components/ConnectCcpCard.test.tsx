// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { ConnectCcpCard } from "@/components/ConnectCcpCard"
import type { useConnectSoftphone } from "@/lib/useConnectSoftphone"

// The Streams wiring itself (sign-in, agent state, incoming/connected contacts) is
// covered by useConnectSoftphone.test.ts against a fake `connect` global, and the
// provider that owns the hidden CCP container across navigation is covered by
// connectSoftphoneContext.test.tsx -- this file only covers what the card renders for a
// given context state, so it mocks the context the card consumes.
vi.mock("@/lib/connectSoftphoneContext", () => ({ useConnectSoftphoneContext: vi.fn() }))

import { useConnectSoftphoneContext as useConnectSoftphoneContextMock } from "@/lib/connectSoftphoneContext"

type HookReturn = ReturnType<typeof useConnectSoftphone> & { enabled: boolean }

function mockHook(overrides: Partial<HookReturn>) {
  const base: HookReturn = {
    phase: "signing-in",
    agentStateName: null,
    agentStates: [],
    settingState: false,
    callerNumber: null,
    muted: false,
    callSeconds: 0,
    error: null,
    containerRef: { current: null },
    openLogin: vi.fn(),
    setAgentState: vi.fn(),
    toggleAvailability: vi.fn(),
    answer: vi.fn(),
    decline: vi.fn(),
    toggleMute: vi.fn(),
    hangup: vi.fn(),
    clearError: vi.fn(),
    enabled: true,
  }
  vi.mocked(useConnectSoftphoneContextMock).mockReturnValue({ ...base, ...overrides })
}

afterEach(() => {
  cleanup()
})

describe("ConnectCcpCard", () => {
  it("shows a sign-in prompt and the third-party cookie hint before the agent appears", () => {
    mockHook({ phase: "signing-in" })
    render(<ConnectCcpCard />)

    expect(screen.getByRole("button", { name: /sign in to connect/i })).toBeTruthy()
    expect(screen.getByText(/third-party cookies/i)).toBeTruthy()
  })

  it("shows the current agent state and a toggle once signed in", () => {
    mockHook({
      phase: "ready",
      agentStateName: "Available",
      agentStates: [
        { name: "Available", routable: true },
        { name: "Offline", routable: false },
      ],
    })
    render(<ConnectCcpCard />)

    expect(screen.getByText("Available")).toBeTruthy()
    expect(screen.getByRole("button", { name: /go offline/i })).toBeTruthy()
  })

  it("shows the caller number and Answer/Decline on an incoming call", () => {
    mockHook({ phase: "incoming", callerNumber: "+15551234567" })
    render(<ConnectCcpCard />)

    expect(screen.getByText(/Incoming call/i)).toBeTruthy()
    expect(screen.getByText("+15551234567")).toBeTruthy()
    expect(screen.getByRole("button", { name: /answer/i })).toBeTruthy()
    expect(screen.getByRole("button", { name: /decline/i })).toBeTruthy()
  })

  it("shows the call timer and Mute/Hang up while connected", () => {
    mockHook({ phase: "connected", callSeconds: 65 })
    render(<ConnectCcpCard />)

    expect(screen.getByText("1:05")).toBeTruthy()
    expect(screen.getByRole("button", { name: /mute/i })).toBeTruthy()
    expect(screen.getByRole("button", { name: /hang up/i })).toBeTruthy()
  })

  it("shows an inline error when the hook reports one", () => {
    mockHook({ phase: "ready", error: "Could not change state to Offline." })
    render(<ConnectCcpCard />)

    expect(screen.getByText("Could not change state to Offline.")).toBeTruthy()
  })

  it("owns no CCP container of its own -- that's ConnectSoftphoneProvider's job now, so it survives this card unmounting", () => {
    mockHook({ phase: "ready", agentStateName: "Available" })
    const { container } = render(<ConnectCcpCard />)

    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
  })

  it("re-renders from context state alone -- a remount after this card was unmounted (e.g. navigating away and back) picks up whatever the provider has by then", () => {
    mockHook({ phase: "signing-in" })
    const { rerender } = render(<ConnectCcpCard />)
    expect(screen.getByRole("button", { name: /sign in to connect/i })).toBeTruthy()

    // Simulate the provider having signed in and gone ringing while this card was
    // unmounted (another page), then the card remounting on navigating back.
    mockHook({ phase: "incoming", callerNumber: "+15551234567" })
    rerender(<ConnectCcpCard />)

    expect(screen.getByText(/Incoming call/i)).toBeTruthy()
    expect(screen.getByText("+15551234567")).toBeTruthy()
  })
})
