"""
Feature engineering for the dispatch recommendation model.

The feature vector is a fixed-length list of numbers. Every column must appear
in the same order here, in the training CSV, and at inference time — if the
order drifts between training and serving, the model silently produces wrong
predictions with no error.

FEATURE_NAMES is the single source of truth for that order.
"""

# Dispatch level labels — used in training data generation and prediction output.
DISPATCH_LEVELS = {
    0: "LOCAL",       # local units only
    1: "MUTUAL_AID",  # call neighboring departments
    2: "AERIAL",      # aerial support + mutual aid
}

# Ordered feature names — must match the column order in train.csv exactly.
FEATURE_NAMES = [
    "lat",                    # fire latitude (geographic signal — terrain, urban density)
    "lon",                    # fire longitude
    "spread_rate_km2_per_hr", # how fast the fire is growing — biggest dispatch driver
    "population_at_risk",     # people within the risk radius (from census enrichment)
    "nearest_station_dist_km",# distance to closest available fire station
    "wind_speed_ms",          # wind accelerates spread dramatically
    "radiative_power",        # satellite-measured fire intensity (MW)
    "hour_of_day",            # night fires are harder to fight (visibility, crew fatigue)
    "is_weekend",             # staffing is thinner on weekends
]


def extract_features(fire_event: dict) -> list:
    """Pull the feature vector from a normalized + enriched fire event dict.

    The enriched schema adds risk_score, wind fields, population_at_risk, and
    nearest_stations. This function is used at inference time (inside the Lambda
    that calls the SageMaker endpoint) so the same logic runs in prod as in training.

    Args:
        fire_event: enriched fire event following the schema in CLAUDE.md

    Returns:
        list of floats in FEATURE_NAMES order
    """
    from datetime import datetime

    detected_at = fire_event.get("detected_at", "")
    try:
        dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        hour_of_day = dt.hour
        is_weekend = 1 if dt.weekday() >= 5 else 0
    except (ValueError, AttributeError):
        # If timestamp is malformed, default to noon weekday (neutral values).
        hour_of_day = 12
        is_weekend = 0

    # nearest_stations is a list sorted by distance — take the closest available one.
    stations = fire_event.get("nearest_stations", [])
    nearest_dist = stations[0]["distance_km"] if stations else 50.0  # assume far if unknown

    return [
        float(fire_event.get("lat", 0.0)),
        float(fire_event.get("lon", 0.0)),
        float(fire_event.get("spread_rate_km2_per_hr", 0.0)),
        float(fire_event.get("population_at_risk", 0)),
        float(nearest_dist),
        float(fire_event.get("wind_speed_ms", 0.0)),
        float(fire_event.get("radiative_power", 0.0)),
        float(hour_of_day),
        float(is_weekend),
    ]
