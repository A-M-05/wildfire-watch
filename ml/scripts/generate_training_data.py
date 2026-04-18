"""
Generate synthetic fire dispatch training data and upload it to S3.

Real sources (USFS Fire Occurrence Database, CAL FIRE historical) require
multi-GB downloads and data-access agreements. For the hackathon we generate
realistic synthetic data using domain-informed heuristics, then add Gaussian
noise so the model has variance to learn from rather than memorizing rules.

Label heuristic (informed by CAL FIRE dispatch protocols):
  - spread_rate > 3.0 km²/hr  OR  (population > 800 AND wind > 8 m/s) → AERIAL (2)
  - spread_rate > 1.2 km²/hr  OR  population > 250                    → MUTUAL_AID (1)
  - else                                                                → LOCAL (0)

~15% noise is added to simulate edge cases, ambiguous dispatches, and data
entry errors in historical records.
"""

import argparse
import os
import random
import numpy as np
import pandas as pd
import boto3

# Bounding box for Southern California — where CAL FIRE incidents concentrate.
LAT_RANGE = (33.5, 34.8)
LON_RANGE = (-118.8, -116.5)

RANDOM_SEED = 42


def _label(spread_rate, population, wind_speed):
    """Deterministic dispatch label from domain heuristics."""
    if spread_rate > 3.0 or (population > 800 and wind_speed > 8.0):
        return 2  # AERIAL
    if spread_rate > 1.2 or population > 250:
        return 1  # MUTUAL_AID
    return 0      # LOCAL


def generate(n_samples: int, noise_rate: float = 0.15) -> pd.DataFrame:
    """Return a DataFrame of n_samples synthetic fire dispatch records."""
    rng = np.random.RandomState(RANDOM_SEED)
    random.seed(RANDOM_SEED)

    rows = []
    for _ in range(n_samples):
        # Sample fire characteristics from realistic distributions.
        lat = rng.uniform(*LAT_RANGE)
        lon = rng.uniform(*LON_RANGE)
        spread_rate = rng.exponential(scale=1.5)             # most fires spread slowly
        population = int(rng.lognormal(mean=5.0, sigma=1.2)) # log-normal: few dense areas
        nearest_dist = rng.uniform(2.0, 45.0)               # km to nearest station
        wind_speed = rng.weibull(a=2.0) * 6.0               # Weibull matches wind data
        radiative_power = rng.exponential(scale=300.0)       # MW, heavy-tailed
        hour_of_day = rng.randint(0, 24)
        is_weekend = int(rng.random() < 0.286)               # 2/7 days are weekends

        label = _label(spread_rate, population, wind_speed)

        # Inject noise to simulate real-world label ambiguity.
        if rng.random() < noise_rate:
            label = rng.randint(0, 3)

        rows.append({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "spread_rate_km2_per_hr": round(spread_rate, 3),
            "population_at_risk": population,
            "nearest_station_dist_km": round(nearest_dist, 2),
            "wind_speed_ms": round(wind_speed, 2),
            "radiative_power": round(radiative_power, 1),
            "hour_of_day": hour_of_day,
            "is_weekend": is_weekend,
            "label": label,
        })

    df = pd.DataFrame(rows)

    # Log class distribution so we can spot severe imbalance before training.
    counts = df["label"].value_counts().sort_index()
    print(f"Generated {n_samples} samples — label distribution:")
    for lvl, name in {0: "LOCAL", 1: "MUTUAL_AID", 2: "AERIAL"}.items():
        print(f"  {name}: {counts.get(lvl, 0)} ({counts.get(lvl, 0)/n_samples*100:.1f}%)")

    return df


def upload_to_s3(df: pd.DataFrame, bucket: str, prefix: str = "training"):
    """Write CSV locally then upload to S3 for SageMaker to consume."""
    local_path = "/tmp/train.csv"
    df.to_csv(local_path, index=False)

    s3 = boto3.client("s3")
    s3_key = f"{prefix}/train.csv"
    s3.upload_file(local_path, bucket, s3_key)
    s3_uri = f"s3://{bucket}/{s3_key}"
    print(f"Uploaded training data → {s3_uri}")
    return s3_uri


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic dispatch training data")
    parser.add_argument("--n-samples", type=int, default=10_000,
                        help="Number of synthetic fire records to generate")
    parser.add_argument("--bucket", type=str,
                        default=os.environ.get("WW_ML_BUCKET", "wildfire-watch-ml-data"),
                        help="S3 bucket to upload training CSV")
    parser.add_argument("--local-only", action="store_true",
                        help="Save CSV locally and skip S3 upload (for testing)")
    parser.add_argument("--output-path", type=str, default="ml/data/train.csv",
                        help="Local output path when --local-only is set")
    args = parser.parse_args()

    df = generate(args.n_samples)

    if args.local_only:
        os.makedirs(os.path.dirname(args.output_path), exist_ok=True)
        df.to_csv(args.output_path, index=False)
        print(f"Saved locally → {args.output_path}")
    else:
        upload_to_s3(df, args.bucket)
