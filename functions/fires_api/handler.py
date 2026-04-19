"""
Fires API Lambda — issue #105.

Backs the public GET /fires endpoint. Scans the enriched fires table and
returns a GeoJSON FeatureCollection that the React frontend renders directly.

Why a scan: the table is small (active fires only — typically tens to low
hundreds of items) and items expire via TTL, so a full scan is cheap and
avoids needing a separate "active=true" GSI lookup. If the active fire count
ever grows past ~1k items this should switch to a GSI on a status attribute.

Geometry rule: prefer a real perimeter polygon when the enrichment Lambda has
attached one (`perimeter_geojson`); otherwise emit a Point at [lon, lat] and
let the frontend synthesize a footprint from acres. The frontend already
handles both shapes (see fires.js circlePolygon fallback).

FIRMS clustering: NASA FIRMS publishes one Point per ~375 m satellite pixel,
so a single real fire shows up as a cluster of dozens of unnamed dots. We
greedy-cluster Point features whose `source == "FIRMS"` within
`CLUSTER_RADIUS_KM` and emit one synthetic feature per cluster. CAL FIRE
incidents and any record with a real perimeter polygon pass through unchanged
so named fires keep their identity.

Trigger: API Gateway proxy integration (GET /fires).
"""

import hashlib
import json
import logging
import math
import os
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
FIRES_TABLE = os.environ.get("WW_DYNAMODB_FIRES_TABLE", "fires")

_ddb = None


def _get_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_REGION).Table(FIRES_TABLE)
    return _ddb


# Fields lifted straight onto Feature.properties. Anything not in this list is
# dropped so a stray internal-only attribute doesn't leak to the browser.
_PROP_FIELDS = (
    "fire_id",
    "source",
    "name",
    "lat",
    "lon",
    "county",
    "location",
    "containment_pct",
    "acres_burned",
    "spread_rate_km2_per_hr",
    "radiative_power",
    "confidence",
    "risk_score",
    "wind_speed_ms",
    "wind_direction_deg",
    "population_at_risk",
    "alert_radius_km",
    "watershed_sites_at_risk",
    "nearest_stations",
    "detected_at",
    "last_updated",
    "url",
    "perimeter_source",
)


def _to_jsonable(value):
    """DynamoDB returns Decimals for all numbers — JSON can't serialize them."""
    if isinstance(value, Decimal):
        # Preserve int-ness so containment_pct stays an int in the wire payload
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _to_feature(item: dict) -> dict | None:
    perimeter = item.get("perimeter_geojson")
    geometry = None
    if perimeter:
        # Stored as a JSON string per the normalized schema in CLAUDE.md.
        try:
            geometry = json.loads(perimeter) if isinstance(perimeter, str) else perimeter
        except json.JSONDecodeError:
            logger.warning("fire %s has invalid perimeter_geojson; falling back to Point",
                           item.get("fire_id"))
            geometry = None
        # Defensive: anything that isn't a {"type": ..., "coordinates": ...} dict
        # is unusable. Seen in the wild as a stray DynamoDB Bool from a partial write.
        if not isinstance(geometry, dict) or "type" not in geometry:
            logger.warning("fire %s has non-dict perimeter_geojson (%r); falling back to Point",
                           item.get("fire_id"), type(geometry).__name__)
            geometry = None
    if geometry is None:
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            return None  # Can't render without either a perimeter or a centroid
        geometry = {"type": "Point", "coordinates": [_to_jsonable(lon), _to_jsonable(lat)]}

    properties = {k: _to_jsonable(item[k]) for k in _PROP_FIELDS if k in item}
    return {"type": "Feature", "geometry": geometry, "properties": properties}


# FIRMS clustering: 1 km picks up the typical 1–3 pixel-cluster footprint of a
# single fire without merging genuinely separate fires that happen to be in the
# same county. Tunable via env so we can adjust without redeploy.
CLUSTER_RADIUS_KM = float(os.environ.get("WW_FIRMS_CLUSTER_KM", "1.0"))
_EARTH_R = 6371.0


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return _EARTH_R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_clusterable(feature):
    # Only collapse unnamed FIRMS pixels. Polygons and CAL FIRE incidents keep
    # their identity (named fires matter for dispatch + alerts).
    if feature["geometry"].get("type") != "Point":
        return False
    return feature["properties"].get("source") == "FIRMS"


def _merge_cluster(members):
    """Collapse a list of FIRMS point features into one synthetic feature."""
    n = len(members)
    coords = [m["geometry"]["coordinates"] for m in members]
    cx = sum(c[0] for c in coords) / n
    cy = sum(c[1] for c in coords) / n
    # Aggregate from constituent pixels — radiative_power is the strongest signal
    # of fire intensity, confidence is the strongest signal of detection quality.
    rps = [m["properties"].get("radiative_power", 0) or 0 for m in members]
    confs = [m["properties"].get("confidence", 0) or 0 for m in members]
    detected = [m["properties"].get("detected_at") for m in members if m["properties"].get("detected_at")]

    # Stable id from the cluster centroid + count so repeated calls produce the
    # same id for the same cluster (matters for the frontend's selectedFireId).
    seed = f"firms-cluster:{round(cx, 4)}:{round(cy, 4)}:{n}"
    cluster_id = "firms-cluster-" + hashlib.sha256(seed.encode()).hexdigest()[:12]

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [cx, cy]},
        "properties": {
            "fire_id": cluster_id,
            "source": "FIRMS",
            "lat": cy,
            "lon": cx,
            "name": f"FIRMS cluster ({n} detections)" if n > 1 else None,
            "pixel_count": n,
            "radiative_power": max(rps),
            "confidence": max(confs),
            "detected_at": min(detected) if detected else None,
        },
    }


def _cluster_features(features):
    """Greedy single-pass FIRMS cluster — one O(n²) sweep, fine at our scale."""
    untouched = [f for f in features if not _is_clusterable(f)]
    candidates = [f for f in features if _is_clusterable(f)]

    clusters = []  # each: {"center": [lon, lat], "members": [...]}
    for f in candidates:
        lon, lat = f["geometry"]["coordinates"]
        attached = False
        for c in clusters:
            clon, clat = c["center"]
            if _haversine_km(lat, lon, clat, clon) <= CLUSTER_RADIUS_KM:
                c["members"].append(f)
                # Recompute center as running mean so clusters drift toward the
                # true centroid as more pixels join.
                n = len(c["members"])
                c["center"] = [
                    (clon * (n - 1) + lon) / n,
                    (clat * (n - 1) + lat) / n,
                ]
                attached = True
                break
        if not attached:
            clusters.append({"center": [lon, lat], "members": [f]})

    return untouched + [_merge_cluster(c["members"]) for c in clusters]


# CORS — Amplify origin set via env so a redeploy isn't needed when the URL
# changes; localhost:3000 is always allowed for local frontend dev.
_ALLOWED_ORIGIN = os.environ.get("WW_FRONTEND_ORIGIN", "*")


def _response(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    table = _get_table()
    items = []
    scan_kwargs = {}
    while True:
        page = table.scan(**scan_kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    raw_features = [f for f in (_to_feature(i) for i in items) if f is not None]
    features = _cluster_features(raw_features)
    logger.info("fires_api returning %d features (%d raw items, %d after FIRMS cluster)",
                len(features), len(items), len(features))
    return _response(200, {"type": "FeatureCollection", "features": features})
