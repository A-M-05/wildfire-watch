"""Safety gate Lambda (#21) - the single choke point before SNS dispatch.

Every advisory passes through here. The order of operations is the safety
contract; reordering breaks #32.

  1. Validate input shape (clear errors for Step Functions to surface)
  2. Generate advisory via Bedrock (``ml.bedrock.advisory_prompt``)
  3. Append the prediction row to the audit hash-chain
     (``functions.alert.audit.log_prediction``). MUST commit before the
     guardrail call so even blocked advisories leave an auditable record
     of what was generated.
  4. Validate via Bedrock Guardrails (``ml.bedrock.guardrails``)
  5. Append a ``guardrails_outcome`` row linked to the prediction
     (never mutate the prior row - that breaks the SHA-256 chain)
  6. Confidence threshold check

Output - Step Functions (#19) routes on ``action``:

  {
    "action": "APPROVED" | "HUMAN_REVIEW_REQUIRED" | "BLOCKED",
    "prediction_id": str,
    "advisory": {"sms": str, "brief": str},   # APPROVED + HUMAN_REVIEW
    "blocked_reason": str | None,             # BLOCKED
  }

Why three actions: a guardrails block isn't human-fixable - the advisory
itself is unsafe. HUMAN_REVIEW_REQUIRED is for the orthogonal case where
the advisory is safe but model confidence is low.

Failure semantics:
  * Bad input -> raise ValueError with a clear message; no audit row.
  * Bedrock advisory generation raises -> propagate; no audit row.
  * log_prediction raises -> propagate; halt before guardrails. The
    audit contract requires the row be committed before any
    safety-relevant downstream action.
  * validate_advisory raises (Bedrock outage, throttling) -> append a
    ``guardrails_outcome`` row marked ``passed=False`` with an error
    reason, THEN re-raise. This keeps the audit chain forensically
    complete even when the validation service is down - otherwise
    Step Functions retries pile up orphan prediction rows with no
    outcome to explain them.
  * append_guardrail_outcome raising -> propagate; the prediction is
    already committed so the chain is intact, just incomplete. Rare;
    Step Functions retry will produce a clean chain.
"""

import os

from functions.alert.audit import append_guardrail_outcome, log_prediction
from ml.bedrock.advisory_prompt import generate_advisory
from ml.bedrock.guardrails import validate_advisory

DEFAULT_CONFIDENCE_THRESHOLD = 0.65


def _require(d: dict, key: str, where: str):
    if not isinstance(d, dict) or key not in d:
        raise ValueError(f"missing required field: {where}.{key}")
    return d[key]


def _validate_input(event: dict) -> tuple[dict, dict]:
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    fire_event = _require(event, "fire_event", "event")
    recommendation = _require(event, "recommendation", "event")
    _require(fire_event, "fire_id", "event.fire_event")
    _require(recommendation, "confidence", "event.recommendation")
    return fire_event, recommendation


def _validate_advisory_shape(advisory) -> dict:
    # Bedrock occasionally returns malformed JSON despite the prompt asking
    # for a strict schema. Catch this BEFORE the audit row is written so
    # we don't end up with an orphan prediction in the chain.
    if not isinstance(advisory, dict) or "sms" not in advisory:
        raise ValueError("advisory must be a dict with an 'sms' key")
    return advisory


def handler(event, context=None):
    fire_event, recommendation = _validate_input(event)
    threshold = float(
        os.environ.get("WW_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD)
    )

    advisory = _validate_advisory_shape(generate_advisory(fire_event, recommendation))

    prediction_id = log_prediction(fire_event["fire_id"], recommendation, advisory)

    try:
        guardrail_result = validate_advisory(
            advisory["sms"], confidence=recommendation["confidence"]
        )
    except Exception as exc:
        # Forensic record: the chain must show that validation was attempted
        # and failed, otherwise the prediction row is an unexplained orphan.
        append_guardrail_outcome(
            fire_event["fire_id"],
            prediction_id,
            passed=False,
            reason=f"guardrails service error: {type(exc).__name__}",
        )
        raise

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
