import os

from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    Tags,
    Duration,
    aws_kinesis as kinesis,
    aws_dynamodb as dynamodb,
    aws_iot as iot,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_iam as iam,
)
from constructs import Construct
import os

TAGS = {"Project": "wildfire-watch", "Env": "hackathon"}
_FUNCTIONS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "functions"))

_FUNCTIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "functions")
)


class CoreStack(Stack):
    """Issue #1 — Kinesis, DynamoDB, Timestream, IoT Core, EventBridge skeleton."""

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        for k, v in TAGS.items():
            Tags.of(self).add(k, v)

        self._provision_kinesis()
        self._provision_dynamodb()
        self._provision_iot()
        self._provision_eventbridge()
        self._provision_dispatch_lambda()

    # ------------------------------------------------------------------
    # Kinesis
    # ------------------------------------------------------------------

    def _provision_kinesis(self):
        self.fire_stream = kinesis.Stream(
            self, "FireStream",
            stream_name="wildfire-watch-fire-events",
            shard_count=2,
            retention_period=None,  # default 24h
        )

        CfnOutput(self, "FireStreamArn",
            value=self.fire_stream.stream_arn,
            export_name="WildfireWatch::Core::FireStreamArn",
        )
        CfnOutput(self, "FireStreamName",
            value=self.fire_stream.stream_name,
            export_name="WildfireWatch::Core::FireStreamName",
        )

    # ------------------------------------------------------------------
    # DynamoDB
    # ------------------------------------------------------------------

    def _provision_dynamodb(self):
        self.fires_table = dynamodb.Table(
            self, "FiresTable",
            table_name="fires",
            partition_key=dynamodb.Attribute(name="fire_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="detected_at", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            time_to_live_attribute="ttl",
        )

        # GSI for querying active fires by source (FIRMS | CALFIRE)
        self.fires_table.add_global_secondary_index(
            index_name="source-detected_at-index",
            partition_key=dynamodb.Attribute(name="source", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="detected_at", type=dynamodb.AttributeType.STRING),
        )

        self.resources_table = dynamodb.Table(
            self, "ResourcesTable",
            table_name="resources",
            partition_key=dynamodb.Attribute(name="station_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="resource_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.residents_table = dynamodb.Table(
            self, "ResidentsTable",
            table_name="residents",
            partition_key=dynamodb.Attribute(name="resident_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.alerts_table = dynamodb.Table(
            self, "AlertsTable",
            table_name="alerts",
            partition_key=dynamodb.Attribute(name="alert_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="fired_at", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI on alerts to query by fire_id
        self.alerts_table.add_global_secondary_index(
            index_name="fire_id-fired_at-index",
            partition_key=dynamodb.Attribute(name="fire_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="fired_at", type=dynamodb.AttributeType.STRING),
        )

        for logical_id, table, env_var in [
            ("FiresTableName", self.fires_table, "WW_DYNAMODB_FIRES_TABLE"),
            ("ResourcesTableName", self.resources_table, "WW_DYNAMODB_RESOURCES_TABLE"),
            ("ResidentsTableName", self.residents_table, "WW_DYNAMODB_RESIDENTS_TABLE"),
            ("AlertsTableName", self.alerts_table, "WW_DYNAMODB_ALERTS_TABLE"),
        ]:
            CfnOutput(self, logical_id,
                value=table.table_name,
                export_name=f"WildfireWatch::Core::{logical_id}",
                description=f"Env var: {env_var}",
            )

    # ------------------------------------------------------------------
    # IoT Core
    # ------------------------------------------------------------------

    def _provision_iot(self):
        self.sensor_thing_type = iot.CfnThingType(
            self, "SensorThingType",
            thing_type_name="wildfire-watch-sensor",
            thing_type_properties=iot.CfnThingType.ThingTypePropertiesProperty(
                thing_type_description="Wildfire IoT sensor device type",
                searchable_attributes=["location", "sensor_type"],
            ),
        )

        self.sensor_policy = iot.CfnPolicy(
            self, "SensorPolicy",
            policy_name="wildfire-watch-sensor-policy",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Connect"],
                        "Resource": [
                            f"arn:aws:iot:{self.region}:{self.account}:client/${{iot:Connection.Thing.ThingName}}"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Publish"],
                        "Resource": [
                            f"arn:aws:iot:{self.region}:{self.account}:topic/wildfire-watch/sensors/*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Subscribe"],
                        "Resource": [
                            f"arn:aws:iot:{self.region}:{self.account}:topicfilter/wildfire-watch/sensors/*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Receive"],
                        "Resource": [
                            f"arn:aws:iot:{self.region}:{self.account}:topic/wildfire-watch/sensors/*"
                        ],
                    },
                ],
            },
        )

    # ------------------------------------------------------------------
    # EventBridge rule skeleton (target wired in issue #10)
    # ------------------------------------------------------------------

    def _provision_eventbridge(self):
        self.fire_threshold_rule = events.Rule(
            self, "FireThresholdRule",
            rule_name="wildfire-watch-fire-threshold",
            description="Fires when enriched event risk_score >= 0.6 — target added in issue #10",
            event_pattern=events.EventPattern(
                source=["wildfire-watch.enrichment"],
                detail_type=["FireEnriched"],
            ),
            enabled=False,  # enabled once target is attached in #10
        )

        CfnOutput(self, "FireThresholdRuleArn",
            value=self.fire_threshold_rule.rule_arn,
            export_name="WildfireWatch::Core::FireThresholdRuleArn",
        )

    # ------------------------------------------------------------------
    # Dispatch trigger Lambda (issue #10)
    # ------------------------------------------------------------------

    def _provision_dispatch_lambda(self):
        # The Lambda evaluates all three OR-thresholds and starts Step Functions.
        # It receives the full enriched fire event from EventBridge so no extra
        # DynamoDB reads are needed at dispatch time.
        self.dispatch_fn = lambda_.Function(
            self, "DispatchTriggerLambda",
            function_name="wildfire-watch-dispatch-trigger",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_FUNCTIONS_DIR, "dispatch")),
            timeout=Duration.seconds(30),
            environment={
                # WW_STEP_FUNCTIONS_ARN is set post-deploy from SafetyStack output.
                # Using empty default so synth doesn't fail before safety stack is deployed.
                "WW_STEP_FUNCTIONS_ARN": "",
                "WW_RISK_SCORE_TRIGGER": "0.6",
                "WW_SPREAD_RATE_TRIGGER": "2.0",
                "WW_POPULATION_TRIGGER": "500",
            },
        )

        # Allow this Lambda to start Step Functions executions on any
        # wildfire-watch state machine (the safety gate machine from #3).
        self.dispatch_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:StartExecution"],
            resources=[f"arn:aws:states:{self.region}:{self.account}:stateMachine:wildfire-watch-*"],
        ))

        # Wire the existing EventBridge rule (created in #1) to this Lambda target
        # and activate it. The rule matches all FireEnriched events — the Lambda
        # does the threshold check so all three OR-conditions can be evaluated.
        self.fire_threshold_rule.add_target(targets.LambdaFunction(self.dispatch_fn))
        self.fire_threshold_rule.node.default_child.enabled = True

        CfnOutput(self, "DispatchTriggerLambdaArn",
            value=self.dispatch_fn.function_arn,
            export_name="WildfireWatch::Core::DispatchTriggerLambdaArn",
        )
