// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { CommandPalette } from "@/components/CommandPalette"
import { ThemeProvider } from "@/lib/theme"

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
// cmdk scrolls the highlighted item into view; jsdom doesn't implement it.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = vi.fn()
}

function renderPalette() {
  render(
    <MemoryRouter>
      <ThemeProvider>
        <CommandPalette />
      </ThemeProvider>
    </MemoryRouter>,
  )
  fireEvent.keyDown(document, { key: "k", metaKey: true })
}

afterEach(() => {
  cleanup()
})

describe("CommandPalette", () => {
  it("has no 'Start call' command -- there is no selected-case context here", async () => {
    renderPalette()
    await screen.findByText(/live call/i)
    expect(screen.queryByText(/start call/i)).toBeNull()
  })

  it("offers Case queue -- the only place a call can actually be started", async () => {
    renderPalette()
    expect(await screen.findByText(/case queue/i)).toBeTruthy()
  })
})
