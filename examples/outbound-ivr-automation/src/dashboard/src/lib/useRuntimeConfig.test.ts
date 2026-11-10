// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest"
import { renderHook, waitFor } from "@testing-library/react"
import { useRuntimeConfig } from "@/lib/useRuntimeConfig"

const CONFIG = {
  apiUrl: "https://api.example.com",
  cognitoDomain: "d",
  cognitoClientId: "c",
  cognitoRegion: "us-east-1",
  region: "us-east-1",
  ccpUrl: "https://example.my.connect.aws/ccp-v2/",
  connectRegion: "us-east-1",
}

const loadConfig = vi.fn()
vi.mock("@/lib/config", () => ({ loadConfig: (...args: unknown[]) => loadConfig(...args) }))

beforeEach(() => {
  loadConfig.mockReset()
})

describe("useRuntimeConfig", () => {
  it("loads and returns the deploy-time config", async () => {
    loadConfig.mockResolvedValue(CONFIG)

    const { result } = renderHook(() => useRuntimeConfig())
    expect(result.current).toBeNull()

    await waitFor(() => expect(result.current).toEqual(CONFIG))
  })

  it("resolves to null if the config fetch fails", async () => {
    loadConfig.mockRejectedValue(new Error("no config.json"))

    const { result } = renderHook(() => useRuntimeConfig())
    await waitFor(() => expect(loadConfig).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })
})
