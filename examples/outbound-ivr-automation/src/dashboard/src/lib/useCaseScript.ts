// aws mode only: resolves the current call's case and turns it into Script-panel
// content (lib/caseScript.ts). Local mode, no call yet, or any lookup failure (no
// matching case, a fetch error) all fall back to the fixed demo script in
// frontDeskScript.ts, unchanged from before this existed.
import { useEffect, useState } from "react"
import { isAws } from "@/lib/backend"
import { getCase, getCases } from "@/lib/api"
import type { CaseSummary } from "@/lib/events"
import { buildCaseScript } from "@/lib/caseScript"
import { FRONT_DESK_SCRIPT, type ScriptSection } from "@/lib/frontDeskScript"

/** `getCases` returns every case's own `auth_ref`, which the live call's own
 * `caseSummary.auth_ref` (set from the `ready` event) can be matched against to find
 * that case's `case_id` -- the id `getCase` needs, and the one piece of this round
 * trip nothing else on the Live call page already carries. */
async function findCaseIdByAuthRef(authRef: string): Promise<string | null> {
  const cases = await getCases()
  return cases.find((c) => c.auth_ref === authRef)?.case_id ?? null
}

export function useCaseScript(caseSummary: CaseSummary | undefined): ScriptSection[] {
  const authRef = isAws ? caseSummary?.auth_ref : undefined
  const [sections, setSections] = useState<ScriptSection[]>(FRONT_DESK_SCRIPT)

  useEffect(() => {
    if (!authRef) {
      setSections(FRONT_DESK_SCRIPT)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const caseId = await findCaseIdByAuthRef(authRef)
        if (!caseId) throw new Error("no case matches this call's auth_ref")
        const record = await getCase(caseId)
        if (!cancelled) setSections(buildCaseScript(record))
      } catch {
        if (!cancelled) setSections(FRONT_DESK_SCRIPT)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [authRef])

  return sections
}
