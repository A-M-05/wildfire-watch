"""
Kinesis consumer Lambda — issue #8.

Reads normalized fire events from the wildfire-watch-fire-events Kinesis
stream and writes them to the DynamoDB fires table. The pollers (#6 FIRMS,
#7 CAL FIRE) already emit records in the canonical schema from CLAUDE.md;
this Lambda is the boundary that lands them in DynamoDB so the enrichment
Lambda (#9, DynamoDB Streams trigger) can pick them up.

CAL FIRE records carry `_calfire_*` extras the poller couldn't fold into the
canonical schema (name, acres burned). Strip the underscore prefix here so
the GET /fires endpoint (#105) can surface them via its property whitelist.

A TTL of 7 days is set on every item so stale fires fall off the table
without us running a janitor — the dispatch + alert paths already pull state
from EventBridge, so old DynamoDB rows have no downstream consumers.

Trigger: KinesisEventSource(fire_stream, starting_position=LATEST, batch_size=10)
"""

import base64
import json
import logging
import os
import time
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
FIRES_TABLE = os.environ.get("WW_DYNAMODB_FIRES_TABLE", "fires")
TTL_DAYS = int(os.environ.get("WW_FIRES_TTL_DAYS", "7"))

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=_REGION).Table(FIRES_TABLE)
    return _table


def _to_decimal(v):
    """DynamoDB Numeric requires Decimal — recurse through nested structures."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_decimal(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_to_decimal(i) for i in v]
    return v


def _normalize(event: dict) -> dict | None:
    """Apply the last bit of cleanup the pollers can't do.

    Returns None for records we should skip (missing required fields, dedup
    state rows that snuck onto the stream, etc.).
    """
    fire_id = event.get("fire_id")
    detected_at = event.get("detected_at")
    if not fire_id or not detected_at:
        return None

    # CAL FIRE dedup rows are keyed CALFIRE_STATE#<id> and only ever live in
    # DynamoDB — the poller doesn't push them to Kinesis, but guard anyway.
    if str(fire_id).startswith("CALFIRE_STATE#"):
        return None

    # Lift CAL FIRE extras into the canonical name/acres fields surfaced by
    # /fires (the public attribute whitelist drops anything underscore-prefixed).
    if "_calfire_name" in event and "name" not in event:
        event["name"] = event.pop("_calfire_name")
    if "_calfire_acres" in event and "acres_burned" not in event:
        event["acres_burned"] = event.pop("_calfire_acres")
    event.pop("_calfire_unique_id", None)

    event.setdefault("last_updated", detected_at)
    event["ttl"] = int(time.time()) + TTL_DAYS * 86400
    return _to_decimal(event)


def handler(event, context):
    table = _get_table()
    written, skipped, errors = 0, 0, 0

    for record in event.get("Records", []):
        raw = record.get("kinesis", {}).get("data")
        if not raw:
            skipped += 1
            continue
        try:
            payload = json.loads(base64.b64decode(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("ingest_decode_failed seq=%s error=%s",
                           record.get("kinesis", {}).get("sequenceNumber"), exc)
            errors += 1
            continue

        item = _normalize(payload)
        if item is None:
            skipped += 1
            continue

        try:
            table.put_item(Item=item)
            written += 1
            logger.info("ingest_put fire_id=%s source=%s detected_at=%s",
                        item.get("fire_id"), item.get("source"), item.get("detected_at"))
        except Exception as exc:
            logger.error("ingest_put_failed fire_id=%s error=%s",
                         item.get("fire_id"), exc, exc_info=True)
            errors += 1

    logger.info("ingest_batch written=%d skipped=%d errors=%d", written, skipped, errors)
    return {"written": written, "skipped": skipped, "errors": errors}
