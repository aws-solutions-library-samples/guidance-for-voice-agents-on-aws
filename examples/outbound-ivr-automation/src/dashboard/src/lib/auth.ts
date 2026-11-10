// Cognito Hosted UI sign-in, authorization code flow with PKCE, via oidc-client-ts (the
// lightest well-maintained OIDC client — not the full Amplify UI). `aws` mode only.
import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts"
import { loadConfig, type RuntimeConfig } from "@/lib/config"

let managerPromise: Promise<UserManager> | null = null

function redirectUri(): string {
  return `${window.location.origin}/auth/callback`
}

async function buildManager(config: RuntimeConfig): Promise<UserManager> {
  return new UserManager({
    authority: `https://${config.cognitoDomain}`,
    client_id: config.cognitoClientId,
    redirect_uri: redirectUri(),
    post_logout_redirect_uri: window.location.origin,
    response_type: "code",
    scope: "openid email profile",
    userStore: new WebStorageStateStore({ store: window.localStorage }),
    automaticSilentRenew: true,
    // Cognito's OIDC discovery document omits a few endpoints oidc-client-ts expects;
    // the Hosted UI's own paths cover what's needed for the code+PKCE flow.
    metadata: {
      issuer: `https://${config.cognitoDomain}`,
      authorization_endpoint: `https://${config.cognitoDomain}/oauth2/authorize`,
      token_endpoint: `https://${config.cognitoDomain}/oauth2/token`,
      userinfo_endpoint: `https://${config.cognitoDomain}/oauth2/userInfo`,
      end_session_endpoint: `https://${config.cognitoDomain}/logout`,
      // Required for signOut()'s revokeTokens() to do anything: oidc-client-ts
      // resolves this with its optional=false form and throws
      // "Metadata does not contain property revocation_endpoint" when it is
      // missing, which signOut() would then swallow into a console warning while
      // the refresh token stayed valid at Cognito for its full 30 days.
      revocation_endpoint: `https://${config.cognitoDomain}/oauth2/revoke`,
      jwks_uri: `https://cognito-idp.${config.cognitoRegion}.amazonaws.com/.well-known/jwks.json`,
    },
  })
}

export async function getUserManager(): Promise<UserManager> {
  if (!managerPromise) {
    managerPromise = loadConfig().then(buildManager)
  }
  return managerPromise
}

export async function getUser(): Promise<User | null> {
  const mgr = await getUserManager()
  return mgr.getUser()
}

export async function getAccessToken(): Promise<string | null> {
  const user = await getUser()
  return user?.access_token ?? null
}

export type UserProfile = { name: string; email: string }

/** Name and email from the Cognito user's ID token, for the sidebar footer. */
export async function getUserProfile(): Promise<UserProfile | null> {
  const user = await getUser()
  if (!user) return null
  const { name, email, "cognito:username": username } = user.profile as Record<string, string | undefined>
  return { name: name ?? username ?? email ?? "Signed in", email: email ?? "" }
}

export async function signIn(): Promise<void> {
  const mgr = await getUserManager()
  await mgr.signinRedirect()
}

export async function signOut(): Promise<void> {
  const mgr = await getUserManager()
  // Revoke before redirecting. `signoutRedirect` alone ends the Hosted UI session
  // and clears local storage, but the refresh token it discards stays valid at
  // Cognito for its full 30 days — so a copy taken off a shared machine still
  // mints access tokens long after someone thinks they signed out. Best effort:
  // Cognito returns an error if the client has no revocation endpoint configured,
  // and failing to revoke must not leave the operator stuck signed in.
  try {
    await mgr.revokeTokens(["refresh_token", "access_token"])
  } catch (err) {
    console.warn("token revocation failed; signing out locally anyway", err)
  }
  await mgr.signoutRedirect()
}

/** Call once on `/auth/callback` after the Hosted UI redirects back. */
export async function completeSignIn(): Promise<User> {
  const mgr = await getUserManager()
  return mgr.signinRedirectCallback()
}
