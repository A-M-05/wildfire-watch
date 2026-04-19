# Wildfire Watch — Technical Pitch Q&A

Anticipated judge / inspector questions, grouped by area, with crisp answers grounded in actual code paths. Read this on the train. Re-read the **Workshop account constraints** section right before going on stage — it's the single most likely source of "gotcha" questions.

> **Honesty contract for this doc:** every claim cites the file it came from. If a question's answer is "stubbed" or "not yet wired," it says so. Better to admit a stub than to bluff and get caught mid-pitch.

---

## Section 1 — Architecture choices

### Q: Why DynamoDB hash chain instead of QLDB? Isn't QLDB literally designed for this?

**A:** AWS no longer accepts new QLDB ledger creations on this account (and is sunsetting the service generally). We needed an immutable audit ledger and built one from primitives: each row in `wildfire-watch-audit` carries a `prev_hash` field that's the SHA-256 of the prior row for the same fire. Any tamper breaks the chain — `verify_chain` paginates the GSI, replays every row, recomputes hashes, returns False on first mismatch. See `functions/alert/audit.py` lines 142-168.

It's not as strong as QLDB's cryptographic journal (we don't detect deletion of the most recent row without an external anchor), but it covers the threats that matter for a hackathon prototype: mutation, middle-row deletion, and inserted rows with fabricated `prev_hash`. We're explicit about that limit in the module docstring (`functions/alert/audit.py:11-18`).

### Q: Why `sns.publish(PhoneNumber=...)` direct instead of Pinpoint?

**A:** This workshop account's SCP blocks `mobiletargeting:CreateApp`, so Pinpoint is unavailable. Pivoted to per-resident `sns.publish` with `PhoneNumber` set directly — see `functions/alert/sender.py`. Documented in `infrastructure/stacks/messaging_stack.py` comments.

The trade-off: we lose Pinpoint's native GPS-radius targeting, so the alert sender does the radius filter itself — a bounding-box DynamoDB scan on `residents` followed by a haversine refinement. Slower at scale, but at hackathon volumes (single-county Type 1 incident, ~thousands of residents) it's well under the Lambda 30s budget.

### Q: Kinesis vs EventBridge — why both?

**A:** Different jobs.
- **Kinesis** (`wildfire-watch-fire-events`, 2 shards, `infrastructure/stacks/core_stack.py:46`) is the firehose — every raw fire detection from FIRMS / CAL FIRE lands here at high cardinality. Replayable, ordered per partition key.
- **EventBridge** (`FireThresholdRule`, `core_stack.py:187`) is for typed business events — specifically `FireEnriched` events emitted after the enrichment Lambda has scored a fire. It's the trigger for the dispatch decision, not the data plane.

Or shorter: Kinesis carries data, EventBridge carries decisions.

### Q: Why Bedrock instead of OpenAI / Anthropic API direct?

**A:** Three reasons specific to this app:
1. **Bedrock Guardrails is a synchronous API call** (`apply_guardrail`) tightly bound to the model invocation. We can run the same Guardrail policy on inputs and outputs without writing our own filter pipeline. See `ml/bedrock/guardrails.py:117`.
2. **IAM, not API keys.** The Lambda's execution role gets `bedrock:InvokeModel` — no key rotation, no secret in Parameter Store.
3. **Prompt caching on the system prompt** is a single-flag opt-in via `cache_control: ephemeral` (`ml/bedrock/advisory_prompt.py:110`) — saves ~500 input tokens per call after the first one in a 5-minute window.

Model used: `anthropic.claude-sonnet-4-6-20241022-v2:0`.

### Q: What's in each CDK stack? Why split it that way?

**A:** Six stacks, split by lifecycle and blast radius:

| Stack | Purpose | Key resources |
|---|---|---|
| `CoreStack` | Always-on data plane | Kinesis stream, 4 DynamoDB tables (`fires`, `resources`, `residents`, `alerts`), IoT Core thing type + policy, EventBridge rule, dispatch trigger Lambda |
| `MessagingStack` | Outbound channels | SNS topic `wildfire-watch-alerts`, SES dispatcher identity |
| `SafetyStack` | The safety contract | `wildfire-watch-audit` table + GSI, Bedrock Guardrail, Step Functions state machine, dispatcher_notify Lambda |
| `MLStack` | Model housing | S3 `wildfire-watch-ml-data`, SageMaker execution role, Glue catalog, Model Package Group |
| `PipelineStack` | Streaming compute | Enrichment Lambda + DynamoDB Streams trigger |
| `FrontendStack` | Hosting | Amplify app |

Splitting lets us redeploy the safety contract without touching the data plane and vice-versa. CloudFormation rolls back at the stack boundary, not across them — important when iterating on Step Functions during a hackathon.

---

## Section 2 — The safety contract (AI Safety track money questions)

### Q: Walk me through the safety gate Lambda. Step by step.

**A:** Six steps in this exact order — see `functions/alert/safety_gate.py:81-131`:

1. **Validate input shape** — fail fast with a clear `ValueError` if `fire_event` or `recommendation` are missing required keys. No audit row written.
2. **Generate the advisory** — call Bedrock (`ml.bedrock.advisory_prompt.generate_advisory`). Returns `{"sms": ..., "brief": ...}`. If Bedrock returns malformed JSON, we raise before writing anything.
3. **`log_prediction`** — append a `prediction` row to the audit chain. This is the contract: the row commits **before** Guardrails runs. Even a blocked advisory leaves an auditable record of what was generated.
4. **`validate_advisory`** — Guardrails check. Two layers (see Q below).
5. **`append_guardrail_outcome`** — append a `guardrails_outcome` row linked to the prediction. We append, never `UpdateItem` the prior row, because mutating breaks the SHA-256 chain.
6. **Confidence threshold check** — if `confidence < 0.65`, return `HUMAN_REVIEW_REQUIRED`. Otherwise `APPROVED`.

If Guardrails itself errors (Bedrock outage, throttling), we still append a `guardrails_outcome` row marked `passed=False` with reason `"guardrails service error"` **before** re-raising. Forensic completeness — Step Functions retries shouldn't pile up orphan prediction rows.

### Q: What are the two layers of Guardrails validation?

**A:** See `ml/bedrock/guardrails.py:1-45`.

**Layer 1 — in-process confidence-consistency check.** Bedrock Guardrails can't see the SageMaker confidence score. So we run a regex pass over the advisory looking for certainty phrases (`"you are safe"`, `"no danger"`, `"all clear"`, etc.) when confidence is below threshold. If the AI is hedging-on-paper but our model is actually uncertain, that's a contradiction Guardrails would miss. Word boundaries enforced — `"you are safer than before"` does not trigger `"you are safe"`.

**Layer 2 — Bedrock `apply_guardrail` API call.** Configured in `infrastructure/stacks/safety_stack.py:94-147`:
- **Word policy**: blocks the same certainty phrases at the model level (`"you are definitely safe"`, `"no risk"`, `"all clear"`, ...)
- **PII policy**: anonymizes `PHONE`, `ADDRESS`, `NAME`, `EMAIL` — residents' contact info must never appear in AI output (CLAUDE.md rule #4).
- **Content filters**: HATE/VIOLENCE/INSULTS at MEDIUM strength. Tuned MEDIUM specifically because LOW over-blocks legitimate emergency language ("fire is threatening", "danger zone").

Both layers must pass for `passed=True`.

### Q: There are two different "confidence" scores in the system. What's the difference?

**A:** Yes — and the distinction matters, judges will probe this.

| Score | Question it answers | Source | Range driver |
|---|---|---|---|
| **Detection confidence** | "Is this hotspot a real fire, or a false alarm?" | NASA FIRMS satellite algorithm (or CAL FIRE = human-confirmed = 1.0) | Sensor signal quality |
| **Dispatch confidence** | "Are we recommending the right resources?" | Our SageMaker dispatch model | Distance from decision boundary |

**The safety gate contract is on dispatch confidence**, not detection. `safety_gate.py:68` requires `confidence` on `recommendation`, not on `fire_event`. Detection confidence is upstream — used as an enrichment filter (we discard FIRMS rows below `low` in some pipelines), not as the human-review gate.

**Honest disclosure**: the frontend `DispatchPanel.jsx` currently reads `p.confidence ?? data?.confidence`, which means on a live FIRMS fire it's bucketing on detection confidence by accident. The mock GeoJSON pre-bakes values that look like dispatch confidence so the demo UI works. Fix is a one-line panel change once the live API surfaces both fields separately. Called out so we don't get caught.

### Q: How is detection confidence scored?

**A:** `functions/scraper/firms_poller.py:42-52`. NASA FIRMS publishes a categorical label per hotspot; we bucket-map to a 0–1 float:

```python
mapping = {"low": 0.35, "nominal": 0.65, "high": 0.90}
```

Numeric FIRMS values (some feeds give 0–100) divided by 100. Default 0.65 on parse failure. CAL FIRE incidents are hardcoded to 1.0 (`calfire_poller.py:136`) because they're human-confirmed.

What this captures: NASA's published false-alarm rate for the MODIS/VIIRS thermal anomaly algorithm — sun-glint off water/glass, gas flares, factory hot roofs, agricultural burns.

### Q: How is dispatch confidence scored?

**A:** Heuristic, not a trained model output. `ml/dispatch_model/features.py:50-64`:

```python
def spread_to_confidence(spread_rate, projected_area):
    # Dispatch decision boundaries from DISPATCH_THRESHOLDS: 0.5 and 1.5 km²/hr
    # (LOCAL → MUTUAL_AID → AERIAL escalation)
    min_distance = min(abs(spread_rate - t) for t in [0.5, 1.5])
    boundary_confidence = min(min_distance / 2.0, 1.0)
    area_factor = min(projected_area / 2.0, 1.0)
    return 0.7 * boundary_confidence + 0.3 * area_factor
```

The intuition: **"how far from the call line are we?"** A fire at 0.55 km²/hr sits right at the LOCAL/MUTUAL_AID boundary — the recommendation is technically correct either way, exactly when we want a human eye, so confidence is forced low and the 0.65 gate trips. A fire at 5.0 km²/hr is nowhere near a boundary — clearly AERIAL — so confidence is high and the gate auto-approves.

Weights:
- **70% boundary distance** — the primary signal. Saturates at 2.0 km²/hr away from any threshold.
- **30% projected area** — corroborating evidence. Large projected burn area reinforces a high spread reading; small area discounts borderline-high readings.

**Strong design property worth landing in the pitch:** with boundaries at 0.5 and 1.5, the MUTUAL_AID band is only 1.0 km²/hr wide. The maximum boundary_confidence inside it is 0.25 (at the midpoint, spread=1.0), so even with full area corroboration the overall confidence caps at **0.475 in the MUTUAL_AID band — every MUTUAL_AID call routes to human review by design.** Auto-dispatch only happens for clearly-AERIAL fires (spread comfortably above 1.5) or clearly-LOCAL incidents (spread well below 0.5 with small area).

`docs/FIRE_SPREAD_PREDICTION.md:49` lays out the intended v2 approach (quantile regression on FIRMS historical, residuals against a Rothermel baseline, confidence = `1 − normalized_prediction_interval_width`). The geometric heuristic ships today because it's defensible without a trained model and degrades safely — borderline calls always trip the gate.

### Q: Why 0.65 confidence threshold? Where did that number come from?

**A:** It's the contract written in `CLAUDE.md` safety rule #3 — "Step Functions pauses for human review on dispatch confidence < 0.65, do not lower without team consensus." Enforced in three places:
- `functions/alert/safety_gate.py:53` (`DEFAULT_CONFIDENCE_THRESHOLD = 0.65`)
- `ml/bedrock/advisory_prompt.py:23` (the SMS gets a `"PRELIMINARY ADVISORY"` flag below threshold)
- `ml/bedrock/guardrails.py:23` (Layer 1 contradiction check uses the same threshold)

The number works with the heuristic above: 0.65 is just above the 0.475 cap inside the MUTUAL_AID band, so the entire MUTUAL_AID band falls under the gate by construction. AERIAL and LOCAL calls only auto-dispatch when they're decisively in their tier (well clear of the nearest boundary, with corroborating area). That's the band where two reasonable dispatchers might disagree on the call — exactly the band where a human should weigh in.

Note also the **0.85 tier** in `DispatchPanel.jsx` (`safetyTone` / `dispatcherTone`): that's a UI display threshold, not part of the safety contract. 0.65–0.85 reads as "ALERT SENT" / "AUTO-DISPATCH (FLAGGED)"; ≥0.85 reads as "HIGH CONFIDENCE". The gate is 0.65; 0.85 is just framing for residents and dispatchers to distinguish marginal-but-approved from slam-dunk.

Honest answer if pressed: the 0.65 threshold isn't from a calibration plot — we don't have production confidence distributions. The architecture supports any threshold via `WW_CONFIDENCE_THRESHOLD`; calibration is a v2 problem.

### Q: What happens if Bedrock returns garbage JSON?

**A:** Three layers of defense:
1. `generate_advisory` parses the response with `json.loads` and raises `ValueError` on malformed JSON (`ml/bedrock/advisory_prompt.py:127-130`).
2. `_validate_advisory_shape` in the safety gate (`functions/alert/safety_gate.py:72`) checks for required keys before calling `log_prediction` — so malformed output never gets an audit row.
3. The 160-char SMS hard limit is enforced post-Bedrock (`advisory_prompt.py:136`) — carriers truncate longer messages silently, so we cap at 157 chars + `...`.

If we wanted a fourth layer, we'd add a Pydantic schema validation. We don't think it's worth the latency.

### Q: What's the human review path? What if nobody answers?

**A:** Walk through `infrastructure/stacks/safety_stack.py:217-303`. The state machine routes `HUMAN_REVIEW_REQUIRED` to `NotifyDispatcherAndWait`, a Step Functions task with `WAIT_FOR_TASK_TOKEN` integration. The dispatcher_notify Lambda (`functions/safety/dispatcher_notify.py`):
1. Publishes an SNS message to the dispatcher topic with the fire details, the Guardrails-validated advisory text, and the task token.
2. Stores the task token in the `fires` DynamoDB row so the dispatcher UI can resume without using the CLI.
3. Returns immediately — Step Functions stays paused.

The dispatcher resumes by calling `sfn:SendTaskSuccess` (approve → AlertSender) or `sfn:SendTaskFailure` (reject → LogAndStop).

**5-minute timeout. No response → fail-closed, no alert sent.** This is `CLAUDE.md` rule #3 in action: a late alert is bad, a wrong alert can get someone killed, an unanswered alert is "no alert."

### Q: How do I know an audit row wasn't tampered with after the fact?

**A:** Run `verify_chain(fire_id)` from `functions/alert/audit.py:142`. It paginates the `fire_id-written_at-index` GSI, replays the chain in chronological order, recomputes each row's SHA-256 from the canonical JSON of its fields, and checks both that (a) the recomputed hash matches `record_hash` and (b) the row's `prev_hash` matches the previous row's `record_hash`.

Returns `True` only if every row checks out. Returns `False` on first mismatch.

What it **catches**: mutation of any row, deletion of a middle row, fabricated `prev_hash` on inserted rows.

What it does **not** catch: deletion of the most recent row (no external anchor), or a fully cascaded rewrite by an adversary with full table write access. Both require an external anchor — periodic snapshot of the latest `record_hash` to S3 with object lock would close that gap. Out of scope for hackathon, called out explicitly in the module docstring (`audit.py:12-18`).

### Q: Why use `attribute_not_exists(prediction_id)` on the `put_item`?

**A:** Two-phase safety. UUIDs collide approximately never, but if two Lambda invocations (Step Functions retry, replay) generated the same UUID, we'd silently overwrite an audit row and the chain would corrupt. The `ConditionExpression` makes the second `put_item` fail with `ConditionalCheckFailedException` instead. See `audit.py:79`.

---

## Section 3 — Data + ML

### Q: Where do the predicted fire ellipses come from?

**A:** Anderson 1983 / Andrews 2018 surface fire spread model. Implemented in two places that produce identical output:
- **Server-side** in `functions/enrich/handler.py` — runs on every enriched fire event, writes the polygon back to DynamoDB.
- **Client-side** in `frontend/src/api/fires.js:60-130` — runs locally for mock fires (which arrive without server-side enrichment) so the visual is consistent across live and mock data.

Inputs: wind speed + bearing (from NOAA), fuel moisture proxy (NOAA RH + days-since-rain bucketed), slope estimate, fuel model bucket. Output: head/flank/back spread rates → ellipse over a 30-minute horizon.

We cap length-to-breadth ratio at 2.0 below 15 mph wind, 3.0 above (`fires.js:66-69`). Anderson's raw curve hits 5+ at single-digit winds, which reads as a needle on the map. The cap is a visual decision documented in the comments — the underlying physics is unmodified.

There's also a deliberate noise overlay (`NOISE_FREQS = [5, 11, 19]`) and "spot fingers" — concentrated in the front 90° arc — to make the perimeters look like real fires (which have spotting and ridgelines), not perfect ellipses. Seeded by `fire_id` so they're stable across renders.

### Q: How do you handle FIRMS satellite data and CAL FIRE data being different?

**A:** Both feed into the same `fires` DynamoDB table with a `source` field (`FIRMS` or `CALFIRE`) and a normalized schema — see CLAUDE.md "fire events use this normalized schema everywhere." Dedup is done by composite `fire_id` (typically `<source>-<external_id>`) so a fire reported by both sources doesn't double-count.

The GSI `source-detected_at-index` lets us query by source — useful for source-specific dashboards or for detecting feed outages (zero new FIRMS rows in 6h → satellite feed problem).

CAL FIRE often only publishes a centroid + acres without a polygon. The frontend (`fires.js`) synthesizes a small footprint when `geometry.type === 'Point'` so the fire stays visible on the map. Real polygons are preferred when available.

### Q: What does the SageMaker endpoint actually predict?

**A:** Two models behind one endpoint pattern (`infrastructure/stacks/ml_stack.py` provisions the housing; `functions/enrich/handler.py` calls them):
1. **Spread rate model** — XGBoost regression on (wind, RH, temp, fuel model, slope, radiative_power, hour-of-day, month) → km²/hour spread rate.
2. **Burn area model** — same features → predicted burned area at horizon T.

Both take CSV in, return CSV out (the standard SageMaker XGBoost container contract).

**Honest disclosure** if pressed: we don't have held-out validation metrics for the demo. The training story (FIRMS historical → 60-min next-detection labels → quantile XGBoost residuals against a Rothermel baseline) is laid out in `docs/FIRE_SPREAD_PREDICTION.md` but the full training pipeline isn't in the repo. The endpoint contract and integration are real; the model itself would need a real dataset before it could ship.

### Q: How do you compute `population_at_risk`?

**A:** Static density zones (`functions/enrich/handler.py`) — SoCal counties are bucketed into urban/suburban/rural with hard-coded population densities, multiplied by the predicted alert radius. **Not** a Census API lookup. Documented in the enrichment module — workshop AWS account doesn't have Location Service or Census API access provisioned, and a real census-tract intersection is a v2 feature.

The number is realistic in shape (urban SoCal fires return tens of thousands; rural returns hundreds) but isn't authoritative. We surface it as an input to the dispatch threshold (`POPULATION_TRIGGER=500` from `core_stack.py:225`), not as a precise count residents see.

### Q: Why three OR-thresholds in the dispatch trigger? Why not a single composite score?

**A:** See `functions/dispatch/handler.py`. Three thresholds OR'd: `risk_score≥0.6`, `spread≥2.0 km²/hr`, `population≥500`. Reasoning:
- A single composite hides why a fire was escalated. With OR-thresholds, the `dispatch_trigger_reason` field tells the dispatcher exactly which signal tripped.
- We weight each independently — a fast-moving fire in a rural area still escalates even if population is low. A composite would dilute that.
- Easy to tune individually based on dispatcher feedback without retraining the risk model.

EventBridge limitation: we can't OR across detail fields in the rule itself, so all `FireEnriched` events hit the dispatch Lambda and the Lambda is the actual gate. Documented inline.

---

## Section 4 — Frontend

### Q: How does the live update path work? WebSocket or polling?

**A:** Both, by design. See `frontend/src/api/websocket.js:1-30`.

- **WebSocket** (`VITE_WS_URL`) is the primary channel. Two message types: `fire_updated` (new perimeter / containment / acres for one fire — patched in place via Mapbox feature-state without a full reload) and `alert_sent` (safety gate ran, audit row written, SMS dispatched — surfaced to AlertBanner).
- **30s polling fallback** runs unconditionally — `setInterval(refreshFires, FIRE_REFRESH_MS)` in `FireMap.jsx:502`. If the WebSocket dies, polling carries the demo. If the WebSocket is hot, polling overwrites with identical data and the patch is a no-op.

The WebSocket auto-reconnects with exponential backoff (1s → 30s cap, `websocket.js:80`) so a transient blip doesn't kill live updates for the rest of the session.

### Q: What's the Mapbox layer architecture?

**A:** GeoJSON sources for fires, alert zones, evacuation routes, fire stations, and reservoirs (reservoir layer was deliberately removed from the demo — see commit history). Each layer uses Mapbox feature-state for the hover/select highlight without a full data round-trip.

The `patchFire(feature)` function (`FireMap.jsx:447`) updates a single fire's properties + alert zone in place, surgical update. Falls through silently if the map isn't ready yet — the next 30s polling refresh picks it up.

### Q: Why the "marching ants" perimeter animation?

**A:** Mapbox `line-dasharray` paint property animated via `requestAnimationFrame`. Visual cue that the perimeter is actively being modeled, not a static historical polygon. Each fire's animation phase is offset by `fire_id` hash so they don't pulse in sync (looks alive, not metronomic).

### Q: How are evacuation routes computed?

**A:** `frontend/src/api/evacRoutes.js`. For each fire, the frontend picks 2-4 candidate destination points outside the predicted alert zone, filters out destinations downwind of the fire (using the wind bearing from the enriched event), and calls Mapbox Directions API. Falls back to the unfiltered nearest if every candidate is downwind.

Routes are cached by `fire_id` so 30s polling doesn't re-hit the Directions API. Cache invalidates when the fire's perimeter changes.

The "Open in Google Maps" deep link uses the destination chosen here — same target as the in-app turn-by-turn.

### Q: How does the Resident vs Dispatcher panel work?

**A:** `frontend/src/DispatchPanel.jsx`. Same fire data, two confidence-bucketed framings:

| Confidence | Resident view | Dispatcher view |
|---|---|---|
| < 0.65 | "NO ALERT SENT" | "HUMAN REVIEW REQUIRED" |
| 0.65 – 0.85 | "ALERT SENT" | "AUTO-DISPATCH (FLAGGED)" |
| ≥ 0.85 | "ALERT SENT (HIGH CONFIDENCE)" | "AUTO-DISPATCH" |

The "✓ Verified · audit {hash}…" green chip shows the first 10 chars of the audit hash. The same hash is the `record_hash` written by the safety gate to DynamoDB — proof that the safety contract ran for this specific recommendation.

**Honest disclosure**: the dispatch panel data is currently stubbed — `frontend/src/api/dispatch.js` generates confidence, advisory text, dispatched units, and audit hash deterministically from the `fire_id` so the same fire always renders the same UI. The `safety_gate.py` Lambda + audit chain are real and deployed; the API integration that swaps the stub for live calls is a one-file change. The stub conforms to the response shape `safety_gate.py` already returns.

---

## Section 5 — Failure modes

### Q: What if the SageMaker endpoint is down?

**A:** Enrichment Lambda has fallback constants (the spread predictor falls back to a Rothermel baseline with reduced confidence). Reduced confidence trips the 0.65 gate, which routes to `HUMAN_REVIEW_REQUIRED`. So a SageMaker outage degrades to "human approves every dispatch" rather than "no dispatches" or worse, "auto-dispatched without confidence."

Documented as a hard constraint in `docs/FIRE_SPREAD_PREDICTION.md:83` ("Fallback is mandatory").

### Q: What if Bedrock is throttled or down?

**A:** `safety_gate.py` catches the exception, appends a `guardrails_outcome` row marked `passed=False, reason="guardrails service error"` to keep the audit chain forensically complete, and re-raises. Step Functions catches the failure and routes to `LogAndStop` — no alert sent. Manual dispatcher takeover required.

The audit row matters here: "we tried to validate, the validator was down, we did not send" is a defensible record. "Silent retry until it works" is not.

### Q: What if SNS rate-limits the per-resident publish?

**A:** Per-resident `sns.publish(PhoneNumber=...)` is in a loop in `functions/alert/sender.py`. Rate limit would surface as an exception on individual calls. We'd want to:
- Catch and continue (don't fail the whole batch on one resident).
- Append a per-resident outcome to the audit chain so we know who didn't get the message.
- Surface "X of Y residents notified" in the dispatcher UI.

Currently the loop doesn't do per-resident catch-and-continue — that's a known sharp edge. Honest answer: "the broadcast SNS topic always succeeds; per-resident is best-effort and we'd add resilience before production."

### Q: What if a Lambda times out mid-Step-Functions execution?

**A:** Step Functions task fails with `States.TaskFailed`. The `notify_and_wait` task has an explicit `add_catch(log_and_stop, errors=["States.TaskFailed"])` (`safety_stack.py:292`). The `safety_gate` task doesn't currently have a retry — a timeout would fail the whole execution. We could add a retry with backoff, but the safety gate is supposed to be fast (Bedrock + two DynamoDB writes); a real timeout indicates an actual problem worth alarming on, not retrying.

### Q: What if someone tampers with a row in the `wildfire-watch-audit` DynamoDB table directly?

**A:** Detected by `verify_chain` (covered above). What's missing: continuous monitoring. `verify_chain` is a function someone has to call; we don't yet have a CloudWatch scheduled rule that runs it across all fires periodically and alarms on failure. That's a half-day add — out of hackathon scope but called out as a known gap.

### Q: What if an attacker has full DynamoDB write access?

**A:** They can do a fully cascaded rewrite — recompute every row's hash chain forward from their tamper point — and `verify_chain` returns True. Same trust model as any system without an external anchor. The mitigation is periodic snapshots of the latest `record_hash` to S3 with Object Lock, or to a separate AWS account. Out of hackathon scope, documented in `audit.py:12-18`.

### Q: Why fail-closed instead of fail-open?

**A:** Wildfire alerts are a domain where a wrong message causes more harm than a missed one. "Evacuate now" sent in error → people clog roads, accidents, lawsuits. "All clear" sent in error → people stay in a fire's path. A missed alert → people rely on other channels (CAL FIRE direct, neighbors, news). Fail-closed errs toward the side where harm is recoverable.

This is `CLAUDE.md` rule #3 explicitly: confidence below 0.65 → human review, no auto-dispatch. We carry it through every error path: Bedrock down → no alert. Guardrails error → no alert. Dispatcher timeout → no alert. SageMaker degraded → human review.

---

## Section 6 — Workshop account constraints (read this last, memorize it)

### Q: Why can't I see the SMS arrive on my phone right now?

**A:** This deploys to an AWS workshop account (`933557033057`, `us-west-2`) provisioned for AWS CloudHacks 2026. SNS SMS is in sandbox mode with no origination identity available in `us-west-2` or `us-east-1`, and `us-east-2` is explicitly denied by SCP. Standalone phone-number sandbox verification fails the same way.

This is a workshop account constraint, not an architectural problem. The `sns.publish(PhoneNumber=...)` call is wired up in `functions/alert/sender.py`; the alert sender's IAM role has `sns:Publish` on `*`. In a real account with origination identities (long codes, toll-free numbers, or 10DLC), the SMS would land on the resident's phone. Demo strategy here is "Option C" — show the audit + safety story instead of live delivery.

### Q: What other workshop constraints affected the design?

**A:** Three notable ones:
- **Pinpoint blocked** by SCP → `sns.publish(PhoneNumber=...)` direct (described above).
- **QLDB no longer accepting new ledger creations** (AWS-wide, not workshop-specific) → DynamoDB hash chain.
- **`AdministratorAccess-Amplify` managed policy not attachable** → inline IAM policy scoped to `/aws/amplify/*` log groups (sufficient for hosting-only Amplify apps).

All three pivots are in commit history and `CLAUDE.md` notes the pivot dates. Not retroactive bluster — they're documented decisions.

### Q: How do you redeploy if the workshop account expires?

**A:** Single command — `cdk deploy --all`. All six stacks are in `infrastructure/stacks/` with `RemovalPolicy.DESTROY` on the data tables (intentional — workshop account, ephemeral resources). Frontend is a `vite build` + Amplify zip upload. The full deploy from a clean account is documented in `DEPLOY.md`.

### Q: What costs are you incurring?

**A:** All workshop-credit. We use pay-per-request DynamoDB (no provisioned capacity), Lambda (sub-millisecond billing on small functions), Bedrock (~$0.003/1K input + $0.015/1K output for Sonnet 4.6, with prompt caching cutting input by ~90%), SageMaker (one endpoint, smallest instance), Kinesis (2 shards), and Amplify hosting. No reserved capacity, no savings plans, no commitments — workshop account is ephemeral.

If we had to estimate per-day cost for the demo footprint: <$5/day on the AWS public price list, less in practice with prompt caching and the 30s polling cadence.

---

## Quick-reference: claims you can defend

| Claim | Where to point |
|---|---|
| "Audit row written before Guardrails" | `safety_gate.py:81-131`, step 3 before step 4 |
| "Hash chain detects tamper" | `audit.py:142` `verify_chain` |
| "Three actions: APPROVED / HUMAN_REVIEW / BLOCKED" | `safety_gate.py:113-131` |
| "Fail-closed on dispatcher timeout" | `safety_stack.py:298` `add_catch` on `States.Timeout` |
| "Two-layer Guardrails" | `guardrails.py:1-45` module docstring |
| "PII anonymized" | `safety_stack.py:123-130` `pii_entities_config` |
| "Confidence threshold 0.65" | `CLAUDE.md` rule #3, `safety_gate.py:53` |
| "Dispatch confidence is boundary-distance heuristic" | `ml/dispatch_model/features.py:50-64` |
| "Detection confidence is FIRMS satellite quality" | `functions/scraper/firms_poller.py:42-52` |
| "No PII in logs" | `sender.py` (logs `resident_id`, never phone) |
| "Bedrock prompt caching" | `advisory_prompt.py:106-112` `cache_control: ephemeral` |
| "WebSocket + polling fallback" | `websocket.js:18-20`, `FireMap.jsx:502` |
| "Anderson 1983 fire ellipse" | `enrich/handler.py`, `fires.js:60-130` |

## Claims you should NOT make

| Claim | Why not |
|---|---|
| "QLDB-equivalent" | Different trust model. Say "tamper-evident hash chain." |
| "Production-ready" | Hackathon prototype, ephemeral account. Say "production-shaped architecture." |
| "Validated against ground truth" | No held-out metrics. Say "framework supports it once trained." |
| "Real-time SMS to thousands" | Workshop account blocks. Say "the call is wired; production account flips it on." |
| "Zero PII anywhere" | We store phone numbers in DynamoDB to send SMS. PII is in the system, just never in logs or AI output.|
| "End-to-end ML pipeline" | Training pipeline isn't in repo. Endpoint contract + integration are real. |
