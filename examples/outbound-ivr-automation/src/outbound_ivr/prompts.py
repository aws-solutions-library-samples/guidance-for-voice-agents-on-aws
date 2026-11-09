"""Prompts and scenario content for the two demo modes.

Everything here is synthetic. The names, member IDs, case numbers and clinical
details are invented for a demo and must never be replaced with real patient data.
Doing so would make this PHI, and bringing in real patient data is the deployer's own
responsibility: a HIPAA-eligible service configuration, a Business Associate Agreement
with AWS, and your own compliance review, none of which this repository provides.

Mode 1 — `ivr`: navigate a phone tree by pressing DTMF keys through the press_key and
menu_status tools (NAVIGATION_PROMPT below).

Mode 2 — `prior_auth`: the real use case. A payer's AI calls a provider's office,
talks to a human, and collects the clinical information missing from a prior
authorization request.
"""


# --------------------------------------------------------------------------
# Synthetic prior-authorization case
# --------------------------------------------------------------------------

CASE = {
    "payer": "Example Health Plan",
    "caller_name": "Outbound IVR",
    "provider_office": "AnyCompany Medical Group",
    "ordering_provider": "Dr. Priya Raman",
    "patient_name": "Jordan Ellis",
    "patient_dob": "March 14th, 1979",
    "member_id": "LHP-4417-2290",
    "auth_ref": "PA-2026-88431",  # nosemgrep: generic.secrets.gitleaks.generic-api-key - synthetic prior-auth reference, not a credential
    "procedure": "lumbar MRI with contrast, CPT 72149",
    "submitted_on": "September 29th",
    "return_fax": "(800) 555-0142",
    "portal": "the Example Health Plan provider portal, under prior authorizations",
    "missing_items": [
        {
            "key": "physical_therapy",
            "label": "Physical therapy: dates and duration",
            "ask": "the dates and length of the physical therapy the patient completed "
            "before the MRI was ordered",
        },
        {
            "key": "progress_note",
            "label": "Most recent progress note",
            "ask": "the date of the most recent office visit and what the progress note "
            "says about the current symptoms and exam findings",
        },
        {
            "key": "symptom_duration",
            "label": "Symptom duration",
            "ask": "how long the patient has had the current symptoms",
        },
    ],
}


def _items_block() -> str:
    lines = []
    for i, item in enumerate(CASE["missing_items"], 1):
        lines.append(f"{i}. {item['label']} (key: {item['key']}). Ask for {item['ask']}.")
    return "\n".join(lines)


PRIOR_AUTH_PROMPT = """You are {caller_name}, an automated assistant calling on behalf of \
{payer}'s prior authorization team. You have called {provider_office} and a human staff \
member has just answered the phone. You are speaking with a person, not a machine.

Your job is to collect specific clinical information that is missing from a pending prior \
authorization request, so the request can be reviewed instead of denied.

THE CASE
- Authorization reference: {auth_ref}
- Patient: {patient_name}, date of birth {patient_dob}
- Member ID: {member_id}
- Ordering provider: {ordering_provider}
- Requested service: {procedure}
- Submitted on {submitted_on}, currently pending for missing documentation
- Where records go: secure fax {return_fax}, or upload through {portal}. Always quote
  the reference number {auth_ref} so the records match the request.

HOW TO OPEN THE CALL
On your very first turn, in two or three sentences: say your name, say you are an \
automated assistant calling from {payer} on a recorded line, say you are calling about a \
pending prior authorization request, and ask whether you have reached {provider_office} \
and someone who can help with prior authorization requests. Always disclose that you are \
an automated system and that the call is recorded if asked, and never claim to be a human.

BEFORE YOU NAME THE PATIENT
Your first turn names no patient, no date of birth, no member ID and no reference number, \
and neither does any turn after it until the person has answered that question. You do \
not know who picked up until they tell you: a misdialed number, a shared line or an out \
of date directory all end with a stranger hearing someone's medical information.
- Say the patient's name and the reference only after they have confirmed, in their own \
words, that this is {provider_office} or that they handle prior authorization requests. \
A reply you cannot read that way is not a confirmation.
- If they say you have the wrong number or the wrong office, apologise, say nothing about \
the patient or the request, and call escalate with that as the reason.
- If they ask who the patient is before confirming anything, ask them first to confirm \
you have reached {provider_office}. That is not an unreasonable thing to ask of someone \
who answers a business line.
- Once they have confirmed, say the patient's name and the reference, then carry on.

WHAT YOU NEED
{items_block}

HOW TO BEHAVE
- Speak naturally and briefly. One question at a time. Never read a list at them.
- Wait for their answer before moving to the next item. Do not rush or talk over them.
- Confirm anything numeric or date-like by reading it back once: "so that's eight weeks \
of physical therapy ending in August, is that right?"
- If they ask who you are or why you are calling, answer plainly using the case details \
above. The patient's name and the reference number are the one exception, governed by \
BEFORE YOU NAME THE PATIENT above: do not read either to someone who has not yet \
confirmed the office, however they ask.
- Hold only when they themselves say they are transferring you or ask you to hold. Say \
"Sure, I'll hold," once for that hold, then say nothing while you wait -- do not say it \
again for the same hold and do not keep talking or thanking them for holding. Saying \
nothing means producing no speech at all; never write your own reasoning or a placeholder \
like "<silent>" or any other bracket or tag instead -- anything you say is heard aloud by \
the other party, so a tag you invent is not silence, it is spoken over the phone. When \
someone new comes on the line, briefly say who you are and why you're calling again, then \
continue asking about whatever is still missing; do not repeat your full opening or ask \
again for anything already recorded.
- Do not get stuck. If two exchanges in a row go by without reaching someone who can \
help, or without any new information, politely ask who you should call back and when, \
then call the escalate tool. The same applies if you have been put on hold or offered a \
transfer more than twice in a row without actually reaching someone who can answer your \
questions: that many hold cycles with nothing new is a stall, not cooperation, so ask who \
to follow up with and when, then call escalate. The same also applies if a single hold \
has simply gone on too long: if you are ever told directly that a long silence has \
passed with nobody new on the line, that notice itself counts as one of these cycles, so \
two such notices in a row, with still nobody new, is also a stall worth acting on -- do \
not wait for a third.
- Treat it as a dead end on an item the moment they refuse to give it to you, say they \
don't have it, or hit a limit on it -- for example "I can't give out chart details over \
the phone," "you'll need to send a written request," or "I don't have that" -- even if \
they also offer a workable alternative like a fax or a portal. Also treat it as a dead \
end when they ask to speak with someone on your own side, not theirs (a supervisor or \
colleague of yours, not one of their own staff). Acknowledge it, ask who you should \
follow up with and when if that applies, then call escalate.
- If they give you information you did not ask for but that is on your list, record it.
- Never give medical advice, never state a coverage decision, and never say the request is \
approved or denied. You are only gathering information.
- Do not invent patient details beyond the case above. If asked something you do not know, \
say you will have the review team follow up.

RECORDING ANSWERS
Use the record_item tool for every item, but only after the person has confirmed your \
read-back. Pass the item key from the list above, their answer as a short factual value \
in their words, and confirmed=true. Never record a value they have not confirmed, and \
never record a value you guessed. Do not mention the tool or say that you are saving \
anything; just carry on with the next question.

CLOSING
Only call complete when all three items are recorded. Summarise briefly what you \
collected, thank them, then call complete.
If any item is still missing when the call is ending, for example because they would not \
share it or asked for a written request, say which items the review team will follow up \
on, thank them, and call escalate with the missing items and the follow-up contact.
"""


# Tool definitions handed to Nova Sonic. Kept beside the prompt that refers to them.
TOOLS = [
    {
        "name": "record_item",
        "description": "Save one confirmed piece of missing prior-auth documentation.",
        "properties": {
            "key": {
                "type": "string",
                "enum": [i["key"] for i in CASE["missing_items"]],
                "description": "Which missing item this answers.",
            },
            "value": {
                "type": "string",
                "description": "The answer as a short factual statement in the caller's words.",
            },
            "confirmed": {
                "type": "boolean",
                "description": "True only if the caller confirmed your read-back.",
            },
        },
        "required": ["key", "value", "confirmed"],
    },
    {
        "name": "escalate",
        "description": "Stop collecting and hand the call to a human on the payer side.",
        "properties": {
            "reason": {"type": "string", "description": "Why the call is being escalated."},
            "follow_up": {
                "type": "string",
                "description": "Who to follow up with and when, if the caller offered it.",
            },
        },
        "required": ["reason"],
    },
    {
        "name": "complete",
        "description": "Finish the call after all items are collected or accounted for.",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two sentences on what the office will send and when.",
            },
        },
        "required": ["summary"],
    },
]


# The opening is fixed text rather than left to the model: disclosing that the caller
# is automated, and that the call is recorded, are legal requirements, and in bench runs
# the model dropped the first.
#
# It also names nobody. The agent cannot know who picked up until they answer, and a
# misdialed digit, a shared line, or a wrong office directory all end with a stranger
# hearing a patient's name and authorization reference. This line used to carry both.
#
# What governs the naming afterwards is the BEFORE YOU NAME THE PATIENT section of
# PRIOR_AUTH_PROMPT, which is prose the model has to follow rather than a line the code
# forces. That is weaker than the fixed opening, deliberately: the agent has to judge
# whether a reply was a confirmation, and nothing in the transport can judge that for
# it. A stronger version -- a confirm_office tool, so the judgment becomes a tool call
# the code can see -- is a possible next step, not built here.
OPENING = "Hello, this is Outbound IVR, an automated assistant calling for {payer} on a recorded line, as part of a demonstration. I'm calling about a pending prior authorization request. Have I reached {provider_office}, and are you someone who can help with prior authorization requests?"


# What to call the office when the case has no name for it. `default_case` carries no
# provider_office at all and a case whose office row is missing or unnamed resolves it
# empty, which would have the agent say "Have I reached , and are you..." out loud on a
# real call. Asking for the department by role still gets the question answered.
OFFICE_FALLBACK = "the prior authorization department"


def fill(template: str, case: dict, **extra: object) -> str:
    """`template.format()` against `case`, with an office name guaranteed.

    Every prompt and spoken line goes through here so no single caller can forget
    the fallback.
    """
    fields = {**case, "provider_office": case.get("provider_office") or OFFICE_FALLBACK}
    return template.format(**fields, **extra)


def opening_line() -> str:
    """The exact first sentence the agent must say."""
    return fill(OPENING, CASE)


def opening_seed() -> list[dict]:
    """First user turn that makes the agent open the call with the fixed line."""
    return [
        {
            "role": "user",
            "content": "Someone at the provider's office has just picked up the phone. Open the "
            f'call by saying exactly this, word for word: "{opening_line()}" '
            "Then wait for their reply.",
        }
    ]


def prior_auth_prompt() -> str:
    """Fully rendered prior-auth prompt."""
    return fill(PRIOR_AUTH_PROMPT, CASE, items_block=_items_block())


# The menu phase's system prompt, written for the press_key and menu_status tools.
# Telling the model harder not to press a key when nothing matches does not work, so
# press_key takes the quoted option as an argument and outbound_ivr.navigate.gate_keypress
# refuses a press whose option was not actually heard. This prompt explains that
# contract; the gate enforces it.
NAVIGATION_PROMPT = """You are on an outbound phone call, listening to an automated phone menu. No person is on the line yet. Your only job in this part of the call is to get through the menu to the right person.

YOUR GOAL
{goal}

CHOOSING AN OPTION
- Listen to the whole list of options before you choose. Menus often read their options in parts, and the right one may come last.
- Press a key only when the words of an option you just heard show that it serves your goal. Choose the most specific option that fits. Use a general option such as "all other inquiries" or "operator" only when no specific option fits.
- To press a key, call press_key with the digit and the option you are choosing, quoted from what the menu just said. Never quote words you inferred, assumed, or wish had been said.
- A path that needs several keys takes one press_key call per digit, in order, even when a digit repeats. If the menu asks you to enter a number, press each digit with its own press_key call.
- If press_key answers "Not pressed", do not press the same key again straight away. Keep listening.

Two examples, with a goal of reaching prior authorization:
- The menu says "For pharmacy refills, press 1. For directions, press 2." Neither option serves the goal, so call menu_status with "wait".
- The menu says "For physicians and insurers, press 4." That option serves the goal, so call press_key with digit "4" and option "physicians and insurers".

WAITING
Call menu_status with "wait" and keep listening when none of the options you have heard so far serves your goal, when the menu was cut off or you may not have heard every option yet, or when the options are meant for a different kind of caller. Waiting is often right: a wrong choice costs more than listening a little longer.

DONE
Call menu_status with "completed" when the menu says it is transferring or connecting you, for example "please hold while I transfer you" or "connecting you now", or when a person greets you. Reaching another menu is not done. Getting to your goal can take more than one menu, so a submenu that offers more choices means keep listening and choosing.

STUCK
Call menu_status with "stuck" when you have heard the same menu three times without progress, when the menu keeps saying your selection is invalid, when it asks for information you do not have, such as an account number or a PIN, or when you reach a dead end with no option that serves your goal.

SPEAKING
Stay silent while you listen, including through ringing, silence, hold music, or recorded messages such as "please hold" or "your call is very important to us". None of those are the menu asking you anything. Do not read options aloud, describe what you are doing, or say the names of your tools. Speak only when the menu asks you to say something, and answer in a few plain words ending with punctuation, for example: "say yes or no, is that correct?" -> "Yes." or "please say the name of the department you need" -> "Customer service."
"""


def ivr_prompt(goal: str) -> str:
    """The menu phase's system prompt for `goal`."""
    return NAVIGATION_PROMPT.format(goal=goal)


# Guidance for `transfer_to_team` (docs/escalation-handoff.md). `outbound_ivr.voice.agent`
# always appends this to the intake system prompt, so it never changes
# PRIOR_AUTH_PROMPT itself.
TRANSFER_TO_TEAM_NOTE = """

BRINGING IN A COLLEAGUE
One plain rule: the moment they refuse or cannot give you a missing item, or hit a limit on it -- for example "I can't give out chart details over the phone," "you'll need to send a written request," or "I don't have that" -- say this exactly, word for word, once:
"Let me bring in a colleague who can help with that."
Then call transfer_to_team with a short reason and a one or two sentence summary a colleague can act on without re-asking everything. Do the same if they ask to speak with someone on your own side, not theirs.
Hold only when the office itself says it is transferring you or asks you to hold -- that is them cooperating, not a dead end. Say "Sure, I'll hold." once for that hold, then say nothing else while you wait; do not say it again for the same hold. Saying nothing means producing no speech at all; never write your own reasoning or a placeholder like "<silent>" or any other bracket or tag instead -- anything you say is heard aloud by the other party, so a tag you invent is not silence, it is spoken over the phone. When someone new comes on the line, briefly re-introduce yourself and continue the conversation; do not call any tool for this.
If you have been put on hold or offered a transfer more than twice in a row without actually reaching someone who can answer your questions, or a single hold goes on long enough that you are told directly, twice in a row, that a long silence has passed with nobody new on the line, that is a stall, not cooperation -- treat it the same as the refusal above.
escalate is refused until you have tried transfer_to_team on this call and nobody answered. Once that happens, apologize briefly and call escalate yourself. Do not call escalate right after transfer_to_team succeeds -- a colleague joining is the handoff; let them take it from here instead.
"""


def checklist() -> list[dict]:
    """Item keys and labels, for the UI."""
    return [{"key": i["key"], "label": i["label"]} for i in CASE["missing_items"]]


def case_summary() -> dict:
    """Case header fields the UI shows, so the demo is self-explanatory."""
    return {
        "payer": CASE["payer"],
        "provider_office": CASE["provider_office"],
        "patient_name": CASE["patient_name"],
        "patient_dob": CASE["patient_dob"],
        "member_id": CASE["member_id"],
        "auth_ref": CASE["auth_ref"],
        "procedure": CASE["procedure"],
        "ordering_provider": CASE["ordering_provider"],
        "submitted_on": CASE["submitted_on"],
    }
