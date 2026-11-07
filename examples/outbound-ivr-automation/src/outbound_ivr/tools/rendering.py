"""Fill `outbound_ivr.prompts`' prior-auth prompt from a case loaded from the store.

`outbound_ivr/prompts.py` is one of the files the design doc says not to edit (it's shared,
framework-free behaviour; see "Ground rules" in docs/design.md). So
instead of adding a case-dict parameter to `prompts.prior_auth_prompt()` itself, this
module reuses its *public* template constants (`PRIOR_AUTH_PROMPT`, `OPENING`) and
reimplements the same formatting over a case dict shaped like `prompts.CASE` --
exactly what `business.load_case()` returns. `prompts.CASE` and its functions are
untouched; a call with `OUTBOUND_IVR_TOOLS=local` and no case ID keeps working exactly as
before.
"""

from __future__ import annotations

from outbound_ivr import prompts


def items_block_for(case: dict) -> str:
    lines = []
    for i, item in enumerate(case["missing_items"], 1):
        lines.append(f"{i}. {item['label']} (key: {item['key']}). Ask for {item['ask']}.")
    return "\n".join(lines)


def prior_auth_prompt_for(case: dict) -> str:
    """`prompts.prior_auth_prompt()`, but filled from `case` instead of `prompts.CASE`."""
    return prompts.fill(prompts.PRIOR_AUTH_PROMPT, case, items_block=items_block_for(case))


def opening_line_for(case: dict) -> str:
    """`prompts.opening_line()`, but filled from `case` instead of `prompts.CASE`."""
    return prompts.fill(prompts.OPENING, case)


def opening_seed_for(case: dict) -> list[dict]:
    line = opening_line_for(case)
    return [
        {
            "role": "user",
            "content": "Someone at the provider's office has just picked up the phone. Open the "
            f'call by saying exactly this, word for word: "{line}" '
            "Then wait for their reply.",
        }
    ]


def checklist_for(case: dict) -> list[dict]:
    return [{"key": i["key"], "label": i["label"]} for i in case["missing_items"]]


def case_summary_for(case: dict) -> dict:
    return {
        "payer": case.get("payer", ""),
        "provider_office": case.get("provider_office", ""),
        "patient_name": case.get("patient_name", ""),
        "patient_dob": case.get("patient_dob", ""),
        "member_id": case.get("member_id", ""),
        "auth_ref": case.get("auth_ref", ""),
        "procedure": case.get("procedure", ""),
        "ordering_provider": case.get("ordering_provider", ""),
        "submitted_on": case.get("submitted_on", ""),
    }
