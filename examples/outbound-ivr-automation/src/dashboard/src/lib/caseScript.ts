// aws mode only: builds the Script panel's content from the case behind the current
// call, instead of the fixed demo script in frontDeskScript.ts. The case comes from
// `getCase` (dashboard/src/lib/api.ts), which carries each missing item's synthetic
// front-desk `answer` straight from scripts/seed_cases.py's cases table. See
// lib/useCaseScript.ts, which calls this once it has resolved that case, and
// components/ScriptSheet.tsx, which renders whatever it returns.
import type { CaseRecord } from "@/lib/events"
import {
  AFTER_INTRO_LINE,
  GOODBYE_LINE,
  PICK_UP_LINE,
  PUSHBACK_LINE,
  READBACK_LINE,
  type ScriptLine,
  type ScriptSection,
} from "@/lib/frontDeskScript"

const PATIENT_FIELDS: { key: keyof NonNullable<CaseRecord["case_summary"]>; label: string }[] = [
  { key: "patient_name", label: "Patient" },
  { key: "patient_dob", label: "Date of birth" },
  { key: "member_id", label: "Member ID" },
  { key: "pa_reference", label: "Authorization reference" },
  { key: "procedure", label: "Procedure" },
]

/** The patient-detail lines to confirm, one per field the case actually has. */
function patientLines(summary: CaseRecord["case_summary"]): ScriptLine[] {
  if (!summary) return []
  return PATIENT_FIELDS.filter(({ key }) => summary[key]).map(({ key, label }) => ({
    label,
    text: `"${summary[key]}"`,
  }))
}

/** One line per checklist item: the answer to read aloud if the case has one, else a
 * hint to make up a plausible answer on the spot (cases seeded without one, or an item
 * `scripts/seed_cases.py` didn't cover). */
function checklistLines(checklist: CaseRecord["checklist"]): ScriptLine[] {
  return (checklist ?? []).map((item) => ({
    label: `When Outbound IVR asks about ${item.label}`,
    text: item.answer
      ? `say "${item.answer}"`
      : "no answer on file -- make up a short, plausible answer and stay consistent with it.",
  }))
}

/** The case-driven Script panel content: same shape and look as `FRONT_DESK_SCRIPT`,
 * built from `record` instead of the fixed demo case. */
export function buildCaseScript(record: CaseRecord): ScriptSection[] {
  return [
    {
      title: "Front desk: you are Sam",
      lines: [
        PICK_UP_LINE,
        AFTER_INTRO_LINE,
        ...patientLines(record.case_summary),
        ...checklistLines(record.checklist),
        READBACK_LINE,
        GOODBYE_LINE,
      ],
    },
    {
      title: "To test escalation",
      lines: [PUSHBACK_LINE],
    },
  ]
}
