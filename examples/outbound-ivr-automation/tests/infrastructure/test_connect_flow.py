"""Validate the generated Amazon Connect mock-provider-office flow.

Checks that the committed JSON is valid Connect flow language, that it's exactly what
`connect/make_flow.py` produces right now, and that the menu logic holds: the transfer
path (3, 3, 2 on the Connect office) reaches the front desk queue, every other key is a
dead end or a re-prompt, star goes back, and no real phone number or queue ARN ever
appears.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

CONNECT_DIR = Path(__file__).resolve().parents[2] / "infrastructure" / "connect"
FLOW_PATH = CONNECT_DIR / "flows" / "outbound-ivr-mock-provider-ivr.json"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def make_flow():
    return _load_module(CONNECT_DIR / "make_flow.py", "outbound_ivr_make_flow")


@pytest.fixture(scope="module")
def prompts(make_flow):
    return make_flow.FLOW_PROMPTS


@pytest.fixture(scope="module")
def flow():
    return json.loads(FLOW_PATH.read_text())


@pytest.fixture(scope="module")
def by_id(flow):
    return {a["Identifier"]: a for a in flow["Actions"]}


def test_committed_json_is_valid():
    assert FLOW_PATH.exists()
    json.loads(FLOW_PATH.read_text())


def test_committed_json_matches_fresh_generation(make_flow):
    """One source of truth: regenerating must reproduce the committed file byte for byte."""
    fresh = json.dumps(make_flow.build_flow(), indent=2) + "\n"
    assert FLOW_PATH.read_text() == fresh


def test_version_is_2019_10_30(flow):
    assert flow["Version"] == "2019-10-30"


def test_start_action_exists(flow, by_id):
    assert flow["StartAction"] in by_id


def test_flow_logging_enabled(flow, by_id):
    logging_actions = [a for a in flow["Actions"] if a["Type"] == "UpdateFlowLoggingBehavior"]
    assert len(logging_actions) == 1
    assert logging_actions[0]["Parameters"]["FlowLoggingBehavior"] == "Enabled"


def test_every_transition_points_to_an_existing_action(flow, by_id):
    for action in flow["Actions"]:
        transitions = action["Transitions"]
        targets = []
        if "NextAction" in transitions:
            targets.append(transitions["NextAction"])
        for err in transitions.get("Errors", []):
            targets.append(err["NextAction"])
        for cond in transitions.get("Conditions", []):
            targets.append(cond["NextAction"])
        for target in targets:
            assert target in by_id, f"{action['Identifier']} ({action['Type']}) -> missing {target}"


def _parameter_strings(flow):
    """All string values under every action's Parameters (not Identifiers, which are UUIDs
    and can coincidentally contain long runs of digits)."""
    for action in flow["Actions"]:
        for value in action["Parameters"].values():
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                yield from (v for v in value.values() if isinstance(v, str))


def test_placeholder_present_and_no_real_phone_number(flow):
    """Default (committed) flow transfers via a queue, not a phone number."""
    assert not [a for a in flow["Actions"] if a["Type"] == "TransferParticipantToThirdParty"]

    set_queue_actions = [a for a in flow["Actions"] if a["Type"] == "UpdateContactTargetQueue"]
    assert len(set_queue_actions) == 1
    assert set_queue_actions[0]["Parameters"]["QueueId"] == "${FRONT_DESK_QUEUE_ARN}"

    transfer_actions = [a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue"]
    assert len(transfer_actions) == 1
    assert transfer_actions[0]["Parameters"] == {}

    # No real phone number (a run of 10+ digits, optionally with a leading +) and no
    # real ARN in any action's parameters -- only the placeholder.
    for value in _parameter_strings(flow):
        if value == "${FRONT_DESK_QUEUE_ARN}":
            continue
        assert not re.search(r"\+?\d{10,}", value), value
        assert "arn:aws" not in value, value


def test_every_prompt_text_appears_in_prompts(flow, prompts):
    prompt_values = set(prompts.values())
    for action in flow["Actions"]:
        text = action["Parameters"].get("Text")
        if text is not None:
            assert text in prompt_values, f"{action['Identifier']}: {text!r} not in PROMPTS"


def _conditions(action):
    return {
        cond["Condition"]["Operands"][0]: cond["NextAction"]
        for cond in action["Transitions"]["Conditions"]
    }


def _walk_to_first_get_participant_input(flow, by_id):
    cur = flow["StartAction"]
    seen = set()
    while True:
        assert cur not in seen, "cycle before reaching the first menu"
        seen.add(cur)
        action = by_id[cur]
        if action["Type"] == "GetParticipantInput":
            return action
        cur = action["Transitions"]["NextAction"]


def _menus_along_path(flow, by_id, make_flow):
    """The three menu actions on the transfer path, from the main menu inward."""
    first, second, _ = make_flow.TRANSFER_PATH
    main_menu = _walk_to_first_get_participant_input(flow, by_id)
    providers_menu = by_id[_conditions(main_menu)[first]]
    auth_menu = by_id[_conditions(providers_menu)[second]]
    return main_menu, providers_menu, auth_menu


def test_transfer_path_differs_from_the_local_office(make_flow):
    assert make_flow.TRANSFER_PATH == ("3", "3", "2")
    assert make_flow.TRANSFER_PATH != ("2", "1", "3")


def test_transfer_path_reaches_the_transfer(flow, by_id, prompts, make_flow):
    """Default (committed, queue) flow: the transfer message leads to
    UpdateContactTargetQueue, then TransferContactToQueue."""
    _main_menu, providers_menu, auth_menu = _menus_along_path(flow, by_id, make_flow)
    assert providers_menu["Type"] == "GetParticipantInput"
    assert auth_menu["Type"] == "GetParticipantInput"
    assert providers_menu["Parameters"]["Text"] == prompts["providers-menu"]
    assert auth_menu["Parameters"]["Text"] == prompts["auth-menu"]

    transfer_message = by_id[_conditions(auth_menu)[make_flow.TRANSFER_PATH[2]]]
    assert transfer_message["Type"] == "MessageParticipant"
    assert transfer_message["Parameters"]["Text"] == prompts["transfer"]

    set_queue_action = by_id[transfer_message["Transitions"]["NextAction"]]
    assert set_queue_action["Type"] == "UpdateContactTargetQueue"

    transfer_action = by_id[set_queue_action["Transitions"]["NextAction"]]
    assert transfer_action["Type"] == "TransferContactToQueue"


def test_each_menu_offers_the_digit_its_prompt_names(make_flow):
    """The spoken menu must say "press N" for the digit that actually leads onward."""
    names = ["main-menu", "providers-menu", "auth-menu"]
    for name, digit in zip(names, make_flow.TRANSFER_PATH):
        assert f"press {digit}" in make_flow.FLOW_PROMPTS[name], (name, digit)


def _is_dead_end_message(action, by_id):
    """A MessageParticipant that leads (maybe via one more message) to DisconnectParticipant."""
    if action["Type"] != "MessageParticipant":
        return False
    nxt = by_id[action["Transitions"]["NextAction"]]
    return nxt["Type"] == "DisconnectParticipant"


def test_every_other_key_is_a_dead_end_or_a_reprompt(flow, by_id, prompts, make_flow):
    menus = _menus_along_path(flow, by_id, make_flow)
    for menu, onward in zip(menus, make_flow.TRANSFER_PATH):
        conditions = _conditions(menu)
        for digit, target_id in conditions.items():
            if digit in (onward, "*"):
                continue
            assert _is_dead_end_message(by_id[target_id], by_id), f"{digit} should dead-end"

    # Invalid input (NoMatchingCondition) re-prompts the same menu rather than dead-ending.
    for menu in menus:
        invalid_error = next(
            e for e in menu["Transitions"]["Errors"] if e["ErrorType"] == "NoMatchingCondition"
        )
        invalid_message = by_id[invalid_error["NextAction"]]
        assert invalid_message["Parameters"]["Text"] == prompts["invalid"]
        reprompt = by_id[invalid_message["Transitions"]["NextAction"]]
        assert reprompt["Type"] == "GetParticipantInput"


def test_star_goes_back(flow, by_id, make_flow):
    main_menu, providers_menu, auth_menu = _menus_along_path(flow, by_id, make_flow)
    # providers-menu's star goes back to main-menu's first attempt.
    assert _conditions(providers_menu)["*"] == main_menu["Identifier"]
    # auth-menu's star goes back to providers-menu's first attempt.
    assert _conditions(auth_menu)["*"] == providers_menu["Identifier"]
    # main-menu has no star handling: it isn't one of the DTMF conditions.
    assert "*" not in _conditions(main_menu)


def test_timeout_retries_match_tries_counter(flow, by_id, prompts):
    """3 attempts per menu: no-input plays after attempts 1 and 2, goodbye after attempt 3."""
    main_menu = _walk_to_first_get_participant_input(flow, by_id)

    def timeout_target(action):
        err = next(
            e for e in action["Transitions"]["Errors"] if e["ErrorType"] == "InputTimeLimitExceeded"
        )
        return by_id[err["NextAction"]]

    attempt1 = main_menu
    no_input_1 = timeout_target(attempt1)
    assert no_input_1["Parameters"]["Text"] == prompts["no-input"]

    attempt2 = by_id[no_input_1["Transitions"]["NextAction"]]
    assert attempt2["Type"] == "GetParticipantInput"
    no_input_2 = timeout_target(attempt2)
    assert no_input_2["Parameters"]["Text"] == prompts["no-input"]

    attempt3 = by_id[no_input_2["Transitions"]["NextAction"]]
    assert attempt3["Type"] == "GetParticipantInput"
    goodbye = timeout_target(attempt3)
    assert goodbye["Parameters"]["Text"] == prompts["goodbye"]
    disconnect = by_id[goodbye["Transitions"]["NextAction"]]
    assert disconnect["Type"] == "DisconnectParticipant"


def test_transfer_failure_plays_frontdesk_busy_then_disconnects(flow, by_id, prompts):
    transfer_action = next(a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue")
    failure_target = by_id[transfer_action["Transitions"]["Errors"][0]["NextAction"]]
    assert failure_target["Parameters"]["Text"] == prompts["frontdesk-busy"]
    disconnect = by_id[failure_target["Transitions"]["NextAction"]]
    assert disconnect["Type"] == "DisconnectParticipant"
    # Also never taken on success, but still a real action (Connect requires it).
    assert by_id[transfer_action["Transitions"]["NextAction"]] is failure_target


def test_single_disconnect_action(flow):
    disconnects = [a for a in flow["Actions"] if a["Type"] == "DisconnectParticipant"]
    assert len(disconnects) == 1
    assert disconnects[0]["Parameters"] == {}
    assert disconnects[0]["Transitions"] == {}


def test_transfer_has_the_error_branches_connect_requires(flow):
    """TransferContactToQueue (the default, queue-based transfer) needs exactly the
    error branches validated against Connect in the Escalation nested stack's handoff
    flow: QueueAtCapacity and NoMatchingError."""
    transfer_action = next(a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue")
    errors = {e["ErrorType"] for e in transfer_action["Transitions"]["Errors"]}
    assert errors == {"QueueAtCapacity", "NoMatchingError"}

    set_queue_action = next(a for a in flow["Actions"] if a["Type"] == "UpdateContactTargetQueue")
    errors = {e["ErrorType"] for e in set_queue_action["Transitions"]["Errors"]}
    assert errors == {"NoMatchingError"}
    assert set_queue_action["Transitions"]["NextAction"] == transfer_action["Identifier"]


def test_menus_do_not_set_dtmf_configuration(flow):
    """Connect rejects DTMFConfiguration on a menu GetParticipantInput."""
    for action in flow["Actions"]:
        if action["Type"] == "GetParticipantInput":
            assert "DTMFConfiguration" not in action["Parameters"]
