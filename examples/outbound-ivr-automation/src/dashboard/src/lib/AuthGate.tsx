// Wraps the SPA in `aws` mode: redirects to the Cognito Hosted UI if there's no signed-in
// user, and renders nothing else until one exists. `local` mode never touches this.
import { useEffect, useState, type ReactNode } from "react"
import type { User } from "oidc-client-ts"
import { getUser, signIn } from "@/lib/auth"

export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null | "loading">("loading")

  useEffect(() => {
    let cancelled = false
    getUser()
      .then((u) => {
        if (cancelled) return
        if (u && !u.expired) {
          setUser(u)
        } else {
          setUser(null)
          void signIn()
        }
      })
      .catch(() => {
        if (!cancelled) setUser(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (user === "loading" || user === null) {
    return (
      <div className="grid h-screen place-items-center text-sm text-muted-foreground">
        Signing in…
      </div>
    )
  }
  return <>{children}</>
}
