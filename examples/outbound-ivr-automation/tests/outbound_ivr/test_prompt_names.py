"""No persona name (e.g. "Sam") ever leaks into anything Outbound IVR itself says or is told:
`outbound_ivr.prompts`, `outbound_ivr.tools.rendering`, or the navigate/intake tools' own
docstrings. Outbound IVR doesn't know who will answer -- a real call is never "Sam".
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("strands")

from outbound_ivr import intake, prompts
from outbound_ivr.tools import rendering
from outbound_ivr.voice import agent
from outbound_ivr.voice.agent import CallState

# The forbidden name: the only persona name this product has ever used for whoever
# plays the office side of a call.
PERSONA_NAMES = ["Sam"]


def _assert_no_persona_name(text: str, where: str) -> None:
    for name in PERSONA_NAMES:
        assert not re.search(rf"\b{name}\b", text), f"{where} names the persona {name!r}: {text!r}"


# --------------------------------------------------------------------------
# outbound_ivr.prompts
# --------------------------------------------------------------------------


def test_prompts_module_has_no_persona_names():
    case = {**prompts.CASE, "caller_name": "Outbound IVR"}
    rendered = [
        prompts.prior_auth_prompt(),
        prompts.opening_line(),
        prompts.ivr_prompt("reach prior authorization"),
        prompts.TRANSFER_TO_TEAM_NOTE,
        prompts.PRIOR_AUTH_PROMPT.format(items_block=prompts._items_block(), **case),
    ]
    for text in rendered:
        _assert_no_persona_name(text, "outbound_ivr.prompts")


# --------------------------------------------------------------------------
# outbound_ivr.tools.rendering
# --------------------------------------------------------------------------


def test_rendering_module_has_no_persona_names():
    case = {
        **prompts.CASE,
        "missing_items": prompts.CASE["missing_items"],
    }
    rendered = [
        rendering.items_block_for(case),
        rendering.prior_auth_prompt_for(case),
        rendering.opening_line_for(case),
        str(rendering.opening_seed_for(case)),
    ]
    for text in rendered:
        _assert_no_persona_name(text, "outbound_ivr.tools.rendering")


# --------------------------------------------------------------------------
# Tool docstrings: navigate and intake
# --------------------------------------------------------------------------


def _state() -> CallState:
    class _Log:
        def write(self, type_, **fields):
            pass

    state = CallState(call_uuid="call-1", mode="intake", log=_Log())
    state.record = intake.new_record()
    return state


def test_navigate_tool_docstrings_have_no_persona_names():
    for t in agent.make_navigate_tools(_state()):
        _assert_no_persona_name(str(t.tool_spec), f"navigate tool {t.tool_name}")


def test_intake_tool_docstrings_have_no_persona_names():
    state = _state()
    state.team_dialer = None
    for t in agent.make_intake_tools(state):
        _assert_no_persona_name(str(t.tool_spec), f"intake tool {t.tool_name}")
