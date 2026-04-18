"""
SageMaker Model Monitor baseline + hourly schedule — issue #20.

Model Monitor watches for distribution shift between the training data
and live inference inputs. If fire behavior in the real world starts
looking different from the training distribution, the model's predictions
become unreliable — even if accuracy metrics look fine on historical data.

Alert condition: Jensen-Shannon divergence > 0.3 on any feature →
  CloudWatch alarm → SNS notification to ML team.

What "distribution shift" means here:
  If the real fires suddenly have much higher spread rates than training data,
  the model has never seen that regime and will extrapolate outside its
  reliable zone. JSD > 0.3 is a strong signal that something has changed.

Run once after the endpoint is live (#13):
  python ml/scripts/setup_monitor.py

The schedule runs hourly from that point forward automatically.
"""

import argparse
import os
import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
ML_BUCKET = os.environ.get("WW_ML_BUCKET", "wildfire-watch-ml-data")
SAGEMAKER_ROLE_ARN = os.environ.get("WW_SAGEMAKER_ROLE_ARN", "")
ENDPOINT_NAME = os.environ.get("WW_SAGEMAKER_ENDPOINT", "wildfire-watch-dispatch")
SNS_ALERT_TOPIC_ARN = os.environ.get("WW_SNS_ALERT_TOPIC_ARN", "")
MONITOR_SCHEDULE_NAME = "wildfire-watch-dispatch-monitor"
JSD_THRESHOLD = 0.3


def upload_baseline_data(data_path: str) -> str:
    """Upload training data to S3 as the Model Monitor baseline dataset."""
    s3 = boto3.client("s3", region_name=REGION)
    s3_key = "monitor/baseline/train.csv"
    s3.upload_file(data_path, ML_BUCKET, s3_key)
    s3_uri = f"s3://{ML_BUCKET}/{s3_key}"
    print(f"Baseline data uploaded → {s3_uri}")
    return s3_uri


def create_monitoring_schedule(role_arn: str, endpoint_name: str, baseline_s3_uri: str):
    """Create the Model Monitor baseline and hourly monitoring schedule.

    Two-step process:
    1. suggest_baseline — runs a one-off job that computes feature statistics
       from the training data (mean, std, distribution) and stores them in S3.
    2. create_monitoring_schedule — runs hourly against live endpoint traffic,
       comparing current input distributions to the baseline statistics.
    """
    import sagemaker
    from sagemaker.model_monitor import DefaultModelMonitor, DatasetFormat, CronExpressionGenerator

    sess = sagemaker.Session(boto_session=boto3.Session(region_name=REGION))

    monitor = DefaultModelMonitor(
        role=role_arn,
        instance_count=1,
        instance_type="ml.m5.xlarge",
        volume_size_in_gb=20,
        max_runtime_in_seconds=1800,  # 30-min cap per monitoring run
        sagemaker_session=sess,
    )

    baseline_output_uri = f"s3://{ML_BUCKET}/monitor/baseline-results/"
    print("Running baseline suggestion job (this takes ~5 minutes) ...")
    monitor.suggest_baseline(
        baseline_dataset=baseline_s3_uri,
        dataset_format=DatasetFormat.csv(header=True),
        output_s3_uri=baseline_output_uri,
        wait=True,
        logs=True,
    )
    print(f"Baseline statistics saved → {baseline_output_uri}")

    monitor_output_uri = f"s3://{ML_BUCKET}/monitor/live-reports/"
    print(f"\nCreating hourly monitoring schedule '{MONITOR_SCHEDULE_NAME}' ...")
    monitor.create_monitoring_schedule(
        monitor_schedule_name=MONITOR_SCHEDULE_NAME,
        endpoint_input=endpoint_name,
        output_s3_uri=monitor_output_uri,
        statistics=monitor.baseline_statistics(),
        constraints=monitor.suggested_constraints(),
        # Hourly is the right cadence for a fire system — slow enough to avoid noise,
        # fast enough to catch a sudden shift during an active fire season.
        schedule_cron_expression=CronExpressionGenerator.hourly(),
    )
    print(f"Monitoring schedule created → runs hourly against '{endpoint_name}'")

    return monitor


def create_cloudwatch_alarm(endpoint_name: str, sns_topic_arn: str):
    """Create a CloudWatch alarm that fires when JSD exceeds the threshold.

    Model Monitor emits feature_baseline_drift metrics to CloudWatch.
    We alarm on the maximum drift across all features — if any single
    feature drifts significantly, we want to know.
    """
    cw = boto3.client("cloudwatch", region_name=REGION)

    alarm_name = "wildfire-watch-model-drift"
    cw.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Dispatch model input distribution drift > {JSD_THRESHOLD} JSD. "
            "Model may be operating outside training distribution — review before trusting predictions."
        ),
        Namespace="aws/sagemaker/Endpoints/data-metrics",
        MetricName="feature_baseline_drift",
        Dimensions=[{"Name": "Endpoint", "Value": endpoint_name}],
        Statistic="Maximum",
        Period=3600,         # 1-hour evaluation window (matches monitoring schedule)
        EvaluationPeriods=1,
        Threshold=JSD_THRESHOLD,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",  # missing data = monitor hasn't run yet, not a problem
        AlarmActions=[sns_topic_arn] if sns_topic_arn else [],
        OKActions=[sns_topic_arn] if sns_topic_arn else [],
    )
    print(f"CloudWatch alarm '{alarm_name}' created (threshold: JSD > {JSD_THRESHOLD})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up Model Monitor for dispatch endpoint")
    parser.add_argument("--endpoint-name", default=ENDPOINT_NAME)
    parser.add_argument("--baseline-data", default="ml/data/train.csv",
                        help="Local CSV path for baseline statistics")
    parser.add_argument("--role-arn", default=SAGEMAKER_ROLE_ARN)
    parser.add_argument("--sns-topic-arn", default=SNS_ALERT_TOPIC_ARN,
                        help="SNS topic for drift alerts (optional)")
    args = parser.parse_args()

    if not args.role_arn:
        print("ERROR: Set WW_SAGEMAKER_ROLE_ARN env var or pass --role-arn")
        raise SystemExit(1)

    baseline_s3 = upload_baseline_data(args.baseline_data)
    create_monitoring_schedule(args.role_arn, args.endpoint_name, baseline_s3)

    if args.sns_topic_arn:
        create_cloudwatch_alarm(args.endpoint_name, args.sns_topic_arn)
    else:
        print("\nSkipping CloudWatch alarm — pass --sns-topic-arn to enable drift alerts")

    print("\nModel Monitor setup complete.")
