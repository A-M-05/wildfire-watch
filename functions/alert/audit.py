"""DynamoDB hash-chain audit log for the AI safety layer.

Every prediction, guardrails outcome, and alert event is appended as a new
row to ``WW_AUDIT_TABLE``. Each row commits the SHA-256 hash of the prior
row for the same fire, so any tampering is detectable by replaying the
chain (see ``verify_chain``).

Hard rule: callers must complete the ``put_item`` before any downstream
action (SMS publish, Step Functions transition). If the write raises,
halt — do not proceed to alerting.

Trust model: ``verify_chain`` detects mutation, deletion of a middle row,
and inserted rows whose ``prev_hash`` is fabricated. It does NOT detect
deletion of the most recent row, or a fully cascaded rewrite by an
adversary with table write access — both require an external anchor
(e.g. periodic snapshot of the latest ``record_hash`` off-system). Out of
scope for the hackathon; the contract test (#32) anchors against a known
good post-condition instead.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

GENESIS_HASH = "0" * 64

_dynamodb = boto3.resource("dynamodb")


def _table():
    return _dynamodb.Table(os.environ["WW_AUDIT_TABLE"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_hash(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _latest_hash_for_fire(fire_id: str) -> str:
    resp = _table().query(
        IndexName="fire_id-written_at-index",
        KeyConditionExpression=Key("fire_id").eq(fire_id),
        ScanIndexForward=False,
        Limit=1,
        ProjectionExpression="record_hash",
    )
    items = resp.get("Items", [])
    return items[0]["record_hash"] if items else GENESIS_HASH


def _to_decimal(value):
    """DynamoDB rejects native floats — convert recursively before put."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _put_record(record: dict) -> None:
    # Convert floats to Decimal first so the hash computed at write-time
    # matches the hash recomputed at read-time (DynamoDB returns Decimals).
    item = _to_decimal(record)
    item["record_hash"] = _canonical_hash(item)
    _table().put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(prediction_id)",
    )


def log_prediction(fire_id: str, recommendation: dict, advisory: dict) -> str:
    """Append the initial prediction row. Returns the new prediction_id."""
    prediction_id = str(uuid.uuid4())
    record = {
        "prediction_id": prediction_id,
        "written_at": _now(),
        "fire_id": fire_id,
        "event": "prediction",
        "recommendation": recommendation,
        "advisory_text": advisory.get("brief", ""),
        "sms_text": advisory.get("sms", ""),
        "confidence": recommendation["confidence"],
        "guardrails_passed": None,
        "alert_sent": False,
        "blocked_reason": None,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    _put_record(record)
    return prediction_id


def append_guardrail_outcome(
    fire_id: str,
    prediction_id: str,
    passed: bool,
    reason: str | None,
) -> str:
    """Append a guardrails outcome row linked to the original prediction."""
    new_id = str(uuid.uuid4())
    record = {
        "prediction_id": new_id,
        "written_at": _now(),
        "fire_id": fire_id,
        "event": "guardrails_outcome",
        "linked_prediction_id": prediction_id,
        "guardrails_passed": passed,
        "blocked_reason": reason,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    _put_record(record)
    return new_id


def mark_alert_sent(fire_id: str, prediction_id: str, alert_id: str) -> str:
    """Append an alert_sent event row. Never UpdateItem the prior row."""
    new_id = str(uuid.uuid4())
    record = {
        "prediction_id": new_id,
        "written_at": _now(),
        "fire_id": fire_id,
        "event": "alert_sent",
        "linked_prediction_id": prediction_id,
        "alert_id": alert_id,
        "prev_hash": _latest_hash_for_fire(fire_id),
    }
    _put_record(record)
    return new_id


def verify_chain(fire_id: str) -> bool:
    """Replay the chain for a fire; return False on any broken link or tamper.

    Paginates the GSI query — a chain spanning multiple 1MB pages must be
    fully replayed or a partial replay would falsely return True.
    """
    expected_prev = GENESIS_HASH
    last_key = None
    while True:
        kwargs = {
            "IndexName": "fire_id-written_at-index",
            "KeyConditionExpression": Key("fire_id").eq(fire_id),
            "ScanIndexForward": True,
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _table().query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("prev_hash") != expected_prev:
                return False
            recomputed = _canonical_hash({k: v for k, v in item.items() if k != "record_hash"})
            if recomputed != item.get("record_hash"):
                return False
            expected_prev = item["record_hash"]
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return True
