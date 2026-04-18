# Hackathon Sprint Schedule

## Hour-by-hour

### 10:00–11:00 | Setup (all parallel)
- **Person A (Infra):** Issues #1, #2 — CDK stacks, Kinesis, DynamoDB, SageMaker
- **Person B (ML):** Issue #12 — pull Hansen/USFS historical fire data, start training
- **Person C (Frontend):** Issue #25 — Mapbox base map with hardcoded fire stations
- **Person D (Data + Safety):** Issues #6, #7 — NASA FIRMS + CAL FIRE pollers

**Checkpoint 10:30:** Can you pull a live fire from CAL FIRE GeoJSON?

### 11:00–13:00 | Core pipeline
- **Person A:** Issues #3, #8 — QLDB + Kinesis consumer Lambda
- **Person B:** Issues #13, #14 — Deploy SageMaker endpoint, write Bedrock prompt
- **Person C:** Issue #26 — Live fire perimeters on map (hardcoded data OK for now)
- **Person D:** Issues #9, #11 — Enrichment Lambda + NOAA wind data

**Checkpoint 13:00:** Push a fake fire event into Kinesis → does it appear in DynamoDB?

### 13:00–14:00 | Lunch + integration
- Wire SageMaker into enrichment Lambda (#9 finishes)
- Team syncs on normalized fire event schema
- Start issues #16, #17 (Guardrails + QLDB)

### 14:00–16:00 | Alert pipeline (demo hero feature)
- **Person A:** Issues #19, #21 — Step Functions gate + safety Lambda
- **Person B:** Issue #15 — Pre-seed 5 demo scenarios
- **Person C:** Issues #27, #28 — Risk radius overlay + dispatch panel
- **Person D:** Issues #4, #22, #23 — SNS/Pinpoint + alert sender + registration

**Checkpoint 16:00:** Trigger a fake alert → does an SMS arrive on a real phone?

### 16:00–17:30 | Safety layer
- Issues #18, #20 — Clarify audit + Model Monitor
- SafetyBadge component in UI
- QLDB record visible in dispatch panel

### 17:30–19:00 | Polish + QuickSight
- Issue #30 — WebSocket real-time map updates
- Issue #29 — Resident registration UI
- QuickSight: response times, resource utilization, alert coverage map

### 19:00–20:00 | Demo rehearsal
- Run `/demo-prep`
- Practice the live alert trigger demo 3×
- Fix critical bugs only

### 20:00–21:00 | Buffer

---

## Demo script

> *"A wildfire just ignited in the hills above Thousand Oaks. Watch what happens."*

1. Manually push a fire event into Kinesis with coordinates and spread rate
2. Map updates — red perimeter appears, risk radius expands over residential areas
3. Two fire stations light up as dispatched
4. Dispatch panel shows Bedrock-generated brief with confidence score
5. QLDB audit trail already populated
6. **"Now watch the phone."**
7. SMS arrives: *"WILDFIRE ALERT: Active fire 2.3 miles from your address. Evacuate via Route 101 North. Do not use surface streets. Live map: [link]"*

> *"That took 47 seconds from fire detection to SMS. And every decision the AI made is logged here — immutably — so that after the fire, investigators know exactly what the system knew, when, and why it did what it did."*

## Winning argument

**Environmental track:** *"We didn't build a dashboard that shows fires. We built a system that responds to them — automatically routing resources and alerting communities in under 60 seconds."*

**AI Safety track:** *"Every evacuation advisory this system sends has been validated by Guardrails, logged to an immutable ledger, audited for equity bias, and — when confidence is low — held for human review. Because in a wildfire, a wrong alert doesn't just waste time. It kills people."*
