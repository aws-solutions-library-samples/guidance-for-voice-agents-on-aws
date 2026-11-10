"""`CallState.say()` must never log or transcript Nova's own invented tags (e.g.
"<silent>...</silent>") as if they were speech, the way it already drops parenthetical
stage directions like "(silence)". See docs/escalation-handoff.md and RESULTS.md's
notes on the leaked "<silent> Thinking Process: ..." turns.
"""

from __future__ import annotations

import pytest

pytest.importorskip("strands")

from outbound_ivr import intake
from outbound_ivr.voice.agent import CallState


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append((type_, fields))


def _state() -> CallState:
    state = CallState(call_uuid="call-1", mode="intake", log=_Log())
    state.record = intake.new_record()
    return state


def _said_texts(state: CallState) -> list[str]:
    return [t["text"] for t in state.record["transcript"] if t["who"] == "agent"]


def test_bare_silent_tag_is_not_logged_as_said():
    state = _state()

    state.say("<silent>\n</silent>")

    assert _said_texts(state) == []
    assert not any(type_ == "said" for type_, _ in state.log.events)


def test_silent_tag_with_thinking_process_body_is_not_logged_as_said():
    state = _state()

    state.say("<silent> Thinking Process: I should wait quietly here. </silent>")

    assert _said_texts(state) == []


def test_real_speech_after_a_tag_is_still_logged():
    # The tag is narration, not speech, but genuine trailing speech in the same turn
    # must still make it into the transcript -- stripping the tag must not swallow it.
    state = _state()

    state.say("<silent> Thinking Process: ... </silent> Sure, I'll hold.")

    assert _said_texts(state) == ["Sure, I'll hold."]


def test_parenthetical_stage_direction_is_still_filtered():
    # Regression guard: the pre-existing "(silence)" filter must still work.
    state = _state()

    state.say("(silence)")

    assert _said_texts(state) == []


def test_ordinary_speech_is_unaffected():
    state = _state()

    state.say("Sure, I'll hold.")

    assert _said_texts(state) == ["Sure, I'll hold."]
