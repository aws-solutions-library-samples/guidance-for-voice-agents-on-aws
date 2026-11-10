"""The menu phase's system prompt: first-party, written for the tools, no text tags.

Before 3.0.1 the prompt was prompt text copied from Pipecat that told the model to answer
with <dtmf>/<ivr> tags, a match rule that added <match> tags, and a note in the agent
saying not to use any tags after all. Nova Sonic sometimes spoke the tags aloud. These
tests pin the replacement's contract so the contradiction cannot come back.
"""

from __future__ import annotations

import re

from outbound_ivr import prompts

GOAL = "Reach prior authorization for an imaging study."


def _rendered() -> str:
    return prompts.ivr_prompt(GOAL)


def test_the_goal_is_rendered_into_the_prompt() -> None:
    text = _rendered()
    assert GOAL in text
    assert "{goal}" not in text


def test_no_text_tag_syntax_anywhere() -> None:
    """Any <dtmf>, <ivr> or <match> tag invites the model to say it aloud."""
    assert not re.search(r"</?(dtmf|ivr|match)\b", _rendered(), re.IGNORECASE)


def test_it_names_both_tools_and_every_status() -> None:
    text = _rendered()
    assert "press_key" in text
    assert "menu_status" in text
    for status in ('"wait"', '"completed"', '"stuck"'):
        assert status in text, f"the prompt must tell the model when to report {status}"


def test_it_keeps_the_rules_the_gate_and_the_tools_rely_on() -> None:
    """press_key takes the quoted option (gate_keypress checks it was heard), and a
    multi-key path is one call per digit."""
    text = _rendered().lower()
    assert "quoted from what the menu just said" in text
    assert "one press_key call per digit" in text
    assert "not pressed" in text, "the prompt must say what to do when the gate refuses"
    assert "stay silent" in text


def test_it_covers_ringing_hold_music_and_multi_menu_paths() -> None:
    """Risks flagged in the 3.0.1 prompt-consolidation review: Nova narrating during
    silence-expected intervals, and treating a submenu as if it were completion."""
    text = _rendered().lower()
    assert "ringing" in text and "hold music" in text
    assert "ending with punctuation" in text
    assert "can take more than one menu" in text


def test_no_copied_third_party_prompt_text_remains() -> None:
    """Distinctive phrases from the Pipecat IVRNavigator prompt this replaced."""
    text = _rendered()
    for phrase in (
        "Do NOT feel pressured",
        "STRATEGY TIPS",
        "SEQUENCE INPUT EXAMPLES",
        "You receive text transcriptions of the IVR system",
    ):
        assert phrase not in text


def test_the_examples_do_not_give_away_the_mock_office_path() -> None:
    """The mock office's own menu wording must not appear in the prompt's examples."""
    text = _rendered().lower()
    for leak in (
        "providers, insurance companies",
        "imaging and procedure prior authorizations",
        "authorizations and referrals",
    ):
        assert leak not in text
