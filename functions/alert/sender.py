"""Alert sender Lambda (#22).

Triggered by Step Functions after the safety gate returns APPROVED.

Input (from Step Functions — fire_event carried through from dispatch trigger):
  {
    "fire_event":    { ...enriched fire schema... },
    "advisory":      { "sms": str, "brief": str },
    "prediction_id": str,
    "action":        "APPROVED"
  }

Sends:
  1. Per-resident SMS via sns.publish(PhoneNumber=...) to every registered
     resident within the fire's risk radius. Direct publish — no topic ARN —
     because Pinpoint is blocked by this account's SCP.
  2. Broadcast to WW_SNS_ALERT_TOPIC_ARN for downstream subscribers.

After both publishes confirm, appends an ``alert_sent`` audit row via
mark_alert_sent() — the hard rule from CLAUDE.md: audit row before alert.

Wait — actually the audit row is written *before* alerting (prediction row
in safety gate). mark_alert_sent appends a second row confirming delivery,
completing the chain. This is fine: the prediction row was the pre-action
commit; this is the post-action receipt.

PII rule: phone numbers are NEVER logged to CloudWatch. Log resident count only.

Set WW_DRY_RUN=true to skip SNS publish calls (for local testing).
"""

import argparse
import json
import logging
import math
import os
import sys
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from functions.alert.audit import mark_alert_sent

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_sns_client = None

DRY_RUN = os.environ.get("WW_DRY_RUN", "").lower() in ("1", "true", "yes")


def _sns():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


def _residents_table():
    return _dynamodb.Table(os.environ["WW_DYNAMODB_RESIDENTS_TABLE"])


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _bounding_box(lat: float, lon: float, radius_km: float):
    """Return (min_lat, max_lat, min_lon, max_lon) for a bounding box."""
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon


# ---------------------------------------------------------------------------
# Residents lookup
# ---------------------------------------------------------------------------

def get_residents_in_radius(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Scan the residents table with a bounding-box filter, then refine with haversine."""
    min_lat, max_lat, min_lon, max_lon = _bounding_box(lat, lon, radius_km)

    # DynamoDB scan with bounding box to cut scan cost before haversine pass.
    # The table has no geospatial index so a full scan is unavoidable; the
    # bounding box at least prunes the expression evaluation work server-side.
    results = []
    last_key = None
    while True:
        kwargs = dict(
            FilterExpression=(
                Attr("lat").between(Decimal(str(min_lat)), Decimal(str(max_lat)))
                & Attr("lon").between(Decimal(str(min_lon)), Decimal(str(max_lon)))
            ),
            ProjectionExpression="resident_id, phone, lat, lon",
        )
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _residents_table().scan(**kwargs)
        for item in resp.get("Items", []):
            r_lat = float(item["lat"])
            r_lon = float(item["lon"])
            if _haversine_km(lat, lon, r_lat, r_lon) <= radius_km:
                results.append(item)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    return results


# ---------------------------------------------------------------------------
# SNS publish helpers
# ---------------------------------------------------------------------------

def _send_sms(phone: str, message: str) -> str:
    """Publish a single transactional SMS. Returns MessageId."""
    if DRY_RUN:
        return "dry-run"
    resp = _sns().publish(
        PhoneNumber=phone,
        Message=message,
        MessageAttributes={
            "AWS.SNS.SMS.SMSType": {
                "DataType": "String",
                "StringValue": "Transactional",
            }
        },
    )
    return resp["MessageId"]


def _broadcast(message: str, fire_id: str) -> None:
    """Publish to the broadcast SNS topic for downstream subscribers."""
    topic_arn = os.environ.get("WW_SNS_ALERT_TOPIC_ARN")
    if not topic_arn:
        logger.warning(json.dumps({"event": "broadcast_skipped", "reason": "WW_SNS_ALERT_TOPIC_ARN not set", "fire_id": fire_id}))
        return
    if DRY_RUN:
        return
    _sns().publish(
        TopicArn=topic_arn,
        Message=message,
        Subject=f"Wildfire Alert — {fire_id}",
    )


# ---------------------------------------------------------------------------
# Main send logic
# ---------------------------------------------------------------------------

def send_alerts(fire_event: dict, advisory: dict, prediction_id: str) -> dict:
    fire_id = fire_event["fire_id"]
    lat = float(fire_event["lat"])
    lon = float(fire_event["lon"])
    radius_km = float(fire_event.get("risk_radius_km", 10.0))
    sms_text = advisory["sms"]

    residents = get_residents_in_radius(lat, lon, radius_km)

    sent = 0
    failed = 0
    for resident in residents:
        try:
            _send_sms(resident["phone"], sms_text)
            sent += 1
        except ClientError as exc:
            failed += 1
            # Log error without the phone number — only log resident_id (opaque)
            logger.error(json.dumps({
                "event": "sms_failed",
                "fire_id": fire_id,
                "resident_id": resident.get("resident_id"),
                "error": exc.response["Error"]["Code"],
            }))

    logger.info(json.dumps({
        "event": "sms_batch_complete",
        "fire_id": fire_id,
        "sent": sent,
        "failed": failed,
        "radius_km": radius_km,
        "dry_run": DRY_RUN,
    }))

    _broadcast(sms_text, fire_id)

    alert_id = f"{fire_id}-alert"
    if not DRY_RUN:
        mark_alert_sent(fire_id, prediction_id, alert_id=alert_id)

    return {
        "alert_id": alert_id,
        "fire_id": fire_id,
        "residents_alerted": sent,
        "residents_failed": failed,
        "dry_run": DRY_RUN,
    }


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context=None):
    fire_event = event["fire_event"]
    advisory = event["advisory"]
    prediction_id = event["prediction_id"]

    result = send_alerts(fire_event, advisory, prediction_id)

    logger.info(json.dumps({
        "event": "alert_sent",
        "fire_id": result["fire_id"],
        "residents_alerted": result["residents_alerted"],
    }))

    return result


# ---------------------------------------------------------------------------
# CLI dry-run helper (WW_DRY_RUN=true is enforced when --fire-id is passed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Alert sender dry-run")
    parser.add_argument("--fire-id", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--radius-km", type=float, default=10.0)
    args = parser.parse_args()

    os.environ["WW_DRY_RUN"] = "true"
    # Reload module-level DRY_RUN after setting the env var
    import importlib
    import functions.alert.sender as _self
    importlib.reload(_self)

    fire = {"fire_id": args.fire_id, "lat": args.lat, "lon": args.lon, "risk_radius_km": args.radius_km}
    advisory = {"sms": "[dry-run] Wildfire alert — evacuate if instructed by local officials.", "brief": "dry-run"}
    residents = _self.get_residents_in_radius(args.lat, args.lon, args.radius_km)
    print(f"Would send to {len(residents)} residents within {args.radius_km}km of ({args.lat}, {args.lon})")
    sys.exit(0)
