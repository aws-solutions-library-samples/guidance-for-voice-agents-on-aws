"""Every boto3.client("<name>") in the stacks' inline Lambda code must be a real
botocore service name. A wrong one only fails at deploy time, inside the Lambda,
with a traceback too long for CloudFormation to show (OutboundIvrVoiceStack once used
"chimesdkvoice" instead of "chime-sdk-voice")."""

from __future__ import annotations

import re
from pathlib import Path

import botocore.session

EXAMPLE_ROOT = Path(__file__).resolve().parents[2]
STACK_DIRS = [EXAMPLE_ROOT / "infrastructure" / "stacks"]
CLIENT_RE = re.compile(r'boto3\.client\(\s*"([^"]+)"')


def test_inline_lambda_boto3_service_names_exist():
    available = set(botocore.session.get_session().get_available_services())
    used = {
        (path.relative_to(EXAMPLE_ROOT).as_posix(), name)
        for d in STACK_DIRS
        for path in d.glob("*.py")
        for name in CLIENT_RE.findall(path.read_text())
    }
    assert used, "found no boto3.client calls; the regex or paths are wrong"
    unknown = sorted((p, n) for p, n in used if n not in available)
    assert not unknown, f"unknown boto3 service names: {unknown}"
