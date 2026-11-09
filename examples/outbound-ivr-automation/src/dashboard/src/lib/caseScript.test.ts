import { describe, expect, it } from "vitest"
import { buildCaseScript } from "@/lib/caseScript"
import type { CaseRecord } from "@/lib/events"

function makeRecord(overrides: Partial<CaseRecord> = {}): CaseRecord {
  return {
    id: "case-1",
    case_summary: {
      patient_name: "Morgan Reyes",
      patient_dob: "July 2nd, 1985",
      member_id: "LHP-5521-0193",
      pa_reference: "PA-90112",
      procedure: "knee MRI without contrast, CPT 73721",
    },
    checklist: [
      { key: "injury_date", label: "Date of injury", answer: "Morgan twisted the knee playing soccer." },
      { key: "exam_findings", label: "Exam findings" },
    ],
    ...overrides,
  }
}

describe("buildCaseScript", () => {
  it("opens and closes with the same lines as the fixed demo script", () => {
    const sections = buildCaseScript(makeRecord())
    const main = sections[0]
    expect(main.lines[0].text).toContain("AnyCompany Medical, this is Sam")
    expect(main.lines.at(-1)?.text).toContain("You're welcome. Bye.")
  })

  it("confirms every patient field the case has", () => {
    const sections = buildCaseScript(makeRecord())
    const labels = sections[0].lines.map((l) => l.label)
    expect(labels).toContain("Patient")
    expect(labels).toContain("Date of birth")
    expect(labels).toContain("Member ID")
    expect(labels).toContain("Authorization reference")
    expect(labels).toContain("Procedure")
    const patientLine = sections[0].lines.find((l) => l.label === "Patient")
    expect(patientLine?.text).toBe('"Morgan Reyes"')
  })

  it("omits patient fields the case doesn't have", () => {
    const sections = buildCaseScript(makeRecord({ case_summary: { patient_name: "Morgan Reyes" } }))
    const labels = sections[0].lines.map((l) => l.label)
    expect(labels).not.toContain("Member ID")
  })

  it("gives a line per checklist item, with the seeded answer when there is one", () => {
    const sections = buildCaseScript(makeRecord())
    const line = sections[0].lines.find((l) => l.label === "When Outbound IVR asks about Date of injury")
    expect(line?.text).toBe('say "Morgan twisted the knee playing soccer."')
  })

  it("hints at making up a plausible answer when the case has none", () => {
    const sections = buildCaseScript(makeRecord())
    const line = sections[0].lines.find((l) => l.label === "When Outbound IVR asks about Exam findings")
    expect(line?.text).toMatch(/make up a short, plausible answer/i)
  })

  it("keeps the pushback section for testing escalation", () => {
    const sections = buildCaseScript(makeRecord())
    const pushback = sections.find((s) => s.title === "To test escalation")
    expect(pushback?.lines[0].text).toMatch(/can't give out chart details/i)
  })

  it("handles a case with no checklist and no case_summary", () => {
    const sections = buildCaseScript({ id: "case-1" })
    expect(sections[0].lines.length).toBeGreaterThan(0)
  })
})
