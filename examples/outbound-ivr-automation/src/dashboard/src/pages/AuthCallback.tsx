// Target of the Cognito Hosted UI's redirect_uri (/auth/callback), aws mode only.
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { completeSignIn } from "@/lib/auth"

export function AuthCallback() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    completeSignIn()
      .then(() => navigate("/", { replace: true }))
      .catch((err) => setError(err instanceof Error ? err.message : "Sign-in failed."))
  }, [navigate])

  return (
    <div className="grid h-screen place-items-center text-sm text-muted-foreground">
      {error ? <span className="text-destructive">{error}</span> : "Finishing sign-in…"}
    </div>
  )
}
