"""Outbound IVR's fixed opening line must disclose both that it is automated and that the
call is recorded: the model is told to say it word for word, so these can't drift."""

from __future__ import annotations

from outbound_ivr import prompts
from outbound_ivr.tools import rendering


def test_opening_discloses_automation_and_recording():
    line = prompts.opening_line()
    assert "automated assistant" in line
    assert "recorded line" in line


def test_case_driven_opening_carries_the_same_disclosures():
    line = rendering.opening_line_for(prompts.CASE)
    assert "automated assistant" in line
    assert "recorded line" in line


def test_transfer_note_names_queue_announcements_as_recordings():
    """Outbound IVR once opened the call to a contact-center queue announcement."""
    from outbound_ivr.voice import agent

    note = " ".join(agent.TRANSFER_NOTE.lower().split())
    assert "your call is very important to us" in note
    assert "order received" in note


# --------------------------------------------------------------------------
# The opening used to name the patient and the authorization reference on the
# very first turn, before anyone had confirmed who picked up. A misdial, a
# shared line or a stale directory then handed a stranger someone's medical
# information. The line is split, and the half that names the patient waits.
# --------------------------------------------------------------------------

_PHI_FIELDS = ("patient_name", "patient_dob", "member_id", "auth_ref")


def test_the_first_line_names_nobody():
    line = prompts.opening_line()
    for field in _PHI_FIELDS:
        assert prompts.CASE[field] not in line, f"{field} must not be in the first thing said"


def test_the_first_line_asks_who_answered():
    line = prompts.opening_line()
    assert prompts.CASE["provider_office"] in line
    assert "prior authorization" in line.lower()


def test_the_prompt_gates_the_disclosure_on_a_confirming_reply():
    prompt = prompts.prior_auth_prompt()
    assert "BEFORE YOU NAME THE PATIENT" in prompt
    collapsed = " ".join(prompt.split())
    assert "Your first turn names no patient" in collapsed
    assert "wrong number" in collapsed


def test_case_driven_opening_also_names_nobody():
    """The dashboard-driven path renders from a case dict, not prompts.CASE."""
    case = dict(prompts.CASE, patient_name="Other Person", auth_ref="PA-OTHER-1")
    line = rendering.opening_line_for(case)
    assert "Other Person" not in line
    assert "PA-OTHER-1" not in line
    assert "automated assistant" in line
    assert "recorded line" in line
