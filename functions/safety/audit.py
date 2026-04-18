"""
DynamoDB audit hash-chain — issue #17.

Implements the immutable audit log for every prediction and alert.
The "immutability" guarantee comes from SHA-256 chaining: each record
hashes the record_hash of its predecessor into its own prev_hash field.
Tampering with any historical record breaks the chain, which verify_chain()
will detect.

Hard rule (from CLAUDE.md and safety_stack.py): the audit PutItem MUST
complete before any downstream action (SNS publish, Step Functions start).
If put_item raises, catch it here and re-raise — do not swallow the error
and proceed to alerting.

DynamoDB table schema (provisioned in safety_stack.py issue #3):
  PK: prediction_id (uuid4)
  SK: written_at (ISO-8601)
  GSI: fire_id-written_at-index (fire_id PK, written_at SK)
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

# Lazily resolved — boto3 resource is not created until first call so unit
# tests can patch before any AWS credentials are needed.
def _table():
    dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    return dynamodb.Table(os.environ["WW_AUDIT_TABLE"])


# The genesis hash seeds the chain for the very first record of each fire.
# Using 64 zeros so it's clearly synthetic — no SHA-256 hash looks like this.
GENESIS_HASH = "0" * 64


def _canonical_hash(record: dict) -> str:
    """SHA-256 of the canonical JSON of a record (excluding record_hash itself).

    sort_keys=True and separators=(",", ":") ensure the JSON is deterministic
    across Python versions and dict insertion orders — a requirement for the
    chain to be reproducible by any auditor.
    """
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _latest_hash_for_fire(fire_id: str) -> str:
    """Fetch the record_hash of the most recent audit entry for this fire.

    Returns GENESIS_HASH if this is the first record — no previous entry exists.
    Queries the GSI sorted newest-first so we only read one item.
    """
    resp = _table().query(
        IndexName="fire_id-written_at-index",
        KeyConditionExpression=Key("fire_id").eq(fire_id),
        ScanIndexForward=False,  # newest first
        Limit=1,
        ProjectionExpression="record_hash",
    )
    items = resp.get("Items", [])
    return items[0]["record_hash"] if items else GENESIS_HASH


def log_prediction(fire_id: str, recommendation: dict, advisory: dict) -> str:
    """Write an audit record for a dispatch prediction.

    Called BEFORE the safety gate runs. Must succeed before any other action.

    Args:
        fire_id: the fire this prediction is for
        recommendation: SageMaker output dict (recommendation, confidence, probabilities)
        advisory: Bedrock advisory dict (sms, brief)

    Returns:
        prediction_id: uuid4 string — passed to subsequent audit calls for this event

    Raises:
        botocore.exceptions.ClientError: on DynamoDB failure — caller must not proceed
    """
    prediction_id = str(uuid.uuid4())
    written_at = datetime.now(timezone.utc).isoformat()

    record = {
        "prediction_id": prediction_id,
        "written_at": written_at,
        "fire_id": fire_id,
        "event": "prediction",
        "recommendation": recommendation,
        "advisory_text": advisory.get("brief", ""),
        "sms_text": advisory.get("sms", ""),
        "confidence": str(recommendation.get("confidence", 0)),  # DynamoDB Decimal-safe
        "guardrails_passed": None,
        "alert_sent": False,
        "blocked_reason": None,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    # record_hash is computed over all fields except itself.
    record["record_hash"] = _canonical_hash(record)

    # ConditionExpression prevents silent overwrite if a uuid4 ever collides
    # (astronomically unlikely, but the audit log must be exact).
    _table().put_item(
        Item=record,
        ConditionExpression="attribute_not_exists(prediction_id)",
    )

    return prediction_id


def append_guardrail_outcome(
    fire_id: str,
    prediction_id: str,
    passed: bool,
    reason: str | None,
) -> None:
    """Append a guardrails outcome record to the chain.

    Append-only — never mutate the original prediction row. Mutation would
    break the hash chain and destroy the audit trail.

    Args:
        fire_id: same fire as the original prediction
        prediction_id: the prediction this outcome links to
        passed: True if Guardrails allowed the advisory
        reason: blocked_reason string if passed=False, else None
    """
    record = {
        "prediction_id": str(uuid.uuid4()),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "fire_id": fire_id,
        "event": "guardrails_outcome",
        "linked_prediction_id": prediction_id,
        "guardrails_passed": passed,
        "blocked_reason": reason,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    record["record_hash"] = _canonical_hash(record)
    _table().put_item(
        Item=record,
        ConditionExpression="attribute_not_exists(prediction_id)",
    )


def mark_alert_sent(fire_id: str, prediction_id: str, alert_id: str) -> None:
    """Append a record confirming the SNS alert was successfully published.

    Called by the alert sender Lambda (#22) after sns.publish() returns 200.
    The audit chain is only complete when this record exists — it proves the
    SMS went out and exactly which prediction it was based on.
    """
    record = {
        "prediction_id": str(uuid.uuid4()),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "fire_id": fire_id,
        "event": "alert_sent",
        "linked_prediction_id": prediction_id,
        "alert_id": alert_id,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    record["record_hash"] = _canonical_hash(record)
    _table().put_item(
        Item=record,
        ConditionExpression="attribute_not_exists(prediction_id)",
    )


def verify_chain(fire_id: str) -> bool:
    """Verify the hash chain is intact for a given fire.

    Replays every audit record in chronological order, recomputing each
    record_hash and checking it matches the stored value. Also verifies
    that each record's prev_hash matches the record_hash of its predecessor.

    Returns True if the chain is intact, False if any record was tampered with.
    Used by the /safety-audit slash command and issue #32 integration tests.
    """
    resp = _table().query(
        IndexName="fire_id-written_at-index",
        KeyConditionExpression=Key("fire_id").eq(fire_id),
        ScanIndexForward=True,  # oldest first — must traverse in order
    )
    items = resp.get("Items", [])

    if not items:
        return True  # no records = vacuously valid

    expected_prev = GENESIS_HASH
    for item in items:
        # Check prev_hash linkage.
        if item.get("prev_hash") != expected_prev:
            return False

        # Recompute record_hash over all fields except record_hash itself.
        stored_hash = item.get("record_hash")
        recomputed = _canonical_hash({k: v for k, v in item.items() if k != "record_hash"})
        if recomputed != stored_hash:
            return False

        expected_prev = stored_hash

    return True
