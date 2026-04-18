# Wildfire Watch — All 32 Issues

This is the canonical dependency graph. **Check here before starting any issue.**

Format: `#N [label] Title | Depends on: X | Blocks: Y`

---

## Infrastructure (1–5)

```
#1  [infra]    Provision Kinesis streams, IoT Core, DynamoDB, Timestream via CDK
               Depends on: nothing
               Blocks: #6, #7, #8, #9, #10, #11, #16, #17

#2  [infra]    Provision SageMaker endpoint, S3 ML bucket, Glue catalog
               Depends on: nothing
               Blocks: #12, #13, #14

#3  [infra]    Provision DynamoDB audit table (hash-chain), Step Functions safety workflow
               Depends on: nothing
               Blocks: #17, #19, #20, #21

#4  [infra]    Provision SNS broadcast topic, SES dispatcher identity
               Depends on: nothing
               Blocks: #22, #23, #24

#5  [infra]    Provision Amplify app, API Gateway, Cognito user pools
               Depends on: nothing
               Blocks: #25, #26, #27, #28, #29, #30
```

## Data Sources (6–11)

```
#6  [data]     NASA FIRMS fire detection poller (every 3h via EventBridge)
               Depends on: #1
               Blocks: #8

#7  [data]     CAL FIRE GeoJSON perimeter poller (every 10min via EventBridge)
               Depends on: #1
               Blocks: #8

#8  [pipeline] Kinesis consumer Lambda — normalize all fire events to common schema
               Depends on: #1, #6, #7
               Blocks: #9, #16, #31

#9  [pipeline] Enrichment Lambda — risk score, watershed check, population radius
               Depends on: #1, #8, #11, #13
               Blocks: #10, #19, #31

#10 [pipeline] EventBridge rule — fire threshold breach → dispatch trigger
               Depends on: #1, #9
               Blocks: #19, #22, #31

#11 [data]     NOAA weather poller — wind speed/direction per fire location
               Depends on: #1
               Blocks: #9
```

## ML (12–15)

```
#12 [ml]       Train SageMaker dispatch model on historical fire/resource data
               Depends on: #2
               Blocks: #13

#13 [ml]       Deploy SageMaker endpoint and verify inference
               Depends on: #2, #12
               Blocks: #9, #14, #18, #20

#14 [ml]       Write and test Bedrock advisory prompt template
               Depends on: #2
               Blocks: #21

#15 [ml]       Pre-compute dispatch recommendations for 5 demo fire scenarios
               Depends on: #13, #14
               Blocks: nothing (demo prep)
```

## AI Safety (16–21)

```
#16 [safety]   Configure Bedrock Guardrails — advisory content rules
               Depends on: #1
               Blocks: #21, #22

#17 [safety]   Implement DynamoDB hash-chain prediction + alert logging
               Depends on: #1, #3
               Blocks: #19, #22, #32

#18 [safety]   Run SageMaker Clarify bias audit on dispatch model
               Depends on: #13
               Blocks: nothing (audit report)

#19 [safety]   Wire Step Functions human review gate (confidence < 0.65)
               Depends on: #3, #9, #10, #17
               Blocks: #22

#20 [safety]   Set up SageMaker Model Monitor baseline + schedule
               Depends on: #3, #13
               Blocks: nothing (monitoring)

#21 [safety]   Safety gate Lambda — Guardrails + audit hash-chain + confidence check
               Depends on: #3, #14, #16, #17
               Blocks: #22, #32
```

## Alerting (22–24)

```
#22 [alert]    Alert sender Lambda — direct SNS SMS by GPS radius
               Depends on: #4, #10, #19, #21
               Blocks: #23, #32

#23 [alert]    Resident registration flow — Cognito + DynamoDB + location storage
               Depends on: #4, #5
               Blocks: #22 (needs registered users to alert), #29

#24 [alert]    Watershed/reservoir water-level evacuation alert — USGS levels+flow → threatened-area advisory
               Depends on: #4, #21
               Blocks: nothing
               Note: contamination-risk variant (EPA TRI + Comprehend) is backlogged
```

## Frontend (25–30)

```
#25 [frontend] Mapbox GL JS base map with fire station markers
               Depends on: #5
               Blocks: #26, #27, #28, #30

#26 [frontend] Live fire perimeter overlay — color-coded by containment %
               Depends on: #5, #25
               Blocks: #27, #30

#27 [frontend] Risk radius overlay + resident alert zone visualization
               Depends on: #5, #25, #26
               Blocks: #28, #30

#28 [frontend] Dispatch panel — resource status, advisory, confidence badge
               Depends on: #5, #27
               Blocks: nothing

#29 [frontend] Resident registration form + alert subscription UI
               Depends on: #5, #23
               Blocks: nothing

#30 [frontend] Wire map to live DynamoDB + WebSocket for real-time updates
               Depends on: #5, #26, #27
               Blocks: nothing
```

## Testing (31–32)

```
#31 [testing]  Unit tests for all Lambda functions (ingest, enrich, dispatch, alert)
               Depends on: #8, #9, #10, #22
               Blocks: nothing

#32 [testing]  Integration test + safety contract test — audit row written before alert fires
               Depends on: #17, #21, #22
               Blocks: nothing
```

---

## Critical Demo Path (minimum viable for fire-to-SMS demo)

```
#1, #2, #3, #4, #5  (infra — all parallelizable)
     ↓
#6, #7, #11         (data pollers — parallel after #1)
     ↓
#8                  (Kinesis consumer)
     ↓
#12 → #13           (SageMaker train + deploy — parallel with above)
     ↓
#9                  (enrichment, needs #8 + #11 + #13)
     ↓
#10, #14, #16, #17  (EventBridge rule + Bedrock prompt + Guardrails + audit chain — parallel)
     ↓
#21                 (safety gate Lambda)
     ↓
#19                 (Step Functions gate)
     ↓
#22                 (alert sender)
     ↓
#25 → #26 → #27 → #28  (frontend map + dispatch panel)
```
