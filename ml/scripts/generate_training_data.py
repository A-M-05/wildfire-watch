"""
Generate synthetic wildfire spread prediction training data.

Instead of predicting dispatch level (classification), we now predict:
  - spread_rate_km2_per_hr: how fast the fire is growing RIGHT NOW
  - projected_area_km2: estimated area burned in the next 30 minutes

The spread rate is computed from a Rothermel-inspired physics formula:
  spread = base_intensity * wind_effect * fuel_effect * slope_effect + noise

This gives us physically realistic training data without needing the
multi-GB USFS Fire Occurrence Database (requires data access agreements).

Usage:
  python ml/scripts/generate_training_data.py --local-only
  python ml/scripts/generate_training_data.py --bucket wildfire-watch-ml-data
"""

import argparse
import os
import math
import numpy as np
import pandas as pd
import boto3

LAT_RANGE = (33.5, 34.8)   # Southern California bounding box
LON_RANGE = (-118.8, -116.5)
RANDOM_SEED = 42


def _compute_spread_rate(wind_speed, fuel_moisture, slope_deg, radiative_power, rng):
    """Rothermel-inspired spread rate formula (simplified for synthetic data).

    Real Rothermel model needs fuel model tables and complex inputs. This
    captures the dominant physical relationships:
      - Wind is the biggest driver (exponential effect above ~5 m/s)
      - Dry fuel (low moisture) burns and spreads much faster
      - Uphill spread is faster than downhill (slope_effect)
      - Higher radiative power means the fire is already burning hot
    """
    # Base intensity from radiative power (higher intensity = faster spread)
    base = (radiative_power / 400.0) ** 0.7

    # Wind effect — exponential above 5 m/s, roughly matches fire behavior observations
    wind_effect = 1.0 + (max(wind_speed - 2.0, 0) / 4.0) ** 1.8

    # Fuel moisture effect — 0-10% (very dry) spreads 3x faster than 30% (moist)
    fuel_effect = max(0.15, 1.0 - fuel_moisture / 35.0)

    # Slope effect — 30° slope approximately doubles spread rate uphill
    slope_effect = 1.0 + (slope_deg / 45.0)

    spread = base * wind_effect * fuel_effect * slope_effect

    # Add realistic noise (~15% std) — real fires have unmodeled variability
    noise = rng.normal(0, spread * 0.15)
    spread = max(0.05, spread + noise)

    return round(spread, 3)


def _compute_projected_area(spread_rate, containment_pct, rng):
    """Estimate area burned in the next 30 minutes.

    Simple model: area grows proportional to spread rate, reduced by containment.
    A circular fire front approximation: area ≈ spread_rate * 0.5 hr * shape_factor.
    Shape factor < 1 because fires aren't perfect circles (terrain, roads, breaks).
    """
    shape_factor = rng.uniform(0.4, 0.8)
    containment_reduction = 1.0 - (containment_pct / 100.0) * 0.6
    area = spread_rate * 0.5 * shape_factor * containment_reduction
    return max(0.01, round(area, 4))


def generate(n_samples: int = 10_000) -> pd.DataFrame:
    """Return a DataFrame of n_samples synthetic fire spread records."""
    rng = np.random.RandomState(RANDOM_SEED)
    rows = []

    for _ in range(n_samples):
        lat = rng.uniform(*LAT_RANGE)
        lon = rng.uniform(*LON_RANGE)
        wind_speed = rng.weibull(a=2.0) * 6.0           # Weibull matches real wind distributions
        wind_direction = rng.uniform(0, 360)
        radiative_power = rng.exponential(scale=300.0)   # MW, heavy-tailed (most fires are small)
        containment_pct = rng.choice(                    # most fires are 0% contained at detection
            [0, 0, 0, 5, 10, 20, 30, 50],
            p=[0.4, 0.15, 0.15, 0.1, 0.08, 0.06, 0.04, 0.02]
        )
        fuel_moisture = rng.uniform(3, 30)               # % — SoCal ranges from bone dry to moist
        slope_deg = rng.exponential(scale=12.0)          # most terrain is gentle, some steep
        slope_deg = min(slope_deg, 60.0)                 # cap at 60°
        hour_of_day = rng.randint(0, 24)
        is_weekend = int(rng.random() < 0.286)

        spread_rate = _compute_spread_rate(
            wind_speed, fuel_moisture, slope_deg, radiative_power, rng
        )
        projected_area = _compute_projected_area(spread_rate, containment_pct, rng)

        rows.append({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "wind_speed_ms": round(wind_speed, 2),
            "wind_direction_deg": round(wind_direction, 1),
            "radiative_power": round(radiative_power, 1),
            "containment_pct": float(containment_pct),
            "fuel_moisture_pct": round(fuel_moisture, 1),
            "slope_deg": round(slope_deg, 1),
            "hour_of_day": float(hour_of_day),
            "is_weekend": float(is_weekend),
            "spread_rate_km2_per_hr": spread_rate,
            "projected_area_km2": projected_area,
        })

    df = pd.DataFrame(rows)

    print(f"Generated {n_samples} samples — spread rate distribution:")
    print(f"  min:    {df['spread_rate_km2_per_hr'].min():.2f} km²/hr")
    print(f"  median: {df['spread_rate_km2_per_hr'].median():.2f} km²/hr")
    print(f"  mean:   {df['spread_rate_km2_per_hr'].mean():.2f} km²/hr")
    print(f"  max:    {df['spread_rate_km2_per_hr'].max():.2f} km²/hr")
    print(f"  → would be LOCAL:      {(df['spread_rate_km2_per_hr'] < 1.2).sum()} ({(df['spread_rate_km2_per_hr'] < 1.2).mean()*100:.1f}%)")
    print(f"  → would be MUTUAL_AID: {((df['spread_rate_km2_per_hr'] >= 1.2) & (df['spread_rate_km2_per_hr'] < 3.0)).sum()} ({((df['spread_rate_km2_per_hr'] >= 1.2) & (df['spread_rate_km2_per_hr'] < 3.0)).mean()*100:.1f}%)")
    print(f"  → would be AERIAL:     {(df['spread_rate_km2_per_hr'] >= 3.0).sum()} ({(df['spread_rate_km2_per_hr'] >= 3.0).mean()*100:.1f}%)")

    return df


def upload_to_s3(df: pd.DataFrame, bucket: str, prefix: str = "training"):
    """Reformat CSV for SageMaker built-in XGBoost (label first, no header) and upload."""
    # Two separate uploads — one model per target
    for target in ["spread_rate_km2_per_hr", "projected_area_km2"]:
        feature_cols = [c for c in df.columns if c not in ["spread_rate_km2_per_hr", "projected_area_km2"]]
        sm_df = df[[target] + feature_cols]

        local_path = f"/tmp/train_{target}.csv"
        sm_df.to_csv(local_path, index=False, header=False)

        s3 = boto3.client("s3")
        s3_key = f"{prefix}/train_{target}.csv"
        s3.upload_file(local_path, bucket, s3_key)
        print(f"Uploaded → s3://{bucket}/{s3_key}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-samples", type=int, default=10_000)
    parser.add_argument("--bucket", type=str,
                        default=os.environ.get("WW_ML_BUCKET", "wildfire-watch-ml-data"))
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--output-path", type=str, default="ml/data/train.csv")
    args = parser.parse_args()

    df = generate(args.n_samples)

    if args.local_only:
        os.makedirs(os.path.dirname(args.output_path), exist_ok=True)
        df.to_csv(args.output_path, index=False)
        print(f"Saved locally → {args.output_path}")
    else:
        upload_to_s3(df, args.bucket)
