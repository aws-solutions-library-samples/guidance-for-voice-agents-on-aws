"""`menu_status("completed")` is judged only on what was heard since the last key press."""

import asyncio

import pytest

pytest.importorskip("strands")

from outbound_ivr.voice import agent


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, type_: str, **fields) -> None:
        self.events.append((type_, fields))


class _Transport:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press_key(self, digit: str) -> None:
        self.pressed.append(digit)


def _tools(state: agent.CallState):
    press_key, menu_status = agent.make_navigate_tools(state)
    return press_key, menu_status


def test_completed_right_after_a_press_needs_new_handoff_language():
    state = agent.CallState(call_uuid="c", mode="full_call", log=_Log())
    state.transport = _Transport()
    press_key, menu_status = _tools(state)
    # The opening disclaimer says "this is"; the submenu line has no handoff words.
    state.heard += [
        "thank you for calling any company medical group this is a demonstration phone system",
        "for providers insurance companies and prior authorizations press two",
    ]
    assert (
        asyncio.run(
            press_key(
                digit="2", option="providers insurance companies and prior authorizations press two"
            )
        )
        == "Pressed."
    )
    state.heard.append("provider and insurance services for authorizations and referrals press one")
    assert (
        asyncio.run(press_key(digit="1", option="for authorizations and referrals press one"))
        == "Pressed."
    )
    state.heard.append("authorizations and referrals")
    assert asyncio.run(menu_status(status="completed")).startswith("Not completed yet")

    state.heard.append("for imaging and procedure prior authorizations press three")
    asyncio.run(
        press_key(digit="3", option="for imaging and procedure prior authorizations press three")
    )
    state.heard.append("thank you please hold while i transfer you to our prior authorization desk")
    assert asyncio.run(menu_status(status="completed")) == "Moving to the intake conversation."


def test_stuck_right_after_a_press_counts_as_wait():
    """The dashboard flashed STUCK after every key press: the model called stuck during
    the moment of silence before the menu answered."""
    log = _Log()
    state = agent.CallState(call_uuid="c", mode="full_call", log=log)
    state.transport = _Transport()
    press_key, menu_status = _tools(state)
    state.heard.append("for providers insurance companies and prior authorizations press three")
    asyncio.run(
        press_key(
            digit="3",
            option="for providers insurance companies and prior authorizations press three",
        )
    )
    assert asyncio.run(menu_status(status="stuck")).startswith("Not stuck")
    assert log.events[-1] == ("status", {"value": "wait", "rejected": "stuck"})

    # Once the menu has said something new, stuck is reported as asked.
    state.heard.append("we're sorry, that is not a valid option")
    assert asyncio.run(menu_status(status="stuck")) == "Status noted."
    assert log.events[-1] == ("status", {"value": "stuck"})
