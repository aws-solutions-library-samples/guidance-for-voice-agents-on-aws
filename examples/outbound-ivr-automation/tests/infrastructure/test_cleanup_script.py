"""scripts/cleanup.sh: not run (it talks to real AWS), but checked for valid shell
syntax, being executable, requiring an explicit --yes before doing anything, and
destroying the three stacks in the documented reverse-dependency order.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "infrastructure" / "scripts" / "cleanup.sh"


def test_cleanup_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "cleanup.sh must be executable (chmod +x)"


def test_cleanup_script_has_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash, "bash not found on PATH"
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_script_requires_explicit_yes() -> None:
    text = SCRIPT.read_text()
    assert "--yes" in text
    assert "yes=0" in text
    assert 'if [ "$yes" = 0 ]' in text


def test_cleanup_script_destroys_stacks_in_reverse_dependency_order() -> None:
    text = SCRIPT.read_text()
    outbound_ivr_idx = text.index("destroy OutboundIvrStack")
    office_idx = text.index("destroy OutboundIvrOfficeStack")
    voice_idx = text.index("destroy OutboundIvrVoiceStack")
    assert outbound_ivr_idx < office_idx < voice_idx, (
        "cleanup.sh must destroy OutboundIvrStack, then OutboundIvrOfficeStack, "
        "then OutboundIvrVoiceStack -- the reverse of deploy.sh/deploy_office.sh's "
        "own deploy order"
    )


def test_cleanup_script_empties_buckets_before_any_destroy() -> None:
    text = SCRIPT.read_text()
    empty_idx = text.index("empty_bucket ")
    first_destroy_idx = text.index("if ! destroy_outbound_ivr")
    assert empty_idx < first_destroy_idx, (
        "buckets must be emptied before any `cdk destroy`, which otherwise fails "
        "on the two non-auto-delete buckets"
    )


def test_cleanup_script_stops_before_office_and_voice_while_agentcore_holds_the_vpc() -> None:
    """AgentCore keeps the Runtime's agentic_ai network interfaces for up to 8 hours
    after the Runtime is deleted, which blocks OutboundIvrStack's deletion. The script
    must explain that and stop, not go on to the voice stack, whose Elastic IP the
    LiveKit host still holds."""
    text = SCRIPT.read_text()
    failure_branch = text[text.index("if ! destroy_outbound_ivr") : text.index('echo "== 3/4')]
    assert "agentic_ai" in failure_branch
    assert "up to 8 hours" in failure_branch
    assert "exit 1" in failure_branch
