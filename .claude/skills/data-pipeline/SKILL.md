# Skill: Data Pipeline (Lambda + Kinesis + EventBridge)

Read this before writing any Lambda, Kinesis producer/consumer, or EventBridge integration.

## Lambda handler pattern

```python
# functions/<domain>/handler.py
import json, os, boto3, logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    logger.info(f"Processing {len(event.get('Records', [event]))} records")
    # process...
    return {"statusCode": 200}
```

## Kinesis producer pattern

```python
kinesis = boto3.client('kinesis')

def push_to_kinesis(fire_event: dict):
    kinesis.put_record(
        StreamName="wildfire-watch-fire-events",
        Data=json.dumps(fire_event),
        PartitionKey=fire_event['fire_id']  # shard by fire_id for ordering
    )
```

## Kinesis consumer pattern (triggered by Lambda event source mapping)

```python
import base64

def handler(event, context):
    for record in event['Records']:
        payload = json.loads(base64.b64decode(record['kinesis']['data']))
        process(payload)
```

## DynamoDB write pattern

```python
import boto3
from decimal import Decimal

ddb = boto3.resource('dynamodb')
table = ddb.Table(os.environ['WW_DYNAMODB_FIRES_TABLE'])

def write_fire(event: dict):
    # Convert floats to Decimal for DynamoDB
    item = {k: Decimal(str(v)) if isinstance(v, float) else v
            for k, v in event.items()}
    table.put_item(Item=item)
```

## EventBridge custom event pattern

```python
events = boto3.client('events')

def emit_fire_enriched(enriched_event: dict):
    events.put_events(Entries=[{
        'Source': 'wildfire-watch.enrichment',
        'DetailType': 'FireEnriched',
        'Detail': json.dumps(enriched_event),
        'EventBusName': 'default'
    }])
```

## Error handling

- Use `try/except` at the record level, not the batch level — partial batch failures are recoverable
- On unrecoverable error, write to DLQ rather than raising (prevents infinite retry loop)
- Log the fire_id on every operation so CloudWatch logs are searchable

## Environment variables

Always read from `os.environ`, never hardcode. Prefix: `WW_`.

## Local testing

```bash
# Invoke Lambda locally with a test event
python -c "
import json
from functions.ingest.handler import handler
with open('tests/fixtures/firms_raw.json') as f:
    event = {'Records': [{'kinesis': {'data': __import__('base64').b64encode(f.read().encode()).decode()}}]}
print(handler(event, None))
"
```
