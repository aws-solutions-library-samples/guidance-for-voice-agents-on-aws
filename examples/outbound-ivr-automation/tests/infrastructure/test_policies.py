"""policies/*.cedar and policies/dogwood/*.dw: each file must exist, be non-empty, and
mention the tool it governs (so a rename or a copy-paste mistake doesn't silently leave
a policy governing the wrong action). infra/tests/test_agentcore_stack.py separately
checks that CDK turns these into the right CloudFormation resources; this file checks
the policy source text itself, independent of CDK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

POLICIES_DIR = Path(__file__).resolve().parents[2] / "infrastructure" / "policies"
DOGWOOD_DIR = POLICIES_DIR / "dogwood"

# tool name -> the Cedar/Dogwood action-name fragment it must appear as
CEDAR_FILES = {
    "record_item": "OutboundIvrTools___record_item",
    "escalate": "OutboundIvrTools___escalate",
    "complete": "OutboundIvrTools___complete",
    "transfer_to_team": "OutboundIvrTools___transfer_to_team",
}

# Dogwood files: more than one file may govern the same tool (transfer_to_team's
# "at most once" rule is a second, separate forbid policy -- see
# policies/dogwood/transfer_to_team_once.dw and policies/README.md).
DOGWOOD_FILES = {
    "record_item": "OutboundIvrTools___record_item",
    "escalate": "OutboundIvrTools___escalate",
    "complete": "OutboundIvrTools___complete",
    "transfer_to_team": "OutboundIvrTools___transfer_to_team",
    "transfer_to_team_once": "OutboundIvrTools___transfer_to_team",
}


@pytest.mark.parametrize("stem,action", sorted(CEDAR_FILES.items()))
def test_cedar_policy_exists_nonempty_and_mentions_its_tool(stem: str, action: str) -> None:
    path = POLICIES_DIR / f"{stem}.cedar"
    assert path.is_file(), f"missing {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty"
    assert action in text, f"{path} must reference action {action!r}"


@pytest.mark.parametrize("stem,action", sorted(DOGWOOD_FILES.items()))
def test_dogwood_policy_exists_nonempty_and_mentions_its_tool(stem: str, action: str) -> None:
    path = DOGWOOD_DIR / f"{stem}.dw"
    assert path.is_file(), f"missing {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty"
    assert action in text, f"{path} must reference action {action!r}"


def test_every_cedar_file_has_a_dogwood_counterpart() -> None:
    """Every *.cedar file in policies/ governs the same tool as at least one
    policies/dogwood/*.dw file (the Dogwood port may split one Cedar rule into more
    than one Dogwood file, as transfer_to_team does, but never fewer).
    """
    cedar_stems = {p.stem for p in POLICIES_DIR.glob("*.cedar")}
    assert cedar_stems == set(CEDAR_FILES), "update CEDAR_FILES to match policies/*.cedar"
    dogwood_actions = set(DOGWOOD_FILES.values())
    cedar_actions = set(CEDAR_FILES.values())
    assert cedar_actions <= dogwood_actions, (
        "every Cedar-governed action needs at least one Dogwood policy: "
        f"missing {cedar_actions - dogwood_actions}"
    )


def test_dogwood_dir_has_no_extra_files_beyond_the_known_set() -> None:
    actual = {p.stem for p in DOGWOOD_DIR.glob("*.dw")}
    assert actual == set(DOGWOOD_FILES), (
        "policies/dogwood/*.dw changed without updating this test's DOGWOOD_FILES: "
        f"got {actual}, expected {set(DOGWOOD_FILES)}"
    )


def test_transfer_to_team_once_is_a_forbid_not_a_permit() -> None:
    """The at-most-once rule must deny the second attempt, not grant the first --
    a `permit` here would do nothing (transfer_to_team.dw already permits), while a
    missing `forbid` would silently drop the cap.
    """
    text = (DOGWOOD_DIR / "transfer_to_team_once.dw").read_text(encoding="utf-8")
    assert "forbid(" in text or "forbid (" in text, text
    # Strip comments before checking there's no bare top-level permit in this file.
    code_lines = [line for line in text.splitlines() if not line.strip().startswith("//")]
    code = "\n".join(code_lines)
    assert "permit(" not in code and "permit (" not in code, (
        "transfer_to_team_once.dw should only forbid, not permit -- the permit lives "
        "in transfer_to_team.dw"
    )


def _seeded_item_keys() -> set[str]:
    import importlib.util

    path = POLICIES_DIR.parent / "scripts" / "seed_cases.py"
    spec = importlib.util.spec_from_file_location("seed_cases", path)
    seed_cases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_cases)
    cases = [seed_cases.demo_case(), *seed_cases.extra_cases()]
    return {item["key"] for case in cases for item in case["missing_items"]}


@pytest.mark.parametrize(
    "path", [POLICIES_DIR / "record_item.cedar", DOGWOOD_DIR / "record_item.dw"]
)
def test_record_item_allowlist_covers_every_seeded_case(path: Path) -> None:
    """A key missing here makes the Gateway deny record_item for that case's calls."""
    import re

    text = path.read_text(encoding="utf-8")
    (listing,) = re.findall(r"\[([^\]]*)\]\.contains\(context\.input\.key\)", text)
    allowed = set(re.findall(r'"([a-z_]+)"', listing))
    assert _seeded_item_keys() <= allowed, sorted(_seeded_item_keys() - allowed)


@pytest.mark.parametrize(
    "path", sorted(POLICIES_DIR.glob("*.cedar")) + sorted(DOGWOOD_DIR.glob("*.dw"))
)
def test_no_policy_uses_in_on_a_string_list(path: Path) -> None:
    """Cedar's `in` is entity membership; the policy engine rejects `"x" in ["x"]`."""
    import re

    assert not re.search(r"\bin \[", path.read_text(encoding="utf-8")), path.name
