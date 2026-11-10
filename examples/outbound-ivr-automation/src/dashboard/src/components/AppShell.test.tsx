// @vitest-environment jsdom
// Change (1): the "Live call" nav link carries ?call=<active id> while one is set (see
// lib/activeCall.ts), so clicking it after leaving the tab doesn't reset the page. Heavy
// siblings (CommandPalette, Toaster, SidebarUserFooter) are stubbed out -- none of them
// are what this is testing, and SidebarUserFooter would otherwise try real network calls
// (Cognito, config.json) under jsdom.
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { AppShell } from "@/components/AppShell"
import { resetActiveCallForTests, setActiveCall } from "@/lib/activeCall"

vi.mock("@/components/CommandPalette", () => ({ CommandPalette: () => null }))
vi.mock("@/components/Toaster", () => ({ Toaster: () => null }))
vi.mock("@/components/SidebarUserFooter", () => ({ SidebarUserFooter: () => null }))
const connectSoftphoneContextMock = vi.fn(() => ({ phase: "signing-in" }))
vi.mock("@/lib/connectSoftphoneContext", () => ({
  ConnectSoftphoneProvider: ({ children }: { children: ReactNode }) => children,
  useConnectSoftphoneContext: () => connectSoftphoneContextMock(),
}))

if (typeof window.matchMedia !== "function") {
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia
}
if (typeof window.ResizeObserver === "undefined") {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

function renderShell(initialEntry = "/") {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AppShell />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  resetActiveCallForTests()
  connectSoftphoneContextMock.mockReset().mockReturnValue({ phase: "signing-in" })
})

describe("AppShell: Live call nav link (aws mode)", () => {
  it("points at the bare route when no call is active", () => {
    renderShell()
    const link = screen.getByRole("link", { name: /live call/i })
    expect(link.getAttribute("href")).toBe("/")
  })

  it("carries ?call=<id> once a call is persisted as active", () => {
    setActiveCall("real-call-uuid")
    renderShell()
    const link = screen.getByRole("link", { name: /live call/i })
    expect(link.getAttribute("href")).toBe("/?call=real-call-uuid")
  })

  it("drops back to the bare route once the active call is cleared (End after-call work)", () => {
    setActiveCall("real-call-uuid")
    renderShell()
    act(() => setActiveCall(null))
    const link = screen.getByRole("link", { name: /live call/i })
    expect(link.getAttribute("href")).toBe("/")
  })
})

describe("AppShell: Connect ringing indicator (aws mode)", () => {
  it("shows a badge on the Live call nav link when a call is ringing while on another page", () => {
    connectSoftphoneContextMock.mockReturnValue({ phase: "incoming" })
    renderShell("/evidence")
    expect(screen.getByLabelText(/incoming call/i)).toBeTruthy()
  })

  it("shows no badge once back on the Live call page, even while ringing", () => {
    connectSoftphoneContextMock.mockReturnValue({ phase: "incoming" })
    renderShell("/")
    expect(screen.queryByLabelText(/incoming call/i)).toBeNull()
  })

  it("shows no badge while idle", () => {
    connectSoftphoneContextMock.mockReturnValue({ phase: "ready" })
    renderShell("/evidence")
    expect(screen.queryByLabelText(/incoming call/i)).toBeNull()
  })
})
