"""Pipeline stack — Kinesis ingest (#8) + DynamoDB Streams enrichment (#9).

Kept separate from CoreStack (which owns the data resources) and ScraperStack
(which owns the producers) so the consumer side has a clear home.
"""

import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_iam as iam,
    aws_kinesis as kinesis,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

_FUNCTIONS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "functions"))
_ENRICH_BUILD = os.path.join(_FUNCTIONS_DIR, "enrich")


class PipelineStack(Stack):
    """Issues #8 + #9 — Kinesis consumer + DynamoDB Streams enrichment Lambdas."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        fire_stream: kinesis.Stream,
        fires_table: dynamodb.Table,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        self._provision_ingest(fire_stream, fires_table)
        self._provision_enrich(fires_table)

    # ------------------------------------------------------------------
    # Issue #8 — Kinesis → DynamoDB
    # ------------------------------------------------------------------

    def _provision_ingest(self, fire_stream: kinesis.Stream, fires_table: dynamodb.Table):
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

    # ------------------------------------------------------------------
    # Issue #9 — DynamoDB Streams → enrichment (NOAA + SageMaker + USGS)
    # ------------------------------------------------------------------

    def _provision_enrich(self, fires_table: dynamodb.Table):
        # The handler imports `noaa_poller` from the scraper package; the
        # build script (scripts/build_enrich.sh) bundles them together flat
        # so Lambda's default sys.path picks both up.
        enrich_fn = lambda_.Function(
            self, "EnrichLambda",
            function_name="wildfire-watch-enrich",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_ENRICH_BUILD),
            # SageMaker invoke + NOAA HTTP + USGS HTTP serially — bound by the
            # slowest external call rather than CPU. 60s gives plenty of slack
            # for cold-start NOAA gridpoint resolution (two HTTP hops).
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={
                "WW_DYNAMODB_FIRES_TABLE": fires_table.table_name,
                # SageMaker endpoint defaults already match the deployed names
                # (wildfire-watch-spread + -area). Leaving env unset would also
                # work; setting them explicitly so a rename in #13 doesn't
                # silently break enrichment.
                "WW_SAGEMAKER_SPREAD_ENDPOINT": "wildfire-watch-spread",
                "WW_SAGEMAKER_AREA_ENDPOINT": "wildfire-watch-area",
                "WW_CONFIDENCE_THRESHOLD": "0.65",
            },
        )

        # Read+write because (a) the handler patches enriched fields back onto
        # the fire row via update_item, and (b) noaa_poller caches its weather
        # responses in the same table under NOAA_CACHE#<lat,lon> keys.
        fires_table.grant_read_write_data(enrich_fn)

        # SageMaker invoke for both regression endpoints (spread + area).
        enrich_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sagemaker:InvokeEndpoint"],
            resources=[
                f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/wildfire-watch-spread",
                f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/wildfire-watch-area",
            ],
        ))

        # FireEnriched events feed the dispatch trigger (#10) via EventBridge.
        enrich_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"],
            resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
        ))

        # DynamoDB Streams trigger. INSERT-only filter happens inside the
        # handler (the handler also skips NOAA cache + CAL FIRE state rows by
        # fire_id prefix). Small batch + bisect_on_error keeps a poison record
        # from blocking the shard for the whole 24h stream retention.
        enrich_fn.add_event_source(lambda_events.DynamoEventSource(
            fires_table,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=5,
            max_batching_window=Duration.seconds(2),
            retry_attempts=2,
            bisect_batch_on_error=True,
        ))

        cdk.CfnOutput(self, "EnrichLambdaArn",
            value=enrich_fn.function_arn,
            export_name="WildfireWatch::Pipeline::EnrichLambdaArn",
        )

        self.enrich_fn = enrich_fn
