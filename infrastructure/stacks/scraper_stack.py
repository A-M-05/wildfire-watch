"""Issues #6 and #7 — FIRMS + CAL FIRE poller Lambdas + EventBridge schedules."""

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
    aws_dynamodb as dynamodb,
)
from constructs import Construct

# Pre-built asset dir — populated by scripts/build_scraper.sh before cdk deploy
_SCRAPER_BUILD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../functions/scraper/build")
)


class ScraperStack(Stack):
    """Issues #6 + #7 — FIRMS (3h) and CAL FIRE (10min) pollers via EventBridge."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        fire_stream: kinesis.Stream,
        fires_table: dynamodb.Table,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        firms_map_key = ssm.StringParameter.value_for_string_parameter(
            self, "/wildfire-watch/firms-map-key"
        )

        # --- Issue #6: NASA FIRMS ---
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

        firms_rule = events.Rule(
            self, "FirmsPollSchedule",
            rule_name="wildfire-watch-firms-poll",
            description="Trigger FIRMS poller every 3 hours",
            schedule=events.Schedule.rate(Duration.hours(3)),
        )
        firms_rule.add_target(targets.LambdaFunction(firms_fn))

        cdk.CfnOutput(self, "FirmsPollLambdaArn",
            value=firms_fn.function_arn,
            export_name="WildfireWatch::Scraper::FirmsPollLambdaArn",
        )

        # --- Issue #7: CAL FIRE ---
        calfire_fn = lambda_.Function(
            self, "CalFirePoller",
            function_name="wildfire-watch-calfire-poller",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="calfire_poller.handler",
            code=lambda_.Code.from_asset(_SCRAPER_BUILD),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "WW_KINESIS_STREAM_NAME": fire_stream.stream_name,
                "WW_DYNAMODB_FIRES_TABLE": fires_table.table_name,
            },
        )

        fire_stream.grant_write(calfire_fn)
        fires_table.grant_read_write_data(calfire_fn)

        calfire_rule = events.Rule(
            self, "CalFirePollSchedule",
            rule_name="wildfire-watch-calfire-poll",
            description="Trigger CAL FIRE perimeter poller every 10 minutes",
            schedule=events.Schedule.rate(Duration.minutes(10)),
        )
        calfire_rule.add_target(targets.LambdaFunction(calfire_fn))

        cdk.CfnOutput(self, "CalFirePollLambdaArn",
            value=calfire_fn.function_arn,
            export_name="WildfireWatch::Scraper::CalFirePollLambdaArn",
        )
