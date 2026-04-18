"""
Issue #11 — NOAA weather poller.

Unlike the FIRMS (#6) and CAL FIRE (#7) pollers, this one is invoked
synchronously by the enrichment Lambda (#9). It resolves a (lat, lon) to
the NOAA gridpoint, fetches the hourly forecast, and returns wind speed
(m/s) and wind direction (degrees). Responses are cached in DynamoDB with
a 30-minute TTL so concurrent enrichments for nearby fires don't hammer
api.weather.gov (which aggressively rate-limits).

Callable two ways:
  - as a Python module: ``from noaa_poller import get_weather``
  - as a Lambda: event ``{"lat": 37.7, "lon": -122.4}`` → wind JSON
"""

import json
import logging
import os
import time
from typing import Optional

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NOAA_BASE = "https://api.weather.gov"
USER_AGENT = "wildfire-watch (https://github.com/A-M-05/wildfire-watch)"
CACHE_TTL_SECONDS = 30 * 60
CACHE_PK_PREFIX = "NOAA_CACHE#"
CACHE_SK = "WEATHER"
MPH_TO_MS = 0.44704

# 16-point compass → degrees from North (clockwise).
_COMPASS_TO_DEG = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}

_ddb_table = None


def _get_table():
    global _ddb_table
    if _ddb_table is None:
        ddb = boto3.resource("dynamodb")
        _ddb_table = ddb.Table(os.environ["WW_DYNAMODB_FIRES_TABLE"])
    return _ddb_table


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_wind_speed_mph(raw: str) -> float:
    """NOAA returns strings like '10 mph' or '5 to 15 mph'. Return the midpoint in mph."""
    if not raw:
        return 0.0
    tokens = [t for t in str(raw).replace("mph", "").replace("to", " ").split() if t]
    nums = []
    for t in tokens:
        try:
            nums.append(float(t))
        except ValueError:
            continue
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def _parse_wind_direction_deg(raw: str) -> float:
    key = str(raw or "").strip().upper()
    return _COMPASS_TO_DEG.get(key, 0.0)


def _cache_key(lat: float, lon: float) -> str:
    return f"{CACHE_PK_PREFIX}{round(lat, 2)},{round(lon, 2)}"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_get(lat: float, lon: float) -> Optional[dict]:
    try:
        resp = _get_table().get_item(Key={
            "fire_id": _cache_key(lat, lon),
            "detected_at": CACHE_SK,
        })
    except Exception as exc:
        logger.warning("noaa_cache_get_failed lat=%s lon=%s error=%s", lat, lon, exc)
        return None

    item = resp.get("Item")
    if not item:
        return None
    ttl = int(item.get("ttl", 0))
    if ttl and ttl < int(time.time()):
        return None
    payload = item.get("payload")
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


def _cache_put(lat: float, lon: float, payload: dict):
    try:
        _get_table().put_item(Item={
            "fire_id": _cache_key(lat, lon),
            "detected_at": CACHE_SK,
            "payload": json.dumps(payload),
            "ttl": int(time.time()) + CACHE_TTL_SECONDS,
        })
    except Exception as exc:
        logger.warning("noaa_cache_put_failed lat=%s lon=%s error=%s", lat, lon, exc)


# ---------------------------------------------------------------------------
# NOAA fetch
# ---------------------------------------------------------------------------

def _http_get_json(url: str) -> dict:
    resp = requests.get(url, timeout=15, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json",
    })
    resp.raise_for_status()
    return resp.json()


def _fetch_noaa(lat: float, lon: float) -> dict:
    """Return first-period hourly forecast for the given point."""
    points = _http_get_json(f"{NOAA_BASE}/points/{lat},{lon}")
    forecast_hourly_url = points.get("properties", {}).get("forecastHourly")
    if not forecast_hourly_url:
        raise RuntimeError(f"NOAA /points returned no forecastHourly URL for {lat},{lon}")

    forecast = _http_get_json(forecast_hourly_url)
    periods = forecast.get("properties", {}).get("periods") or []
    if not periods:
        raise RuntimeError(f"NOAA hourly forecast returned no periods for {lat},{lon}")

    period = periods[0]
    wind_mph = _parse_wind_speed_mph(period.get("windSpeed"))
    return {
        "wind_speed_ms": round(wind_mph * MPH_TO_MS, 2),
        "wind_direction_deg": _parse_wind_direction_deg(period.get("windDirection")),
        "temperature_f": period.get("temperature"),
        "short_forecast": period.get("shortForecast"),
        "forecast_time": period.get("startTime"),
        "source": "NOAA",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_weather(lat: float, lon: float) -> dict:
    """Return wind speed (m/s) + direction (deg) for (lat, lon), using cache when warm."""
    cached = _cache_get(lat, lon)
    if cached is not None:
        logger.info("noaa_cache_hit lat=%s lon=%s", lat, lon)
        cached["_cache"] = "hit"
        return cached

    logger.info("noaa_cache_miss lat=%s lon=%s", lat, lon)
    payload = _fetch_noaa(lat, lon)
    _cache_put(lat, lon, payload)
    payload["_cache"] = "miss"
    return payload


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    try:
        lat = float(event["lat"])
        lon = float(event["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("noaa_bad_event event=%s error=%s", event, exc)
        return {"statusCode": 400, "error": "event requires numeric 'lat' and 'lon'"}

    try:
        weather = get_weather(lat, lon)
    except requests.HTTPError as exc:
        logger.error("noaa_http_error lat=%s lon=%s status=%s", lat, lon, exc.response.status_code)
        return {"statusCode": 502, "error": f"NOAA HTTP {exc.response.status_code}"}
    except Exception as exc:
        logger.error("noaa_fetch_failed lat=%s lon=%s error=%s", lat, lon, exc)
        return {"statusCode": 500, "error": str(exc)}

    return {"statusCode": 200, **weather}


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--no-cache", action="store_true", help="Skip DynamoDB cache (direct fetch)")
    args = parser.parse_args()

    if args.no_cache:
        print(json.dumps(_fetch_noaa(args.lat, args.lon), indent=2))
    else:
        print(json.dumps(get_weather(args.lat, args.lon), indent=2, default=str))
