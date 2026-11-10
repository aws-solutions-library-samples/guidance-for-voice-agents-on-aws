// Persists the Live call page's current call id across navigation, so leaving this tab
// (Case queue, Evidence) and coming back shows the same call -- with its transcript and
// events -- instead of resetting to "waiting for a call" (the page only ever knew the
// call from `?call=` in the URL, which a plain nav click back to "/" drops). See
// pages/LiveCall.tsx (reads/writes this) and AppShell.tsx's "Live call" nav link (reads
// it to carry `?call=<id>` while one is set). A plain module-level store with
// sessionStorage as the backing, same subscribe pattern as lib/toast.ts, since the nav
// link and the page don't share a closer common ancestor than the router.
const KEY = "outbound_ivr.activeCall"

type Listener = () => void
const listeners = new Set<Listener>()

function notify(): void {
  listeners.forEach((fn) => fn())
}

/** The call id the Live call page is currently showing -- started, or ended and still in
 * after-call review -- or null once "End after-call work" (or local mode, which never
 * calls `setActiveCall`) clears it. */
export function getActiveCall(): string | null {
  try {
    return sessionStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function setActiveCall(call: string | null): void {
  try {
    if (call) sessionStorage.setItem(KEY, call)
    else sessionStorage.removeItem(KEY)
  } catch {
    // sessionStorage unavailable (private browsing, or a test's non-DOM environment) --
    // the nav link just won't persist across navigation, same as the bug this fixes.
  }
  notify()
}

export function subscribeActiveCall(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Where the "Live call" nav link should point: the active call's live-call route while
 * one is set, otherwise the bare route. See AppShell.tsx. */
export function liveCallNavTarget(activeCall: string | null): string {
  return activeCall ? `/?call=${encodeURIComponent(activeCall)}` : "/"
}

/** Test-only: reset the module-level store between tests, since both the sessionStorage
 * key and the listener set otherwise persist across every test in a run. */
export function resetActiveCallForTests(): void {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    // ignore
  }
  listeners.clear()
}
