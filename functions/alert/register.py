"""Resident registration Lambda handler (#23).

POST /residents/register

Auth: API Gateway's Cognito authorizer verifies the JWT and attaches
verified claims to ``event.requestContext.authorizer.claims``. We trust
those claims (the authorizer rejects malformed tokens upstream) and use
``sub`` as the ``resident_id``.

Body schema (JSON):
  {
    "phone": "+15555550100",         # required, E.164
    "address": "123 Main St, ...",   # optional - server-geocoded if present
    "lat": 37.7749,                  # alternative to address
    "lon": -122.4194,
    "alert_radius_km": 10            # optional, default 10
  }

Either ``address`` or ``lat``+``lon`` must be provided. Frontends that
already use Mapbox can geocode in-browser and skip the server round-trip.

PII handling: phone numbers are written to DynamoDB (encrypted at rest by
the table's AWS-owned KMS key) and NEVER logged to CloudWatch. Only the
opaque Cognito ``sub`` is logged.
"""

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3

E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
DEFAULT_ALERT_RADIUS_KM = Decimal("10")
MAX_ALERT_RADIUS_KM = Decimal("100")

_dynamodb = boto3.resource("dynamodb")
_location_client = None


def _table():
    return _dynamodb.Table(os.environ["WW_DYNAMODB_RESIDENTS_TABLE"])


def _location():
    global _location_client
    if _location_client is None:
        _location_client = boto3.client("location")
    return _location_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _resident_id(event: dict) -> str | None:
    try:
        return event["requestContext"]["authorizer"]["claims"]["sub"]
    except (KeyError, TypeError):
        return None


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def _geocode(address: str) -> tuple[Decimal, Decimal]:
    """Geocode an address via AWS Location Service. Returns (lat, lon)."""
    index = os.environ.get("WW_LOCATION_PLACE_INDEX")
    if not index:
        raise ValueError("server-side geocoding unavailable (WW_LOCATION_PLACE_INDEX unset)")
    resp = _location().search_place_index_for_text(
        IndexName=index, Text=address, MaxResults=1
    )
    results = resp.get("Results", [])
    if not results:
        raise ValueError("no geocoding match for address")
    # Location Service returns Point as [longitude, latitude] (GeoJSON order).
    lon, lat = results[0]["Place"]["Geometry"]["Point"]
    return _to_decimal(lat), _to_decimal(lon)


def _resolve_location(body: dict) -> tuple[Decimal, Decimal] | dict:
    """Return (lat, lon) on success, or an error response dict on failure."""
    if "address" in body:
        try:
            return _geocode(body["address"])
        except ValueError as e:
            return _response(400, {"error": str(e)})

    if "lat" in body and "lon" in body:
        try:
            return _to_decimal(body["lat"]), _to_decimal(body["lon"])
        except (InvalidOperation, TypeError):
            return _response(400, {"error": "lat/lon must be numeric"})

    return _response(400, {"error": "must provide either 'address' or 'lat'+'lon'"})


def handler(event, context=None):
    resident_id = _resident_id(event)
    if not resident_id:
        return _response(401, {"error": "unauthorized"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    phone = body.get("phone")
    if not phone or not isinstance(phone, str) or not E164_RE.match(phone):
        return _response(400, {"error": "phone must be E.164 format (+1...)"})

    location = _resolve_location(body)
    if isinstance(location, dict):  # error response
        return location
    lat, lon = location

    if not (Decimal("-90") <= lat <= Decimal("90")):
        return _response(400, {"error": "lat out of range [-90, 90]"})
    if not (Decimal("-180") <= lon <= Decimal("180")):
        return _response(400, {"error": "lon out of range [-180, 180]"})

    try:
        radius = _to_decimal(body.get("alert_radius_km", DEFAULT_ALERT_RADIUS_KM))
    except (InvalidOperation, TypeError):
        return _response(400, {"error": "alert_radius_km must be numeric"})
    if radius <= 0 or radius > MAX_ALERT_RADIUS_KM:
        return _response(400, {"error": f"alert_radius_km must be in (0, {MAX_ALERT_RADIUS_KM}]"})

    _table().put_item(
        Item={
            "resident_id": resident_id,
            "phone": phone,
            "lat": lat,
            "lon": lon,
            "alert_radius_km": radius,
            "registered_at": _now(),
        }
    )

    # PII rule: never log the phone number. resident_id is an opaque Cognito sub.
    print(f"resident registered: {resident_id}")
    return _response(201, {"resident_id": resident_id})
