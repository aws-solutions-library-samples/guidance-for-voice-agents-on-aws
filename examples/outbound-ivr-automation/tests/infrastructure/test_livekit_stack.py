"""Assertions for outbound-ivr-livekit: the security group ingress is only from the
Chime Voice Connector CIDR or the VPC's own CIDR (never 0.0.0.0/0 or any other
range), ElastiCache has TLS and AUTH on with no public connection path, the ECS
task runs in host network mode with the init container gating livekit-server and
livekit-sip, no secret ever reaches a plain environment variable, the EIP
associator is scoped to exactly the one allocation, no literal phone number
appears anywhere in the template outside the one SSM parameter fed by context
values (and the one documented synthetic placeholder the outbound trunk's
`numbers` field requires), this stack no longer creates or manages any Chime
resource of its own, and every IAM statement this stack writes is scoped, never
a bare wildcard.
"""

from __future__ import annotations

import json
import re

from stacks.constants import CHIME_VOICE_CONNECTOR_CIDR

from infrastructure.conftest import TEST_CELL_NUMBER, TEST_OFFICE_NUMBER

# A conservative E.164-ish pattern: + followed by 8-15 digits.
_PHONE_RE = re.compile(r"\+\d{8,15}")

# The outbound trunk's `numbers` field (livekit-api's SIPOutboundTrunkInfo)
# must be non-empty; this is a synthetic, non-real placeholder, same
# convention as ../../sip_setup.py's own trunk -- never a real number.
_SYNTHETIC_TRUNK_NUMBER = "+15550100100"


def _security_groups(template):
    return template.find_resources("AWS::EC2::SecurityGroup")


def _iter_resource_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_resource_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_resource_strings(item)


def _task_definition(template):
    (td,) = template.find_resources("AWS::ECS::TaskDefinition").values()
    return td


def _containers(template):
    return _task_definition(template)["Properties"]["ContainerDefinitions"]


def _container(template, name):
    (c,) = [c for c in _containers(template) if c["Name"] == name]
    return c


def test_compute_security_group_ingress_is_chime_cidr_or_vpc_cidr_only(livekit_template) -> None:
    groups = _security_groups(livekit_template)
    compute_sgs = {
        k: v
        for k, v in groups.items()
        if v["Properties"].get("GroupDescription", "").startswith("outbound_ivr: livekit-server")
    }
    assert len(compute_sgs) == 1
    (sg,) = compute_sgs.values()
    rules = sg["Properties"].get("SecurityGroupIngress", [])
    assert len(rules) == 5, "expected exactly 5 ingress rules on the compute security group"

    chime_rules = [r for r in rules if r.get("CidrIp") == CHIME_VOICE_CONNECTOR_CIDR]
    assert {(r["FromPort"], r["ToPort"], r["IpProtocol"]) for r in chime_rules} == {
        (5061, 5061, "tcp"),
        (10000, 20000, "udp"),
    }

    vpc_cidr_rules = [r for r in rules if r.get("CidrIp") != CHIME_VOICE_CONNECTOR_CIDR]
    assert len(vpc_cidr_rules) == 3
    for rule in vpc_cidr_rules:
        # Each VPC-internal rule's CidrIp is a cross-nested-stack reference to
        # the VPC's own CIDR (a `Ref` to a parameter the Network nested stack's
        # Outputs feed, CDK's own wiring between sibling NestedStacks -- never
        # a literal CIDR string, and never 0.0.0.0/0).
        cidr = rule["CidrIp"]
        assert isinstance(cidr, dict) and "Ref" in cidr and "CidrBlock" in cidr["Ref"]
    assert {(r["FromPort"], r["ToPort"], r["IpProtocol"]) for r in vpc_cidr_rules} == {
        (7880, 7880, "tcp"),
        (7881, 7881, "tcp"),
        (50000, 50019, "udp"),
    }

    for rule in rules:
        assert rule.get("CidrIp") != "0.0.0.0/0", "no ingress rule may be open to the world"


def test_cache_security_group_allows_only_the_compute_security_group(livekit_template) -> None:
    groups = _security_groups(livekit_template)
    cache_sgs = {
        k: v
        for k, v in groups.items()
        if v["Properties"].get("GroupDescription", "").startswith("outbound_ivr: ElastiCache")
    }
    assert len(cache_sgs) == 1
    (sg,) = cache_sgs.values()
    rules = sg["Properties"].get("SecurityGroupIngress", [])
    assert len(rules) == 1
    (rule,) = rules
    assert rule["FromPort"] == rule["ToPort"] == 6379
    assert "SourceSecurityGroupId" in rule, "must reference the compute SG, not a CIDR"
    assert "CidrIp" not in rule


def test_cache_is_vpc_only_with_auth_and_kms(livekit_template) -> None:
    (cache,) = livekit_template.find_resources("AWS::ElastiCache::ServerlessCache").values()
    props = cache["Properties"]
    assert props["ConnectionType"] == "vpc"
    assert props["Engine"] == "valkey"
    assert "UserGroupId" in props, "no user group means the passwordless built-in default user"
    assert "KmsKeyId" in props, "encryption at rest must use the stack's own CMK, not the default"
    # Minimum metered storage lives in the usage limit, not a free-floating cache:
    # bounding cost is explicit, not accidental.
    assert "CacheUsageLimits" in props


def test_cache_user_requires_a_password(livekit_template) -> None:
    (user,) = livekit_template.find_resources("AWS::ElastiCache::User").values()
    props = user["Properties"]
    assert props["UserName"] == "default", "must be named default to seat it in the user group"
    assert props["UserId"] != "default", "must not be the built-in no-password default user"
    assert props.get("NoPasswordRequired") in (None, False)
    auth_mode = props["AuthenticationMode"]
    assert auth_mode["Type"] == "password"
    assert len(auth_mode["Passwords"]) == 1

    (user_group,) = livekit_template.find_resources("AWS::ElastiCache::UserGroup").values()
    assert user_group["Properties"]["UserIds"] == [props["UserId"]]


def test_task_definition_uses_host_network_mode_on_ec2(livekit_template) -> None:
    td = _task_definition(livekit_template)
    props = td["Properties"]
    assert props["NetworkMode"] == "host"
    assert props["RequiresCompatibilities"] == ["EC2"]


def test_asg_capacity_provider_runs_one_host_in_the_public_subnets(livekit_template) -> None:
    """ECS on EC2, not Managed Instances: Managed Instances deny
    ec2:AssociateAddress, and the host must hold the allowlisted Elastic IP."""
    assert not livekit_template.find_resources(
        "AWS::ECS::CapacityProvider", {"Properties": {"ManagedInstancesProvider": {}}}
    )
    (cp,) = livekit_template.find_resources("AWS::ECS::CapacityProvider").values()
    assert "AutoScalingGroupProvider" in cp["Properties"]
    (asg,) = livekit_template.find_resources("AWS::AutoScaling::AutoScalingGroup").values()
    props = asg["Properties"]
    assert (props["MinSize"], props["MaxSize"]) == ("1", "1")
    assert "LaunchTemplate" in props, "launch configurations cannot be created in new accounts"
    (lt,) = livekit_template.find_resources("AWS::EC2::LaunchTemplate").values()
    data = lt["Properties"]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"
    assert data["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] is True


def test_runtime_reaches_livekit_by_a_private_dns_name_the_lambda_keeps_current(
    livekit_template,
) -> None:
    """Auto Scaling rejects launch templates that pin a private IP, so the host gets a
    private DNS record instead: the EIP Lambda UPSERTs it on every task start and
    deletes it (as a custom resource) on stack deletion, before the zone goes."""
    template = livekit_template.to_json()
    (lt,) = livekit_template.find_resources("AWS::EC2::LaunchTemplate").values()
    assert "NetworkInterfaces" not in lt["Properties"]["LaunchTemplateData"]
    (zone_id, zone) = next(
        iter(livekit_template.find_resources("AWS::Route53::HostedZone").items())
    )
    assert zone["Properties"]["Name"].rstrip(".") == "outbound_ivr.internal"
    assert "VPCs" in zone["Properties"]
    lambdas = livekit_template.find_resources("AWS::Lambda::Function")
    associator = next(f for f in lambdas.values() if "RECORD_NAME" in json.dumps(f))
    env = associator["Properties"]["Environment"]["Variables"]
    assert env["RECORD_NAME"] == "livekit.outbound_ivr.internal"
    code = associator["Properties"]["Code"]["ZipFile"]
    assert "UPSERT" in code and '"DELETE"' in code
    (cleanup,) = [
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::CloudFormation::CustomResource"
    ]
    assert zone_id in json.dumps(cleanup)


def test_service_enables_ecs_exec_and_the_asg_capacity_provider(livekit_template) -> None:
    (service,) = livekit_template.find_resources("AWS::ECS::Service").values()
    props = service["Properties"]
    assert props["EnableExecuteCommand"] is True
    (strategy,) = props["CapacityProviderStrategy"]
    assert strategy["Weight"] == 1
    assert props["DeploymentConfiguration"]["DeploymentCircuitBreaker"]["Enable"] is True


def test_eip_associator_fires_while_the_task_is_pending(livekit_template) -> None:
    """The init container blocks the task in PENDING until the Elastic IP is
    attached, so the associator must not wait for RUNNING."""
    (rule,) = livekit_template.find_resources("AWS::Events::Rule").values()
    assert "PENDING" in rule["Properties"]["EventPattern"]["detail"]["lastStatus"]


def test_init_container_gates_both_livekit_containers_on_success(livekit_template) -> None:
    containers = _containers(livekit_template)
    names = {c["Name"] for c in containers}
    assert {"config-renderer", "livekit-server", "livekit-sip"} <= names

    renderer = _container(livekit_template, "config-renderer")
    assert renderer["Essential"] is False

    for name in ("livekit-server", "livekit-sip"):
        container = _container(livekit_template, name)
        deps = {d["ContainerName"]: d["Condition"] for d in container.get("DependsOn", [])}
        assert deps.get("config-renderer") == "SUCCESS", (
            f"{name} must depend on config-renderer with condition SUCCESS"
        )


def test_livekit_sip_also_waits_for_livekit_server_to_start(livekit_template) -> None:
    sip = _container(livekit_template, "livekit-sip")
    deps = {d["ContainerName"]: d["Condition"] for d in sip.get("DependsOn", [])}
    assert deps.get("livekit-server") == "START"


def test_trunk_setup_container_is_not_essential_and_waits_for_livekit_server(
    livekit_template,
) -> None:
    trunk_setup = _container(livekit_template, "trunk-setup")
    assert trunk_setup["Essential"] is False
    deps = {d["ContainerName"]: d["Condition"] for d in trunk_setup.get("DependsOn", [])}
    assert deps.get("livekit-server") == "START"


def test_init_container_waits_on_the_shared_eip_not_a_literal(livekit_template) -> None:
    renderer = _container(livekit_template, "config-renderer")
    env = {e["Name"]: e["Value"] for e in renderer.get("Environment", [])}
    assert "EXPECTED_PUBLIC_IP" in env
    # The value is a deploy-time SSM dynamic reference (a template parameter
    # resolved from /outbound-ivr/voice/eip/1/public-ip), never a literal IP string.
    assert isinstance(env["EXPECTED_PUBLIC_IP"], dict)

    command = renderer["Command"][0]
    assert "EXPECTED_PUBLIC_IP" in command
    assert "max_attempts" in command and "exit 1" in command, (
        "the init container must time out rather than wait forever"
    )


def test_no_container_carries_a_plaintext_secret_environment_variable(livekit_template) -> None:
    # Every credential (Redis AUTH, the Voice Connector digest credentials, the
    # LiveKit API secret) must arrive as an ECS `secrets` entry, resolved by the
    # execution role at container start -- never a plain `environment` entry,
    # and never a resolved CloudFormation dynamic reference baked into one either.
    secret_like_names = {"PASSWORD", "SECRET", "AUTH", "TOKEN"}
    for container in _containers(livekit_template):
        for entry in container.get("Environment", []):
            name = entry["Name"]
            assert not any(token in name.upper() for token in secret_like_names), (
                f"{container['Name']}'s environment carries a secret-shaped plain variable: {name}"
            )
            value = entry.get("Value")
            if isinstance(value, str):
                assert "{{resolve:secretsmanager" not in value

        secret_names = {s["Name"] for s in container.get("Secrets", [])}
        if container["Name"] == "config-renderer":
            assert secret_names == {"REDIS_PASSWORD", "LIVEKIT_API_SECRET"}
        elif container["Name"] == "trunk-setup":
            assert secret_names == {"LIVEKIT_API_SECRET", "SIP_USERNAME", "SIP_PASSWORD"}
        else:
            assert secret_names == set()


def test_ecs_secrets_reference_secretsmanager_fields_not_values(livekit_template) -> None:
    for container in _containers(livekit_template):
        for secret in container.get("Secrets", []):
            value_from = secret["ValueFrom"]
            # Fn::Join assembling "<secret-arn>:<json-field>::" -- never a
            # bare resolved value.
            assert isinstance(value_from, dict)


def test_eip_associator_scoped_to_the_one_allocation(livekit_template) -> None:
    # This stack must not allocate its own Elastic IP.
    assert livekit_template.find_resources("AWS::EC2::EIP") == {}

    policies = livekit_template.find_resources("AWS::IAM::Policy")
    # Fall back to searching every policy for the ReassociateLiveKitEip sid if
    # naming above doesn't match exactly.
    found_scoped_statement = False
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") == "ReassociateLiveKitEip":
                found_scoped_statement = True
                resources = (
                    statement["Resource"]
                    if isinstance(statement["Resource"], list)
                    else [statement["Resource"]]
                )
                elastic_ip_resources = [
                    r
                    for r in resources
                    if (isinstance(r, str) and ":elastic-ip/" in r)
                    or (isinstance(r, dict) and ":elastic-ip/" in json.dumps(r))
                ]
                assert len(elastic_ip_resources) == 1, (
                    "the associator's ec2:AssociateAddress grant must name exactly "
                    "one elastic-ip resource, not a wildcard across the pool"
                )
    assert found_scoped_statement, "expected a ReassociateLiveKitEip IAM statement"


def test_no_chime_resource_or_iam_action_remains_in_this_stack(livekit_template) -> None:
    # This stack no longer manages a Voice Connector of its own -- see
    # stacks/livekit_stack.py's module docstring. Confirm no trace of one
    # remains: no Custom::Chime* resource, and no chime: IAM action in any
    # policy this stack writes.
    resources = livekit_template.to_json()["Resources"]
    chime_resources = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Type", "").startswith("Custom::Chime")
    ]
    assert chime_resources == []

    for policy in livekit_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            for action in actions:
                assert not (isinstance(action, str) and action.startswith("chime:")), (
                    f"unexpected Chime IAM action left in this stack: {action}"
                )


def test_iam_statements_are_scoped_not_wildcarded_except_documented_exceptions(
    livekit_template,
) -> None:
    # The only bare "*" resources allowed anywhere in this stack's own IAM
    # statements are the ones cdk-nag suppressions document by name (ECS
    # Exec's ssmmessages:* actions, ecr:GetAuthorizationToken,
    # ecs:DescribeContainerInstances/ec2:DescribeInstances, and
    # ec2:AssociateAddress's instance/* target, which cannot be known at
    # synth time because the Auto Scaling group picks the instance), plus the
    # ECS agent's own unscoped actions that CDK's cluster wiring adds to the
    # host role.
    ecs_agent_actions = {
        "ecs:Poll",
        "ecs:StartTelemetrySession",
        "ecs:DiscoverPollEndpoint",
        "ecr:GetAuthorizationToken",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    allowed_wildcard_sids = {
        "EcsExecChannel",
        "EcrAuth",
        "DescribeOwnClusterContainerInstances",
        "DescribeInstancesForPrivateIp",
    }
    for policy in livekit_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            sid = statement.get("Sid")
            for candidate in _iter_resource_strings(statement["Resource"]):
                actions = statement["Action"]
                actions = {actions} if isinstance(actions, str) else set(actions)
                if candidate == "*" and actions <= ecs_agent_actions:
                    continue
                if candidate == "*":
                    assert sid in allowed_wildcard_sids, (
                        f"statement {sid!r} grants a bare '*' resource, not documented as an exception"
                    )
                if isinstance(candidate, str) and candidate.endswith(":instance/*"):
                    assert sid == "ReassociateLiveKitEip"


def test_two_generated_secrets_no_hardcoded_dev_keys(livekit_template) -> None:
    # Just the ElastiCache AUTH token and the LiveKit API key/secret pair now
    # -- the Voice Connector's own SIP digest credentials are OutboundIvrVoiceStack's
    # secret (imported, not generated here).
    secrets = livekit_template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 2
    for secret in secrets.values():
        generator = secret["Properties"]["GenerateSecretString"]
        assert generator["ExcludePunctuation"] is True
        assert generator["PasswordLength"] >= 32


def test_outbound_trunk_requires_tls_and_srtp_from_the_shared_task(livekit_template) -> None:
    trunk_setup = _container(livekit_template, "trunk-setup")
    rendered = trunk_setup["Command"][0]

    assert "SIP_TRANSPORT_TLS" in rendered
    assert "SIP_MEDIA_ENCRYPT_REQUIRE" in rendered

    # The digest credentials are read back out of the environment
    # (os.environ[...]) at runtime, delivered as ECS secrets -- never a
    # resolved secret value baked into the template itself.
    assert 'os.environ["SIP_USERNAME"]' in rendered
    assert 'os.environ["SIP_PASSWORD"]' in rendered
    assert "{{resolve:secretsmanager" not in rendered


def test_no_resolved_secret_value_in_any_custom_resource(livekit_template) -> None:
    # A resolved "{{resolve:secretsmanager...}}" dynamic reference inside a
    # Custom::* resource's own properties would end up in plaintext in
    # CloudWatch Logs the moment this stack is deployed.
    resources = livekit_template.to_json()["Resources"]
    for logical_id, resource in resources.items():
        if not resource.get("Type", "").startswith("Custom::"):
            continue
        rendered = json.dumps(resource.get("Properties", {}))
        assert "{{resolve:secretsmanager" not in rendered, (
            f"{logical_id} ({resource['Type']}) carries a resolved secret "
            "value in its own properties"
        )


def test_everything_taggable_carries_the_project_tag(livekit_template) -> None:
    tag_bearing_types = (
        "AWS::EC2::SecurityGroup",
        "AWS::SecretsManager::Secret",
        "AWS::KMS::Key",
        "AWS::ElastiCache::ServerlessCache",
        "AWS::ECS::Cluster",
    )
    for resource_type in tag_bearing_types:
        for resource in livekit_template.find_resources(resource_type).values():
            tags = {t["Key"]: t["Value"] for t in resource["Properties"].get("Tags", [])}
            assert tags.get("project") == "outbound-ivr", f"{resource_type} missing project tag"


def test_no_phone_number_literal_outside_the_context_fed_ssm_parameter(livekit_template) -> None:
    """The only way a real phone number may reach this template is through
    `-c testCellNumber=... -c officeNumber=...` at synth time, landing in exactly
    one SSM parameter -- plus the one documented synthetic placeholder the
    outbound trunk's `numbers` field requires (never a real number; see
    ../../sip_setup.py's own, identical convention). Guards against a future
    edit hardcoding a real one instead."""
    full_template = json.dumps(livekit_template.to_json())
    matches = set(_PHONE_RE.findall(full_template))
    allowed = {TEST_CELL_NUMBER, TEST_OFFICE_NUMBER, _SYNTHETIC_TRUNK_NUMBER}
    assert matches <= allowed, (
        f"unexpected phone-number-like literal(s) in the template: {matches - allowed}"
    )

    ssm_params = livekit_template.find_resources("AWS::SSM::Parameter")
    destination_params = [
        p
        for p in ssm_params.values()
        if p["Properties"]["Name"] == "/outbound-ivr/test-destination-numbers"
    ]
    (param,) = destination_params
    stored = json.loads(param["Properties"]["Value"])
    assert stored == {"cell": TEST_CELL_NUMBER, "office": TEST_OFFICE_NUMBER}


def test_no_stack_reads_an_ssm_parameter_it_creates(livekit_template) -> None:
    """CloudFormation resolves SSM-typed parameters before creating anything, so a
    stack that reads a parameter it also creates fails on its first deploy
    ("Unable to fetch parameters ... from parameter store")."""
    template = livekit_template.to_json()
    created = {
        r["Properties"]["Name"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter" and isinstance(r["Properties"].get("Name"), str)
    }
    read = {
        p.get("Default")
        for p in template.get("Parameters", {}).values()
        if str(p.get("Type", "")).startswith("AWS::SSM::Parameter::Value")
    }
    assert not created & read, f"reads parameters it creates: {created & read}"


_EC2_DESCRIPTION_CHARS = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]*$")


def test_security_group_descriptions_use_only_characters_ec2_accepts(livekit_template) -> None:
    """EC2 rejects rule and group descriptions with any other character (an
    apostrophe failed the first deploy)."""
    bad = []
    for logical_id, r in livekit_template.to_json()["Resources"].items():
        props = r.get("Properties", {})
        texts = []
        if r["Type"] == "AWS::EC2::SecurityGroup":
            texts.append(props.get("GroupDescription"))
            for key in ("SecurityGroupIngress", "SecurityGroupEgress"):
                texts += [rule.get("Description") for rule in props.get(key) or []]
        elif r["Type"] in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"):
            texts.append(props.get("Description"))
        bad += [
            (logical_id, t)
            for t in texts
            if isinstance(t, str) and not _EC2_DESCRIPTION_CHARS.match(t)
        ]
    assert not bad, bad


def test_rendered_configs_match_what_the_livekit_images_read(livekit_template) -> None:
    """LiveKit's redis block takes use_tls (its tls key is a settings block), and
    the livekit/sip image's entrypoint is fixed to --config=/sip/config.yaml."""
    script = json.dumps(_container(livekit_template, "config-renderer")["Command"])
    assert "use_tls: true" in script
    # The AgentCore Runtime in the isolated subnets can only reach the private IP.
    assert "advertise_internal_ip: true" in script
    # A missed BYE must not leave the call up for two minutes.
    assert "media_timeout: 10s" in script
    assert "\\n  tls: true" not in script
    assert "/shared/config.yaml" in script
    sip = _container(livekit_template, "livekit-sip")
    assert {m["ContainerPath"] for m in sip["MountPoints"]} == {"/sip"}


def test_host_launches_with_the_livekit_security_group(livekit_template) -> None:
    """Without it the host gets the VPC's default group: no Redis (the cache only
    admits the LiveKit group) and no SIP/RTP from Chime."""
    (lt,) = livekit_template.find_resources("AWS::EC2::LaunchTemplate").values()
    groups = lt["Properties"]["LaunchTemplateData"]["SecurityGroupIds"]
    (sg_id,) = [
        k
        for k, r in livekit_template.find_resources("AWS::EC2::SecurityGroup").items()
        if "livekit-server" in r["Properties"]["GroupDescription"]
    ]
    assert {"Fn::GetAtt": [sg_id, "GroupId"]} in groups


# --------------------------------------------------------------------------
# Container health checks. Neither container had one, so a livekit-server that
# hung without exiting failed every call silently: the ASG only checks the EC2
# instance and ECS only notices a process that actually dies.
#
# The commands are pinned here because they are image-specific and verified
# against the images themselves: livekit-server has wget and /bin/sh, no curl
# and no bash; livekit/sip has bash with /dev/tcp but no wget, no curl, no nc,
# and a /bin/sh that is dash. A plausible-looking edit to either one would fail
# every check and have ECS restart the task forever.
# --------------------------------------------------------------------------


def _container(livekit_template, name: str) -> dict:
    (task_def,) = livekit_template.find_resources("AWS::ECS::TaskDefinition").values()
    (container,) = [c for c in task_def["Properties"]["ContainerDefinitions"] if c["Name"] == name]
    return container


def test_livekit_server_health_check_asks_the_http_server(livekit_template) -> None:
    check = _container(livekit_template, "livekit-server")["HealthCheck"]
    assert check["Command"][0] == "CMD-SHELL"
    assert "wget" in check["Command"][1], "curl and bash are not in this image"
    assert "127.0.0.1:7880" in check["Command"][1]
    assert check["Retries"] == 3
    assert check["StartPeriod"] >= 60, "the config sidecar has to finish first"


def test_livekit_sip_health_check_uses_bash_not_the_default_shell(livekit_template) -> None:
    check = _container(livekit_template, "livekit-sip")["HealthCheck"]
    assert check["Command"][0] == "CMD", "CMD-SHELL would run dash, which has no /dev/tcp"
    assert check["Command"][1] == "bash"
    assert "/dev/tcp/127.0.0.1/5061" in check["Command"][3]


def test_both_essential_containers_have_a_health_check(livekit_template) -> None:
    (task_def,) = livekit_template.find_resources("AWS::ECS::TaskDefinition").values()
    for container in task_def["Properties"]["ContainerDefinitions"]:
        if container.get("Essential"):
            assert "HealthCheck" in container, f"{container['Name']} can hang unnoticed"


def test_trunk_setup_pins_livekit_api_to_the_runtimes_version() -> None:
    """The trunk-setup container installs livekit-api at every task start; an unpinned
    install would pull whatever is newest on the index. It must match the version the
    Runtime itself is locked to (the root pyproject.toml's livekit extra)."""
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "stacks" / "livekit_stack.py"
    ).read_text()
    match = re.search(r"pip install --quiet livekit-api==([0-9.]+)\n", source)
    assert match, "livekit-api must be pinned in the trunk-setup script"
    root_pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert f'"livekit-api=={match.group(1)}"' in root_pyproject
