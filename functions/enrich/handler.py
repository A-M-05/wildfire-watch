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

import hashlib
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


def _estimate_slope(lat: float, lon: float) -> float:
    """Rough terrain slope estimate from SoCal region (DEM lookup in v2)."""
    if lat > 34.3:
        return 25.0   # San Gabriel / San Bernardino mountains
    elif lon < -118.5:
        return 8.0    # coastal (Malibu, Ventura) — gentle
    elif lon > -117.2:
        return 5.0    # desert edge — flat
    else:
        return 15.0   # inland foothills / canyons


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

    # Confidence: low near decision boundaries (0.5 and 1.5 km/hr).
    # Normalise by 2.0 so 2 km/hr from any boundary = max confidence.
    min_dist = min(abs(spread_rate - 0.5), abs(spread_rate - 1.5))
    confidence = round(min(min_dist / 2.0, 1.0) * 0.7 + min(projected_area_30min / 2.0, 1.0) * 0.3, 3)

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
# Predicted fire perimeter — wind-driven ellipse from Rothermel/SageMaker
# spread rate. Anderson 1983 / Andrews 2018 length-to-breadth + head-to-back.
#
# Inputs are scalars from the model + NOAA wind. Output is a GeoJSON Polygon
# ring suitable for `perimeter_geojson`. Existing CAL FIRE polygons take
# priority — see enrich_fire() guard.
# ------------------------------------------------------------------

_ELLIPSE_VERTICES = 64   # higher than the smooth-oval default so noise reads as fingers
_ELLIPSE_T_MIN_HR = 0.5  # floor: even brand-new fires get a visible footprint
_ELLIPSE_T_MAX_HR = 24.0 # ceiling: stale fires shouldn't grow without bound
_DEG_LAT_KM = 111.0      # degrees-per-km for equirectangular projection
_ELLIPSE_LB_MAX_CALM = 2.0   # visual cap for sub-Santa-Ana winds. Anderson's
                             # raw curve hits 5+ at single-digit wind, which
                             # reads as a needle on the map even with cap 3.5.
_ELLIPSE_LB_MAX_WINDY = 3.0  # SoCal Santa Ana fires can stretch further IRL,
                             # but the demo map is the constraint — keep them
                             # visibly fatter so the shape, not the length, reads.
_WINDY_THRESHOLD_MPH = 15.0
_NOISE_AMPLITUDE = 0.22  # ±22% baseline radius warp — heel scale, head bumps higher
_NOISE_FREQS = (5, 11, 19)        # higher-frequency content reads as fingers, not waves
_NOISE_WEIGHTS = (0.5, 0.3, 0.2)  # base frequency dominates; harmonics add texture
# Asymmetric envelope so the heel reads calm and the head gets lumpy spotting,
# which is how real fires look (heel = backing fire, slow & smooth; head = embers).
_HEAD_BIAS_MIN = 0.3
_HEAD_BIAS_MAX = 1.4
# Sharp Gaussian "spot fingers" — as if embers jumped a ridgeline ahead of the
# main front. Concentrated in the front 90° arc and stable per fire_id.
_SPOT_COUNT = 2
_SPOT_AMPLITUDE = 0.5
_SPOT_WIDTH_RAD = math.radians(12)

# Terrain-aware spread: sample elevation around the fire and stretch the warp
# in the uphill direction. Rule of thumb (Rothermel/Andrews): rate of spread
# roughly doubles per ~20° of upslope. We compress that into a unit-less stretch
# applied to the radius warp, so the perimeter visibly bulges uphill.
_TERRAIN_SAMPLES = 8                   # 8 cardinal directions around the fire
_TERRAIN_RADIUS_KM = 5.0               # how far out we probe for relief
_TERRAIN_STRETCH = 0.6                 # max additional radius warp toward uphill (60%)
_OPEN_TOPO_URL = "https://api.opentopodata.org/v1/aster30m"  # free, no auth, ~3s budget


def _sample_uphill(lat: float, lon: float) -> tuple[float, float] | None:
    """Probe elevation around the fire, return (uphill_bearing_rad, strength_0_1).

    strength is 0 on flat ground and 1 when the local relief is ≥300 m
    (steep canyon scale). Returns None on any API failure so the caller can
    fall back to the flat-ground ellipse.
    """
    deg_per_km_lat = 1.0 / _DEG_LAT_KM
    deg_per_km_lon = 1.0 / (_DEG_LAT_KM * max(math.cos(math.radians(lat)), 0.01))
    bearings = [2 * math.pi * i / _TERRAIN_SAMPLES for i in range(_TERRAIN_SAMPLES)]
    pts = []
    for b in bearings:
        d_lat = math.cos(b) * _TERRAIN_RADIUS_KM * deg_per_km_lat
        d_lon = math.sin(b) * _TERRAIN_RADIUS_KM * deg_per_km_lon
        pts.append(f"{lat + d_lat},{lon + d_lon}")
    try:
        resp = requests.get(
            _OPEN_TOPO_URL,
            params={"locations": "|".join(pts)},
            timeout=3,
        )
        resp.raise_for_status()
        elevations = [r.get("elevation") for r in resp.json().get("results", [])]
    except Exception as exc:
        logger.warning("terrain_sample_failed lat=%s lon=%s err=%s", lat, lon, exc)
        return None
    if any(e is None for e in elevations) or len(elevations) != _TERRAIN_SAMPLES:
        return None
    # Treat samples as a vector field: weight each direction by elevation.
    ex = sum(e * math.sin(b) for e, b in zip(elevations, bearings))
    ey = sum(e * math.cos(b) for e, b in zip(elevations, bearings))
    bearing = math.atan2(ex, ey)  # math convention: x=east, y=north
    relief = max(elevations) - min(elevations)
    strength = min(1.0, relief / 300.0)
    return bearing, strength


def _length_to_breadth(wind_mph: float) -> float:
    """Anderson 1983 fire-shape ratio. Calm wind → near-circular, strong wind → cigar.

    The visual cap is wind-gated: above ~15 mph (Santa Ana territory) we let the
    ellipse stretch further because real SoCal fires actually do, and clipping
    them flat would understate severity. Below the threshold we keep the tighter
    cap so a mild fire doesn't look dramatic for no reason.
    """
    u = max(0.0, wind_mph)
    lb = 0.936 * math.exp(0.2566 * u) + 0.461 * math.exp(-0.1548 * u) - 0.397
    cap = _ELLIPSE_LB_MAX_WINDY if u > _WINDY_THRESHOLD_MPH else _ELLIPSE_LB_MAX_CALM
    return max(1.0, min(lb, cap))


def _seed_from_id(fire_id: str) -> float:
    """Cheap deterministic 0..1 seed from fire_id so each fire's noise is stable."""
    h = hashlib.sha1(fire_id.encode()).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF


def _hours_since(detected_at: str) -> float:
    """Age of the fire in hours, clamped to [_ELLIPSE_T_MIN_HR, _ELLIPSE_T_MAX_HR]."""
    try:
        dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (ValueError, AttributeError, TypeError):
        age = 1.0
    return max(_ELLIPSE_T_MIN_HR, min(age, _ELLIPSE_T_MAX_HR))


def predicted_perimeter(fire: dict) -> dict | None:
    """Build a GeoJSON Polygon footprint from spread rate + wind for a single fire.

    Returns None when there's not enough signal (no spread_rate or no lat/lon)
    so the caller can fall back to whatever geometry is already there.
    """
    spread = float(fire.get("spread_rate_km_hr") or 0)
    if spread <= 0:
        return None
    try:
        lat = float(fire["lat"])
        lon = float(fire["lon"])
    except (KeyError, ValueError, TypeError):
        return None

    wind_ms = float(fire.get("wind_speed_ms") or 0)
    # NOAA reports wind direction as the bearing the wind is COMING FROM, so
    # the fire spreads 180° opposite. Default downhill-east when missing.
    wind_from_deg = float(fire.get("wind_direction_deg") or 270.0)
    spread_bearing_rad = math.radians((wind_from_deg + 180.0) % 360.0)

    wind_mph = wind_ms * 2.23694
    lb = _length_to_breadth(wind_mph)
    # Head-to-back ratio (Alexander 1985): determines how off-center the
    # ignition point sits inside the ellipse.
    hb = (lb + math.sqrt(max(lb * lb - 1.0, 0.0))) / max(lb - math.sqrt(max(lb * lb - 1.0, 0.0)), 1e-6)

    t = _hours_since(fire.get("detected_at", ""))
    # Forward (head) and backward (heel) growth from the ignition point.
    head_km = spread * t
    back_km = head_km / hb
    a_km = (head_km + back_km) / 2.0       # semi-major axis
    b_km = a_km / lb                       # semi-minor axis
    # Ellipse center is offset toward the head from the ignition point.
    center_offset_km = (head_km - back_km) / 2.0

    # Equirectangular projection — fine at fire scale (sub-100 km), avoids
    # importing pyproj into the Lambda. cos(lat) corrects east-west scale.
    deg_per_km_lat = 1.0 / _DEG_LAT_KM
    deg_per_km_lon = 1.0 / (_DEG_LAT_KM * max(math.cos(math.radians(lat)), 0.01))

    # Center coords: shift ignition point by center_offset along spread_bearing.
    cx_km = math.sin(spread_bearing_rad) * center_offset_km
    cy_km = math.cos(spread_bearing_rad) * center_offset_km
    center_lon = lon + cx_km * deg_per_km_lon
    center_lat = lat + cy_km * deg_per_km_lat

    # Build the ellipse: parametric coords in local km, rotate to align major
    # axis with the spread bearing, then project to lon/lat. A weighted sum of
    # high-frequency sines warps the radius so the perimeter reads as an
    # irregular burn scar; phases are seeded on fire_id so the shape is stable.
    seed = _seed_from_id(str(fire.get("fire_id") or f"{lat},{lon}"))
    phases = [seed * 2 * math.pi * (k + 1.7) for k in range(len(_NOISE_FREQS))]
    # Spot-finger bearings concentrated in the front 90° arc (theta ∈ [π/4, 3π/4])
    # so the bumps look like ember jumps ahead of the head, not random wobble.
    spot_bearings = [
        math.pi / 4 + (math.pi / 2) * ((seed * (k + 3.1)) % 1.0)
        for k in range(_SPOT_COUNT)
    ]

    # Terrain bias — vertices facing uphill bulge out, downhill compress in.
    # Skip the sample on the SageMaker-only "current_area_km2" sentinel because
    # repeat HTTP calls in a tight loop blow the Lambda budget; the sample is
    # ~300 ms when the API is healthy.
    uphill = _sample_uphill(lat, lon) if fire.get("fire_id") else None

    cos_b = math.cos(spread_bearing_rad)
    sin_b = math.sin(spread_bearing_rad)
    ring = []
    for i in range(_ELLIPSE_VERTICES):
        theta = 2 * math.pi * i / _ELLIPSE_VERTICES
        # Weighted sum: base octave dominates, harmonics add fingery texture.
        warp = sum(
            w * math.sin(theta * f + phases[k])
            for k, (f, w) in enumerate(zip(_NOISE_FREQS, _NOISE_WEIGHTS))
        )
        # Front-heavy envelope: heel (theta ≈ -π/2) ≈ HEAD_BIAS_MIN of amplitude,
        # head (theta ≈ +π/2) gets up to 1 + HEAD_BIAS_MAX. Calm heels, rough heads.
        head_bias = (1.0 + math.sin(theta)) / 2.0
        r = 1.0 + warp * _NOISE_AMPLITUDE * (_HEAD_BIAS_MIN + _HEAD_BIAS_MAX * head_bias)
        # Spot fingers — sharp Gaussian bumps that read as ridge-jumping spotting.
        for sb in spot_bearings:
            d = ((theta - sb + math.pi) % (2 * math.pi)) - math.pi
            r += _SPOT_AMPLITUDE * math.exp(-(d * d) / (2 * _SPOT_WIDTH_RAD ** 2))
        if uphill is not None:
            uphill_bearing, uphill_strength = uphill
            # `theta` runs counter-clockwise from local +x (east). Convert ring
            # angle into a compass bearing (clockwise from north) so we can
            # compare directly against uphill_bearing.
            vert_bearing = math.atan2(math.cos(theta) * sin_b + math.sin(theta) * cos_b,
                                       math.cos(theta) * cos_b - math.sin(theta) * sin_b)
            align = math.cos(vert_bearing - uphill_bearing)  # 1 uphill, -1 downhill
            r *= 1.0 + _TERRAIN_STRETCH * uphill_strength * max(align, -0.5)
        # Local coords with major axis along +y (north). cos(θ) → cross-axis (b).
        local_x = b_km * math.cos(theta) * r
        local_y = a_km * math.sin(theta) * r
        # Rotate so +y aligns with spread bearing. Rotation matrix for clockwise-from-north.
        east_km =  local_x * cos_b + local_y * sin_b
        north_km = -local_x * sin_b + local_y * cos_b
        ring.append([
            center_lon + east_km * deg_per_km_lon,
            center_lat + north_km * deg_per_km_lat,
        ])
    ring.append(ring[0])  # close the ring

    return {"type": "Polygon", "coordinates": [ring]}


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

    spread_score = min(spread / 8.0, 1.0)              # normalise to 8 km/hr (Santa Ana max)
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
        "fuel_moisture_pct", "slope_deg",
        "population_at_risk", "watershed_sites_at_risk",
        "nearest_stations", "dispatch_recommendation",
        "spread_rate_km_hr", "spread_rate_km2_per_hr", "spread_projections", "enriched_at",
        "perimeter_geojson", "perimeter_source", "alert_radius_km",
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
        # Estimate dead fine fuel moisture from temperature (Rothermel feature).
        # Higher temps → drier fuel. Calibrated to SoCal fire-weather observations:
        # 100°F → ~8%, 80°F → ~14%, 110°F → ~5%.
        temp_f = float(wind.get("temperature_f") or 85.0)
        fire.setdefault("fuel_moisture_pct", max(3.0, 20.0 - (temp_f - 60.0) * 0.3))
    except Exception as e:
        logger.warning(f"fire_id={fire.get('fire_id')} NOAA lookup failed: {e}")
        fire.setdefault("wind_speed_ms", 0.0)
        fire.setdefault("wind_direction_deg", 0.0)
        fire.setdefault("fuel_moisture_pct", 8.0)

    # Estimate terrain slope from lat/lon region (SoCal proxy; DEM lookup in v2).
    fire.setdefault("slope_deg", _estimate_slope(lat, lon))

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
    # Backward-compat alias: area growth rate (km²/hr) for existing consumers
    # (FireMap, DispatchPanel, dispatcher_notify, dispatch handler, advisory_prompt).
    # Derived as (projected_area_30min - initial_area) / 0.5 hr.
    initial_area = float(fire.get("current_area_km2", 0.01))
    fire["spread_rate_km2_per_hr"] = round(
        max(0.0, rec["projected_area_30min_km2"] - initial_area) / 0.5, 4
    )

    # 6. Risk score — computed locally, used by EventBridge dispatch trigger.
    fire["risk_score"] = compute_risk_score(fire)

    # 7. Predicted perimeter — only synthesize when we don't already have a real
    # one. CAL FIRE incidents arrive with a mapped MultiPolygon perimeter from
    # the scraper (#7); FIRMS hotspots are points and need the ellipse.
    if not fire.get("perimeter_geojson"):
        ellipse = predicted_perimeter(fire)
        if ellipse is not None:
            fire["perimeter_geojson"] = json.dumps(ellipse)
            fire["perimeter_source"] = "predicted"

    # 8. Alert radius — used by the frontend as a *buffer* around the perimeter
    # for the evac halo, and by the resident-alerter to decide who to text.
    # Scale with how far the head can spread so big fires get bigger zones.
    spread_km_hr = float(fire.get("spread_rate_km_hr") or 0)
    head_km = spread_km_hr * _hours_since(fire.get("detected_at", ""))
    fire.setdefault("alert_radius_km", round(max(1.5, head_km + 1.0), 2))

    fire["enriched_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return fire


# ------------------------------------------------------------------
# Lambda handler
# ------------------------------------------------------------------

_NON_FIRE_PREFIXES = ("NOAA_CACHE#", "CALFIRE_STATE#")


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
        # NULL must map to None (not True) so downstream truthy-checks behave; otherwise
        # `perimeter_geojson: NULL` gets re-written as Bool True and breaks /fires.
        fire = {
            k: (None if "NULL" in v else list(v.values())[0])
            for k, v in new_image.items()
        }

        fire_id = fire.get("fire_id", "unknown")
        # The fires table also holds NOAA weather cache + CAL FIRE dedup state
        # rows (same primary key shape, distinguishable by fire_id prefix).
        # Skip them — they aren't fires.
        if any(str(fire_id).startswith(p) for p in _NON_FIRE_PREFIXES):
            continue
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
