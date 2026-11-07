// What the person playing AnyCompany Medical Group's front desk (Sam) needs on hand
// during a call. The phone menu is already visible in the transcript as Outbound IVR navigates
// it, so this only covers the human side: Sam's lines, and the two paths that test
// escalation and internal transfer.

export type ScriptLine = {
  label: string
  text: string
}

export type ScriptSection = {
  title: string
  lines: ScriptLine[]
}

// Reusable lines, not specific to the demo case: dashboard/src/lib/caseScript.ts (aws
// mode's case-driven script) reuses these so every case's script opens and closes the
// same way the fixed demo script always has -- only the patient details and checklist
// answers in between change per case.
export const PICK_UP_LINE: ScriptLine = {
  label: "Pick up",
  text: '"AnyCompany Medical, this is Sam, how can I help?"',
}
export const AFTER_INTRO_LINE: ScriptLine = {
  label: "After Outbound IVR introduces itself",
  text: '"Sure, I can help with that."',
}
export const READBACK_LINE: ScriptLine = {
  label: "When Outbound IVR reads it back",
  text: '"Yes, that\'s right."',
}
export const GOODBYE_LINE: ScriptLine = { label: "Goodbye", text: '"You\'re welcome. Bye."' }
export const PUSHBACK_LINE: ScriptLine = {
  label: "Pushback",
  text: '"Sorry, I can\'t give out chart details over the phone. You\'ll need to send a written request to Maria in medical records."',
}
export const TRANSFER_LINE: ScriptLine = {
  label: "Transfer",
  text: '"Let me transfer you to the right department, please hold."',
}

export const FRONT_DESK_SCRIPT: ScriptSection[] = [
  {
    title: "Front desk: you are Sam",
    lines: [
      PICK_UP_LINE,
      AFTER_INTRO_LINE,
      {
        label: "Physical therapy",
        text: '"Jordan did eight weeks of physical therapy, twice a week, from June sixteenth to August eleventh."',
      },
      {
        label: "Progress note",
        text: '"The last visit was September twenty-second. Low back pain going down the left leg, with numbness in the left foot."',
      },
      { label: "Symptom duration", text: '"About four months. It started in early June."' },
      READBACK_LINE,
      GOODBYE_LINE,
    ],
  },
  {
    title: "To test escalation",
    lines: [PUSHBACK_LINE],
  },
  {
    title: "To test an internal transfer",
    lines: [TRANSFER_LINE],
  },
]
