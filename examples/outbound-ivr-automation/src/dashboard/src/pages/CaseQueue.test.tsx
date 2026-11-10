// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { CaseQueue } from "@/pages/CaseQueue"
import { getCases, updateCase, type QueuedCase } from "@/lib/api"
import { startCallForCase } from "@/lib/startCallAction"

vi.mock("@/lib/api", () => ({ getCases: vi.fn(), updateCase: vi.fn() }))
vi.mock("@/lib/startCallAction", () => ({ startCallForCase: vi.fn() }))

function LocationCapture() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search}</div>
}

function renderQueue() {
  render(
    <MemoryRouter initialEntries={["/queue"]}>
      <Routes>
        <Route path="/queue" element={<CaseQueue />} />
        <Route path="/" element={<LocationCapture />} />
      </Routes>
    </MemoryRouter>,
  )
}

const CASE: QueuedCase = {
  case_id: "case-1",
  status: "pending",
  attempts: 0,
  outcome: null,
  updated_at: "2026-10-18T00:00:00Z",
  patient_name: "Jordan Rivera",
}

afterEach(() => {
  cleanup()
})

beforeEach(() => {
  vi.mocked(getCases).mockReset().mockResolvedValue([CASE])
  vi.mocked(startCallForCase).mockReset()
  vi.mocked(updateCase).mockReset()
})

describe("CaseQueue: Start call navigation", () => {
  it("navigates to the real call id the backend minted, not the case id", async () => {
    vi.mocked(startCallForCase).mockResolvedValue("real-call-uuid")

    renderQueue()
    const button = await screen.findByRole("button", { name: /start call/i })
    fireEvent.click(button)

    await waitFor(() => expect(startCallForCase).toHaveBeenCalledWith("case-1"))
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/?call=real-call-uuid"),
    )
  })

  it("does not navigate when the start attempt fails", async () => {
    vi.mocked(startCallForCase).mockResolvedValue(null)

    renderQueue()
    const button = await screen.findByRole("button", { name: /start call/i })
    fireEvent.click(button)

    await waitFor(() => expect(startCallForCase).toHaveBeenCalledWith("case-1"))
    // Still on the case queue page -- no navigation happened.
    expect(screen.queryByTestId("location")).toBeNull()
  })
})

describe("CaseQueue: table layout", () => {
  it("puts the Start call action in the first column of each row", async () => {
    renderQueue()
    const row = (await screen.findAllByRole("row")).find((r) =>
      within(r).queryByRole("button", { name: /start call/i }),
    )
    expect(row).toBeDefined()
    const cells = within(row!).getAllByRole("cell")
    expect(within(cells[0]).getByRole("button", { name: /start call/i })).toBeTruthy()
  })

  it("zebra-stripes rows with the theme's muted token", async () => {
    vi.mocked(getCases).mockResolvedValue([
      { ...CASE, case_id: "case-1" },
      { ...CASE, case_id: "case-2" },
    ])

    renderQueue()
    const rows = await screen.findAllByRole("row")
    const dataRows = rows.filter((r) => within(r).queryByRole("button", { name: /start call/i }))
    expect(dataRows).toHaveLength(2)
    for (const row of dataRows) {
      expect(row.className).toContain("even:bg-muted/40")
    }
  })

  it("shows a generic subtitle that doesn't hardcode the backing table name", async () => {
    renderQueue()
    await screen.findByRole("button", { name: /start call/i })
    expect(screen.queryByText(/outbound-ivr-cases/i)).toBeNull()
    expect(screen.getByText(/cases table/i)).toBeTruthy()
  })
})

describe("CaseQueue: Edit case dialog", () => {
  it("saves edited fields and status, then updates the row in place", async () => {
    const updated: QueuedCase = {
      ...CASE,
      status: "escalated",
      patient_name: "Jordan R. Rivera",
      updated_at: "2026-10-19T00:00:00Z",
    }
    vi.mocked(updateCase).mockResolvedValue(updated)

    renderQueue()
    const editButton = await screen.findByRole("button", { name: /edit case case-1/i })
    fireEvent.click(editButton)

    const dialog = await screen.findByRole("dialog")
    const statusSelect = within(dialog).getByLabelText(/status/i)
    fireEvent.change(statusSelect, { target: { value: "escalated" } })

    const nameInput = within(dialog).getByLabelText(/patient name/i)
    fireEvent.change(nameInput, { target: { value: "Jordan R. Rivera" } })

    fireEvent.click(within(dialog).getByRole("button", { name: /^save$/i }))

    await waitFor(() =>
      expect(updateCase).toHaveBeenCalledWith(
        "case-1",
        expect.objectContaining({
          fields: expect.objectContaining({ status: "escalated", patient_name: "Jordan R. Rivera" }),
        }),
      ),
    )

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    expect(await screen.findByText("Jordan R. Rivera")).toBeTruthy()
  })

  it("requires a second click to confirm reset, then resets the case to pending", async () => {
    const resetCase: QueuedCase = { ...CASE, status: "pending", attempts: 0, outcome: null }
    vi.mocked(updateCase).mockResolvedValue(resetCase)

    renderQueue()
    const editButton = await screen.findByRole("button", { name: /edit case case-1/i })
    fireEvent.click(editButton)

    const dialog = await screen.findByRole("dialog")
    const resetButton = within(dialog).getByRole("button", { name: /reset case/i })
    fireEvent.click(resetButton)

    // First click just asks for confirmation -- no request yet.
    expect(updateCase).not.toHaveBeenCalled()
    const confirmButton = within(dialog).getByRole("button", { name: /confirm reset/i })

    fireEvent.click(confirmButton)

    await waitFor(() => expect(updateCase).toHaveBeenCalledWith("case-1", { reset: true }))
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
  })

  it("shows the error message and keeps the dialog open when save fails", async () => {
    vi.mocked(updateCase).mockRejectedValue(new Error("case is on a live call"))

    renderQueue()
    const editButton = await screen.findByRole("button", { name: /edit case case-1/i })
    fireEvent.click(editButton)

    const dialog = await screen.findByRole("dialog")
    fireEvent.click(within(dialog).getByRole("button", { name: /^save$/i }))

    await waitFor(() => expect(updateCase).toHaveBeenCalled())
    expect(await within(dialog).findByText("case is on a live call")).toBeTruthy()
    expect(screen.getByRole("dialog")).toBeTruthy()
  })
})

describe("CaseQueue: a case on a live call", () => {
  // The Runtime marks a case "calling" once the office has been dialed.
  beforeEach(() => {
    vi.mocked(getCases).mockResolvedValue([{ ...CASE, status: "calling" }])
  })

  it("can't be dialed again", async () => {
    renderQueue()
    const button = await screen.findByRole("button", { name: /start call/i })
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it("locks the edit form but still allows Reset", async () => {
    renderQueue()
    fireEvent.click(await screen.findByRole("button", { name: /edit case case-1/i }))

    const dialog = await screen.findByRole("dialog")
    expect(within(dialog).getByText(/on a live call/i)).toBeTruthy()
    expect((within(dialog).getByLabelText(/status/i) as HTMLSelectElement).disabled).toBe(true)
    expect(
      (within(dialog).getByRole("button", { name: /^save$/i }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      (within(dialog).getByRole("button", { name: /reset case/i }) as HTMLButtonElement).disabled,
    ).toBe(false)
  })
})
