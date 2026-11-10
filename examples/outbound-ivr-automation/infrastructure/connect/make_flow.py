"""Generate the Amazon Connect mock-provider-office flow.

AnyCompany Medical Group's Connect office has its own menu, with shared common lines
(welcome, invalid, no-input, transfer, goodbye, closed lines) coming from
`connect/prompts.py`. The flow (Flow language, Version 2019-10-30) encodes the menu
logic: timeouts, a fixed number of retries on no input or an invalid key, star to go
back a level, dead ends that play a closing message and disconnect, and a transfer
that hands the call to the front desk: it sets the working queue to the front desk's
Connect queue (UpdateContactTargetQueue), then runs TransferContactToQueue, and the
`outbound-ivr-front-desk` agent answers in the dashboard's embedded CCP.

Regenerate after changing the prompts or the menu logic:

    uv run python connect/make_flow.py

Action identifiers are uuid5(NAMESPACE, name), so the output is stable across runs: the
same prompts and menu structure always produce byte-identical JSON
(see tests/test_connect_flow.py, which checks the committed file against a fresh run).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

# Make `prompts` importable by package-relative name whether this module is run
# directly (`python connect/make_flow.py`) or loaded by path (e.g. by tests), since
# connect/ has no __init__.py and is not on sys.path in the latter case.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import PROMPTS

FLOW_PATH = Path(__file__).resolve().parent / "flows" / "outbound-ivr-mock-provider-ivr.json"

# Fixed, arbitrary namespace: action identifiers are uuid5(NAMESPACE, name), so they never
# change across runs as long as the name doesn't change.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "outbound-ivr-mock-provider-ivr")

# The front desk's Connect queue ARN (OutboundIvrOfficeStack's outbound-ivr-front-desk
# queue) is a deploy-time placeholder filled by scripts/deploy_flow.sh from SSM
# /outbound-ivr/connect/front-desk-queue-arn (published by office_stack.py's
# OutboundIvrOfficeStack), never a real ARN.
FRONT_DESK_QUEUE_ARN = "${FRONT_DESK_QUEUE_ARN}"

# Seconds to wait for the first DTMF digit.
INPUT_TIME_LIMIT_SECONDS = "6"

# 3 attempts per menu level before giving up.
ATTEMPTS_PER_MENU = 3

# The path to the prior authorization desk is 3, 3, 2. Shared lines (welcome, invalid,
# no-input, transfer, goodbye, closed lines) come from PROMPTS; these replace or add to them.
CONNECT_PROMPTS = {
    "main-menu": "For billing questions, press 1. For patients and appointment scheduling, press 2. For providers, insurance companies, and prior authorizations, press 3.",
    "providers-menu": "Provider and insurance services. For claim status, press 1. For eligibility and benefits, press 2. For authorizations and referrals, press 3. To return to the main menu, press star.",
    "auth-menu": "Authorizations and referrals. For referral status, press 1. For imaging and procedure prior authorizations, press 2. For pharmacy prior authorizations, press 3. To go back, press star.",
    "eligibility-closed": "Our eligibility and benefits line is closed for this demonstration. Goodbye.",
}
FLOW_PROMPTS = {**PROMPTS, **CONNECT_PROMPTS}

# The digits that reach the transfer to the front desk.
TRANSFER_PATH = ("3", "3", "2")

# Menu structure. "options" maps a DTMF digit to either:
#   ("dead-end", <FLOW_PROMPTS key>)  -- play the closing prompt, then disconnect
#   ("menu", <menu name>)             -- go to that menu's first attempt
#   ("transfer", None)                -- play the transfer prompt, then transfer to the
#                                         front desk queue
# "star" is the menu to return to (or None if star isn't handled, same as any other
# unrecognized key).
MENUS: dict[str, dict[str, Any]] = {
    "main-menu": {
        "options": {
            "1": ("dead-end", "billing-closed"),
            "2": ("dead-end", "patients-closed"),
            "3": ("menu", "providers-menu"),
        },
        "star": None,
    },
    "providers-menu": {
        "options": {
            "1": ("dead-end", "claims-closed"),
            "2": ("dead-end", "eligibility-closed"),
            "3": ("menu", "auth-menu"),
        },
        "star": "main-menu",
    },
    "auth-menu": {
        "options": {
            "1": ("dead-end", "referrals-closed"),
            "2": ("transfer", None),
            "3": ("dead-end", "pharmacy-closed"),
        },
        "star": "providers-menu",
    },
}


class FlowBuilder:
    def __init__(self) -> None:
        self.actions: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self._dead_ends: dict[str, str] = {}

    def _id(self, name: str) -> str:
        return str(uuid.uuid5(NAMESPACE, name))

    def _add(self, name: str, action: dict[str, Any]) -> str:
        identifier = self._id(name)
        action = {"Identifier": identifier, **action}
        if identifier not in self.actions:
            self.actions[identifier] = action
            self.order.append(identifier)
        return identifier

    def message(self, name: str, text: str, next_id: str) -> str:
        return self._add(
            name,
            {
                "Type": "MessageParticipant",
                "Parameters": {"Text": text},
                "Transitions": {
                    "NextAction": next_id,
                    "Errors": [{"NextAction": next_id, "ErrorType": "NoMatchingError"}],
                    "Conditions": [],
                },
            },
        )

    def disconnect(self) -> str:
        return self._add(
            "disconnect",
            {"Type": "DisconnectParticipant", "Parameters": {}, "Transitions": {}},
        )

    def dead_end(self, prompt_key: str, disconnect_id: str) -> str:
        """A closing message followed by disconnect; shared by any option that reaches it."""
        if prompt_key not in self._dead_ends:
            self._dead_ends[prompt_key] = self.message(
                prompt_key, FLOW_PROMPTS[prompt_key], disconnect_id
            )
        return self._dead_ends[prompt_key]

    def get_input(
        self,
        name: str,
        text: str,
        conditions: list[tuple[str, str]],
        invalid_next: str,
        timeout_next: str,
    ) -> str:
        return self._add(
            name,
            {
                "Type": "GetParticipantInput",
                "Parameters": {
                    "Text": text,
                    "InputTimeLimitSeconds": INPUT_TIME_LIMIT_SECONDS,
                    # A menu (StoreInput False) matches "*" like any other key, so it
                    # can mean "go back". Connect rejects DTMFConfiguration here.
                    "StoreInput": "False",
                },
                "Transitions": {
                    "NextAction": invalid_next,
                    "Errors": [
                        {"NextAction": timeout_next, "ErrorType": "InputTimeLimitExceeded"},
                        {"NextAction": invalid_next, "ErrorType": "NoMatchingCondition"},
                        {"NextAction": invalid_next, "ErrorType": "NoMatchingError"},
                    ],
                    "Conditions": [
                        {
                            "NextAction": target,
                            "Condition": {"Operator": "Equals", "Operands": [digit]},
                        }
                        for digit, target in conditions
                    ],
                },
            },
        )

    def transfer_to_queue(self, name: str, queue_full_next: str) -> str:
        """Front-desk transfer via a Connect queue: set the working queue, then
        transfer the contact to it. Action shapes (UpdateContactTargetQueue's
        "QueueId" parameter, TransferContactToQueue's required-but-never-taken-on-
        success NextAction, and its QueueAtCapacity/NoMatchingError error branches)
        match the Escalation nested stack's handoff flow
        (stacks/escalation_stack.py), which Connect validated."""
        transfer_id = self._add(
            f"{name}-to-queue",
            {
                "Type": "TransferContactToQueue",
                "Parameters": {},
                "Transitions": {
                    # Required, and must be a real action, but never taken on
                    # success: the contact is in the queue and this flow is over.
                    "NextAction": queue_full_next,
                    "Errors": [
                        {"NextAction": queue_full_next, "ErrorType": error}
                        for error in ("QueueAtCapacity", "NoMatchingError")
                    ],
                    "Conditions": [],
                },
            },
        )
        return self._add(
            f"{name}-set-queue",
            {
                "Type": "UpdateContactTargetQueue",
                "Parameters": {"QueueId": FRONT_DESK_QUEUE_ARN},
                "Transitions": {
                    "NextAction": transfer_id,
                    "Errors": [{"NextAction": transfer_id, "ErrorType": "NoMatchingError"}],
                    "Conditions": [],
                },
            },
        )


def build_flow() -> dict[str, Any]:

    b = FlowBuilder()

    disconnect_id = b.disconnect()
    goodbye_id = b.message("goodbye", FLOW_PROMPTS["goodbye"], disconnect_id)
    frontdesk_busy_id = b.message("frontdesk-busy", FLOW_PROMPTS["frontdesk-busy"], disconnect_id)

    def menu_attempt1_id(menu_name: str) -> str:
        return b._id(f"{menu_name}-attempt1")

    def resolve_target(option: tuple[str, Any]) -> str:
        kind, value = option
        if kind == "dead-end":
            return b.dead_end(value, disconnect_id)
        if kind == "menu":
            return menu_attempt1_id(value)
        if kind == "transfer":
            transfer_id = b.transfer_to_queue("auth-menu-transfer", frontdesk_busy_id)
            return b.message("transfer", FLOW_PROMPTS["transfer"], transfer_id)
        raise ValueError(kind)

    for menu_name, menu in MENUS.items():
        prompt_text = FLOW_PROMPTS[menu_name]
        conditions = [(digit, resolve_target(option)) for digit, option in menu["options"].items()]
        if menu["star"] is not None:
            conditions.append(("*", menu_attempt1_id(menu["star"])))

        # Build the 3-attempt timeout chain back to front, so each attempt's timeout error
        # can point at the next attempt (or, for the last attempt, at the goodbye message).
        timeout_next = goodbye_id
        attempt_ids: list[str] = []
        for attempt in range(ATTEMPTS_PER_MENU, 0, -1):
            attempt_name = f"{menu_name}-attempt{attempt}"
            invalid_name = f"{attempt_name}-invalid"
            invalid_id = b._id(invalid_name)
            attempt_id = b._id(attempt_name)
            b.message(invalid_name, FLOW_PROMPTS["invalid"], attempt_id)
            b.get_input(attempt_name, prompt_text, conditions, invalid_id, timeout_next)
            attempt_ids.append(attempt_id)
            if attempt > 1:
                no_input_name = f"{attempt_name}-timeout"
                timeout_next = b.message(no_input_name, FLOW_PROMPTS["no-input"], attempt_id)

    welcome_id = b.message("welcome", FLOW_PROMPTS["welcome"], menu_attempt1_id("main-menu"))
    logging_id = b._add(
        "flow-logging",
        {
            "Type": "UpdateFlowLoggingBehavior",
            "Parameters": {"FlowLoggingBehavior": "Enabled"},
            "Transitions": {"NextAction": welcome_id, "Errors": [], "Conditions": []},
        },
    )
    voice_id = b._add(
        "voice",
        {
            "Type": "UpdateContactTextToSpeechVoice",
            "Parameters": {"TextToSpeechVoice": "Joanna", "TextToSpeechEngine": "neural"},
            "Transitions": {
                "NextAction": logging_id,
                "Errors": [{"NextAction": logging_id, "ErrorType": "NoMatchingError"}],
                "Conditions": [],
            },
        },
    )

    # Reorder so the entry chain (voice, logging, welcome) comes first, for readability;
    # everything else keeps its creation order.
    head = [voice_id, logging_id, welcome_id]
    ordered = head + [i for i in b.order if i not in head]

    return {
        "Version": "2019-10-30",
        "StartAction": voice_id,
        "Actions": [b.actions[i] for i in ordered],
    }


def main() -> None:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()

    flow = build_flow()
    FLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLOW_PATH.write_text(json.dumps(flow, indent=2) + "\n")
    print(f"{FLOW_PATH}  {len(flow['Actions'])} actions")


if __name__ == "__main__":
    main()
