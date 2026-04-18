# Skill: AWS Infrastructure (CDK)

Read this before writing any CDK stack.

## Project conventions

- Language: Python CDK (`aws-cdk-lib`)
- All stacks inherit from `cdk.Stack`
- Resource names: `wildfire-watch-<resource>` (kebab-case)
- Env var exports: `WW_<RESOURCE>` (screaming snake)
- Tags: `{"Project": "wildfire-watch", "Env": "hackathon"}` on every construct
- Removal policy: `RemovalPolicy.DESTROY` (hackathon — not prod)

## Stack pattern

```python
# infrastructure/stacks/core_stack.py
from aws_cdk import Stack, RemovalPolicy, CfnOutput
from aws_cdk import aws_kinesis as kinesis
from constructs import Construct

class CoreStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.fire_stream = kinesis.Stream(
            self, "FireStream",
            stream_name="wildfire-watch-fire-events",
            shard_count=2
        )

        CfnOutput(self, "FireStreamArn",
            value=self.fire_stream.stream_arn,
            export_name="WildfireWatch::Core::FireStreamArn"
        )
```

## Cross-stack references

Import from another stack's export:
```python
stream_arn = Fn.import_value("WildfireWatch::Core::FireStreamArn")
```

Or pass as constructor param (preferred for same-app stacks):
```python
# In app.py
core = CoreStack(app, "WildfireWatchCore")
ml = MLStack(app, "WildfireWatchML", fire_stream=core.fire_stream)
```

## DynamoDB pattern

```python
from aws_cdk import aws_dynamodb as dynamodb

table = dynamodb.Table(self, "FiresTable",
    table_name="fires",
    partition_key=dynamodb.Attribute(name="fire_id", type=dynamodb.AttributeType.STRING),
    sort_key=dynamodb.Attribute(name="detected_at", type=dynamodb.AttributeType.STRING),
    billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
    removal_policy=RemovalPolicy.DESTROY,
    stream=dynamodb.StreamViewType.NEW_IMAGE  # needed for enrichment trigger
)
```

## Lambda pattern

```python
from aws_cdk import aws_lambda as lambda_
from aws_cdk import Duration

fn = lambda_.Function(self, "IngestLambda",
    runtime=lambda_.Runtime.PYTHON_3_11,
    handler="handler.handler",
    code=lambda_.Code.from_asset("functions/ingest"),
    timeout=Duration.seconds(30),
    environment={
        "WW_DYNAMODB_FIRES_TABLE": fires_table.table_name,
        "WW_KINESIS_STREAM_ARN": fire_stream.stream_arn,
    }
)
fires_table.grant_write_data(fn)
fire_stream.grant_read(fn)
```

## EventBridge rule pattern

```python
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets

rule = events.Rule(self, "FireThresholdRule",
    event_pattern=events.EventPattern(
        source=["wildfire-watch.enrichment"],
        detail_type=["FireEnriched"],
        detail={"risk_score": [{"numeric": [">=", 0.6]}]}
    )
)
rule.add_target(targets.SfnStateMachine(state_machine))
```

## Deploy commands

```bash
cd infrastructure
pip install -r requirements.txt
cdk bootstrap  # first time only
cdk deploy --all  # deploy all stacks
cdk diff        # preview changes
```
