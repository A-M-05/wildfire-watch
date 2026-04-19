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

Trigger: API Gateway proxy integration (GET /fires).
"""

import json
import logging
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
    if geometry is None:
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            return None  # Can't render without either a perimeter or a centroid
        geometry = {"type": "Point", "coordinates": [_to_jsonable(lon), _to_jsonable(lat)]}

    properties = {k: _to_jsonable(item[k]) for k in _PROP_FIELDS if k in item}
    return {"type": "Feature", "geometry": geometry, "properties": properties}


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

    features = [f for f in (_to_feature(i) for i in items) if f is not None]
    logger.info("fires_api returning %d features (%d raw items)", len(features), len(items))
    return _response(200, {"type": "FeatureCollection", "features": features})
