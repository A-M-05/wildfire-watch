# Wildfire Watch — Master Context

Read this file at the start of every session.

## What we're building

A real-time wildfire resource dispatch and community alert system. Two hackathon tracks:
- **Environmental** — live fire detection + evacuation prediction for at-risk communities
- **AI Safety** — every AI recommendation is audited, validated, and gated before reaching people

**Demo target:** Fire detected → resources dispatched → resident SMS in under 60 seconds.

## Stack

| Layer | Tech |
|---|---|
| Infra | AWS CDK (TypeScript) |
| Backend | Python 3.11 Lambdas |
| ML | SageMaker + Bedrock (claude-sonnet-4-6) |
| Frontend | React 18 + Mapbox GL JS |
| Hosting | AWS Amplify |
| DB | DynamoDB (serving + audit hash-chain) + Timestream (time-series) |
| Messaging | Kinesis (pipeline) + SNS (alerts, direct-to-phone) |

> **Architecture pivot (2026-04-18):** This account's SCP blocks Pinpoint and AWS no longer accepts new QLDB ledger creation. We replaced QLDB with a DynamoDB audit table whose immutability comes from a SHA-256 hash chain written by the safety gate Lambda (#17/#21), and replaced Pinpoint per-resident SMS with `sns.publish(PhoneNumber=...)` direct calls from the alert sender (#22). Broadcast SNS topic + SES dispatcher email still apply.

## Safety rules — non-negotiable

1. **Audit record is written before any alert fires.** A hash-chained record must be committed to the DynamoDB audit table (`wildfire-watch-audit`) before the SMS goes out. This is a hard contract. See issue #32.
2. **Guardrails before SNS publish.** Every Bedrock advisory passes through Guardrails. No exceptions.
3. **Confidence gate.** If SageMaker dispatch confidence < 0.65, Step Functions pauses for human review. Do not lower this threshold without team consensus.
4. **No PII in logs.** Resident phone numbers and addresses are never written to CloudWatch.

## Issue workflow

1. Check `docs/ISSUES.md` for dependencies before starting anything
2. Run `/claim N` to assign yourself and verify deps are clear-- make sure the issue is not already claimed either on the md or ON GITHUB
3. Work the issue using the relevant agent in `.claude/agents/`
4. Read the relevant SKILL.md in `.claude/skills/` before writing code
5. Commit with `[#N]` in the message
6. Close the GitHub issue when done
7. Run `/status` to see what's newly unblocked

## Agents

Each domain has a dedicated agent file in `.claude/agents/`. Use them — they carry domain context and conventions that save time.

## Conventions

- Lambda handlers live in `functions/<domain>/handler.py`
- CDK stacks live in `infrastructure/stacks/<name>_stack.py`
- All environment variables are prefixed: `WW_` (e.g. `WW_KINESIS_STREAM_ARN`)
- Fire events use this normalized schema everywhere:

```json
{
  "fire_id": "string",
  "source": "FIRMS | CALFIRE",
  "lat": float,
  "lon": float,
  "perimeter_geojson": "GeoJSON string | null",
  "containment_pct": float,
  "radiative_power": float,
  "detected_at": "ISO8601",
  "spread_rate_km2_per_hr": float,
  "confidence": float
}
```

- Enriched events add:

```json
{
  "risk_score": float,
  "wind_speed_ms": float,
  "wind_direction_deg": float,
  "population_at_risk": int,
  "nearest_stations": [{"station_id": "string", "distance_km": float, "available": bool}]
}
```

## Data sources

| Source | Endpoint | Cadence |
|---|---|---|
| NASA FIRMS | firms.modaps.eosdis.nasa.gov/api | Every 3h |
| CAL FIRE | fire.ca.gov/incidents (GeoJSON) | Every 10-15 min |
| NOAA Weather | api.weather.gov | Hourly |
| EPA TRI | epa.gov/toxics-release-inventory | Annual (static) |
| US Census | api.census.gov | Annual (static) |

## Environment variables (all required)

```
WW_KINESIS_STREAM_ARN
WW_DYNAMODB_FIRES_TABLE
WW_DYNAMODB_RESOURCES_TABLE
WW_DYNAMODB_RESIDENTS_TABLE
WW_DYNAMODB_ALERTS_TABLE
WW_TIMESTREAM_DB
WW_TIMESTREAM_TABLE
WW_AUDIT_TABLE                    # DynamoDB audit ledger (replaces QLDB)
WW_SAGEMAKER_ENDPOINT
WW_SNS_ALERT_TOPIC_ARN            # broadcast topic; per-resident SMS uses sns.publish PhoneNumber direct
WW_SES_DISPATCHER_IDENTITY        # verified SES sender for dispatcher email
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
