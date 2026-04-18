"""
SageMaker serving script — runs inside the XGBoost container at endpoint startup.

SageMaker calls these four functions in order for every inference request:
  model_fn  → called ONCE at container start to load the model from disk
  input_fn  → called PER REQUEST to deserialize the raw HTTP body
  predict_fn → called PER REQUEST to run the model
  output_fn  → called PER REQUEST to serialize the result back to JSON

The container passes the path to the unpacked model.tar.gz as `model_dir`.
Our training script (train.py) wrote model.xgb and feature_meta.json there.
"""

import json
import os
import sys
import numpy as np

# inference.py lives alongside model.py and features.py in the model artifact.
sys.path.insert(0, os.path.dirname(__file__))
from model import load_model, predict
from features import FEATURE_NAMES


def model_fn(model_dir: str):
    """Load model from disk. Called once when the container starts."""
    model = load_model(model_dir)

    # Validate feature metadata matches what this code expects — catches
    # mismatches between a retrained model and an old inference script.
    meta_path = os.path.join(model_dir, "feature_meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        saved_features = meta.get("feature_names", [])
        if saved_features != FEATURE_NAMES:
            raise RuntimeError(
                f"Feature mismatch — model trained on {saved_features}, "
                f"inference code expects {FEATURE_NAMES}. Redeploy with matching artifact."
            )

    return model


def input_fn(request_body: str, content_type: str = "application/json") -> np.ndarray:
    """Parse the incoming request body into a numpy array.

    Expected input format (from SKILL.md):
      {"features": [lat, lon, spread_rate, population, station_dist, wind, radiative_power, hour, is_weekend]}
    """
    if content_type != "application/json":
        raise ValueError(f"Unsupported content type: {content_type}. Use application/json.")

    body = json.loads(request_body)

    if "features" not in body:
        raise ValueError("Request body must contain a 'features' key with a list of floats.")

    features = body["features"]
    if len(features) != len(FEATURE_NAMES):
        raise ValueError(
            f"Expected {len(FEATURE_NAMES)} features ({FEATURE_NAMES}), got {len(features)}."
        )

    return features  # predict() in model.py handles the np.array conversion


def predict_fn(features: list, model) -> dict:
    """Run the dispatch model and return a structured prediction dict."""
    result = predict(model, features)

    # Log for CloudWatch — but never log PII (no phone numbers, addresses, names).
    print(json.dumps({
        "event": "dispatch_prediction",
        "recommendation": result["recommendation"],
        "confidence": round(result["confidence"], 4),
        "below_threshold": result["confidence"] < float(os.environ.get("WW_CONFIDENCE_THRESHOLD", "0.65")),
    }))

    return result


def output_fn(prediction: dict, accept: str = "application/json") -> str:
    """Serialize the prediction dict to a JSON string."""
    if accept != "application/json":
        raise ValueError(f"Unsupported accept type: {accept}. Use application/json.")
    return json.dumps(prediction)
