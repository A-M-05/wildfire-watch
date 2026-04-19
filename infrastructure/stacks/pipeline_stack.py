"""Pipeline stack — Kinesis → DynamoDB ingest Lambda (#8).

Kept separate from CoreStack (which owns the data resources) and ScraperStack
(which owns the producers) so the consumer side has a clear home. The
enrichment Lambda (#9) will land here too once it's deployed.
"""

import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_kinesis as kinesis,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

_FUNCTIONS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "functions"))


class PipelineStack(Stack):
    """Issue #8 — Kinesis consumer Lambda that writes fires to DynamoDB."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        fire_stream: kinesis.Stream,
        fires_table: dynamodb.Table,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        ingest_fn = lambda_.Function(
            self, "IngestLambda",
            function_name="wildfire-watch-ingest",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(_FUNCTIONS_DIR, "ingest")),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "WW_DYNAMODB_FIRES_TABLE": fires_table.table_name,
            },
        )

        fires_table.grant_write_data(ingest_fn)

        # LATEST so a redeploy doesn't replay 24h of stale records. batch_size=10
        # keeps each invocation small enough that a poison message only blocks a
        # tiny window. retry_attempts=2 — we don't want to wedge the shard on a
        # malformed payload, and ingest is idempotent (put_item by primary key).
        ingest_fn.add_event_source(lambda_events.KinesisEventSource(
            fire_stream,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=10,
            max_batching_window=Duration.seconds(2),
            retry_attempts=2,
        ))

        cdk.CfnOutput(self, "IngestLambdaArn",
            value=ingest_fn.function_arn,
            export_name="WildfireWatch::Pipeline::IngestLambdaArn",
        )

        self.ingest_fn = ingest_fn
