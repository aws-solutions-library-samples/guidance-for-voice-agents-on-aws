// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, renderHook, waitFor } from "@testing-library/react"

let awsFlag = true
vi.mock("@/lib/backend", () => ({ get isAws() { return awsFlag } }))

const getCasesMock = vi.fn()
const getCaseMock = vi.fn()
vi.mock("@/lib/api", () => ({
  getCases: (...args: unknown[]) => getCasesMock(...args),
  getCase: (...args: unknown[]) => getCaseMock(...args),
}))

import { useCaseScript } from "@/lib/useCaseScript"
import { FRONT_DESK_SCRIPT } from "@/lib/frontDeskScript"

beforeEach(() => {
  awsFlag = true
  getCasesMock.mockReset()
  getCaseMock.mockReset()
})

afterEach(() => cleanup())

describe("useCaseScript", () => {
  it("falls back to the fixed demo script when there is no case summary yet", () => {
    const { result } = renderHook(() => useCaseScript(undefined))
    expect(result.current).toBe(FRONT_DESK_SCRIPT)
  })

  it("falls back to the fixed demo script in local mode even with a case summary", () => {
    awsFlag = false
    const { result } = renderHook(() => useCaseScript({ pa_reference: "PA-90112" }))
    expect(result.current).toBe(FRONT_DESK_SCRIPT)
    expect(getCasesMock).not.toHaveBeenCalled()
  })

  it("resolves the case by pa_reference and builds its script", async () => {
    getCasesMock.mockResolvedValue([
      { case_id: "case-90112", status: "pending", attempts: 0, outcome: null, updated_at: "", pa_reference: "PA-90112" },
    ])
    getCaseMock.mockResolvedValue({
      id: "case-90112",
      case_summary: { patient_name: "Morgan Reyes" },
      checklist: [{ key: "injury_date", label: "Date of injury", answer: "Twisted it playing soccer." }],
    })

    const { result } = renderHook(() => useCaseScript({ pa_reference: "PA-90112" }))

    await waitFor(() => expect(result.current).not.toBe(FRONT_DESK_SCRIPT))

    expect(getCaseMock).toHaveBeenCalledWith("case-90112")
    const line = result.current[0].lines.find((l) => l.label === "Patient")
    expect(line?.text).toBe('"Morgan Reyes"')
  })

  it("falls back to the fixed demo script when no case matches the pa_reference", async () => {
    getCasesMock.mockResolvedValue([{ case_id: "other", status: "pending", attempts: 0, outcome: null, updated_at: "", pa_reference: "PA-OTHER" }])

    const { result } = renderHook(() => useCaseScript({ pa_reference: "PA-90112" }))

    await waitFor(() => expect(getCasesMock).toHaveBeenCalled())
    expect(result.current).toBe(FRONT_DESK_SCRIPT)
    expect(getCaseMock).not.toHaveBeenCalled()
  })

  it("falls back to the fixed demo script when the fetch fails", async () => {
    getCasesMock.mockRejectedValue(new Error("network error"))

    const { result } = renderHook(() => useCaseScript({ pa_reference: "PA-90112" }))

    await waitFor(() => expect(getCasesMock).toHaveBeenCalled())
    expect(result.current).toBe(FRONT_DESK_SCRIPT)
  })
})
