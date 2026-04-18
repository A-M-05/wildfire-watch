# System Architecture

## Data flow

```
[NASA FIRMS]    ──┐
[CAL FIRE]      ──┤──► Kinesis Data Streams ──► Normalize Lambda (#8)
[IoT / CAD]     ──┘                                    │
                                                        ▼
[NOAA Weather] ─────────────────────────────► Enrichment Lambda (#9)
[USGS Water]                                           │
[Census / EPA]                                         │ enriched event
                                                       ▼
                                              EventBridge Rule (#10)
                                              (threshold breach?)
                                                       │
                              ┌────────────────────────┤
                              ▼                        ▼
                     SageMaker Inference          DynamoDB (fires table)
                     (dispatch scores)            Timestream (metrics)
                              │
                              ▼
                      Safety Gate Lambda (#21)
                      ├─ Bedrock advisory generation
                      ├─ Guardrails validation (#16)
                      ├─ QLDB audit write (#17)
                      └─ Confidence check
                              │
                    ┌─────────┴──────────┐
                    │ confidence ≥ 0.65  │ confidence < 0.65
                    ▼                    ▼
             Alert Sender (#22)   Step Functions (#19)
             Pinpoint SMS              Human dispatcher
             SNS broadcast             approval required
                    │                        │
                    └──────────┬─────────────┘
                               ▼
                         Residents get SMS
                         QLDB updated with outcome
```

## AWS services map

```
Ingestion:     IoT Core → Kinesis → Lambda
Storage:       DynamoDB (serving) | Timestream (metrics) | S3 (ML data, archives)
ML:            SageMaker (dispatch model) | Bedrock claude-sonnet-4-6 (advisories)
Safety:        Bedrock Guardrails | QLDB | SageMaker Clarify | Model Monitor
Orchestration: Step Functions | EventBridge | Lambda
Alerting:      Pinpoint (SMS) | SNS (broadcast)
Frontend:      Amplify | API Gateway (REST + WebSocket) | Cognito
Analytics:     QuickSight | Glue | Athena
Geospatial:    Location Service | Rekognition | Comprehend
```

## Key design decisions

**Kinesis over SQS:** Multiple consumers need the same events (enrichment, Timestream writer, monitoring). Kinesis fan-out handles this; SQS would require copying messages.

**QLDB over DynamoDB for audit:** QLDB provides cryptographic verification of the audit trail — a hash chain that proves records weren't tampered with post-incident. DynamoDB doesn't offer this.

**Step Functions for human gate:** The human-in-the-loop pause needs durable state (dispatcher may not respond for minutes). Step Functions handles long-running waits with built-in retry + timeout without burning Lambda compute.

**Pinpoint over raw SNS for SMS:** Pinpoint supports GPS-radius segmentation natively — given a lat/lon and radius, it targets the right residents. SNS would require us to do the geospatial filtering in Lambda.

**Bedrock Guardrails as hard gate:** Guardrails is a synchronous call that returns PASS/BLOCK before we touch Pinpoint. It's not advisory — if it blocks, the advisory is rewritten or escalated.
