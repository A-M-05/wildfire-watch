# Safety Agent

**Owns:** QLDB audit log, Bedrock Guardrails, SageMaker Clarify, Model Monitor, Step Functions gate
**Issues:** #16, #17, #18, #19, #20, #21

## Responsibilities

This agent owns the AI Safety layer. Every line of code here is load-bearing — it's what makes the system trustworthy enough to use in a real emergency.

## The four safety mechanisms

1. **Bedrock Guardrails (#16)** — blocks advisories with false certainty, PII, or unsafe content
2. **QLDB audit log (#17)** — immutable record of every prediction and alert, written BEFORE action
3. **SageMaker Clarify (#18)** — equity audit of the dispatch model
4. **Human-in-the-loop gate (#19)** — Step Functions pause when confidence < 0.65
5. **Model Monitor (#20)** — detects distribution shift in fire behavior inputs
6. **Safety gate Lambda (#21)** — orchestrates all of the above in one place

## File layout

```
functions/
└── alert/
    ├── safety_gate.py     ← Issue #21: orchestrates Guardrails + QLDB + confidence
    └── requirements.txt
ml/
└── bedrock/
    └── guardrails.py      ← Issue #16: Guardrails config + validation call
```

## Issue #16 — Bedrock Guardrails config

Guardrail rules to configure in Bedrock console + CDK:
- **Block** advisories claiming certainty when confidence < 0.65 (detect phrases like "you are safe", "no danger")
- **Block** advisories naming specific individuals
- **Block** advisories contradicting the official confidence score
- **Allow** standard emergency management language
- **PII filter** — strip any phone numbers or addresses that leak into the advisory text

```python
# guardrails.py
def validate_advisory(advisory_text: str, confidence: float) -> dict:
    bedrock = boto3.client('bedrock-runtime')
    response = bedrock.apply_guardrail(
        guardrailIdentifier=os.environ['WW_BEDROCK_GUARDRAIL_ID'],
        guardrailVersion='DRAFT',
        source='OUTPUT',
        content=[{'text': {'text': advisory_text}}]
    )
    return {
        'passed': response['action'] == 'NONE',
        'blocked_reason': response.get('outputs', [{}])[0].get('text')
    }
```

## Issue #17 — QLDB audit logging

**Hard rule:** QLDB write must complete before any alert is sent. This is a contract, not a preference.

```python
# Write a prediction record
def log_prediction(fire_id, dispatch_recommendation, confidence, advisory_text):
    qldb_driver.execute_lambda(
        lambda txn: txn.execute_statement(
            "INSERT INTO predictions ?",
            {
                'fire_id': fire_id,
                'timestamp': datetime.utcnow().isoformat(),
                'dispatch_recommendation': dispatch_recommendation,
                'confidence': confidence,
                'advisory_text': advisory_text,
                'guardrails_passed': None,  # updated after validation
                'alert_sent': False
            }
        )
    )

# Update after alert fires
def mark_alert_sent(prediction_id, alert_id):
    qldb_driver.execute_lambda(
        lambda txn: txn.execute_statement(
            "UPDATE predictions SET alert_sent = true, alert_id = ? WHERE prediction_id = ?",
            alert_id, prediction_id
        )
    )
```

## Issue #18 — Clarify bias audit

Run after #13 (model deployed). Check for disparate impact across:
- Income level (by ZIP code, from Census data)
- Urban vs. rural
- Historical response time data

Output: a bias report PDF in S3. Flag if any group receives >15% slower recommended response.

## Issue #19 — Step Functions human gate

State machine flow:
```
EvaluateConfidence → [confidence >= 0.65] → AutoApprove → AlertSender
                  → [confidence < 0.65]  → NotifyDispatcher → WaitForApproval (timeout: 5min)
                                                             → [approved] → AlertSender
                                                             → [timeout]  → EscalateAndAlert
```

## Issue #20 — Model Monitor

Baseline: distribution of input features from training data.
Schedule: hourly check against live inference inputs.
Alert: if Jensen-Shannon divergence > 0.3, emit CloudWatch alarm → SNS notification to ML team.

## Issue #21 — Safety gate Lambda

This Lambda is the single choke point. Nothing reaches Pinpoint without passing through here.

```python
def handler(event, context):
    fire_event = event['fire_event']
    recommendation = event['recommendation']

    # 1. Generate advisory via Bedrock
    advisory = generate_advisory(fire_event, recommendation)

    # 2. Write to QLDB (MUST happen before anything else)
    prediction_id = log_prediction(fire_event['fire_id'], recommendation, advisory)

    # 3. Validate with Guardrails
    guardrail_result = validate_advisory(advisory['text'], recommendation['confidence'])
    if not guardrail_result['passed']:
        update_qldb_blocked(prediction_id, guardrail_result['blocked_reason'])
        raise ValueError(f"Advisory blocked by Guardrails: {guardrail_result['blocked_reason']}")

    # 4. Check confidence threshold
    if recommendation['confidence'] < float(os.environ['WW_CONFIDENCE_THRESHOLD']):
        return {'action': 'HUMAN_REVIEW_REQUIRED', 'prediction_id': prediction_id}

    return {'action': 'APPROVED', 'prediction_id': prediction_id, 'advisory': advisory}
```

## Verification

```bash
# Safety contract test — QLDB record must exist before alert
python -m pytest tests/test_safety_contract.py -v
# See issue #32 for the full contract test
```
