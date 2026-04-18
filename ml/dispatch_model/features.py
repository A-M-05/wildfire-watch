"""
Feature definitions for the wildfire spread prediction model.

Inputs: weather + terrain + fire intensity measurements
Outputs: spread_rate_km2_per_hr (primary), projected_area_km2 (30-min projection)

Dispatch recommendation is derived from the spread rate prediction using
simple thresholds — more explainable and auditable than a black-box classifier
for a life-safety decision.
"""

# Ordered feature names — must match column order in train.csv and at inference time.
FEATURE_NAMES = [
    "lat",                 # latitude — proxy for vegetation/climate zone
    "lon",                 # longitude — proxy for terrain type
    "wind_speed_ms",       # wind speed in m/s — biggest driver of fire spread
    "wind_direction_deg",  # direction — affects spread toward populated areas
    "radiative_power",     # fire radiative power (MW) — current fire intensity
    "containment_pct",     # % contained — higher = slower future spread
    "fuel_moisture_pct",   # fuel moisture % — dry fuel spreads faster
    "slope_deg",           # terrain slope in degrees — uphill spread is faster
    "hour_of_day",         # afternoon = lower humidity = faster spread
    "is_weekend",          # affects dispatcher staffing
]

# What the model predicts
TARGET_NAMES = ["spread_rate_km2_per_hr", "projected_area_km2"]

# Rule-based dispatch thresholds applied to the spread rate prediction.
# Rules are more auditable than ML for this decision — a dispatcher can
# understand and challenge "spread > 3.0 → AERIAL" in a way they can't
# challenge a gradient-boosted tree.
DISPATCH_THRESHOLDS = {
    "AERIAL":     3.0,   # km²/hr
    "MUTUAL_AID": 1.2,
    # below 1.2 → LOCAL
}


def spread_to_dispatch(spread_rate: float) -> tuple:
    """Convert a spread rate prediction to a dispatch recommendation."""
    if spread_rate >= DISPATCH_THRESHOLDS["AERIAL"]:
        return "AERIAL", 2
    elif spread_rate >= DISPATCH_THRESHOLDS["MUTUAL_AID"]:
        return "MUTUAL_AID", 1
    else:
        return "LOCAL", 0


def spread_to_confidence(spread_rate: float, projected_area: float) -> float:
    """Derive a 0-1 confidence score from spread predictions.

    Confidence is low near decision boundaries (1.2 and 3.0 km²/hr) where
    the dispatch call is ambiguous — this is exactly when we want a human
    in the loop. Far from boundaries, confidence is high and auto-approval
    is appropriate.
    """
    thresholds = sorted(DISPATCH_THRESHOLDS.values())
    min_distance = min(abs(spread_rate - t) for t in thresholds)
    # 2.0 km²/hr from any boundary = max confidence
    boundary_confidence = min(min_distance / 2.0, 1.0)
    # Large projected area reinforces a high spread rate reading
    area_factor = min(projected_area / 2.0, 1.0)
    return round(0.7 * boundary_confidence + 0.3 * area_factor, 3)


def extract_features(fire_event: dict) -> list:
    """Extract model input features from an enriched fire event dict."""
    from datetime import datetime

    detected_at = fire_event.get("detected_at", "")
    try:
        dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        hour = dt.hour
        is_weekend = 1 if dt.weekday() >= 5 else 0
    except (ValueError, AttributeError):
        hour, is_weekend = 12, 0

    return [
        float(fire_event.get("lat", 0)),
        float(fire_event.get("lon", 0)),
        float(fire_event.get("wind_speed_ms", 0)),
        float(fire_event.get("wind_direction_deg", 0)),
        float(fire_event.get("radiative_power", 0)),
        float(fire_event.get("containment_pct", 0)),
        float(fire_event.get("fuel_moisture_pct", 15)),
        float(fire_event.get("slope_deg", 10)),
        float(hour),
        float(is_weekend),
    ]
