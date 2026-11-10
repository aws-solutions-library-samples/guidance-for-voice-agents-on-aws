import { describe, expect, it } from "vitest"
import { FRONT_DESK_SCRIPT } from "@/lib/frontDeskScript"

describe("FRONT_DESK_SCRIPT", () => {
  it("has at least one section with at least one line", () => {
    expect(FRONT_DESK_SCRIPT.length).toBeGreaterThan(0)
    for (const section of FRONT_DESK_SCRIPT) {
      expect(section.title.length).toBeGreaterThan(0)
      expect(section.lines.length).toBeGreaterThan(0)
      for (const line of section.lines) {
        expect(line.label.length).toBeGreaterThan(0)
        expect(line.text.length).toBeGreaterThan(0)
      }
    }
  })

  it("covers the front desk pickup, the three evidence items, and the goodbye", () => {
    const [frontDesk] = FRONT_DESK_SCRIPT
    const labels = frontDesk.lines.map((l) => l.label)
    expect(labels).toContain("Pick up")
    expect(labels).toContain("Physical therapy")
    expect(labels).toContain("Progress note")
    expect(labels).toContain("Symptom duration")
    expect(labels).toContain("Goodbye")
  })

  it("covers the escalation pushback and an internal transfer", () => {
    const allText = FRONT_DESK_SCRIPT.flatMap((s) => s.lines.map((l) => l.text)).join(" ")
    expect(allText).toMatch(/can't give out chart details/i)
    expect(allText).toMatch(/transfer you to the right department/i)
  })
})
