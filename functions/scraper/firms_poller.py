"""
Issue #6 — NASA FIRMS fire detection poller.

Triggered every 3h by EventBridge. Fetches VIIRS SNPP NRT detections for
California and pushes normalised fire events to the Kinesis stream.
"""

import base64
import csv
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from io import StringIO

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# California bounding box: west,south,east,north
BBOX = "-124.4,32.5,-114.1,42.0"
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

_kinesis = None


def _get_kinesis():
    global _kinesis
    if _kinesis is None:
        _kinesis = boto3.client("kinesis")
    return _kinesis


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _confidence_to_float(raw: str) -> float:
    """Convert FIRMS confidence to 0–1 float."""
    raw = str(raw).strip().lower()
    mapping = {"low": 0.35, "nominal": 0.65, "high": 0.90}
    if raw in mapping:
        return mapping[raw]
    try:
        val = float(raw)
        return val / 100.0 if val > 1 else val
    except ValueError:
        return 0.65


def _make_fire_id(lat: str, lon: str, acq_date: str, acq_time: str) -> str:
    key = f"FIRMS:{lat}:{lon}:{acq_date}:{acq_time}"
    return "firms-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _normalize(row: dict) -> dict:
    """Map a FIRMS CSV row to the canonical fire event schema."""
    lat = float(row["latitude"])
    lon = float(row["longitude"])
    acq_date = row.get("acq_date", "")
    acq_time = row.get("acq_time", "0000").zfill(4)

    try:
        dt = datetime.strptime(f"{acq_date} {acq_time}", "%Y-%m-%d %H%M")
        dt = dt.replace(tzinfo=timezone.utc)
        detected_at = dt.isoformat()
    except ValueError:
        detected_at = datetime.now(timezone.utc).isoformat()

    return {
        "fire_id": _make_fire_id(row["latitude"], row["longitude"], acq_date, acq_time),
        "source": "FIRMS",
        "lat": lat,
        "lon": lon,
        "perimeter_geojson": None,
        "containment_pct": 0.0,
        "radiative_power": float(row.get("bright_ti4", 0) or 0),
        "detected_at": detected_at,
        "spread_rate_km2_per_hr": 0.0,
        "confidence": _confidence_to_float(row.get("confidence", "nominal")),
    }


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_firms(map_key: str) -> list[dict]:
    url = f"{FIRMS_BASE}/{map_key}/VIIRS_SNPP_NRT/{BBOX}/1"
    logger.info(f"Fetching FIRMS data: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(StringIO(resp.text))
    events = []
    for row in reader:
        try:
            events.append(_normalize(row))
        except Exception as exc:
            logger.warning(f"Skipping malformed FIRMS row: {exc} | row={row}")
    logger.info(f"Fetched {len(events)} FIRMS detections")
    return events


# ---------------------------------------------------------------------------
# Kinesis push
# ---------------------------------------------------------------------------

def _push_to_kinesis(events: list[dict], stream_name: str):
    kinesis = _get_kinesis()
    success = 0
    for event in events:
        try:
            resp = kinesis.put_record(
                StreamName=stream_name,
                Data=json.dumps(event),
                PartitionKey=event["fire_id"],
            )
            success += 1
            logger.info(
                "kinesis_put fire_id=%s shard=%s seq=%s payload=%s",
                event["fire_id"],
                resp["ShardId"],
                resp["SequenceNumber"],
                json.dumps(event),
            )
        except Exception as exc:
            logger.error(
                "kinesis_put_failed fire_id=%s error=%s payload=%s",
                event["fire_id"],
                exc,
                json.dumps(event),
            )
    logger.info("kinesis_summary stream=%s pushed=%d total=%d", stream_name, success, len(events))


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    map_key = os.environ["WW_FIRMS_MAP_KEY"]
    stream_name = os.environ["WW_KINESIS_STREAM_NAME"]

    events = _fetch_firms(map_key)
    if events:
        _push_to_kinesis(events, stream_name)
    return {"statusCode": 200, "pushed": len(events)}


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print events without pushing to Kinesis")
    parser.add_argument("--map-key", default=os.environ.get("WW_FIRMS_MAP_KEY", ""))
    args = parser.parse_args()

    if not args.map_key:
        print("ERROR: provide --map-key or set WW_FIRMS_MAP_KEY", file=sys.stderr)
        sys.exit(1)

    events = _fetch_firms(args.map_key)
    if args.dry_run:
        print(json.dumps(events, indent=2))
    else:
        stream = os.environ.get("WW_KINESIS_STREAM_NAME", "wildfire-watch-fire-events")
        _push_to_kinesis(events, stream)
