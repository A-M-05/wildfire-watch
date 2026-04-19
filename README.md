# Wildfire Watch

A real-time wildfire resource dispatch and community alert system built for two hackathon tracks:

- **Environmental** — live fire monitoring for evacuation prediction
- **AI Safety** — every AI recommendation is audited, validated, and gated before reaching people

**Demo target:** Fire detected → resources dispatched → resident SMS in under 60 seconds.

---

## Architecture

| Layer | Tech |
|---|---|
| Infra | AWS CDK (TypeScript) |
| Backend | Python 3.11 Lambdas |
| ML | SageMaker + Bedrock (`claude-sonnet-4-6`) |
| Frontend | React 18 + Mapbox GL JS |
| Hosting | AWS Amplify |
| DB | DynamoDB (serving + audit hash-chain) + Timestream (time-series) |
| Messaging | Kinesis (pipeline) + SNS (alerts, direct-to-phone) |

### Pipeline

```
FIRMS / CAL FIRE → ingest → enrich (NOAA) → safety gate → dispatch → alert sender → resident SMS
                                                          ↓
                                                   DynamoDB audit
                                                   (SHA-256 hash chain)
```

---

## Project Structure

```
functions/
  ingest/       # Pulls NASA FIRMS + CAL FIRE events onto Kinesis
  enrich/       # Adds weather and population risk data
  safety/       # AI safety gate: Guardrails + Bedrock advisory + audit write
  dispatch/     # SageMaker confidence check + Step Functions orchestration
  alert/        # SNS broadcast + per-resident direct SMS
  scraper/      # Scheduled data scrapers
  fires_api/    # REST API for frontend fire data

infrastructure/stacks/
  core_stack.py       # DynamoDB tables, Kinesis stream
  pipeline_stack.py   # Kinesis → Lambda pipeline
  safety_stack.py     # Bedrock Guardrails, audit DynamoDB, Step Functions
  ml_stack.py         # SageMaker endpoint
  messaging_stack.py  # SNS topic, SES dispatcher identity
  scraper_stack.py    # EventBridge scheduled scrapers
  frontend_stack.py   # Amplify hosting

frontend/src/
  FireMap.jsx         # Mapbox GL JS live fire map
  DispatchPanel.jsx   # Dispatcher dashboard
  AlertBanner.jsx     # Community alert display
  SafetyBadge.jsx     # AI confidence + audit status indicator
  RegisterForm.jsx    # Resident registration

ml/                   # SageMaker training scripts and model artifacts
```

---

## Data Sources

| Source | Endpoint | Cadence |
|---|---|---|
| NASA FIRMS | firms.modaps.eosdis.nasa.gov/api | Every 3h |
| CAL FIRE | fire.ca.gov/incidents (GeoJSON) | Every 10–15 min |
| NOAA Weather | api.weather.gov | Hourly |
| EPA TRI | epa.gov/toxics-release-inventory | Annual (static) |
| US Census | api.census.gov | Annual (static) |

---

## AI Safety Rules

1. **Audit record written before any alert fires.** A SHA-256 hash-chained record is committed to `wildfire-watch-audit` (DynamoDB) before any SMS goes out.
2. **Guardrails before SNS publish.** Every Bedrock advisory passes through AWS Bedrock Guardrails — no exceptions.
3. **Confidence gate.** If SageMaker dispatch confidence < 0.65, Step Functions pauses for human review.
4. **No PII in logs.** Resident phone numbers and addresses are never written to CloudWatch.

---

## Setup & Deploy

### Prerequisites

- Node.js 18+ and Python 3.11
- AWS CLI configured with appropriate permissions
- CDK bootstrapped in target account/region

### Install

```bash
# CDK (infrastructure)
npm install

# Lambda dependencies
pip install -r requirements.txt

# Frontend
cd frontend && npm install
```

### Deploy

```bash
# Deploy all stacks
cdk deploy --all

# Or deploy individually
cdk deploy WildfireWatchCore
cdk deploy WildfireWatchPipeline
cdk deploy WildfireWatchSafety
```

See [DEPLOY.md](DEPLOY.md) for full deployment guide including Amplify setup.

### Required Environment Variables

```
WW_KINESIS_STREAM_ARN
WW_DYNAMODB_FIRES_TABLE
WW_DYNAMODB_RESOURCES_TABLE
WW_DYNAMODB_RESIDENTS_TABLE
WW_DYNAMODB_ALERTS_TABLE
WW_TIMESTREAM_DB
WW_TIMESTREAM_TABLE
WW_AUDIT_TABLE
WW_SAGEMAKER_ENDPOINT
WW_SNS_ALERT_TOPIC_ARN
WW_SES_DISPATCHER_IDENTITY
WW_COGNITO_RESIDENTS_POOL_ID
WW_COGNITO_RESIDENTS_CLIENT_ID
WW_COGNITO_DISPATCHERS_POOL_ID
WW_COGNITO_DISPATCHERS_CLIENT_ID
WW_API_GATEWAY_URL
WW_WEBSOCKET_URL
WW_MAPBOX_TOKEN
WW_BEDROCK_GUARDRAIL_ID
WW_STEP_FUNCTIONS_ARN
WW_CONFIDENCE_THRESHOLD=0.65
```

---

## Fire Event Schema

```json
{
  "fire_id": "string",
  "source": "FIRMS | CALFIRE",
  "lat": 34.05,
  "lon": -118.24,
  "perimeter_geojson": "GeoJSON string | null",
  "containment_pct": 0.0,
  "radiative_power": 120.5,
  "detected_at": "2026-04-19T00:00:00Z",
  "spread_rate_km2_per_hr": 1.2,
  "confidence": 0.91
}
```
