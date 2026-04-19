"""
Issue #7 — CAL FIRE GeoJSON perimeter poller.

Triggered every 10 min by EventBridge. Fetches active CA incidents and pushes
perimeter updates to the Kinesis stream, deduplicating by perimeter hash.
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# www.fire.ca.gov is fronted by Akamai which 403s programmatic clients (and
# silently returns an empty FeatureCollection from some IPs). incidents.fire.ca.gov
# is the same data, no edge filtering. inactive=true returns recently-updated
# incidents too — CAL FIRE marks fires `IsActive=False` quickly even when
# they're <24h old and still relevant for our dispatch demo. We re-filter
# client-side by Updated timestamp.
CALFIRE_URL = "https://incidents.fire.ca.gov/umbraco/Api/IncidentApi/GeoJsonList?inactive=true"
CALFIRE_USER_AGENT = "Mozilla/5.0 (compatible; wildfire-watch; +https://github.com/A-M-05/wildfire-watch)"
# Hold a fire on the map for 7 days after its last update — long enough that
# a Friday-night demo of a Tuesday fire still has data, short enough that
# year-old incidents aren't surfaced.
CALFIRE_MAX_AGE_DAYS = 7

_STATE_PREFIX = "CALFIRE_STATE#"
_STATE_SORT_KEY = "STATE"  # fixed sort key for dedup state rows (not real fire events)

_kinesis = None
_ddb_table = None


def _get_kinesis():
    global _kinesis
    if _kinesis is None:
        _kinesis = boto3.client("kinesis")
    return _kinesis


def _get_table():
    global _ddb_table
    if _ddb_table is None:
        ddb = boto3.resource("dynamodb")
        _ddb_table = ddb.Table(os.environ["WW_DYNAMODB_FIRES_TABLE"])
    return _ddb_table


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _centroid(geometry: dict) -> tuple[float, float]:
    """Return (lat, lon) centroid of a Polygon or MultiPolygon geometry."""
    geo_type = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    if geo_type == "Point" and coords:
        return (float(coords[1]), float(coords[0]))
    elif geo_type == "Polygon" and coords:
        ring = coords[0]
    elif geo_type == "MultiPolygon" and coords:
        ring = coords[0][0]
    else:
        return (0.0, 0.0)

    if not ring:
        return (0.0, 0.0)

    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _perimeter_hash(geometry: dict) -> str:
    return hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _make_fire_id(unique_id: str) -> str:
    return "calfire-" + hashlib.sha256(unique_id.encode()).hexdigest()[:16]


def _parse_date(raw: str) -> str:
    """Parse CAL FIRE date strings to ISO8601, falling back to now."""
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return datetime.now(timezone.utc).isoformat()


def _normalize(feature: dict) -> dict:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}

    unique_id = str(props.get("UniqueId") or props.get("uniqueId") or "")
    lat, lon = _centroid(geometry)
    containment_raw = props.get("PercentContained") or props.get("percentContained") or 0

    # Only carry through real polygon perimeters — for Point-only incidents
    # (the common case for small fires) we leave this null so the enrich Lambda
    # can synthesize an ellipse from spread rate + wind. Storing the Point as
    # perimeter_geojson would short-circuit that.
    geo_type = geometry.get("type") if geometry else None
    perimeter = json.dumps(geometry) if geo_type in ("Polygon", "MultiPolygon") else None

    return {
        "fire_id": _make_fire_id(unique_id),
        "source": "CALFIRE",
        "lat": lat,
        "lon": lon,
        "perimeter_geojson": perimeter,
        "containment_pct": float(containment_raw) / 100.0,
        "radiative_power": 0.0,
        "detected_at": _parse_date(
            str(props.get("StartedDateOnly") or props.get("startedDateOnly") or "")
        ),
        "spread_rate_km2_per_hr": 0.0,
        "confidence": 1.0,
        # Extra CAL FIRE fields carried through for enrichment
        "_calfire_unique_id": unique_id,
        "_calfire_name": props.get("Name") or props.get("name") or "",
        "_calfire_acres": float(props.get("AcresBurned") or props.get("acresBurned") or 0),
    }


# ---------------------------------------------------------------------------
# Dedup — only push when perimeter has changed
# ---------------------------------------------------------------------------

def _get_last_hash(unique_id: str) -> str | None:
    try:
        resp = _get_table().get_item(Key={
            "fire_id": _STATE_PREFIX + unique_id,
            "detected_at": _STATE_SORT_KEY,
        })
        return resp.get("Item", {}).get("perimeter_hash")
    except Exception as exc:
        logger.warning("ddb_get_hash_failed unique_id=%s error=%s", unique_id, exc)
        return None


def _save_hash(unique_id: str, perimeter_hash: str):
    try:
        _get_table().put_item(Item={
            "fire_id": _STATE_PREFIX + unique_id,
            "detected_at": _STATE_SORT_KEY,
            "perimeter_hash": perimeter_hash,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.warning("ddb_save_hash_failed unique_id=%s error=%s", unique_id, exc)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _is_recent(props: dict) -> bool:
    """Keep incidents touched in the last CALFIRE_MAX_AGE_DAYS days."""
    raw = str(props.get("Updated") or props.get("updated")
              or props.get("StartedDateOnly") or props.get("startedDateOnly") or "")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw[:19].replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return age_days <= CALFIRE_MAX_AGE_DAYS


def _fetch_calfire() -> list[tuple[dict, str]]:
    """Return list of (normalized_event, perimeter_hash) for changed incidents."""
    logger.info("Fetching CAL FIRE GeoJSON: %s", CALFIRE_URL)
    resp = requests.get(CALFIRE_URL, timeout=30, headers={"User-Agent": CALFIRE_USER_AGENT})
    resp.raise_for_status()

    geojson = resp.json()
    features = geojson.get("features") or []
    raw_count = len(features)
    features = [f for f in features if _is_recent(f.get("properties") or {})]
    logger.info("calfire_raw_features count=%d recent=%d", raw_count, len(features))

    changed = []
    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        unique_id = str(props.get("UniqueId") or props.get("uniqueId") or "")

        if not unique_id:
            logger.warning("calfire_skip_missing_id props=%s", props)
            continue

        p_hash = _perimeter_hash(geometry)
        last_hash = _get_last_hash(unique_id)

        if p_hash == last_hash:
            logger.info("calfire_no_change unique_id=%s", unique_id)
            continue

        try:
            event = _normalize(feature)
            changed.append((event, unique_id, p_hash))
        except Exception as exc:
            logger.warning("calfire_normalize_failed unique_id=%s error=%s", unique_id, exc)

    logger.info("calfire_changed count=%d of %d", len(changed), len(features))
    return changed


# ---------------------------------------------------------------------------
# Kinesis push
# ---------------------------------------------------------------------------

def _push_to_kinesis(events_with_meta: list[tuple[dict, str, str]], stream_name: str):
    kinesis = _get_kinesis()
    success = 0
    for event, unique_id, p_hash in events_with_meta:
        try:
            resp = kinesis.put_record(
                StreamName=stream_name,
                Data=json.dumps(event),
                PartitionKey=event["fire_id"],
            )
            _save_hash(unique_id, p_hash)
            success += 1
            logger.info(
                "kinesis_put fire_id=%s shard=%s seq=%s",
                event["fire_id"],
                resp["ShardId"],
                resp["SequenceNumber"],
            )
        except Exception as exc:
            logger.error(
                "kinesis_put_failed fire_id=%s error=%s",
                event["fire_id"],
                exc,
            )
    logger.info("kinesis_summary stream=%s pushed=%d total=%d", stream_name, success, len(events_with_meta))


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    stream_name = os.environ["WW_KINESIS_STREAM_NAME"]

    changed = _fetch_calfire()
    if changed:
        _push_to_kinesis(changed, stream_name)
    return {"statusCode": 200, "pushed": len(changed)}


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print events without pushing to Kinesis")
    args = parser.parse_args()

    changed = _fetch_calfire()
    if args.dry_run:
        print(json.dumps([e for e, _, _ in changed], indent=2))
    else:
        stream = os.environ.get("WW_KINESIS_STREAM_NAME", "wildfire-watch-fire-events")
        _push_to_kinesis(changed, stream)
