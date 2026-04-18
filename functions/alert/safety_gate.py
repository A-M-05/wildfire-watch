"""Safety gate Lambda (#21) - the single choke point before SNS dispatch.

Every advisory passes through here. The order of operations is the safety
contract; reordering breaks #32.

  1. Generate advisory via Bedrock (``ml.bedrock.advisory_prompt``)
  2. Append the prediction row to the audit hash-chain
     (``functions.alert.audit.log_prediction``). MUST commit before the
     guardrail call so even blocked advisories leave an auditable record
     of what was generated.
  3. Validate via Bedrock Guardrails (``ml.bedrock.guardrails``)
  4. Append a ``guardrails_outcome`` row linked to the prediction
     (never mutate the prior row - that breaks the SHA-256 chain)
  5. Confidence threshold check

Output - Step Functions (#19) routes on ``action``:

  {
    "action": "APPROVED" | "HUMAN_REVIEW_REQUIRED" | "BLOCKED",
    "prediction_id": str,
    "advisory": {"sms": str, "brief": str},   # APPROVED + HUMAN_REVIEW
    "blocked_reason": str | None,             # BLOCKED
  }

Why the third action: a guardrails block isn't human-fixable - the
advisory itself is unsafe. HUMAN_REVIEW_REQUIRED is for the orthogonal
case where the advisory is safe but model confidence is low.

Failure semantics: if Bedrock advisory generation raises, NO audit row
is written - nothing safety-relevant happened. If ``log_prediction``
raises, the function halts (no guardrail call attempted) - we never
proceed past step 2 without a committed audit row.
"""

import os

from functions.alert.audit import append_guardrail_outcome, log_prediction
from ml.bedrock.advisory_prompt import generate_advisory
from ml.bedrock.guardrails import validate_advisory

DEFAULT_CONFIDENCE_THRESHOLD = 0.65


def handler(event, context=None):
    fire_event = event["fire_event"]
    recommendation = event["recommendation"]
    threshold = float(
        os.environ.get("WW_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD)
    )

    advisory = generate_advisory(fire_event, recommendation)

    prediction_id = log_prediction(fire_event["fire_id"], recommendation, advisory)

    guardrail_result = validate_advisory(
        advisory["sms"], confidence=recommendation["confidence"]
    )

    append_guardrail_outcome(
        fire_event["fire_id"],
        prediction_id,
        passed=guardrail_result["passed"],
        reason=guardrail_result.get("blocked_reason"),
    )

    if not guardrail_result["passed"]:
        return {
            "action": "BLOCKED",
            "prediction_id": prediction_id,
            "blocked_reason": guardrail_result["blocked_reason"],
        }

    if recommendation["confidence"] < threshold:
        return {
            "action": "HUMAN_REVIEW_REQUIRED",
            "prediction_id": prediction_id,
            "advisory": advisory,
        }

    return {
        "action": "APPROVED",
        "prediction_id": prediction_id,
        "advisory": advisory,
    }
