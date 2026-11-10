// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { LoadingRows } from "@/components/LoadingRows"

afterEach(cleanup)

describe("LoadingRows", () => {
  it("shows a labelled busy placeholder with the requested rows", () => {
    const { container } = render(<LoadingRows label="Loading cases…" rows={3} />)
    expect(screen.getByRole("status").getAttribute("aria-busy")).toBe("true")
    expect(screen.getByText("Loading cases…")).toBeTruthy()
    expect(container.querySelectorAll('[data-slot="skeleton"], .animate-pulse').length).toBe(3)
  })
})
