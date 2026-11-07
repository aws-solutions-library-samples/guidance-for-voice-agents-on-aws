"""record_item / escalate / complete over a CaseStore, applying outbound_ivr.intake's rules
against a case loaded from the store instead of the fixed prompts.CASE."""

import pytest

from outbound_ivr.tools import business
from outbound_ivr.tools.store import InMemoryCaseStore, default_case


def make_case(case_id="case-1", **fields):
    return default_case(
        case_id,
        missing_items=[
            {"key": "physical_therapy", "label": "PT", "question": "dates and duration"},
            {"key": "progress_note", "label": "Progress note", "question": "most recent note"},
        ],
        **fields,
    )


@pytest.fixture
def store():
    s = InMemoryCaseStore()
    s.put_case(make_case())
    return s


def test_record_item_saves_a_missing_key(store):
    result = business.record_item(store, "case-1", "call-1", "physical_therapy", "6 weeks", True)
    assert result == "Saved."
    case = store.get_case("case-1")
    assert case["results"]["physical_therapy"]["value"] == "6 weeks"
    assert case["results"]["physical_therapy"]["confirmed"] is True
    assert case["results"]["physical_therapy"]["call"] == "call-1"


def test_record_item_rejects_a_key_not_on_this_case(store):
    result = business.record_item(store, "case-1", "call-1", "symptom_duration", "3 months", True)
    assert "not one of this case's missing items" in result
    assert "symptom_duration" not in store.get_case("case-1")["results"]


def test_record_item_unknown_case(store):
    assert (
        business.record_item(store, "nope", "call-1", "physical_therapy", "x", True)
        == business.UNKNOWN_CASE
    )


def test_escalate_requires_a_reason(store):
    assert business.escalate(store, "case-1", "call-1", "") == "A reason is required to escalate."
    assert (
        business.escalate(store, "case-1", "call-1", "   ") == "A reason is required to escalate."
    )
    assert store.get_case("case-1")["outcome"] is None


def test_escalate_sets_outcome(store):
    result = business.escalate(
        store, "case-1", "call-1", "no one available", follow_up="call back Tuesday"
    )
    assert result.startswith("Escalated.")
    case = store.get_case("case-1")
    assert case["status"] == "escalated"
    assert case["outcome"] == {
        "type": "escalated",
        "reason": "no one available",
        "follow_up": "call back Tuesday",
    }


def test_complete_with_all_items_confirmed(store):
    business.record_item(store, "case-1", "call-1", "physical_therapy", "6 weeks", True)
    business.record_item(store, "case-1", "call-1", "progress_note", "seen last week", True)
    result = business.complete(store, "case-1", "call-1", "Office will fax records.")
    assert result.startswith("Done.")
    case = store.get_case("case-1")
    assert case["status"] == "complete"
    assert case["outcome"] == {"type": "complete", "summary": "Office will fax records."}


def test_complete_with_items_missing_becomes_escalated(store):
    business.record_item(store, "case-1", "call-1", "physical_therapy", "6 weeks", True)
    result = business.complete(store, "case-1", "call-1", "All set.")
    assert "being escalated instead" in result
    case = store.get_case("case-1")
    assert case["status"] == "escalated"
    assert case["outcome"]["type"] == "escalated"
    assert case["outcome"]["reason"] == "incomplete at close"
    assert case["outcome"]["missing"] == ["progress_note"]
    assert case["outcome"]["model_summary"] == "All set."


def test_complete_only_counts_confirmed_results(store):
    # Recorded but not confirmed -- still missing.
    store.record_result("case-1", "call-1", "physical_therapy", "6 weeks", confirmed=False)
    business.record_item(store, "case-1", "call-1", "progress_note", "seen last week", True)
    result = business.complete(store, "case-1", "call-1", "All set.")
    assert "being escalated instead" in result
    assert store.get_case("case-1")["outcome"]["missing"] == ["physical_therapy"]


def test_a_final_outcome_is_not_overwritten(store):
    business.escalate(store, "case-1", "call-1", "first reason")
    result = business.escalate(store, "case-1", "call-2", "second reason")
    assert result == business.NO_FINAL_OUTCOME
    assert store.get_case("case-1")["outcome"]["reason"] == "first reason"

    result = business.complete(store, "case-1", "call-2", "ignored")
    assert result == business.NO_FINAL_OUTCOME
    assert store.get_case("case-1")["status"] == "escalated"


def test_load_case_shaped_like_prompts_case(store):
    store.put_office(
        {"office_id": "office-1", "name": "AnyCompany Medical Group", "number": "+15555550100"}
    )
    case = make_case(
        office_id="office-1",
        payer="Example Health Plan",
        patient_name="Jordan Ellis",
        auth_ref="PA-88431",
    )
    store.put_case(case)
    loaded = business.load_case(store, "case-1")
    assert loaded["payer"] == "Example Health Plan"
    assert loaded["patient_name"] == "Jordan Ellis"
    assert loaded["provider_office"] == "AnyCompany Medical Group"
    assert loaded["missing_items"] == [
        {"key": "physical_therapy", "label": "PT", "ask": "dates and duration"},
        {"key": "progress_note", "label": "Progress note", "ask": "most recent note"},
    ]


def test_load_case_unknown_returns_none(store):
    assert business.load_case(store, "nope") is None


def test_load_case_never_copies_an_answer_field_into_the_rendered_prompt(store):
    """scripts/seed_cases.py stores a synthetic front-desk `answer` on each missing
    item for the dashboard's Script panel (aws mode) -- it must never reach Outbound IVR.
    `load_case` must copy only key/label/ask, and the rendered prompt built from that
    shape must not contain the answer text anywhere."""
    from outbound_ivr.tools import rendering

    store.put_case(
        default_case(
            "case-1",
            office_id="",
            payer="Example Health Plan",
            patient_name="Jordan Ellis",
            patient_dob="March 14th, 1979",
            member_id="LHP-4417-2290",
            auth_ref="PA-88431",
            procedure="lumbar MRI with contrast, CPT 72149",
            provider_office="AnyCompany Medical Group",
            ordering_provider="Dr. Priya Raman",
            submitted_on="September 29th",
            return_fax="(800) 555-0142",
            portal="the Example Health Plan provider portal",
            missing_items=[
                {
                    "key": "physical_therapy",
                    "label": "PT",
                    "question": "dates and duration",
                    "answer": "SECRET-ANSWER-SHOULD-NOT-LEAK",
                },
                {
                    "key": "progress_note",
                    "label": "Progress note",
                    "question": "most recent note",
                    "answer": "ANOTHER-SECRET-ANSWER",
                },
            ],
        )
    )

    loaded = business.load_case(store, "case-1")
    assert loaded["missing_items"] == [
        {"key": "physical_therapy", "label": "PT", "ask": "dates and duration"},
        {"key": "progress_note", "label": "Progress note", "ask": "most recent note"},
    ]
    for item in loaded["missing_items"]:
        assert "answer" not in item

    prompt = rendering.prior_auth_prompt_for(loaded)
    assert "SECRET-ANSWER-SHOULD-NOT-LEAK" not in prompt
    assert "ANOTHER-SECRET-ANSWER" not in prompt
    opening = rendering.opening_line_for(loaded)
    assert "SECRET-ANSWER-SHOULD-NOT-LEAK" not in opening


def test_transfer_to_team_requires_a_reason(store):
    assert (
        business.transfer_to_team(store, "case-1", "call-1", "", "summary")
        == "A reason is required to transfer to the team."
    )
    assert business.transfer_to_team(store, "case-1", "call-1", "   ", "summary") == (
        "A reason is required to transfer to the team."
    )
    assert store.get_case("case-1")["outcome"] is None


def test_transfer_to_team_unknown_case(store):
    assert (
        business.transfer_to_team(store, "nope", "call-1", "need a person", "s")
        == business.UNKNOWN_CASE
    )


def test_transfer_to_team_records_a_provisional_outcome(store):
    result = business.transfer_to_team(
        store, "case-1", "call-1", "front desk wants a supervisor", "summary text"
    )
    assert result == "Requested."
    case = store.get_case("case-1")
    assert case["status"] == "handoff_requested"
    assert case["outcome"] == {
        "type": "transfer_to_team",
        "reason": "front desk wants a supervisor",
        "summary": "summary text",
    }


def test_transfer_to_team_is_not_a_final_outcome_escalate_can_still_run(store):
    business.transfer_to_team(store, "case-1", "call-1", "need a person", "summary")
    result = business.escalate(store, "case-1", "call-1", "no one answered")
    assert result.startswith("Escalated.")
    assert store.get_case("case-1")["status"] == "escalated"


def test_mark_handoff_joined_sets_the_final_outcome(store):
    business.transfer_to_team(store, "case-1", "call-1", "need a person", "summary text")
    result = business.mark_handoff_joined(
        store, "case-1", "call-1", "need a person", "summary text", "team-abc"
    )
    assert result == "Handed off."
    case = store.get_case("case-1")
    assert case["status"] == "handed off"
    assert case["outcome"]["type"] == "handed_off"
    assert case["outcome"]["joined_identity"] == "team-abc"
    assert case["outcome"]["reason"] == "need a person"
    assert case["outcome"]["summary"] == "summary text"
    assert "joined_at" in case["outcome"]


def test_mark_handoff_joined_unknown_case(store):
    assert (
        business.mark_handoff_joined(store, "nope", "call-1", "r", "s", "team-abc")
        == business.UNKNOWN_CASE
    )


def test_a_handed_off_outcome_is_not_overwritten(store):
    business.transfer_to_team(store, "case-1", "call-1", "need a person", "summary")
    business.mark_handoff_joined(store, "case-1", "call-1", "need a person", "summary", "team-abc")
    result = business.escalate(store, "case-1", "call-2", "too late now")
    assert result == business.NO_FINAL_OUTCOME
    assert store.get_case("case-1")["outcome"]["type"] == "handed_off"
