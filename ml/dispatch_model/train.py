"""
SageMaker training entry point for the wildfire dispatch recommendation model.

SageMaker runs this script inside a managed container. It passes:
  - Hyperparameters as CLI flags (--max-depth, etc.)
  - The training data path via SM_CHANNEL_TRAIN env var
  - The output model path via SM_MODEL_DIR env var

After training, SageMaker tars SM_MODEL_DIR into model.tar.gz and stores it
in S3. Issue #13 then deploys that artifact as an endpoint.

Run locally (for testing without SageMaker):
  python train.py --local --train-path ml/data/train.csv --model-dir /tmp/model
"""

import argparse
import json
import os
import sys
import boto3
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score

# SageMaker injects the training script's directory into sys.path so we can
# import sibling modules (features.py, model.py) without a package install.
sys.path.insert(0, os.path.dirname(__file__))
from features import FEATURE_NAMES
from model import build_model, save_model


def load_data(train_path: str) -> tuple:
    """Load training CSV and split into features (X) and labels (y)."""
    csv_path = os.path.join(train_path, "train.csv")
    df = pd.read_csv(csv_path)

    # Validate that the CSV has all expected columns — catches data drift early.
    missing = set(FEATURE_NAMES) - set(df.columns)
    if missing:
        raise ValueError(f"Training data missing columns: {missing}")

    X = df[FEATURE_NAMES].values.astype(np.float32)
    y = df["label"].values.astype(int)
    return X, y


def evaluate(model, X_val, y_val) -> dict:
    """Return accuracy and per-class metrics — logged to CloudWatch by SageMaker."""
    y_pred = model.predict(X_val)
    acc = accuracy_score(y_val, y_pred)
    report = classification_report(
        y_val, y_pred,
        target_names=["LOCAL", "MUTUAL_AID", "AERIAL"],
        output_dict=True,
    )
    print(f"\nValidation accuracy: {acc:.4f}")
    print(classification_report(y_val, y_pred, target_names=["LOCAL", "MUTUAL_AID", "AERIAL"]))
    return {"accuracy": acc, "classification_report": report}


def register_model(model_s3_uri: str, metrics: dict, model_package_group: str):
    """Register the trained model in SageMaker Model Registry.

    Model Registry lets us version, compare, and approve models before deploying.
    Issue #13 reads the latest "Approved" version from this group and deploys it.
    """
    sm = boto3.client("sagemaker")

    # The inference spec tells SageMaker which container to use for serving.
    # We use the AWS-managed XGBoost container so we don't need a custom image.
    inference_spec = {
        "Containers": [
            {
                "Image": _xgboost_inference_image(),
                "ModelDataUrl": model_s3_uri,
                "Framework": "XGBOOST",
                "FrameworkVersion": "1.7-1",
            }
        ],
        "SupportedContentTypes": ["application/json"],
        "SupportedResponseMIMETypes": ["application/json"],
        "SupportedTransformInstanceTypes": ["ml.m5.large"],
        "SupportedRealtimeInferenceInstanceTypes": ["ml.m5.large"],
    }

    response = sm.create_model_package(
        ModelPackageGroupName=model_package_group,
        ModelPackageDescription=f"XGBoost dispatch model — accuracy {metrics['accuracy']:.4f}",
        InferenceSpecification=inference_spec,
        ModelApprovalStatus="PendingManualApproval",  # issue #13 approves + deploys
        ModelMetrics={
            "ModelQuality": {
                "Statistics": {
                    "ContentType": "application/json",
                    "S3Uri": "",  # populated by Clarify in issue #18
                }
            }
        },
        CustomerMetadataProperties={
            "accuracy": str(round(metrics["accuracy"], 4)),
            "local_f1": str(round(metrics["classification_report"]["LOCAL"]["f1-score"], 4)),
            "mutual_aid_f1": str(round(metrics["classification_report"]["MUTUAL_AID"]["f1-score"], 4)),
            "aerial_f1": str(round(metrics["classification_report"]["AERIAL"]["f1-score"], 4)),
        },
    )
    arn = response["ModelPackageArn"]
    print(f"\nRegistered model package: {arn}")
    return arn


def _xgboost_inference_image() -> str:
    """Return the AWS-managed XGBoost 1.7-1 container URI for us-west-2."""
    # ECR image URIs are region-specific. Hardcoding us-west-2 to match the project region.
    # If you change region in cdk.json, update this too.
    return "246618743249.dkr.ecr.us-west-2.amazonaws.com/sagemaker-xgboost:1.7-1"


def train(args):
    print(f"Loading data from {args.train_path} ...")
    X, y = load_data(args.train_path)

    # 80/20 train-validation split — stratified so all dispatch levels are
    # represented in both splits even if the dataset is class-imbalanced.
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )
    print(f"Train: {len(X_train)} samples | Val: {len(X_val)} samples")

    print(f"\nTraining XGBoost (max_depth={args.max_depth}, n_estimators={args.n_estimators}) ...")
    model = build_model(
        max_depth=args.max_depth,
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
    )
    # eval_set lets XGBoost print validation loss each round — useful for spotting overfitting.
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=25,  # print every 25 rounds
    )

    metrics = evaluate(model, X_val, y_val)

    model_path = save_model(model, args.model_dir)
    print(f"\nModel saved → {model_path}")

    # Register in Model Registry unless running locally (no SageMaker context).
    if not args.local and args.model_package_group:
        # SageMaker stores the artifact at a known S3 path after training completes.
        bucket = os.environ.get("WW_ML_BUCKET", "wildfire-watch-ml-data")
        model_s3_uri = f"s3://{bucket}/models/{os.environ.get('TRAINING_JOB_NAME', 'local')}/output/model.tar.gz"
        register_model(model_s3_uri, metrics, args.model_package_group)

    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # SageMaker hyperparameter arguments — passed via the Estimator in issue #12 CDK/notebook.
    parser.add_argument("--max-depth", type=int, default=6,
                        help="XGBoost max tree depth (higher = more complex, risk of overfitting)")
    parser.add_argument("--n-estimators", type=int, default=150,
                        help="Number of boosting rounds")
    parser.add_argument("--learning-rate", type=float, default=0.1,
                        help="Step size shrinkage — lower = slower but more robust")

    # SageMaker injects SM_CHANNEL_TRAIN and SM_MODEL_DIR automatically.
    # We default to those env vars so the script works without flags in the container.
    parser.add_argument("--train-path", type=str,
                        default=os.environ.get("SM_CHANNEL_TRAIN", "ml/data"),
                        help="Directory containing train.csv")
    parser.add_argument("--model-dir", type=str,
                        default=os.environ.get("SM_MODEL_DIR", "/tmp/model"),
                        help="Directory to write model artifact")
    parser.add_argument("--model-package-group", type=str,
                        default=os.environ.get("WW_MODEL_PACKAGE_GROUP", "wildfire-watch-dispatch"),
                        help="SageMaker Model Registry group to register into")

    # Local mode skips S3 upload and Model Registry — useful for fast iteration.
    parser.add_argument("--local", action="store_true",
                        help="Run locally without SageMaker context")

    args = parser.parse_args()
    train(args)
