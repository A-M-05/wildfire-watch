"""
Pre-seed 5 demo fire scenarios in DynamoDB — issue #15.

Writes fully-enriched fire records (including dispatch recommendations) to
the fires table so the demo runs end-to-end without live API calls to NOAA,
USGS, Census, or SageMaker. The records look identical to what the enrichment
Lambda (#9) would produce for real fires.

Each scenario is designed to exercise a different part of the system:
  demo-thousand-oaks  — hero scenario: suburban, high pop, MUTUAL_AID
  demo-malibu         — fast wind-driven spread, AERIAL dispatch
  demo-inland-empire  — industrial area, chemical risk, AERIAL dispatch
  demo-big-bear       — remote, low pop, terrain challenge, LOCAL dispatch
  demo-san-fernando   — urban interface, multiple stations, AERIAL dispatch

Usage:
  python ml/scripts/seed_demo_data.py
  python ml/scripts/seed_demo_data.py --dry-run   (print records, don't write)
  python ml/scripts/seed_demo_data.py --delete     (remove demo records)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3

FIRES_TABLE = os.environ.get("WW_DYNAMODB_FIRES_TABLE", "fires")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

# ISO-8601 timestamp pinned to a realistic demo time (2pm on a weekday —
# good for demo since it's peak fire-weather hours and full staffing).
DEMO_DETECTED_AT = "2026-04-18T14:00:00Z"

DEMO_SCENARIOS = [
    {
        # Hero scenario — shown first in the demo. Moderate spread + high pop
        # produces MUTUAL_AID with high confidence, clear dispatch panel.
        "fire_id": "demo-thousand-oaks",
        "source": "CALFIRE",
        "lat": 34.1705,
        "lon": -118.8376,
        "perimeter_geojson": None,
        "containment_pct": 0.0,
        "radiative_power": 450.0,
        "detected_at": DEMO_DETECTED_AT,
        "spread_rate_km2_per_hr": 2.1,
        "confidence": 0.91,
        # Enriched fields
        "wind_speed_ms": 6.2,
        "wind_direction_deg": 270.0,
        "population_at_risk": 1200,
        "watershed_sites_at_risk": [],
        "risk_score": 0.665,
        "nearest_stations": [
            {"station_id": "LAC-003", "distance_km": 0.1, "available": True},
            {"station_id": "LAC-002", "distance_km": 9.4, "available": True},
            {"station_id": "LAC-004", "distance_km": 14.2, "available": True},
        ],
        "dispatch_recommendation": {
            "dispatch_level": 1,
            "recommendation": "MUTUAL_AID",
            "confidence": 0.93,
            "probabilities": {"LOCAL": 0.02, "MUTUAL_AID": 0.93, "AERIAL": 0.05},
        },
        "enriched_at": DEMO_DETECTED_AT,
    },
    {
        # Coastal wind-driven fire — fast spread, triggers AERIAL dispatch.
        # Good for showing the confidence gate at work (high confidence → auto-approve).
        "fire_id": "demo-malibu",
        "source": "CALFIRE",
        "lat": 34.0259,
        "lon": -118.7798,
        "perimeter_geojson": None,
        "containment_pct": 0.0,
        "radiative_power": 820.0,
        "detected_at": DEMO_DETECTED_AT,
        "spread_rate_km2_per_hr": 4.8,
        "confidence": 0.88,
        "wind_speed_ms": 12.5,
        "wind_direction_deg": 250.0,
        "population_at_risk": 600,
        "watershed_sites_at_risk": ["USGS-11106550", "USGS-11113500"],
        "risk_score": 0.79,
        "nearest_stations": [
            {"station_id": "LAC-001", "distance_km": 1.2, "available": True},
            {"station_id": "LAC-002", "distance_km": 18.5, "available": True},
            {"station_id": "LAC-003", "distance_km": 22.1, "available": True},
        ],
        "dispatch_recommendation": {
            "dispatch_level": 2,
            "recommendation": "AERIAL",
            "confidence": 0.88,
            "probabilities": {"LOCAL": 0.01, "MUTUAL_AID": 0.11, "AERIAL": 0.88},
        },
        "enriched_at": DEMO_DETECTED_AT,
    },
    {
        # Industrial area — warehouse fire with chemical risk. Shows watershed
        # alert integration and how industrial fires get elevated response.
        "fire_id": "demo-inland-empire",
        "source": "FIRMS",
        "lat": 34.0555,
        "lon": -117.1825,
        "perimeter_geojson": None,
        "containment_pct": 5.0,
        "radiative_power": 1200.0,
        "detected_at": DEMO_DETECTED_AT,
        "spread_rate_km2_per_hr": 1.8,
        "confidence": 0.85,
        "wind_speed_ms": 5.0,
        "wind_direction_deg": 180.0,
        "population_at_risk": 850,
        "watershed_sites_at_risk": ["USGS-10259000"],
        "risk_score": 0.72,
        "nearest_stations": [
            {"station_id": "LAC-010", "distance_km": 2.4, "available": True},
            {"station_id": "LAC-008", "distance_km": 28.3, "available": True},
            {"station_id": "LAC-009", "distance_km": 45.1, "available": False},
        ],
        "dispatch_recommendation": {
            "dispatch_level": 2,
            "recommendation": "AERIAL",
            "confidence": 0.81,
            "probabilities": {"LOCAL": 0.03, "MUTUAL_AID": 0.16, "AERIAL": 0.81},
        },
        "enriched_at": DEMO_DETECTED_AT,
    },
    {
        # Remote mountain fire — low population, long station distance.
        # Good for showing LOCAL dispatch and the contrast with suburban scenarios.
        # Confidence is high but recommendation is LOCAL — shows the model
        # understands terrain, not just population.
        "fire_id": "demo-big-bear",
        "source": "FIRMS",
        "lat": 34.2439,
        "lon": -116.9114,
        "perimeter_geojson": None,
        "containment_pct": 20.0,
        "radiative_power": 95.0,
        "detected_at": DEMO_DETECTED_AT,
        "spread_rate_km2_per_hr": 0.4,
        "confidence": 0.76,
        "wind_speed_ms": 2.1,
        "wind_direction_deg": 90.0,
        "population_at_risk": 40,
        "watershed_sites_at_risk": [],
        "risk_score": 0.21,
        "nearest_stations": [
            {"station_id": "LAC-009", "distance_km": 5.8, "available": True},
            {"station_id": "LAC-010", "distance_km": 48.2, "available": True},
            {"station_id": "LAC-008", "distance_km": 72.3, "available": False},
        ],
        "dispatch_recommendation": {
            "dispatch_level": 0,
            "recommendation": "LOCAL",
            "confidence": 0.76,
            "probabilities": {"LOCAL": 0.76, "MUTUAL_AID": 0.21, "AERIAL": 0.03},
        },
        "enriched_at": DEMO_DETECTED_AT,
    },
    {
        # Urban interface — dense population, multiple stations available.
        # Shows the system at its most critical: city fire, many people, tight window.
        # Low confidence (0.67) — just above the 0.65 gate, so it auto-approves
        # but the dispatch panel shows the confidence badge in amber.
        "fire_id": "demo-san-fernando",
        "source": "CALFIRE",
        "lat": 34.2811,
        "lon": -118.4407,
        "perimeter_geojson": None,
        "containment_pct": 0.0,
        "radiative_power": 680.0,
        "detected_at": DEMO_DETECTED_AT,
        "spread_rate_km2_per_hr": 3.2,
        "confidence": 0.82,
        "wind_speed_ms": 7.8,
        "wind_direction_deg": 300.0,
        "population_at_risk": 2500,
        "watershed_sites_at_risk": [],
        "risk_score": 0.84,
        "nearest_stations": [
            {"station_id": "LAC-005", "distance_km": 1.4, "available": True},
            {"station_id": "LAC-004", "distance_km": 8.8, "available": True},
            {"station_id": "LAC-006", "distance_km": 11.2, "available": True},
        ],
        "dispatch_recommendation": {
            "dispatch_level": 2,
            "recommendation": "AERIAL",
            "confidence": 0.87,
            "probabilities": {"LOCAL": 0.01, "MUTUAL_AID": 0.12, "AERIAL": 0.87},
        },
        "enriched_at": DEMO_DETECTED_AT,
    },
]


def _to_decimal(v):
    """Recursively convert floats to Decimal for DynamoDB storage."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_decimal(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_to_decimal(i) for i in v]
    return v


def seed(dry_run: bool = False):
    """Write all demo scenarios to DynamoDB."""
    if not dry_run:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.Table(FIRES_TABLE)

    for scenario in DEMO_SCENARIOS:
        item = _to_decimal(scenario)
        print(f"  {scenario['fire_id']}: {scenario['dispatch_recommendation']['recommendation']} "
              f"(confidence={scenario['dispatch_recommendation']['confidence']}, "
              f"risk={scenario['risk_score']})")
        if not dry_run:
            table.put_item(
                Item=item,
                # Overwrite existing demo records so re-running seed is idempotent.
                ConditionExpression="attribute_not_exists(fire_id) OR begins_with(fire_id, :prefix)",
                ExpressionAttributeValues={":prefix": Decimal("0") if False else "demo-"},
            )

    count = len(DEMO_SCENARIOS)
    action = "Would write" if dry_run else "Seeded"
    print(f"\n{action} {count} demo scenarios to '{FIRES_TABLE}'.")


def delete_demo_records():
    """Remove all demo-* records from the fires table."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(FIRES_TABLE)
    deleted = 0
    for scenario in DEMO_SCENARIOS:
        try:
            table.delete_item(
                Key={"fire_id": scenario["fire_id"], "detected_at": DEMO_DETECTED_AT}
            )
            deleted += 1
        except Exception as e:
            print(f"  Warning: could not delete {scenario['fire_id']}: {e}")
    print(f"Deleted {deleted} demo records from '{FIRES_TABLE}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed demo fire scenarios into DynamoDB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print records without writing to DynamoDB")
    parser.add_argument("--delete", action="store_true",
                        help="Delete existing demo records instead of seeding")
    args = parser.parse_args()

    print(f"Demo fire scenarios ({'DRY RUN — ' if args.dry_run else ''}table: {FIRES_TABLE}):\n")

    if args.delete:
        delete_demo_records()
    else:
        seed(dry_run=args.dry_run)
