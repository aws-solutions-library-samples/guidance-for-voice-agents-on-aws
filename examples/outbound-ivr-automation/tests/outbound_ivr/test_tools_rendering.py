"""The prior-auth prompt filled from a case dict, instead of the fixed prompts.CASE.

Doesn't touch outbound_ivr.prompts.py; checks that filling from business.load_case(case)
produces the same shape of prompt as prompts.prior_auth_prompt() does for the demo case.
"""

from outbound_ivr import prompts
from outbound_ivr.tools import business, rendering
from outbound_ivr.tools.store import InMemoryCaseStore, default_case


def test_prior_auth_prompt_for_matches_the_demo_case():
    store = InMemoryCaseStore()
    store.put_office({"office_id": "office-1", "name": prompts.CASE["provider_office"]})
    store.put_case(
        default_case(
            "case-1",
            payer=prompts.CASE["payer"],
            patient_name=prompts.CASE["patient_name"],
            patient_dob=prompts.CASE["patient_dob"],
            member_id=prompts.CASE["member_id"],
            auth_ref=prompts.CASE["auth_ref"],
            procedure=prompts.CASE["procedure"],
            ordering_provider=prompts.CASE["ordering_provider"],
            submitted_on=prompts.CASE["submitted_on"],
            return_fax=prompts.CASE["return_fax"],
            portal=prompts.CASE["portal"],
            office_id="office-1",
            missing_items=[
                {"key": i["key"], "label": i["label"], "question": i["ask"]}
                for i in prompts.CASE["missing_items"]
            ],
        )
    )
    case = business.load_case(store, "case-1")

    rendered = rendering.prior_auth_prompt_for(case)
    original = prompts.prior_auth_prompt()
    assert rendered == original


def test_opening_line_for_matches_the_demo_case():
    store = InMemoryCaseStore()
    store.put_case(
        default_case(
            "case-1",
            payer=prompts.CASE["payer"],
            patient_name=prompts.CASE["patient_name"],
            auth_ref=prompts.CASE["auth_ref"],
            # The opening now asks "have I reached <office>", so the office name is
            # part of what has to match.
            provider_office=prompts.CASE["provider_office"],
        )
    )
    case = business.load_case(store, "case-1")
    assert rendering.opening_line_for(case) == prompts.opening_line()


def test_checklist_for_a_different_case():
    case = {
        "missing_items": [
            {"key": "x", "label": "X", "ask": "ask for x"},
        ]
    }
    assert rendering.checklist_for(case) == [{"key": "x", "label": "X"}]


def test_prompts_case_is_unaffected():
    # Importing outbound_ivr.tools.rendering must not change prompts.CASE's own behaviour.
    assert prompts.prior_auth_prompt() == prompts.PRIOR_AUTH_PROMPT.format(
        items_block=prompts._items_block(), **prompts.CASE
    )


def _case_without_an_office() -> dict:
    """What `load_case` returns when the case has no office row: provider_office
    resolves to the empty string. Real, not hypothetical: `default_case` carries
    no provider_office at all and an office row can be missing or unnamed."""
    store = InMemoryCaseStore()
    store.put_case(default_case("case-no-office", payer="Example Health"))
    return business.load_case(store, "case-no-office")


def test_opening_line_falls_back_when_the_case_has_no_office_name():
    """Without a fallback the agent says "Have I reached , and are you..." aloud."""
    case = _case_without_an_office()
    assert case["provider_office"] == ""
    line = rendering.opening_line_for(case)
    assert prompts.OFFICE_FALLBACK in line
    assert "reached ," not in line


def test_prompt_falls_back_the_same_way():
    prompt = rendering.prior_auth_prompt_for(_case_without_an_office())
    assert prompts.OFFICE_FALLBACK in prompt
