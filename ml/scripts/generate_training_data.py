"""
Generate synthetic wildfire spread rate training data using the Rothermel model.

The Rothermel (1972) rate of spread model is the standard used by USFS, CAL FIRE,
and NWCG for operational fire behavior prediction. We use it here to generate
physically realistic training data without needing the multi-GB USFS Fire
Occurrence Database.

What the model learns:
  Given: wind speed, fuel moisture, slope, radiative power, location, time of day
  Predict: linear spread rate (km/hr)

The spread rate is then used by spread_projection.py to compute area at any
future time horizon (0.5hr, 1hr, 6hr, 24hr) using the expanding circle model.

Usage:
  python ml/scripts/generate_training_data.py --local-only
  python ml/scripts/generate_training_data.py --bucket wildfire-watch-ml-data
"""

import argparse
import math
import os
import sys
import numpy as np
import pandas as pd
import boto3

# Add project root so we can import the Rothermel implementation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

LAT_RANGE = (33.5, 34.8)
LON_RANGE = (-118.8, -116.5)
RANDOM_SEED = 42


def _rothermel_spread(wind_speed_ms, fuel_moisture_pct, slope_deg, lat, lon, rng):
    """Rothermel-based spread rate with calibrated SoCal fuel models.

    Fuel model constants from Andrews (2018) RMRS-GTR-266:
      chaparral (model 4): dominant SoCal fuel, high wind sensitivity
      brush (model 5):     coastal shrubland, lower fuel load
      grass:               desert-edge, fast but lower intensity
      timber_litter:       mountain conifer, slower but sustained

    Wind converts from m/s at 6m height → midflame mph (×0.4 × 2.237).
    Slope factor uses Rothermel φ_s with β=0.05 (chaparral packing ratio).
    """
    from ml.dispatch_model.spread_projection import (
        rothermel_spread_rate, _infer_fuel_model, FUEL_MODELS
    )
    import math

    ros = rothermel_spread_rate(wind_speed_ms, fuel_moisture_pct, slope_deg, lat, lon)

    # Add realistic noise (~10% std) — real fires have unmodeled variability
    # from spotting, firebrands, and localized terrain effects
    noise_factor = rng.normal(1.0, 0.10)
    return max(0.05, round(ros * noise_factor, 3))


def generate(n_samples: int = 10_000) -> pd.DataFrame:
    """Return a DataFrame of n_samples synthetic fire spread records."""
    rng = np.random.RandomState(RANDOM_SEED)
    rows = []

    for _ in range(n_samples):
        lat = rng.uniform(*LAT_RANGE)
        lon = rng.uniform(*LON_RANGE)

        # Wind — Weibull scaled to SoCal fire-weather conditions (Santa Ana events reach 15-25 m/s)
        wind_speed_ms = rng.weibull(a=2.0) * 9.0

        wind_direction_deg = rng.uniform(0, 360)

        # Radiative power — log-normal (most fires small, few very large)
        radiative_power = rng.exponential(scale=300.0)

        # Containment — most fires are 0% contained at initial detection
        containment_pct = float(rng.choice(
            [0, 0, 0, 5, 10, 20, 30, 50],
            p=[0.4, 0.15, 0.15, 0.1, 0.08, 0.06, 0.04, 0.02]
        ))

        # Fuel moisture — fires are reported under fire-weather conditions (3–15% dead fine fuel)
        # Afternoon fires (hour 12-18) tend to have lower moisture
        hour_of_day = int(rng.randint(0, 24))
        is_weekend = float(rng.random() < 0.286)

        # Diurnal moisture variation — drier in afternoon
        base_moisture = rng.uniform(3, 15)
        if 12 <= hour_of_day <= 18:
            fuel_moisture_pct = max(3.0, base_moisture * 0.75)
        else:
            fuel_moisture_pct = base_moisture

        # Slope — most SoCal terrain is gentle with some steep canyons
        slope_deg = min(rng.exponential(scale=10.0), 58.0)

        spread_rate = _rothermel_spread(
            wind_speed_ms, fuel_moisture_pct, slope_deg, lat, lon, rng
        )

        # Projected area in 30 minutes (used as secondary regression target)
        from ml.dispatch_model.spread_projection import project_area
        current_area = rng.uniform(0.005, 0.5)   # fire size at detection (0.5–50 ha)
        projected_area_km2 = project_area(spread_rate, current_area, time_hours=0.5)

        rows.append({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "wind_speed_ms": round(wind_speed_ms, 2),
            "wind_direction_deg": round(wind_direction_deg, 1),
            "radiative_power": round(radiative_power, 1),
            "containment_pct": containment_pct,
            "fuel_moisture_pct": round(fuel_moisture_pct, 1),
            "slope_deg": round(slope_deg, 1),
            "hour_of_day": float(hour_of_day),
            "is_weekend": is_weekend,
            "spread_rate_km_hr": spread_rate,           # linear spread (km/hr) — PRIMARY target
            "projected_area_30min_km2": projected_area_km2,   # secondary target
        })

    df = pd.DataFrame(rows)

    print(f"Generated {n_samples} samples using Rothermel model — spread rate (km/hr):")
    print(f"  min:    {df['spread_rate_km_hr'].min():.3f}")
    print(f"  median: {df['spread_rate_km_hr'].median():.3f}")
    print(f"  mean:   {df['spread_rate_km_hr'].mean():.3f}")
    print(f"  p90:    {df['spread_rate_km_hr'].quantile(0.9):.3f}")
    print(f"  max:    {df['spread_rate_km_hr'].max():.3f}")
    print(f"  → would dispatch LOCAL:      {(df['spread_rate_km_hr'] < 0.5).sum()} ({(df['spread_rate_km_hr'] < 0.5).mean()*100:.1f}%)")
    print(f"  → would dispatch MUTUAL_AID: {((df['spread_rate_km_hr'] >= 0.5) & (df['spread_rate_km_hr'] < 1.5)).sum()} ({((df['spread_rate_km_hr'] >= 0.5) & (df['spread_rate_km_hr'] < 1.5)).mean()*100:.1f}%)")
    print(f"  → would dispatch AERIAL:     {(df['spread_rate_km_hr'] >= 1.5).sum()} ({(df['spread_rate_km_hr'] >= 1.5).mean()*100:.1f}%)")

    return df


def prepare_sagemaker_csv(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    """Reformat for SageMaker built-in XGBoost: label first, no header."""
    feature_cols = [c for c in df.columns
                    if c not in ["spread_rate_km_hr", "projected_area_30min_km2"]]
    return df[[target_col] + feature_cols]


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

        # Also save SageMaker-formatted CSVs for easy upload
        sm_spread = prepare_sagemaker_csv(df, "spread_rate_km_hr")
        sm_spread.to_csv("ml/data/train_spread.csv", index=False, header=False)
        sm_area = prepare_sagemaker_csv(df, "projected_area_30min_km2")
        sm_area.to_csv("ml/data/train_area.csv", index=False, header=False)
        print(f"Saved → {args.output_path}")
        print(f"Saved SageMaker CSVs → ml/data/train_spread.csv, ml/data/train_area.csv")
    else:
        s3 = boto3.client("s3")
        for target, fname in [
            ("spread_rate_km_hr", "train_spread.csv"),
            ("projected_area_30min_km2", "train_area.csv"),
        ]:
            sm_df = prepare_sagemaker_csv(df, target)
            local = f"/tmp/{fname}"
            sm_df.to_csv(local, index=False, header=False)
            s3.upload_file(local, args.bucket, f"training/{fname}")
            print(f"Uploaded → s3://{args.bucket}/training/{fname}")
