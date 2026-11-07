"""Assertions for outbound-ivr-voice: the Elastic IP pool and the Voice Connector (plus
its termination settings and termination-credentials custom resource) all carry
RemovalPolicy.DESTROY, the Voice Connector's on_update calls UpdateVoiceConnector
rather than re-running CreateVoiceConnector, its on_delete calls DeleteVoiceConnector,
the termination's CidrAllowedList is exactly the two EIPs, no Chime IAM statement uses
a bare wildcard except CreateVoiceConnector, no resolved secret value appears in any
custom resource's own properties, every taggable resource carries the project tag, and
the seven SSM parameters are named and wired exactly per the contract.
"""

from __future__ import annotations

import json

from stacks.voice_stack import (
    CHIME_VOICE_CONNECTOR_ARN_PATTERN,
    PARAM_CONNECTOR_ID,
    PARAM_CREDENTIALS_SECRET_ARN,
    PARAM_OUTBOUND_HOST,
    eip_allocation_id_param,
    eip_public_ip_param,
)


def _iter_resource_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_resource_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_resource_strings(item)


def _flatten_create_payload(create) -> str:
    """`AwsCustomResource.Create`/`.Update` is a JSON string when every parameter
    is a literal, or a CloudFormation `Fn::Join` of string fragments and
    intrinsics (e.g. `Fn::GetAtt`) when one isn't known until deploy. Flatten
    either shape to one string, substituting a placeholder for each intrinsic,
    so the *shape* of the JSON (keys, literal values) can still be checked
    without a CloudFormation client to resolve the token."""
    if isinstance(create, str):
        return create
    parts = create["Fn::Join"][1]
    return "".join(part if isinstance(part, str) else "<token>" for part in parts)


def test_eips_and_voice_connector_resources_are_destroyed(voice_template) -> None:
    eips = voice_template.find_resources("AWS::EC2::EIP")
    assert len(eips) == 2
    for eip in eips.values():
        assert eip["DeletionPolicy"] == "Delete"
        assert eip["UpdateReplacePolicy"] == "Delete"

    for resource_type in (
        "Custom::ChimeVoiceConnector",
        "Custom::ChimeVoiceConnectorTermination",
        "Custom::ChimeVoiceConnectorTerminationCredentials",
    ):
        (resource,) = voice_template.find_resources(resource_type).values()
        assert resource["DeletionPolicy"] == "Delete"
        assert resource["UpdateReplacePolicy"] == "Delete"


def test_voice_connector_requires_encryption_and_is_tagged(voice_template) -> None:
    (resource,) = voice_template.find_resources("Custom::ChimeVoiceConnector").values()
    create = json.loads(_flatten_create_payload(resource["Properties"]["Create"]))
    parameters = create["parameters"]
    assert parameters["RequireEncryption"] is True
    assert {"Key": "project", "Value": "outbound-ivr"} in parameters["Tags"]


def test_voice_connector_on_delete_calls_delete_voice_connector(voice_template) -> None:
    (resource,) = voice_template.find_resources("Custom::ChimeVoiceConnector").values()
    delete = json.loads(_flatten_create_payload(resource["Properties"]["Delete"]))
    assert delete["service"] == "ChimeSDKVoice"
    assert delete["action"] == "deleteVoiceConnector"
    # The existing connector's own physical id, not a literal.
    assert delete["parameters"]["VoiceConnectorId"] == "PHYSICAL:RESOURCEID:"


def test_voice_connector_on_update_calls_update_not_create(voice_template) -> None:
    # CreateVoiceConnector is a POST that mints a brand-new VoiceConnectorId
    # every call -- re-running it on update (instead of UpdateVoiceConnector)
    # would silently create a second, duplicate connector. See
    # stacks/voice_stack.py's module docstring.
    (resource,) = voice_template.find_resources("Custom::ChimeVoiceConnector").values()
    update = json.loads(_flatten_create_payload(resource["Properties"]["Update"]))
    assert update["service"] == "ChimeSDKVoice"
    assert update["action"] == "updateVoiceConnector"
    parameters = update["parameters"]
    assert parameters["RequireEncryption"] is True
    assert parameters["Name"] == "outbound-ivr-vc"
    # The existing connector's own physical id (AwsCustomResource's runtime
    # placeholder, substituted by the framework Lambda at invocation time
    # from the real custom resource event -- never a new connector), not a
    # literal id and never a second CreateVoiceConnector call.
    assert parameters["VoiceConnectorId"] == "PHYSICAL:RESOURCEID:"

    create = json.loads(_flatten_create_payload(resource["Properties"]["Create"]))
    assert create["action"] == "createVoiceConnector"


def test_termination_allows_only_the_two_pool_eips(voice_template) -> None:
    (eip0_id, eip1_id) = voice_template.find_resources("AWS::EC2::EIP").keys()
    (resource,) = voice_template.find_resources("Custom::ChimeVoiceConnectorTermination").values()
    create_raw = resource["Properties"]["Create"]
    parts = create_raw["Fn::Join"][1]
    assert {"Fn::GetAtt": [eip0_id, "PublicIp"]} in parts, (
        "slot 0's EIP must be in the allowed list"
    )
    assert {"Fn::GetAtt": [eip1_id, "PublicIp"]} in parts, (
        "slot 1's EIP must be in the allowed list"
    )

    create = json.loads(_flatten_create_payload(create_raw))
    termination = create["parameters"]["Termination"]
    assert termination["CallingRegions"] == ["US"]
    assert termination["CpsLimit"] == 1
    assert termination["Disabled"] is False
    assert termination["CidrAllowedList"] == ["<token>/32", "<token>/32"], (
        "exactly two hosts (the pool's two EIPs), each as a /32"
    )
    # No on_delete at all: deleting the Voice Connector itself (this resource's
    # dependency) already removes its termination settings -- see
    # stacks/voice_stack.py's module docstring.
    assert "Delete" not in resource["Properties"]


def test_no_resolved_secret_value_in_any_custom_resource(voice_template) -> None:
    # A resolved "{{resolve:secretsmanager...}}" dynamic
    # reference inside a Custom::* resource's own properties would end up in
    # plaintext in CloudWatch Logs the moment this stack is deployed.
    resources = voice_template.to_json()["Resources"]
    for logical_id, resource in resources.items():
        if not resource.get("Type", "").startswith("Custom::"):
            continue
        rendered = json.dumps(resource.get("Properties", {}))
        assert "{{resolve:secretsmanager" not in rendered, (
            f"{logical_id} ({resource['Type']}) carries a resolved secret value "
            "in its own properties"
        )


def test_chime_iam_statements_scoped_except_create(voice_template) -> None:
    # Only chime:CreateVoiceConnector may use a bare "*" resource; every
    # other Chime action must be scoped to the vc/* resource-type ARN, never
    # a full wildcard -- see stacks/voice_stack.py's module docstring.
    policies = voice_template.find_resources("AWS::IAM::Policy")
    for logical_id, policy in policies.items():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            chime_actions = [a for a in actions if isinstance(a, str) and a.startswith("chime:")]
            if not chime_actions:
                continue
            resources = list(_iter_resource_strings(statement["Resource"]))
            if chime_actions == ["chime:CreateVoiceConnector"]:
                assert resources == ["*"], f"{logical_id}: {chime_actions}"
            else:
                for action in chime_actions:
                    assert action != "chime:CreateVoiceConnector", (
                        f"{logical_id}: CreateVoiceConnector must stand alone, "
                        "not share a statement with another Chime action"
                    )
                for resource in resources:
                    assert resource != "*", f"{logical_id}: {chime_actions} grants a bare '*'"
                    assert resource == CHIME_VOICE_CONNECTOR_ARN_PATTERN, (
                        f"{logical_id}: {chime_actions} resource {resource!r} is not "
                        f"scoped to {CHIME_VOICE_CONNECTOR_ARN_PATTERN}"
                    )


def test_publishes_every_contract_ssm_parameter(voice_template) -> None:
    expected_names = {
        PARAM_CONNECTOR_ID,
        PARAM_OUTBOUND_HOST,
        PARAM_CREDENTIALS_SECRET_ARN,
        eip_allocation_id_param(0),
        eip_public_ip_param(0),
        eip_allocation_id_param(1),
        eip_public_ip_param(1),
    }
    params = voice_template.find_resources("AWS::SSM::Parameter")
    names = {p["Properties"]["Name"] for p in params.values()}
    assert names == expected_names
    for param in params.values():
        assert param["Properties"]["Tags"] == {"project": "outbound-ivr"}

    (eip0_id, eip1_id) = voice_template.find_resources("AWS::EC2::EIP").keys()
    by_name = {p["Properties"]["Name"]: p for p in params.values()}
    assert by_name[eip_allocation_id_param(0)]["Properties"]["Value"] == {
        "Fn::GetAtt": [eip0_id, "AllocationId"]
    }
    assert by_name[eip_public_ip_param(0)]["Properties"]["Value"] == {
        "Fn::GetAtt": [eip0_id, "PublicIp"]
    }
    assert by_name[eip_allocation_id_param(1)]["Properties"]["Value"] == {
        "Fn::GetAtt": [eip1_id, "AllocationId"]
    }
    assert by_name[eip_public_ip_param(1)]["Properties"]["Value"] == {
        "Fn::GetAtt": [eip1_id, "PublicIp"]
    }


def test_credentials_secret_arn_parameter_carries_no_secret_value(voice_template) -> None:
    # The SSM parameter must carry the secret's ARN (an identifier), never
    # its value -- the handler Lambda is the only thing that ever reads
    # the value, and only at invocation time.
    params = voice_template.find_resources("AWS::SSM::Parameter")
    (param,) = (
        p for p in params.values() if p["Properties"]["Name"] == PARAM_CREDENTIALS_SECRET_ARN
    )
    value = param["Properties"]["Value"]
    rendered = json.dumps(value)
    assert "{{resolve:secretsmanager" not in rendered

    (secret_id,) = voice_template.find_resources("AWS::SecretsManager::Secret").keys()
    # Secret.secret_arn on a plain Secret resource synthesizes as a bare Ref
    # (CloudFormation's Ref for AWS::SecretsManager::Secret returns the ARN)
    # -- never a resolved value, just an identifier pointing back at the
    # secret.
    assert value == {"Ref": secret_id}


def test_credentials_secret_generated_not_hardcoded(voice_template) -> None:
    (secret,) = voice_template.find_resources("AWS::SecretsManager::Secret").values()
    generator = secret["Properties"]["GenerateSecretString"]
    assert generator["ExcludePunctuation"] is True
    assert generator["PasswordLength"] >= 32
    assert {"Key": "project", "Value": "outbound-ivr"} in secret["Properties"]["Tags"]


def test_everything_taggable_carries_the_project_tag(voice_template) -> None:
    tag_bearing_types = ("AWS::EC2::EIP", "AWS::SecretsManager::Secret")
    for resource_type in tag_bearing_types:
        for resource in voice_template.find_resources(resource_type).values():
            tags = {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}
            assert tags.get("project") == "outbound-ivr", f"{resource_type} missing project tag"

    for resource in voice_template.find_resources("AWS::SSM::Parameter").values():
        assert resource["Properties"]["Tags"].get("project") == "outbound-ivr"


def test_the_project_tag_is_frozen_at_its_deployed_value(voice_template) -> None:
    """The project tag rides inside the createVoiceConnector custom resource's
    parameters. Changing the tag changes that call, which CloudFormation treats as a
    possible replacement of the Voice Connector itself -- see stacks/voice_stack.py's
    VOICE_PROJECT_TAG comment. This pins it."""
    from stacks import voice_stack

    assert voice_stack.VOICE_PROJECT_TAG == "outbound-ivr"
    import json

    assert '{"Key": "project", "Value": "outbound_ivr"}' not in json.dumps(voice_template.to_json())
