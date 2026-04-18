"""Issue #6 — FIRMS poller Lambda + EventBridge 3h schedule."""

import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
    aws_ssm as ssm,
    aws_kinesis as kinesis,
)
from constructs import Construct

# Pre-built asset dir — populated by scripts/build_scraper.sh before cdk deploy
_SCRAPER_BUILD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../functions/scraper/build")
)


class ScraperStack(Stack):
    """Issue #6 — NASA FIRMS poller triggered every 3h by EventBridge."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        fire_stream: kinesis.Stream,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        firms_map_key = ssm.StringParameter.value_for_string_parameter(
            self, "/wildfire-watch/firms-map-key"
        )

        firms_fn = lambda_.Function(
            self, "FirmsPoller",
            function_name="wildfire-watch-firms-poller",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="firms_poller.handler",
            code=lambda_.Code.from_asset(_SCRAPER_BUILD),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "WW_FIRMS_MAP_KEY": firms_map_key,
                "WW_KINESIS_STREAM_NAME": fire_stream.stream_name,
            },
        )

        fire_stream.grant_write(firms_fn)

        rule = events.Rule(
            self, "FirmsPollSchedule",
            rule_name="wildfire-watch-firms-poll",
            description="Trigger FIRMS poller every 3 hours",
            schedule=events.Schedule.rate(Duration.hours(3)),
        )
        rule.add_target(targets.LambdaFunction(firms_fn))

        cdk.CfnOutput(self, "FirmsPollLambdaArn",
            value=firms_fn.function_arn,
            export_name="WildfireWatch::Scraper::FirmsPollLambdaArn",
        )
