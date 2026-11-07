// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { ScriptSheet } from "@/components/ScriptSheet"

afterEach(() => cleanup())

describe("ScriptSheet", () => {
  it("is closed until the Script button is clicked, then opens the panel", () => {
    render(<ScriptSheet />)

    expect(screen.queryByText("Front desk script")).toBeNull()

    fireEvent.click(screen.getByRole("button", { name: /script/i }))

    expect(screen.getByText("Front desk script")).toBeTruthy()
    expect(screen.getByText(/Pick up/i)).toBeTruthy()
  })

  it("closes when the close control is used", () => {
    render(<ScriptSheet />)

    fireEvent.click(screen.getByRole("button", { name: /script/i }))
    expect(screen.getByText("Front desk script")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: /close/i }))

    expect(screen.queryByText("Front desk script")).toBeNull()
  })

  it("shows the fixed demo script's content with no case summary (local mode, or no call yet)", () => {
    render(<ScriptSheet />)
    fireEvent.click(screen.getByRole("button", { name: /script/i }))
    expect(screen.getByText(/AnyCompany Medical, this is Sam/)).toBeTruthy()
  })
})
