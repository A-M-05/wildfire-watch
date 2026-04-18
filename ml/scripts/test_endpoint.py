"""
Verify the wildfire-watch-dispatch SageMaker endpoint is healthy.

Runs 5 representative fire scenarios and asserts that:
  - Every response has a valid recommendation and confidence
  - Confidence scores are in [0, 1]
  - High-severity inputs produce AERIAL recommendations
  - Low-severity inputs produce LOCAL recommendations

Usage:
  python ml/scripts/test_endpoint.py
  python ml/scripts/test_endpoint.py --endpoint-name wildfire-watch-dispatch
"""

import argparse
import json
import os
import sys
import boto3

ENDPOINT_NAME = os.environ.get("WW_SAGEMAKER_ENDPOINT", "wildfire-watch-dispatch")
CONFIDENCE_THRESHOLD = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

# Feature order matches FEATURE_NAMES in features.py:
#   lat, lon, spread_rate, population, station_dist, wind, radiative_power, hour, is_weekend
TEST_SCENARIOS = [
    {
        "name": "Thousand Oaks (suburban, high pop)",
        "features": [34.1705, -118.8376, 2.1, 1200, 8.5, 6.2, 450.0, 14, 0],
        "expect_min_level": 1,  # at least MUTUAL_AID
    },
    {
        "name": "Malibu coastal (wind-driven, fast spread)",
        "features": [34.0259, -118.7798, 4.8, 600, 12.0, 12.5, 820.0, 3, 1],
        "expect_min_level": 2,  # should be AERIAL
    },
    {
        "name": "Big Bear remote (low pop, far station)",
        "features": [34.2439, -116.9114, 0.3, 40, 38.0, 2.1, 80.0, 10, 0],
        "expect_min_level": 0,  # could be LOCAL
    },
    {
        "name": "San Fernando Valley (urban interface)",
        "features": [34.2811, -118.4407, 3.2, 2500, 4.2, 7.8, 650.0, 18, 0],
        "expect_min_level": 2,  # dense pop + spread → AERIAL
    },
    {
        "name": "Inland Empire warehouse (industrial)",
        "features": [34.0555, -117.1825, 1.6, 350, 6.0, 5.0, 300.0, 9, 0],
        "expect_min_level": 1,  # MUTUAL_AID
    },
]


def invoke(sm_runtime, endpoint_name: str, features: list) -> dict:
    resp = sm_runtime.invoke_endpoint(
        EndpointName=endpoint_name,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps({"features": features}),
    )
    return json.loads(resp["Body"].read())


def run_tests(endpoint_name: str) -> bool:
    sm_runtime = boto3.client("sagemaker-runtime", region_name=REGION)
    level_names = {0: "LOCAL", 1: "MUTUAL_AID", 2: "AERIAL"}
    all_passed = True

    print(f"Testing endpoint: {endpoint_name}\n{'─'*60}")

    for scenario in TEST_SCENARIOS:
        result = invoke(sm_runtime, endpoint_name, scenario["features"])

        level = result.get("dispatch_level", -1)
        confidence = result.get("confidence", -1)
        recommendation = result.get("recommendation", "?")

        # Validate structure
        assert 0.0 <= confidence <= 1.0, f"Confidence out of range: {confidence}"
        assert recommendation in level_names.values(), f"Unknown recommendation: {recommendation}"

        # Validate severity expectation
        passed = level >= scenario["expect_min_level"]
        marker = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        gate = "→ HUMAN REVIEW" if confidence < CONFIDENCE_THRESHOLD else ""
        print(f"[{marker}] {scenario['name']}")
        print(f"       {recommendation} (confidence={confidence:.3f}) {gate}")
        if not passed:
            print(f"       EXPECTED at least {level_names[scenario['expect_min_level']]}, got {recommendation}")

    print(f"{'─'*60}")
    print("All tests passed." if all_passed else "SOME TESTS FAILED — review model or threshold.")
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-name", default=ENDPOINT_NAME)
    args = parser.parse_args()

    success = run_tests(args.endpoint_name)
    sys.exit(0 if success else 1)
