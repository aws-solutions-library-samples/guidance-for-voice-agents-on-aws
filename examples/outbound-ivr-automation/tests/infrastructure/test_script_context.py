"""The deploy, office and cleanup scripts share one CDK context list.

infrastructure/app.py builds all three stacks on every cdk command, so a command that
passed only its own stack's context failed on another stack's required value (for
example `deploy.sh synth` died on the office stack's missing agentEmail). These tests
keep every script on the shared list in app_context.sh.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "infrastructure" / "scripts"
CDK_SCRIPTS = ["deploy.sh", "deploy_office.sh", "cleanup.sh"]


@pytest.mark.parametrize("name", CDK_SCRIPTS)
def test_script_sources_the_shared_context(name: str) -> None:
    text = (SCRIPTS / name).read_text()
    assert 'source "$ROOT/infrastructure/scripts/app_context.sh"' in text


@pytest.mark.parametrize("name", CDK_SCRIPTS)
def test_script_runs_cdk_only_through_cdk_app(name: str) -> None:
    code = [
        line
        for line in (SCRIPTS / name).read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    direct = [
        line
        for line in code
        if re.search(r"(^|[;&|(]\s*)cdk\s+(synth|deploy|destroy|diff)\b", line)
    ]
    assert direct == [], f"{name} calls cdk directly instead of cdk_app: {direct}"


def test_shared_context_carries_every_stack_requirement() -> None:
    text = (SCRIPTS / "app_context.sh").read_text()
    for key in (
        "agentEmail",
        "claimOfficeNumber",
        "claimEvidenceNumber",
        "dashboardDistDir",
        "testCellNumber",
    ):
        assert f"{key}=" in text, f"app_context.sh does not pass {key}"


def test_shared_context_appends_the_list_to_every_cdk_call() -> None:
    assert shutil.which("bash"), "bash not found on PATH"
    # A fixed command run from the scripts folder; the inputs app_context.sh reads come
    # in through the environment, so no part of the command comes from a variable.
    env = {
        **os.environ,
        "ROOT": "/example",
        "AGENT_EMAIL": "agent@example.com",
        "FRONT_DESK_NUMBER": "+15555550100",
    }
    result = subprocess.run(  # nosec B603 - static argv, this repo's own script
        ["bash", "-c", 'source ./app_context.sh && printf "%s\\n" "${app_ctx[@]}"'],
        cwd=SCRIPTS,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    args = result.stdout.splitlines()
    assert "agentEmail=agent@example.com" in args
    assert "testCellNumber=+15555550100" in args
    assert "dashboardDistDir=/example/src/dashboard/dist" in args
    assert 'cdk "$@" "${app_ctx[@]}"' in (SCRIPTS / "app_context.sh").read_text()
