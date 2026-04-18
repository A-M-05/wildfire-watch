"""
Enrichment Lambda — issue #9.

Triggered by DynamoDB Streams on the fires table (INSERT events only).
Adds wind, nearest stations, population, watershed risk, and a SageMaker
dispatch recommendation to each raw fire event, then writes the enriched
record back to DynamoDB and emits a FireEnriched event to EventBridge.

The SageMaker call must come AFTER wind/stations/population are gathered —
those are all model features. The feature order is fixed in ml/dispatch_model/features.py.

Trigger: DynamoDB Streams (fires table, NEW_IMAGE on INSERT)
Emits: EventBridge `wildfire-watch.enrichment` / `FireEnriched`
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import requests

# noaa_poller lives in the scraper package — Lambda layer or bundled in deployment.
# For local testing, add functions/scraper to sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
from noaa_poller import get_weather

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ------------------------------------------------------------------
# AWS clients (module-level so Lambda reuses connections across invocations)
# ------------------------------------------------------------------

_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
_usgs_session = requests.Session()
_usgs_session.headers["User-Agent"] = "wildfire-watch (https://github.com/A-M-05/wildfire-watch)"

# Lazy clients — not instantiated until first call so unit tests can run without AWS creds.
_ddb = None
_sm_runtime = None
_events_client = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_REGION)
    return _ddb


def _get_sm():
    global _sm_runtime
    if _sm_runtime is None:
        _sm_runtime = boto3.client("sagemaker-runtime", region_name=_REGION)
    return _sm_runtime


def _get_events():
    global _events_client
    if _events_client is None:
        _events_client = boto3.client("events", region_name=_REGION)
    return _events_client

FIRES_TABLE = os.environ.get("WW_DYNAMODB_FIRES_TABLE", "fires")
SAGEMAKER_SPREAD_ENDPOINT = os.environ.get("WW_SAGEMAKER_SPREAD_ENDPOINT", "wildfire-watch-spread")
SAGEMAKER_AREA_ENDPOINT   = os.environ.get("WW_SAGEMAKER_AREA_ENDPOINT",   "wildfire-watch-area")
CONFIDENCE_THRESHOLD = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))

# Dispatch trigger thresholds (from pipeline-agent.md)
RISK_SCORE_TRIGGER = 0.6
SPREAD_RATE_TRIGGER = 2.0
POPULATION_TRIGGER = 500


# ------------------------------------------------------------------
# Nearest fire stations (static LA/SoCal dataset)
# ------------------------------------------------------------------

# Real deployment would query AWS Location Service. For the hackathon we use
# a static list of SoCal fire stations so enrichment works without extra infra.
_FIRE_STATIONS = [
    {"station_id": "LAC-001", "name": "Station 1 — Malibu", "lat": 34.0259, "lon": -118.7798},
    {"station_id": "LAC-002", "name": "Station 2 — Agoura Hills", "lat": 34.1531, "lon": -118.7618},
    {"station_id": "LAC-003", "name": "Station 3 — Thousand Oaks", "lat": 34.1705, "lon": -118.8376},
    {"station_id": "LAC-004", "name": "Station 4 — Simi Valley", "lat": 34.2694, "lon": -118.7815},
    {"station_id": "LAC-005", "name": "Station 5 — San Fernando", "lat": 34.2811, "lon": -118.4407},
    {"station_id": "LAC-006", "name": "Station 6 — Burbank", "lat": 34.1808, "lon": -118.3090},
    {"station_id": "LAC-007", "name": "Station 7 — Pasadena", "lat": 34.1478, "lon": -118.1445},
    {"station_id": "LAC-008", "name": "Station 8 — Azusa", "lat": 34.1336, "lon": -117.9076},
    {"station_id": "LAC-009", "name": "Station 9 — Big Bear", "lat": 34.2439, "lon": -116.9114},
    {"station_id": "LAC-010", "name": "Station 10 — Inland Empire", "lat": 34.0555, "lon": -117.1825},
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two coordinates in kilometres."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_nearest_stations(lat: float, lon: float, n: int = 5) -> list:
    """Return the n closest fire stations sorted by distance."""
    stations = []
    for s in _FIRE_STATIONS:
        dist = _haversine_km(lat, lon, s["lat"], s["lon"])
        stations.append({
            "station_id": s["station_id"],
            "distance_km": round(dist, 2),
            # In production: query apparatus availability from resources table.
            # For hackathon, all stations are available unless they're the same
            # county as the fire (simplified unavailability proxy).
            "available": dist > 5.0,
        })
    return sorted(stations, key=lambda x: x["distance_km"])[:n]


# ------------------------------------------------------------------
# Population at risk (Census approximation)
# ------------------------------------------------------------------

# Census API calls are slow and require an API key; we approximate from
# SoCal urban density zones for the hackathon. Each zone is a (lat, lon, radius_km,
# people_per_km2) tuple. Population = sum of zone_density * overlap_area.
_DENSITY_ZONES = [
    (34.05, -118.24, 30.0, 3200),   # Greater LA core
    (34.17, -118.38, 15.0, 1800),   # San Fernando Valley
    (34.14, -118.80, 10.0, 900),    # Thousand Oaks / Conejo Valley
    (34.03, -118.78, 8.0, 700),     # Malibu coast
    (34.28, -118.78, 12.0, 600),    # Simi Valley
    (34.10, -117.30, 20.0, 1400),   # Inland Empire
    (34.24, -116.91, 15.0, 120),    # Big Bear (sparse)
]


def get_population_at_risk(lat: float, lon: float, radius_km: float = 10.0) -> int:
    """Estimate population within radius_km of the fire using density zones."""
    total = 0
    for zone_lat, zone_lon, zone_radius, density in _DENSITY_ZONES:
        d = _haversine_km(lat, lon, zone_lat, zone_lon)
        # Overlap distance — how much of the fire radius falls inside the zone.
        overlap = max(0.0, (radius_km + zone_radius) - d)
        if overlap > 0:
            overlap_area_km2 = math.pi * min(overlap, radius_km) ** 2
            total += int(density * overlap_area_km2 * 0.15)  # 0.15 = rough overlap fraction
    return min(total, 50_000)  # cap at 50k to avoid runaway estimates


# ------------------------------------------------------------------
# Watershed risk (USGS Water Services)
# ------------------------------------------------------------------

USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"


def get_watershed_sites_at_risk(lat: float, lon: float, radius_km: float = 50.0) -> list:
    """Query USGS for water monitoring sites within radius_km of the fire.

    Returns a list of site IDs. An empty list means no monitored sites nearby
    — this is common for remote fires. A non-empty list triggers the watershed
    alert sub-system (#24).
    """
    # USGS bounding box from center + radius (approximate degrees)
    deg = radius_km / 111.0
    params = {
        "format": "rdb",
        "bBox": f"{lon - deg:.4f},{lat - deg:.4f},{lon + deg:.4f},{lat + deg:.4f}",
        "siteType": "ST,LK",     # streams and lakes
        "siteStatus": "active",
        "hasDataTypeCd": "dv",   # daily values available
    }
    try:
        resp = _usgs_session.get(USGS_SITE_URL, params=params, timeout=5)
        resp.raise_for_status()
        # USGS returns RDB (tab-delimited); extract site numbers from column 2.
        sites = []
        for line in resp.text.splitlines():
            if line.startswith("#") or line.startswith("agency_cd") or line.startswith("5s"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == "USGS":
                sites.append(f"USGS-{parts[1]}")
        return sites[:10]  # cap at 10 for the event payload
    except Exception as e:
        logger.warning(f"USGS lookup failed: {e} — watershed_sites_at_risk set to []")
        return []


# ------------------------------------------------------------------
# SageMaker dispatch recommendation
# ------------------------------------------------------------------

def get_dispatch_recommendation(fire_event: dict) -> dict:
    """Call the two SageMaker regression endpoints and return spread predictions + dispatch.

    Feature order: lat, lon, wind_speed_ms, wind_direction_deg, radiative_power,
                   containment_pct, fuel_moisture_pct, slope_deg, hour_of_day, is_weekend
    Must match FEATURE_NAMES in ml/dispatch_model/features.py.
    """
    detected_at = fire_event.get("detected_at", "")
    try:
        dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        hour = dt.hour
        is_weekend = 1 if dt.weekday() >= 5 else 0
    except (ValueError, AttributeError):
        hour, is_weekend = 12, 0

    features = [
        float(fire_event.get("lat", 0)),
        float(fire_event.get("lon", 0)),
        float(fire_event.get("wind_speed_ms", 0)),
        float(fire_event.get("wind_direction_deg", 0)),
        float(fire_event.get("radiative_power", 0)),
        float(fire_event.get("containment_pct", 0)),
        float(fire_event.get("fuel_moisture_pct", 8.0)),   # default to fire-weather conditions
        float(fire_event.get("slope_deg", 10.0)),
        float(hour),
        float(is_weekend),
    ]

    csv_body = ",".join(str(f) for f in features)
    sm = _get_sm()

    spread_resp = sm.invoke_endpoint(
        EndpointName=SAGEMAKER_SPREAD_ENDPOINT,
        ContentType="text/csv", Accept="text/csv", Body=csv_body,
    )
    spread_rate = max(0.0, float(spread_resp["Body"].read().decode().strip()))

    area_resp = sm.invoke_endpoint(
        EndpointName=SAGEMAKER_AREA_ENDPOINT,
        ContentType="text/csv", Accept="text/csv", Body=csv_body,
    )
    projected_area_30min = max(0.0, float(area_resp["Body"].read().decode().strip()))

    # Dispatch thresholds (km/hr linear spread rate) — rule-based for auditability
    if spread_rate >= 1.5:
        recommendation, dispatch_level = "AERIAL", 2
    elif spread_rate >= 0.5:
        recommendation, dispatch_level = "MUTUAL_AID", 1
    else:
        recommendation, dispatch_level = "LOCAL", 0

    # Confidence: low near decision boundaries (0.5 and 1.5 km/hr)
    min_dist = min(abs(spread_rate - 0.5), abs(spread_rate - 1.5))
    confidence = round(min(min_dist / 1.0, 1.0) * 0.7 + min(projected_area_30min / 2.0, 1.0) * 0.3, 3)

    # Time-horizon area projections for UI time slider
    import math as _math
    current_area = float(fire_event.get("current_area_km2", 0.01))
    r0 = _math.sqrt(max(current_area, 0.001) / _math.pi)
    projections = {
        label: round(_math.pi * (r0 + spread_rate * t) ** 2, 4)
        for label, t in [("30min", 0.5), ("1hr", 1), ("3hr", 3), ("6hr", 6), ("12hr", 12), ("24hr", 24)]
    }

    return {
        "spread_rate_km_hr":       round(spread_rate, 3),
        "projected_area_30min_km2": round(projected_area_30min, 3),
        "spread_projections":       projections,
        "recommendation":           recommendation,
        "dispatch_level":           dispatch_level,
        "confidence":               confidence,
    }


# ------------------------------------------------------------------
# Risk score
# ------------------------------------------------------------------

def compute_risk_score(fire_event: dict) -> float:
    """Combine fire characteristics into a 0-1 risk score.

    Weights chosen so that a fast-spreading fire near many people reliably
    clears the 0.6 dispatch threshold. The SageMaker confidence score is
    not used here — risk_score is an independent signal for the EventBridge
    dispatch trigger, while confidence gates human review.
    """
    spread = float(fire_event.get("dispatch_recommendation", {}).get("spread_rate_km_hr", 0))
    population = float(fire_event.get("population_at_risk", 0))
    wind = float(fire_event.get("wind_speed_ms", 0))
    radiative = float(fire_event.get("radiative_power", 0))
    containment = float(fire_event.get("containment_pct", 0))

    spread_score = min(spread / 5.0, 1.0)              # normalise to 5 km²/hr max
    pop_score = min(population / 2000.0, 1.0)          # normalise to 2k people
    wind_score = min(wind / 15.0, 1.0)                 # normalise to 15 m/s
    radiative_score = min(radiative / 1000.0, 1.0)     # normalise to 1000 MW
    containment_penalty = (100.0 - containment) / 100.0  # 0% contained = 1.0 penalty

    score = (
        0.35 * spread_score +
        0.30 * pop_score +
        0.15 * wind_score +
        0.10 * radiative_score +
        0.10 * containment_penalty
    )
    return round(min(score, 1.0), 4)


# ------------------------------------------------------------------
# DynamoDB + EventBridge writers
# ------------------------------------------------------------------

def _to_decimal(v):
    """Convert floats to Decimal for DynamoDB — required for Numeric types."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_decimal(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_to_decimal(i) for i in v]
    return v


def write_enriched_fire(enriched: dict) -> None:
    """Update the fires table record with enriched fields."""
    table = _get_ddb().Table(FIRES_TABLE)
    # Use update_item to patch enriched fields onto the existing record without
    # overwriting fields written by the ingest Lambda (#8).
    update_expr = []
    attr_values = {}
    attr_names = {}

    enriched_fields = [
        "risk_score", "wind_speed_ms", "wind_direction_deg",
        "population_at_risk", "watershed_sites_at_risk",
        "nearest_stations", "dispatch_recommendation",
        "spread_rate_km_hr", "spread_projections", "enriched_at",
    ]
    for field in enriched_fields:
        if field in enriched:
            placeholder = f"#f_{field}"
            val_key = f":v_{field}"
            attr_names[placeholder] = field
            attr_values[val_key] = _to_decimal(enriched[field])
            update_expr.append(f"{placeholder} = {val_key}")

    if not update_expr:
        return

    table.update_item(
        Key={"fire_id": enriched["fire_id"], "detected_at": enriched["detected_at"]},
        UpdateExpression="SET " + ", ".join(update_expr),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def emit_fire_enriched(enriched: dict) -> None:
    """Emit a FireEnriched event to EventBridge for the dispatch trigger (#10)."""
    # EventBridge detail has a 256KB limit — omit large nested objects if needed.
    _get_events().put_events(Entries=[{
        "Source": "wildfire-watch.enrichment",
        "DetailType": "FireEnriched",
        "Detail": json.dumps(enriched, default=str),
        "EventBusName": "default",
    }])
    logger.info(f"fire_id={enriched['fire_id']} FireEnriched emitted "
                f"risk_score={enriched.get('risk_score')} "
                f"confidence={enriched.get('dispatch_recommendation', {}).get('confidence')}")


# ------------------------------------------------------------------
# Main enrichment logic
# ------------------------------------------------------------------

def enrich_fire(fire: dict) -> dict:
    """Run the full enrichment pipeline for a single fire event.

    Returns the enriched event dict (a superset of the input).
    Raises on SageMaker failure — partial enrichment is worse than none
    because downstream callers assume all fields are present.
    """
    lat = float(fire["lat"])
    lon = float(fire["lon"])

    # 1. Wind data — needed as SageMaker feature.
    try:
        wind = get_weather(lat, lon)
        fire["wind_speed_ms"] = wind.get("wind_speed_ms", 0.0)
        fire["wind_direction_deg"] = wind.get("wind_direction_deg", 0.0)
    except Exception as e:
        logger.warning(f"fire_id={fire.get('fire_id')} NOAA lookup failed: {e}")
        fire.setdefault("wind_speed_ms", 0.0)
        fire.setdefault("wind_direction_deg", 0.0)

    # 2. Nearest fire stations — needed as SageMaker feature.
    fire["nearest_stations"] = get_nearest_stations(lat, lon)

    # 3. Population at risk — needed as SageMaker feature.
    fire["population_at_risk"] = get_population_at_risk(lat, lon)

    # 4. Watershed sites — independent of SageMaker, can run alongside.
    fire["watershed_sites_at_risk"] = get_watershed_sites_at_risk(lat, lon)

    # 5. SageMaker dispatch recommendation — requires all features above.
    rec = get_dispatch_recommendation(fire)
    fire["dispatch_recommendation"] = rec
    fire["spread_rate_km_hr"]        = rec["spread_rate_km_hr"]
    fire["spread_projections"]       = rec["spread_projections"]

    # 6. Risk score — computed locally, used by EventBridge dispatch trigger.
    fire["risk_score"] = compute_risk_score(fire)

    fire["enriched_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return fire


# ------------------------------------------------------------------
# Lambda handler
# ------------------------------------------------------------------

def handler(event, context):
    """DynamoDB Streams trigger — process INSERT records only."""
    processed = 0
    errors = 0

    for record in event.get("Records", []):
        # Only enrich new fires (INSERT), not updates to existing ones.
        if record.get("eventName") != "INSERT":
            continue

        new_image = record.get("dynamodb", {}).get("NewImage", {})
        # DynamoDB Streams returns values in DynamoDB JSON format — flatten to Python.
        fire = {k: list(v.values())[0] for k, v in new_image.items()}

        fire_id = fire.get("fire_id", "unknown")
        try:
            enriched = enrich_fire(fire)
            write_enriched_fire(enriched)
            emit_fire_enriched(enriched)
            processed += 1
            logger.info(f"fire_id={fire_id} enriched OK "
                        f"risk_score={enriched['risk_score']} "
                        f"should_dispatch={enriched['risk_score'] >= RISK_SCORE_TRIGGER}")
        except Exception as e:
            # Log and continue — don't let one bad record kill the batch.
            # The fire will remain in DynamoDB without enrichment fields.
            logger.error(f"fire_id={fire_id} enrichment failed: {e}", exc_info=True)
            errors += 1

    logger.info(f"Enrichment batch complete: {processed} processed, {errors} errors")
    return {"processed": processed, "errors": errors}
