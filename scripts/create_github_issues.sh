#!/usr/bin/env bash
# Creates all 32 GitHub issues for Wildfire Watch.
# Run once after the repo is set up.
# Requires: gh CLI authenticated (gh auth login)
#
# Usage: bash scripts/create_github_issues.sh

set -e

REPO="A-M-05/wildfire-watch"

echo "Creating labels..."
gh label create infra    --color "0075ca" --description "AWS CDK + provisioning" --repo $REPO 2>/dev/null || true
gh label create data     --color "e4e669" --description "External API pollers" --repo $REPO 2>/dev/null || true
gh label create pipeline --color "d93f0b" --description "Kinesis + Lambda + EventBridge" --repo $REPO 2>/dev/null || true
gh label create ml       --color "7057ff" --description "SageMaker + Bedrock" --repo $REPO 2>/dev/null || true
gh label create safety   --color "b60205" --description "QLDB + Guardrails + Clarify + Monitor" --repo $REPO 2>/dev/null || true
gh label create alert    --color "0e8a16" --description "SNS + Pinpoint + dispatch" --repo $REPO 2>/dev/null || true
gh label create frontend --color "1d76db" --description "React + Mapbox GL JS" --repo $REPO 2>/dev/null || true
gh label create testing  --color "5319e7" --description "Unit + integration tests" --repo $REPO 2>/dev/null || true

echo "Creating issues..."

# ─── INFRASTRUCTURE ──────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[infra] Provision Kinesis streams, IoT Core, DynamoDB, Timestream via CDK" \
  --label "infra" \
  --body "## What this does
Provisions all core data infrastructure via CDK.

## Depends on
None — start immediately.

## Blocks
#6, #7, #8, #9, #10, #11, #16, #17

## Checklist
- [ ] Kinesis stream: \`wildfire-watch-fire-events\` (2 shards)
- [ ] DynamoDB tables: \`fires\`, \`resources\`, \`residents\`, \`alerts\` (PAY_PER_REQUEST)
- [ ] DynamoDB Streams enabled on \`fires\` table (NEW_IMAGE)
- [ ] Timestream DB: \`wildfire-watch\` + table \`fire-metrics\`
- [ ] IoT Core: thing type + policy for sensor feeds
- [ ] All CDK exports following \`WildfireWatch::Core::<Resource>\` pattern

## Agent
Read \`.claude/agents/infra-agent.md\` and \`.claude/skills/aws-infra/SKILL.md\` before starting.

## Verification
\`\`\`bash
aws cloudformation describe-stacks --stack-name WildfireWatchCore --query 'Stacks[0].StackStatus'
# Should return: CREATE_COMPLETE
\`\`\`"

gh issue create --repo $REPO \
  --title "[infra] Provision SageMaker endpoint, S3 ML bucket, Glue catalog" \
  --label "infra" \
  --body "## What this does
Provisions all ML infrastructure via CDK.

## Depends on
None — start immediately.

## Blocks
#12, #13, #14

## Checklist
- [ ] S3 bucket: \`wildfire-watch-ml-data\`
- [ ] SageMaker execution role (S3 + Kinesis read)
- [ ] Glue database: \`wildfire_watch\`
- [ ] SageMaker Model Registry configured

## Agent
Read \`.claude/agents/infra-agent.md\` and \`.claude/skills/aws-infra/SKILL.md\`.

## Verification
\`\`\`bash
aws cloudformation describe-stacks --stack-name WildfireWatchML --query 'Stacks[0].StackStatus'
\`\`\`"

gh issue create --repo $REPO \
  --title "[infra] Provision QLDB ledger, Step Functions safety workflow" \
  --label "infra" \
  --body "## What this does
Provisions the audit and orchestration infrastructure.

## Depends on
None — start immediately.

## Blocks
#17, #19, #20, #21

## Checklist
- [ ] QLDB ledger: \`wildfire-watch-audit\`
- [ ] QLDB tables: \`predictions\`, \`alerts\`
- [ ] Step Functions state machine skeleton (logic added in #19)
- [ ] IAM role for Step Functions → Lambda

## Agent
Read \`.claude/agents/infra-agent.md\` and \`.claude/skills/aws-infra/SKILL.md\`.

## Verification
\`\`\`bash
aws qldb describe-ledger --name wildfire-watch-audit --query 'State'
# Should return: ACTIVE
\`\`\`"

gh issue create --repo $REPO \
  --title "[infra] Provision SNS topics, Pinpoint app, SES config" \
  --label "infra" \
  --body "## What this does
Provisions all messaging infrastructure.

## Depends on
None — start immediately.

## Blocks
#22, #23, #24

## Checklist
- [ ] SNS topic: \`wildfire-watch-alerts\`
- [ ] Pinpoint app: \`wildfire-watch\`
- [ ] Pinpoint SMS channel enabled
- [ ] SES identity verified (dispatcher email fallback)

## Agent
Read \`.claude/agents/infra-agent.md\` and \`.claude/skills/aws-infra/SKILL.md\`.

## Verification
\`\`\`bash
aws cloudformation describe-stacks --stack-name WildfireWatchMessaging --query 'Stacks[0].StackStatus'
\`\`\`"

gh issue create --repo $REPO \
  --title "[infra] Provision Amplify app, API Gateway, Cognito user pools" \
  --label "infra" \
  --body "## What this does
Provisions all frontend and auth infrastructure.

## Depends on
None — start immediately.

## Blocks
#25, #26, #27, #28, #29, #30

## Checklist
- [ ] Cognito user pool: \`wildfire-watch-users\` (residents)
- [ ] Cognito user pool: \`wildfire-watch-dispatchers\`
- [ ] API Gateway REST API + WebSocket API
- [ ] Amplify app wired to \`frontend/\` directory

## Agent
Read \`.claude/agents/infra-agent.md\` and \`.claude/skills/aws-infra/SKILL.md\`.

## Verification
\`\`\`bash
aws cloudformation describe-stacks --stack-name WildfireWatchFrontend --query 'Stacks[0].StackStatus'
\`\`\`"

# ─── DATA SOURCES ────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[data] NASA FIRMS fire detection poller (every 3h via EventBridge)" \
  --label "data" \
  --body "## What this does
Polls NASA FIRMS API every 3h and pushes normalized fire events to Kinesis.

## Depends on
#1

## Blocks
#8

## Implementation
- File: \`functions/scraper/firms_poller.py\`
- Endpoint: \`https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{bbox}/1\`
- Push to Kinesis with \`source: \"FIRMS\"\`
- Fields: latitude, longitude, bright_ti4 (radiative power), confidence, acq_date, acq_time

## Agent
Read \`.claude/agents/data-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
\`\`\`bash
python functions/scraper/firms_poller.py --dry-run
# Should print normalized fire events
\`\`\`"

gh issue create --repo $REPO \
  --title "[data] CAL FIRE GeoJSON perimeter poller (every 10min via EventBridge)" \
  --label "data" \
  --body "## What this does
Polls CAL FIRE active incidents every 10 min and pushes perimeter updates to Kinesis.

## Depends on
#1

## Blocks
#8

## Implementation
- File: \`functions/scraper/calfire_poller.py\`
- Endpoint: \`https://www.fire.ca.gov/umbraco/api/IncidentApi/GeoJsonList?inactive=false\`
- Dedup by UniqueId — only push if perimeter changed since last poll
- Push with \`source: \"CALFIRE\"\`

## Agent
Read \`.claude/agents/data-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
\`\`\`bash
python functions/scraper/calfire_poller.py --dry-run
# Should print active CA fire list
\`\`\`"

gh issue create --repo $REPO \
  --title "[pipeline] Kinesis consumer Lambda — normalize all fire events to common schema" \
  --label "pipeline" \
  --body "## What this does
Consumes Kinesis stream and normalizes FIRMS + CAL FIRE events to the common fire event schema defined in CLAUDE.md.

## Depends on
#1, #6, #7

## Blocks
#9, #16, #31

## Implementation
- File: \`functions/ingest/handler.py\`
- Triggered by Kinesis event source mapping
- Writes normalized events to DynamoDB \`fires\` table + Timestream

## Schema
Every output event must include all fields from CLAUDE.md fire event schema.

## Agent
Read \`.claude/agents/pipeline-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
\`\`\`bash
aws kinesis put-record --stream-name wildfire-watch-fire-events --partition-key test --data '{\"source\":\"FIRMS\",\"lat\":34.2,\"lon\":-118.5,\"radiative_power\":45.2,\"confidence\":0.9,\"detected_at\":\"2026-04-17T10:00:00Z\"}'
# Then: aws dynamodb scan --table-name fires --limit 5
\`\`\`"

gh issue create --repo $REPO \
  --title "[pipeline] Enrichment Lambda — risk score, watershed check, population radius" \
  --label "pipeline" \
  --body "## What this does
Triggered by DynamoDB Streams on the fires table. Adds wind data, SageMaker dispatch score, nearest stations, watershed risk, and population at risk to each fire event.

## Depends on
#1, #8, #11, #13

## Blocks
#10, #19, #31

## Implementation
- File: \`functions/enrich/handler.py\`
- Triggered by DynamoDB Streams (INSERT on fires table)
- Calls: NOAA poller, SageMaker endpoint, Location Service, USGS, Census API (cached)
- Writes enriched event back to DynamoDB and emits FireEnriched to EventBridge

## Agent
Read \`.claude/agents/pipeline-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
Push a test fire event → check DynamoDB fires table for enriched fields within 30s."

gh issue create --repo $REPO \
  --title "[pipeline] EventBridge rule — fire threshold breach → dispatch trigger" \
  --label "pipeline" \
  --body "## What this does
EventBridge rule that fires when an enriched fire event crosses dispatch thresholds, routing to the Step Functions safety workflow.

## Depends on
#1, #9

## Blocks
#19, #22, #31

## Thresholds (any one triggers)
- risk_score >= 0.6
- spread_rate_km2_per_hr >= 2.0
- population_at_risk >= 500

## Agent
Read \`.claude/agents/pipeline-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
Push an enriched event with risk_score=0.8 → confirm Step Functions execution starts."

gh issue create --repo $REPO \
  --title "[data] NOAA weather poller — wind speed/direction per fire location" \
  --label "data" \
  --body "## What this does
Fetches wind speed and direction from NOAA for a given fire location. Called synchronously by the enrichment Lambda, not via Kinesis.

## Depends on
#1

## Blocks
#9

## Implementation
- File: \`functions/scraper/noaa_poller.py\`
- Endpoint: \`https://api.weather.gov/points/{lat},{lon}\` → then hourly forecast
- Cache responses in DynamoDB with 30-min TTL

## Agent
Read \`.claude/agents/data-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`."

# ─── ML ──────────────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[ml] Train SageMaker dispatch model on historical fire/resource data" \
  --label "ml" \
  --body "## What this does
Trains an XGBoost dispatch recommendation model on historical fire/resource/outcome data.

## Depends on
#2

## Blocks
#13

## Implementation
- File: \`ml/dispatch_model/train.py\`
- Data: USFS Fire Occurrence Database + CAL FIRE historical + synthetic
- Features: fire size, spread rate, wind, population density, station distance, time of day, terrain
- Label: optimal resource allocation
- Output: confidence score via predict_proba

## Agent
Read \`.claude/agents/ml-agent.md\` and \`.claude/skills/ml-pipeline/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[ml] Deploy SageMaker endpoint and verify inference" \
  --label "ml" \
  --body "## What this does
Deploys the trained dispatch model to a SageMaker endpoint and verifies it returns confidence scores.

## Depends on
#2, #12

## Blocks
#9, #14, #18, #20

## Implementation
- Deploy to \`ml.m5.large\` instance
- Endpoint name: \`wildfire-watch-dispatch\`
- Verify: invoke with test features → confidence score between 0 and 1

## Agent
Read \`.claude/agents/ml-agent.md\` and \`.claude/skills/ml-pipeline/SKILL.md\`.

## Verification
\`\`\`bash
aws sagemaker describe-endpoint --endpoint-name wildfire-watch-dispatch --query 'EndpointStatus'
# Should return: InService
\`\`\`"

gh issue create --repo $REPO \
  --title "[ml] Write and test Bedrock advisory prompt template" \
  --label "ml" \
  --body "## What this does
Writes the Bedrock prompt template that generates SMS advisories and dispatch briefs from enriched fire event data.

## Depends on
#2

## Blocks
#21

## Implementation
- File: \`ml/bedrock/advisory_prompt.py\`
- Model: \`anthropic.claude-sonnet-4-6-20241022-v2:0\`
- Output format: \`{\"sms\": \"...\", \"brief\": \"...\"}\` (SMS max 160 chars)
- Use prompt caching for the stable system prompt (see SKILL.md)
- If confidence < 0.65: SMS must include 'PRELIMINARY ADVISORY - HUMAN REVIEW PENDING'
- Never output 'you are definitely safe' or similar false certainty

## Agent
Read \`.claude/agents/ml-agent.md\` and \`.claude/skills/ml-pipeline/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[ml] Pre-compute dispatch recommendations for 5 demo fire scenarios" \
  --label "ml" \
  --body "## What this does
Pre-seeds 5 realistic demo fire scenarios in DynamoDB so the demo works reliably without live API calls.

## Depends on
#13, #14

## Blocks
Nothing (demo prep)

## Scenarios
1. \`demo-thousand-oaks\` — suburban hills, high population (hero demo)
2. \`demo-malibu\` — coastal, wind-driven, fast spread
3. \`demo-inland-empire\` — industrial area, chemical risk
4. \`demo-big-bear\` — remote, low population, terrain challenge
5. \`demo-san-fernando\` — urban interface, multiple stations

## Agent
Read \`.claude/agents/ml-agent.md\`."

# ─── AI SAFETY ───────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[safety] Configure Bedrock Guardrails — advisory content rules" \
  --label "safety" \
  --body "## What this does
Configures Bedrock Guardrails with rules that validate every evacuation advisory before it reaches residents.

## Depends on
#1

## Blocks
#21, #22

## Rules to configure
- BLOCK: advisories claiming certainty when confidence < 0.65 (e.g. 'you are safe', 'no danger')
- BLOCK: advisories naming specific individuals
- BLOCK: advisories contradicting the official confidence score
- PII filter: strip phone numbers and addresses from advisory text

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`.

## Verification
\`\`\`python
from ml.bedrock.guardrails import validate_advisory
result = validate_advisory('You are definitely safe.', confidence=0.3)
assert not result['passed']
\`\`\`"

gh issue create --repo $REPO \
  --title "[safety] Implement QLDB prediction + alert logging" \
  --label "safety" \
  --body "## What this does
Implements the QLDB audit log — immutable record of every prediction and alert, written BEFORE any action is taken.

## Depends on
#1, #3

## Blocks
#19, #22, #32

## Hard rule
QLDB write must complete before any downstream action. If QLDB throws, halt — do not proceed to alerting.

## Implementation
- \`log_prediction()\` — write before safety gate runs
- \`update_guardrail_result()\` — update after Guardrails validates
- \`mark_alert_sent()\` — update after Pinpoint confirms delivery

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[safety] Run SageMaker Clarify bias audit on dispatch model" \
  --label "safety" \
  --body "## What this does
Runs a bias audit on the dispatch model to verify it doesn't recommend slower response times for lower-income or underserved areas.

## Depends on
#13

## Blocks
Nothing (produces an audit report)

## Sensitive features to audit
- Income bracket (by ZIP, from Census data)
- Urban vs. rural classification
- Historical response time disparities

## Pass criteria
No group shows >15% slower recommended response time.

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[safety] Wire Step Functions human review gate (confidence < 0.65)" \
  --label "safety" \
  --body "## What this does
Implements the Step Functions state machine logic that pauses dispatch for human review when model confidence is below threshold.

## Depends on
#3, #9, #10, #17

## Blocks
#22

## State machine flow
\`\`\`
EvaluateConfidence
  → [>= 0.65] AutoApprove → AlertSender
  → [< 0.65]  NotifyDispatcher → WaitForApproval (timeout: 5min)
                               → [approved] AlertSender
                               → [timeout]  EscalateAndAlert
\`\`\`

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[safety] Set up SageMaker Model Monitor baseline + schedule" \
  --label "safety" \
  --body "## What this does
Sets up Model Monitor to detect when live fire behavior inputs deviate from the training distribution — flagging when the model is operating outside its confidence zone.

## Depends on
#3, #13

## Blocks
Nothing (continuous monitoring)

## Alert condition
Jensen-Shannon divergence > 0.3 → CloudWatch alarm → SNS to ML team

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[safety] Safety gate Lambda — Guardrails + QLDB + confidence check" \
  --label "safety" \
  --body "## What this does
The single choke point Lambda that every advisory must pass through before reaching Pinpoint. Orchestrates: Bedrock advisory generation → QLDB write → Guardrails validation → confidence check.

## Depends on
#3, #14, #16, #17

## Blocks
#22, #32

## Order of operations (non-negotiable)
1. Generate advisory via Bedrock
2. Write to QLDB (MUST happen first)
3. Validate with Guardrails
4. Check confidence threshold
5. Return APPROVED or HUMAN_REVIEW_REQUIRED

## Agent
Read \`.claude/agents/safety-agent.md\` and \`.claude/skills/ai-safety/SKILL.md\`."

# ─── ALERTING ────────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[alert] Alert sender Lambda — Pinpoint SMS + SNS by GPS radius" \
  --label "alert" \
  --body "## What this does
Sends SMS alerts to registered residents within the fire's risk radius via Pinpoint, after receiving APPROVED from the safety gate.

## Depends on
#4, #10, #19, #21

## Blocks
#23, #32

## Implementation
- File: \`functions/alert/sender.py\`
- Query DynamoDB residents table for users within risk radius
- Send via Pinpoint (TRANSACTIONAL SMS)
- NEVER log phone numbers to CloudWatch — log resident count only
- Call mark_alert_sent() in QLDB after Pinpoint confirms

## Agent
Read \`.claude/agents/alert-agent.md\` and \`.claude/skills/data-pipeline/SKILL.md\`.

## Verification
\`\`\`bash
WW_DRY_RUN=true python functions/alert/sender.py --fire-id demo-check-001 --lat 34.2 --lon -118.5 --radius-km 10
# Should print 'Would send to N residents' without calling Pinpoint
\`\`\`"

gh issue create --repo $REPO \
  --title "[alert] Resident registration flow — Cognito + DynamoDB + location storage" \
  --label "alert" \
  --body "## What this does
Handles resident registration: Cognito auth, address geocoding via Location Service, and storing the resident's location in DynamoDB for radius-based alert targeting.

## Depends on
#4, #5

## Blocks
#22 (needs registered users to alert), #29

## Implementation
- File: \`functions/alert/register.py\`
- API Gateway endpoint: POST /residents/register
- Geocode address → lat/lon via Location Service
- Store in DynamoDB residents table (phone encrypted at rest)

## Agent
Read \`.claude/agents/alert-agent.md\`."

gh issue create --repo $REPO \
  --title "[alert] Watershed contamination alert — USGS feed → Comprehend → advisory" \
  --label "alert" \
  --body "## What this does
Detects watershed contamination risk when a fire is near chemical sites, generates an advisory via Bedrock, and alerts downstream residents.

## Depends on
#4, #21

## Blocks
Nothing

## Flow
USGS anomaly → EPA TRI lookup (chemical sites within 10km) → Comprehend (extract threats from news/scanner) → Bedrock advisory → safety gate → Pinpoint

## Agent
Read \`.claude/agents/alert-agent.md\`."

# ─── FRONTEND ────────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[frontend] Mapbox GL JS base map with fire station markers" \
  --label "frontend" \
  --body "## What this does
Initializes the Mapbox map centered on California and adds fire station markers from data/fire_stations.geojson, color-coded by availability.

## Depends on
#5

## Blocks
#26, #27, #28, #30

## Implementation
- File: \`frontend/src/FireMap.jsx\`
- Style: mapbox://styles/mapbox/dark-v11
- Fire truck markers: green dot = available, red dot = deployed
- Load from \`data/fire_stations.geojson\`

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`.

## Verification
\`cd frontend && npm start\` → map loads, CA visible, station markers showing"

gh issue create --repo $REPO \
  --title "[frontend] Live fire perimeter overlay — color-coded by containment %" \
  --label "frontend" \
  --body "## What this does
Adds a GeoJSON fill layer showing active fire perimeters, color-coded from red (0% contained) to green (100% contained).

## Depends on
#5, #25

## Blocks
#27, #30

## Color scale
- 0–25%: #ff2200 (red)
- 25–50%: #ff6600 (orange)
- 50–75%: #ffaa00 (amber)
- 75–100%: #22cc44 (green)

## Data source
REST call to /fires/active every 30s (use hardcoded data/demo scenario if API not ready)

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[frontend] Risk radius overlay + resident alert zone visualization" \
  --label "frontend" \
  --body "## What this does
Draws a semi-transparent circle overlay around each active fire showing the alert radius, with a popup showing population at risk and nearest evacuation route.

## Depends on
#5, #25, #26

## Blocks
#28, #30

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[frontend] Dispatch panel — resource status, advisory, confidence badge" \
  --label "frontend" \
  --body "## What this does
Sidebar panel shown when a fire is selected. Shows: fire info, dispatched resources, Bedrock-generated advisory, SafetyBadge with confidence score + Guardrails status + QLDB link, and human review status.

## Depends on
#5, #27

## Blocks
Nothing

## Components
- \`DispatchPanel.jsx\` — main sidebar
- \`SafetyBadge.jsx\` — confidence color (green ≥0.65, amber 0.4–0.65, red <0.4)

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[frontend] Resident registration form + alert subscription UI" \
  --label "frontend" \
  --body "## What this does
Registration form for residents to sign up for SMS wildfire alerts. Calls POST /residents/register via Cognito-authenticated API Gateway.

## Depends on
#5, #23

## Blocks
Nothing

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`."

gh issue create --repo $REPO \
  --title "[frontend] Wire map to live DynamoDB + WebSocket for real-time updates" \
  --label "frontend" \
  --body "## What this does
Connects the map to live data: REST polling for fire list + WebSocket for real-time perimeter updates, dispatch events, and alert status.

## Depends on
#5, #26, #27

## Blocks
Nothing

## WebSocket message types
- \`fire_updated\` → update perimeter GeoJSON on map
- \`alert_sent\` → show AlertBanner
- \`resource_dispatched\` → update station marker to red (deployed)

## Agent
Read \`.claude/agents/frontend-agent.md\` and \`.claude/skills/frontend/SKILL.md\`."

# ─── TESTING ─────────────────────────────────────────────────────────────────

gh issue create --repo $REPO \
  --title "[testing] Unit tests for all Lambda functions (ingest, enrich, dispatch, alert)" \
  --label "testing" \
  --body "## What this does
Unit tests for all four core Lambda functions using moto to mock AWS services.

## Depends on
#8, #9, #10, #22

## Blocks
Nothing

## Coverage required
- Normalize Lambda: FIRMS schema, CAL FIRE schema, schema completeness
- Enrichment Lambda: risk score calculation, SageMaker call, EventBridge emit
- Dispatch Lambda: threshold logic, EventBridge rule matching
- Alert sender: radius query, Pinpoint call, QLDB update

## Agent
Read \`.claude/agents/test-agent.md\`."

gh issue create --repo $REPO \
  --title "[testing] Integration test + safety contract test — QLDB written before alert fires" \
  --label "testing" \
  --body "## What this does
The critical safety contract test. Verifies that a QLDB prediction record is written BEFORE any Pinpoint SMS is sent — the hard rule from CLAUDE.md.

## Depends on
#17, #21, #22

## Blocks
Nothing

## The contract
\`qldb_write_timestamp < pinpoint_send_timestamp\` — always, no exceptions.

## Also tests
- Guardrails blocks 'you are definitely safe' when confidence < 0.65
- Low-confidence dispatch routes to Step Functions human review path

## Agent
Read \`.claude/agents/test-agent.md\`."

echo ""
echo "✓ All 32 issues created at https://github.com/$REPO/issues"
echo ""
echo "Next: install gh CLI and run this script to push issues to GitHub."
echo "  brew install gh && gh auth login"
echo "  bash scripts/create_github_issues.sh"
