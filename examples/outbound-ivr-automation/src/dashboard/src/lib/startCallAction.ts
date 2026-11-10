import { startCall } from "@/lib/api"
import { showToast } from "@/lib/toast"

/** Start a call for a case picked from the case queue page. Returns the real call id the
 * backend minted (so the caller can navigate to that call's live-call view), or null if
 * the start attempt failed. */
export async function startCallForCase(caseId: string): Promise<string | null> {
  try {
    const { call } = await startCall(caseId)
    showToast(`Call started — ${call}`, "ok")
    return call
  } catch (err) {
    showToast(err instanceof Error ? err.message : "Could not start the call.", "err")
    return null
  }
}
