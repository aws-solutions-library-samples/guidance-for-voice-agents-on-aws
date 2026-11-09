// @vitest-environment jsdom
// Covers the case-driven script (aws mode) and its fallback, with lib/useCaseScript.ts
// stubbed directly -- that hook's own fetch/fallback logic has its own tests
// (lib/useCaseScript.test.ts); this only checks ScriptSheet renders whatever it returns.
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { ScriptSheet } from "@/components/ScriptSheet"
import { FRONT_DESK_SCRIPT } from "@/lib/frontDeskScript"

const useCaseScriptMock = vi.fn()
vi.mock("@/lib/useCaseScript", () => ({ useCaseScript: (...args: unknown[]) => useCaseScriptMock(...args) }))

afterEach(() => cleanup())

describe("ScriptSheet (case-driven script)", () => {
  it("renders the case-driven script useCaseScript returns", () => {
    useCaseScriptMock.mockReturnValue([
      {
        title: "Front desk: you are Sam",
        lines: [
          { label: "Patient", text: '"Morgan Reyes"' },
          { label: "When Outbound IVR asks about Date of injury", text: 'say "Twisted it playing soccer."' },
        ],
      },
    ])

    render(<ScriptSheet caseSummary={{ pa_reference: "PA-90112" }} />)
    fireEvent.click(screen.getByRole("button", { name: /script/i }))

    expect(screen.getByText('"Morgan Reyes"')).toBeTruthy()
    expect(screen.getByText(/Date of injury/)).toBeTruthy()
    expect(useCaseScriptMock).toHaveBeenCalledWith({ pa_reference: "PA-90112" })
  })

  it("renders the fixed demo script when useCaseScript falls back to it", () => {
    useCaseScriptMock.mockReturnValue(FRONT_DESK_SCRIPT)

    render(<ScriptSheet caseSummary={{ pa_reference: "PA-90112" }} />)
    fireEvent.click(screen.getByRole("button", { name: /script/i }))

    expect(screen.getByText(/AnyCompany Medical, this is Sam/)).toBeTruthy()
  })
})
