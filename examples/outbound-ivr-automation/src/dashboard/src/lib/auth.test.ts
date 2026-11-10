// @vitest-environment jsdom
// signOut has to revoke at Cognito, not just locally. `signoutRedirect` ends the
// Hosted UI session and clears local storage, but the refresh token it discards
// stays valid for its full 30 days, so a copy taken off a shared machine still
// mints access tokens for a month after someone signs out.
import { beforeEach, describe, expect, it, vi } from "vitest"

const revokeTokens = vi.fn()
const signoutRedirect = vi.fn()
const constructedWith: Record<string, unknown>[] = []

vi.mock("oidc-client-ts", () => ({
  UserManager: class {
    constructor(config: Record<string, unknown>) {
      constructedWith.push(config)
    }
    revokeTokens = revokeTokens
    signoutRedirect = signoutRedirect
  },
  WebStorageStateStore: class {},
}))

vi.mock("@/lib/config", () => ({
  loadConfig: async () => ({
    apiUrl: "https://api.example.com",
    cognitoDomain: "example.auth.us-east-1.amazoncognito.com",
    cognitoClientId: "client-id",
    cognitoRegion: "us-east-1",
    region: "us-east-1",
    ccpUrl: "",
    connectRegion: "us-east-1",
  }),
}))

describe("metadata", () => {
  it("declares a revocation endpoint, without which revokeTokens throws", async () => {
    // oidc-client-ts resolves this with its optional=false form and throws
    // "Metadata does not contain property revocation_endpoint" when it is absent,
    // which signOut()'s catch would swallow into a console warning while the
    // refresh token stayed valid at Cognito for its full 30 days. Cognito's own
    // discovery document omits it, which is why this metadata object is hand
    // built in the first place.
    constructedWith.length = 0
    const { getUserManager } = await import("@/lib/auth")
    await getUserManager()

    const metadata = constructedWith[0].metadata as Record<string, string>
    expect(metadata.revocation_endpoint).toBe(
      "https://example.auth.us-east-1.amazoncognito.com/oauth2/revoke",
    )
  })
})

describe("signOut", () => {
  beforeEach(() => {
    revokeTokens.mockReset().mockResolvedValue(undefined)
    signoutRedirect.mockReset().mockResolvedValue(undefined)
    vi.resetModules()
  })

  it("revokes the refresh token before redirecting", async () => {
    const { signOut } = await import("@/lib/auth")
    await signOut()

    expect(revokeTokens).toHaveBeenCalledWith(["refresh_token", "access_token"])
    expect(signoutRedirect).toHaveBeenCalled()
    expect(revokeTokens.mock.invocationCallOrder[0]).toBeLessThan(
      signoutRedirect.mock.invocationCallOrder[0],
    )
  })

  it("still signs the operator out when revocation fails", async () => {
    // Cognito errors if the app client has no revocation endpoint. Failing to
    // revoke must not leave someone stuck signed in.
    revokeTokens.mockRejectedValue(new Error("revocation not supported"))
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})

    const { signOut } = await import("@/lib/auth")
    await expect(signOut()).resolves.toBeUndefined()

    expect(signoutRedirect).toHaveBeenCalled()
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
