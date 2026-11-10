"""outbound-ivr-ops: the alarms that tell someone this stack has stopped working.

Without these alarms, the first signal that the one LiveKit host had gone, or that
the Elastic IP never got re-associated, or that the tools Lambda was failing every
write, would be a dead phone call that somebody happened to notice. A nested stack
of `OutboundIvrStack`, so it ships with everything it watches -- an alarm in a
separate stack nobody deploys would be worse than no alarm, because it would read
as covered.

**Subscribing is opt-in.** `-c alertEmail=you@example.com` adds an email
subscription (AWS then sends a confirmation link that has to be clicked, once).
Without it the topic and the alarms still exist and still go to ALARM in the
console, which is most of the value: the point is that the state is visible and
recorded, not that it necessarily pages.

The topic is encrypted with the project's own customer-managed key rather than
`alias/aws/sns`, because the AWS-managed SNS key does not let CloudWatch publish
to a topic at all -- a CMK whose policy grants `cloudwatch.amazonaws.com` is the
documented way to have both encryption and working alarms.
"""

from __future__ import annotations

from aws_cdk import Annotations, Duration, NestedStack, Tags
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from cdk_nag import NagSuppressions
from constructs import Construct

from .constants import PROJECT_TAG, RESOURCE_PREFIX

# Container Insights publishes the task count here. The namespace is the same at
# ENABLED as at ENHANCED; enhanced only adds per-container metrics, which is why
# livekit_stack.py was able to drop back to ENABLED without losing this alarm.
CONTAINER_INSIGHTS_NAMESPACE = "ECS/ContainerInsights"


class OpsNestedStack(NestedStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        kms_key: kms.IKey,
        cluster_name: str,
        service_name: str,
        host_group_name: str,
        tables: dict[str, dynamodb.ITable],
        functions: dict[str, _lambda.IFunction],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name=f"{RESOURCE_PREFIX}-alerts",
            display_name="outbound_ivr alarms",
            master_key=kms_key,
        )
        # CloudWatch has to be able to use the key to publish an encrypted
        # message, or every alarm action fails silently.
        kms_key.grant_encrypt_decrypt(iam.ServicePrincipal("cloudwatch.amazonaws.com"))
        # Creating ANY explicit resource policy replaces the broad default SNS
        # gives a new topic, so without this Allow the first alarm to fire fails
        # its publish with an authorization error visible only in that alarm's own
        # History tab, and -c alertEmail delivers nothing.
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsToPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:cloudwatch:{self.region}:{self.account}:alarm:{RESOURCE_PREFIX}-*"
                    },
                },
            )
        )
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            self.topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        else:
            Annotations.of(self).add_warning(
                "alertEmail context is not set: the alarms below will go to ALARM in "
                "the console and notify nobody. Pass -c alertEmail=you@example.com to "
                "get an email (AWS sends one confirmation link to click)."
            )

        action = cw_actions.SnsAction(self.topic)
        self.alarms: list[cloudwatch.Alarm] = []

        def alarm(
            construct_id_: str,
            *,
            metric: cloudwatch.IMetric,
            description: str,
            threshold: float,
            comparison: cloudwatch.ComparisonOperator,
            evaluation_periods: int = 2,
            treat_missing_data: cloudwatch.TreatMissingData = cloudwatch.TreatMissingData.MISSING,
        ) -> None:
            created = cloudwatch.Alarm(
                self,
                construct_id_,
                alarm_name=f"{RESOURCE_PREFIX}-{construct_id_}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                comparison_operator=comparison,
                evaluation_periods=evaluation_periods,
                treat_missing_data=treat_missing_data,
            )
            created.add_alarm_action(action)
            self.alarms.append(created)

        # ------------------------------------------------------------------
        # The media plane. One host, one task, and losing either drops every
        # call in progress and refuses new ones until the ASG relaunches and
        # the EIP/DNS Lambda catches up.
        # ------------------------------------------------------------------
        alarm(
            "livekit-task-gone",
            metric=cloudwatch.Metric(
                namespace=CONTAINER_INSIGHTS_NAMESPACE,
                metric_name="RunningTaskCount",
                dimensions_map={"ClusterName": cluster_name, "ServiceName": service_name},
                statistic="Minimum",
                period=Duration.minutes(1),
            ),
            description=(
                "The LiveKit task is not running. Every call in progress is already "
                "dropped and no new call can be placed."
            ),
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            # No data here means nothing is reporting, which is the failure, not
            # the absence of one.
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        alarm(
            "livekit-host-gone",
            metric=cloudwatch.Metric(
                namespace="AWS/AutoScaling",
                metric_name="GroupInServiceInstances",
                dimensions_map={"AutoScalingGroupName": host_group_name},
                statistic="Minimum",
                period=Duration.minutes(1),
            ),
            description=(
                "The LiveKit host is out of service. Distinct from the task alarm on "
                "purpose: a task that will not start on a healthy host is a different "
                "problem from a host that is gone."
            ),
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )

        # ------------------------------------------------------------------
        # Everything that writes during a live call. A throttle or an
        # unhandled error here is a call that loses its evidence while the
        # caller is still on the line, and the agent will not say so.
        # ------------------------------------------------------------------
        for label, table in tables.items():
            alarm(
                f"{label}-throttled",
                metric=cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ThrottledRequests",
                    dimensions_map={"TableName": table.table_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                description=(
                    f"The {label} table is throttling. On-demand tables throttle while "
                    "they scale, so this is the warning that a burst outran it."
                ),
                threshold=1,
                comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
            )

        for label, function in functions.items():
            alarm(
                f"{label}-errors",
                metric=function.metric_errors(statistic="Sum", period=Duration.minutes(5)),
                description=(
                    f"The {label} Lambda is erroring. For the tools Lambda that means "
                    "an agent's writes are failing mid-call; for the lookup Lambda it "
                    "means handoffs fall back to no whispered summary; for the API "
                    "Lambda it means the dashboard is blind."
                ),
                threshold=1,
                comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
            )

        NagSuppressions.add_resource_suppressions(
            self.topic,
            [
                {
                    "id": "AwsSolutions-SNS2",
                    "reason": (
                        "The topic IS encrypted, with the project's customer-managed "
                        "key (master_key above). This rule reads the generated "
                        "KmsMasterKeyId as unset when the key arrives as a nested "
                        "stack parameter reference."
                    ),
                }
            ],
            apply_to_children=True,
        )

        Tags.of(self).add("project", PROJECT_TAG)
