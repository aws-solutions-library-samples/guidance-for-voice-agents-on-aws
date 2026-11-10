import { beforeEach, describe, expect, it, vi } from "vitest"
import { startCall } from "@/lib/api"
import { showToast } from "@/lib/toast"
import { startCallForCase } from "@/lib/startCallAction"

vi.mock("@/lib/api", () => ({ startCall: vi.fn() }))
vi.mock("@/lib/toast", () => ({ showToast: vi.fn() }))

beforeEach(() => {
  vi.mocked(startCall).mockReset()
  vi.mocked(showToast).mockReset()
})

describe("startCallForCase (aws mode)", () => {
  it("returns the real call id the backend minted, not the case id", async () => {
    vi.mocked(startCall).mockResolvedValue({ call: "real-call-uuid" })

    const result = await startCallForCase("case-1")

    expect(result).toBe("real-call-uuid")
    expect(startCall).toHaveBeenCalledWith("case-1")
    expect(showToast).toHaveBeenCalledWith("Call started — real-call-uuid", "ok")
  })

  it("returns null and shows the error toast when the start attempt fails", async () => {
    vi.mocked(startCall).mockRejectedValue(new Error("this case is already calling"))

    const result = await startCallForCase("case-1")

    expect(result).toBeNull()
    expect(showToast).toHaveBeenCalledWith("this case is already calling", "err")
  })
})
