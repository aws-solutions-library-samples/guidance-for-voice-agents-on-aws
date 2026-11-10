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

/** `getCases` returns every case's own `pa_reference`, which the live call's own
 * `caseSummary.pa_reference` (set from the `ready` event) can be matched against to find
 * that case's `case_id` -- the id `getCase` needs, and the one piece of this round
 * trip nothing else on the Live call page already carries. */
async function findCaseIdByPaReference(paReference: string): Promise<string | null> {
  const cases = await getCases()
  return cases.find((c) => c.pa_reference === paReference)?.case_id ?? null
}

export function useCaseScript(caseSummary: CaseSummary | undefined): ScriptSection[] {
  const paReference = isAws ? caseSummary?.pa_reference : undefined
  const [sections, setSections] = useState<ScriptSection[]>(FRONT_DESK_SCRIPT)

  useEffect(() => {
    if (!paReference) {
      setSections(FRONT_DESK_SCRIPT)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const caseId = await findCaseIdByPaReference(paReference)
        if (!caseId) throw new Error("no case matches this call's pa_reference")
        const record = await getCase(caseId)
        if (!cancelled) setSections(buildCaseScript(record))
      } catch {
        if (!cancelled) setSections(FRONT_DESK_SCRIPT)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [paReference])

  return sections
}
