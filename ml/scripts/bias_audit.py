"""
SageMaker Clarify bias audit for the dispatch recommendation model — issue #18.

Checks whether the model recommends slower/lower-tier dispatch for underserved
communities. This is an equity requirement: a wildfire system that sends fewer
resources to low-income ZIP codes causes direct harm.

Sensitive features audited:
  - income_bracket (0=low, 1=mid, 2=high) — derived from ZIP→Census lookup
  - is_rural (0=urban, 1=rural) — from population density
  - historical_response_quartile (1-4) — historical station response times

Pass criterion: no group shows >15% difference in AERIAL+MUTUAL_AID dispatch
rate compared to the overall base rate.

Output: bias report written to s3://wildfire-watch-ml-data/bias-reports/<timestamp>/

Run after model deployment (#13):
  python ml/scripts/bias_audit.py --endpoint-name wildfire-watch-dispatch
"""

import argparse
import os
from datetime import datetime

import boto3
import pandas as pd
import numpy as np

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
ML_BUCKET = os.environ.get("WW_ML_BUCKET", "wildfire-watch-ml-data")
SAGEMAKER_ROLE_ARN = os.environ.get("WW_SAGEMAKER_ROLE_ARN", "")
ENDPOINT_NAME = os.environ.get("WW_SAGEMAKER_ENDPOINT", "wildfire-watch-dispatch")
DISPARITY_THRESHOLD = 0.15  # 15% max allowed group disparity


def add_demographic_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add synthetic demographic proxies to the training data for bias analysis.

    In production these would come from a ZIP→Census lookup. For the hackathon
    we approximate them from the geographic coordinates already in the dataset.

    income_bracket: 0=low (rural lat), 1=mid, 2=high (urban cluster)
    is_rural: 1 if population_at_risk < 100
    historical_response_quartile: 1-4, based on nearest_station_dist_km
    """
    # Approximate income from latitude bands (very rough CA proxy — coastal = higher income).
    df["income_bracket"] = pd.cut(
        df["lon"].abs(),
        bins=[0, 117.5, 118.2, 119.0, 999],
        labels=[2, 1, 1, 0],   # further inland = lower income proxy
    ).astype(int)

    df["is_rural"] = (df["population_at_risk"] < 100).astype(int)

    df["historical_response_quartile"] = pd.qcut(
        df["nearest_station_dist_km"],
        q=4,
        labels=[1, 2, 3, 4],  # 1=fast (close station), 4=slow (far station)
    ).astype(int)

    return df


def compute_group_disparity(df: pd.DataFrame, predictions: np.ndarray, facet: str) -> dict:
    """Compute dispatch rate disparity between demographic groups.

    Measures whether protected groups (low income, rural) receive high-tier
    dispatch (MUTUAL_AID or AERIAL) at significantly different rates.
    A large negative disparity for a disadvantaged group means the model
    is systematically recommending lower-tier response for that group.

    Args:
        df: feature DataFrame with demographic columns added
        predictions: array of predicted class labels (0=LOCAL, 1=MUTUAL_AID, 2=AERIAL)
        facet: column name of the demographic feature to analyze

    Returns:
        dict: {group_value: dispatch_rate, "disparity": max_abs_difference, "passes": bool}
    """
    # High-tier dispatch = MUTUAL_AID or AERIAL (class >= 1)
    df = df.copy()
    df["high_tier"] = (predictions >= 1).astype(int)

    base_rate = df["high_tier"].mean()
    result = {"base_rate": round(float(base_rate), 4)}

    group_rates = df.groupby(facet)["high_tier"].mean()
    for group_val, rate in group_rates.items():
        result[f"group_{group_val}_rate"] = round(float(rate), 4)

    disparities = [(abs(float(rate) - float(base_rate))) for rate in group_rates]
    max_disparity = max(disparities) if disparities else 0.0
    result["max_disparity"] = round(max_disparity, 4)
    result["passes"] = max_disparity <= DISPARITY_THRESHOLD

    return result


def run_bias_audit(endpoint_name: str, data_path: str, output_prefix: str) -> dict:
    """Run the full bias audit: load data, get predictions, compute disparities."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dispatch_model"))
    from features import FEATURE_NAMES

    print(f"Loading audit data from {data_path} ...")
    df = pd.read_csv(data_path)
    df = add_demographic_features(df)

    # Get predictions from the live endpoint for each row.
    sm_runtime = boto3.client("sagemaker-runtime", region_name=REGION)
    import json

    print(f"Invoking endpoint '{endpoint_name}' for {len(df)} samples ...")
    predictions = []
    for _, row in df.iterrows():
        features = row[FEATURE_NAMES].tolist()
        resp = sm_runtime.invoke_endpoint(
            EndpointName=endpoint_name,
            ContentType="application/json",
            Accept="application/json",
            Body=json.dumps({"features": features}),
        )
        result = json.loads(resp["Body"].read())
        predictions.append(result["dispatch_level"])

    predictions = np.array(predictions)

    # Run disparity analysis for each sensitive facet.
    report = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "endpoint": endpoint_name,
        "n_samples": len(df),
        "disparity_threshold": DISPARITY_THRESHOLD,
        "facets": {},
    }

    all_pass = True
    for facet in ["income_bracket", "is_rural", "historical_response_quartile"]:
        result = compute_group_disparity(df, predictions, facet)
        report["facets"][facet] = result
        status = "PASS" if result["passes"] else "FAIL"
        print(f"  {facet}: max_disparity={result['max_disparity']:.3f} [{status}]")
        if not result["passes"]:
            all_pass = False

    report["overall_pass"] = all_pass

    # Write report to S3.
    s3 = boto3.client("s3", region_name=REGION)
    report_key = f"{output_prefix}/bias_report.json"
    s3.put_object(
        Bucket=ML_BUCKET,
        Key=report_key,
        Body=json.dumps(report, indent=2).encode(),
        ContentType="application/json",
    )
    print(f"\nBias report saved → s3://{ML_BUCKET}/{report_key}")

    if not all_pass:
        print("\nWARNING: Bias audit FAILED — one or more groups exceed the 15% disparity threshold.")
        print("Review the report and retrain with fairness constraints before production use.")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SageMaker Clarify bias audit")
    parser.add_argument("--endpoint-name", default=ENDPOINT_NAME)
    parser.add_argument("--data-path", default="ml/data/train.csv",
                        help="CSV file to audit (should be a held-out test set ideally)")
    parser.add_argument("--output-prefix", default=f"bias-reports/{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
                        help="S3 key prefix for the report")
    args = parser.parse_args()

    report = run_bias_audit(args.endpoint_name, args.data_path, args.output_prefix)
    overall = "PASSED" if report["overall_pass"] else "FAILED"
    print(f"\nBias audit {overall}.")
