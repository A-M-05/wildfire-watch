# Pipeline Agent

**Owns:** Kinesis consumer, enrichment, EventBridge dispatch trigger
**Issues:** #8 (normalize), #9 (enrich), #10 (EventBridge rule)

## Responsibilities

- Write the Kinesis consumer Lambda that normalizes all fire events to the schema in `CLAUDE.md`
- Write the enrichment Lambda that adds risk score, weather, watershed, population data
- Wire the EventBridge rule that triggers dispatch when a fire crosses thresholds

## File layout

```
functions/
├── ingest/
│   ├── handler.py         ← Issue #8: Kinesis consumer + normalizer
│   └── requirements.txt
├── enrich/
│   ├── handler.py         ← Issue #9: enrichment Lambda
│   └── requirements.txt
└── dispatch/
    ├── handler.py         ← Issue #10: EventBridge rule target
    └── requirements.txt
```

## Issue #8 — Kinesis normalizer

Input: raw records from FIRMS or CAL FIRE (different schemas)
Output: normalized fire event (see CLAUDE.md schema) written to DynamoDB `fires` table + Timestream

```python
# Handler signature
def handler(event, context):
    for record in event['Records']:
        raw = json.loads(base64.b64decode(record['kinesis']['data']))
        normalized = normalize(raw)           # source-specific parsing
        write_to_dynamodb(normalized)
        write_to_timestream(normalized)
```

## Issue #9 — Enrichment Lambda

Triggered by DynamoDB Streams on the `fires` table (INSERT events only).
Calls:
1. NOAA poller (`functions/scraper/noaa_poller.py`) for wind data
2. SageMaker endpoint for dispatch confidence score
3. Location Service for nearest fire stations
4. USGS endpoint for watershed sites within 50km
5. Census API (cached) for population at risk

Writes enriched event back to DynamoDB `fires` table and emits to EventBridge.

**SageMaker call:**
```python
import boto3
sm = boto3.client('sagemaker-runtime')
response = sm.invoke_endpoint(
    EndpointName=os.environ['WW_SAGEMAKER_ENDPOINT'],
    ContentType='application/json',
    Body=json.dumps(features)
)
```

## Issue #10 — EventBridge rule

Rule pattern:
```json
{
  "source": ["wildfire-watch.enrichment"],
  "detail-type": ["FireEnriched"],
  "detail": {
    "risk_score": [{"numeric": [">=", 0.6]}]
  }
}
```

Target: Step Functions state machine (safety gate, issue #19)
Also emits to SNS for downstream consumers.

## Threshold for triggering dispatch

- `risk_score >= 0.6` OR
- `spread_rate_km2_per_hr >= 2.0` OR
- `population_at_risk >= 500`

## Verification

```bash
# Push a fake fire event and watch it flow through
aws kinesis put-record \
  --stream-name wildfire-watch-fire-events \
  --partition-key test \
  --data '{"source":"FIRMS","lat":34.2,"lon":-118.5,"radiative_power":45.2,"confidence":0.9,"detected_at":"2026-04-17T10:00:00Z"}'

# Then check DynamoDB
aws dynamodb scan --table-name fires --limit 5
```
