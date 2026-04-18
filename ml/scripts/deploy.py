"""
Deploy the wildfire dispatch model to a SageMaker real-time endpoint.

What this script does:
  1. Finds the latest model package in the wildfire-watch-dispatch registry group
  2. Approves it (moves it from PendingManualApproval → Approved)
  3. Packages the inference code alongside the model artifact
  4. Deploys (or updates) the wildfire-watch-dispatch endpoint on ml.m5.large
  5. Runs a smoke test to confirm the endpoint is healthy

Run this after a successful training job (#12). You only need to run it once
per training run — it updates the endpoint in-place if it already exists.

Usage:
  python ml/scripts/deploy.py
  python ml/scripts/deploy.py --approve-only      # approve but don't deploy yet
  python ml/scripts/deploy.py --no-approve        # deploy whatever is already Approved
"""

import argparse
import json
import os
import sys
import time
import boto3

ENDPOINT_NAME = "wildfire-watch-dispatch"
MODEL_PACKAGE_GROUP = os.environ.get("WW_MODEL_PACKAGE_GROUP", "wildfire-watch-dispatch")
SAGEMAKER_ROLE_ARN = os.environ.get("WW_SAGEMAKER_ROLE_ARN", "")
INSTANCE_TYPE = "ml.m5.large"
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")


def get_latest_model_package(sm_client, group_name: str, status_filter: str = None) -> dict:
    """Return the most recently created model package from the registry group.

    SageMaker Model Registry stores every training run as a versioned package.
    We sort by creation time and take the newest one.
    """
    kwargs = {
        "ModelPackageGroupName": group_name,
        "SortBy": "CreationTime",
        "SortOrder": "Descending",
        "MaxResults": 1,
    }
    if status_filter:
        kwargs["ModelApprovalStatus"] = status_filter

    resp = sm_client.list_model_packages(**kwargs)
    packages = resp.get("ModelPackageSummaryList", [])

    if not packages:
        raise RuntimeError(
            f"No model packages found in group '{group_name}' "
            f"(status filter: {status_filter or 'any'}). "
            "Run the training job (#12) first."
        )
    return packages[0]


def approve_model_package(sm_client, package_arn: str) -> None:
    """Approve a model package so it can be deployed.

    SageMaker enforces approval status — you can't deploy a PendingManualApproval
    package. This is a lightweight human-in-the-loop gate before production serving.
    In a real setup a human would review Clarify bias metrics (#18) before approving.
    """
    sm_client.update_model_package(
        ModelPackageArn=package_arn,
        ModelApprovalStatus="Approved",
    )
    print(f"Approved model package: {package_arn}")


def deploy_endpoint(sm_client, package_arn: str, role_arn: str) -> str:
    """Create or update the SageMaker endpoint.

    SageMaker endpoint lifecycle:
      Model → EndpointConfig → Endpoint

    If the endpoint already exists, we create a new config and update it — this
    does a blue/green swap with no downtime (the old version keeps serving until
    the new one passes health checks).
    """
    import sagemaker
    from sagemaker import ModelPackage

    # Use the SageMaker Python SDK which handles the Model/Config/Endpoint
    # three-step dance and blue/green updates automatically.
    sess = sagemaker.Session(boto_session=boto3.Session(region_name=REGION))

    model = ModelPackage(
        role=role_arn,
        model_package_arn=package_arn,
        sagemaker_session=sess,
        # Point at our custom inference.py so the container uses our
        # input_fn/predict_fn/output_fn instead of the default CSV handler.
        source_dir=os.path.join(os.path.dirname(__file__), "..", "dispatch_model"),
        entry_point="inference.py",
    )

    print(f"Deploying to endpoint '{ENDPOINT_NAME}' on {INSTANCE_TYPE} ...")
    print("(This takes ~5 minutes — SageMaker is provisioning the instance)")

    model.deploy(
        endpoint_name=ENDPOINT_NAME,
        instance_type=INSTANCE_TYPE,
        initial_instance_count=1,
        update_endpoint=True,  # in-place update if endpoint already exists
    )

    return ENDPOINT_NAME


def wait_for_endpoint(sm_client, endpoint_name: str, timeout_s: int = 600) -> str:
    """Poll until the endpoint is InService or fails."""
    print(f"Waiting for endpoint '{endpoint_name}' to be InService ...")
    start = time.time()

    while time.time() - start < timeout_s:
        resp = sm_client.describe_endpoint(EndpointName=endpoint_name)
        status = resp["EndpointStatus"]
        print(f"  Status: {status}")

        if status == "InService":
            return status
        if status in ("Failed", "OutOfService"):
            reason = resp.get("FailureReason", "unknown")
            raise RuntimeError(f"Endpoint deployment failed: {reason}")

        time.sleep(20)  # SageMaker status updates every ~20s

    raise TimeoutError(f"Endpoint not InService after {timeout_s}s")


def smoke_test(endpoint_name: str) -> None:
    """Invoke the endpoint with a known fire event and validate the response structure."""
    sm_runtime = boto3.client("sagemaker-runtime", region_name=REGION)

    # Thousand Oaks fire scenario (demo hero — high population, moderate spread).
    # Features match FEATURE_NAMES order from features.py.
    test_features = [
        34.1705,   # lat
        -118.8376, # lon
        2.1,       # spread_rate_km2_per_hr
        1200,      # population_at_risk
        8.5,       # nearest_station_dist_km
        6.2,       # wind_speed_ms
        450.0,     # radiative_power (MW)
        14,        # hour_of_day (2pm)
        0,         # is_weekend
    ]

    print(f"\nSmoke test — invoking '{endpoint_name}' with Thousand Oaks scenario ...")
    resp = sm_runtime.invoke_endpoint(
        EndpointName=endpoint_name,
        ContentType="application/json",
        Accept="application/json",
        Body=json.dumps({"features": test_features}),
    )
    result = json.loads(resp["Body"].read())

    # Validate response structure — not the values (model may vary), just the shape.
    assert "confidence" in result, f"Missing 'confidence' in response: {result}"
    assert "recommendation" in result, f"Missing 'recommendation' in response: {result}"
    assert 0.0 <= result["confidence"] <= 1.0, f"Confidence out of range: {result['confidence']}"
    assert result["recommendation"] in ("LOCAL", "MUTUAL_AID", "AERIAL"), \
        f"Unknown recommendation: {result['recommendation']}"

    print(f"  Recommendation : {result['recommendation']}")
    print(f"  Confidence     : {result['confidence']:.4f}")
    print(f"  Probabilities  : {result['probabilities']}")
    confidence_threshold = float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65"))
    if result["confidence"] < confidence_threshold:
        print(f"  NOTE: confidence below {confidence_threshold} — Step Functions would route to human review")
    print("\nSmoke test passed.")


def main():
    parser = argparse.ArgumentParser(description="Deploy wildfire dispatch model to SageMaker")
    parser.add_argument("--approve-only", action="store_true",
                        help="Approve the latest pending model package but do not deploy")
    parser.add_argument("--no-approve", action="store_true",
                        help="Skip approval — deploy whatever is already Approved")
    parser.add_argument("--role-arn", type=str, default=SAGEMAKER_ROLE_ARN,
                        help="SageMaker execution role ARN (defaults to WW_SAGEMAKER_ROLE_ARN env var)")
    args = parser.parse_args()

    if not args.role_arn:
        print("ERROR: Set WW_SAGEMAKER_ROLE_ARN env var or pass --role-arn")
        sys.exit(1)

    sm = boto3.client("sagemaker", region_name=REGION)

    if args.no_approve:
        # Find an already-approved package to deploy.
        pkg = get_latest_model_package(sm, MODEL_PACKAGE_GROUP, status_filter="Approved")
    else:
        # Find the latest package regardless of status, then approve it.
        pkg = get_latest_model_package(sm, MODEL_PACKAGE_GROUP)
        print(f"Latest model package: {pkg['ModelPackageArn']} (status: {pkg['ModelApprovalStatus']})")
        if pkg["ModelApprovalStatus"] != "Approved":
            approve_model_package(sm, pkg["ModelPackageArn"])

    if args.approve_only:
        print("--approve-only set, stopping before deploy.")
        return

    deploy_endpoint(sm, pkg["ModelPackageArn"], args.role_arn)
    wait_for_endpoint(sm, ENDPOINT_NAME)
    smoke_test(ENDPOINT_NAME)

    print(f"\nEndpoint '{ENDPOINT_NAME}' is live.")
    print(f"Set WW_SAGEMAKER_ENDPOINT={ENDPOINT_NAME} in your Lambda environment variables.")


if __name__ == "__main__":
    main()
