"""
XGBoost dispatch recommendation model — build, save, load, predict.

Keeping this separate from train.py means the inference Lambda (#13) can import
load_model and predict without pulling in any training dependencies.
"""

import os
import json
import xgboost as xgb
from features import DISPATCH_LEVELS, FEATURE_NAMES


def build_model(max_depth: int = 6, n_estimators: int = 150, learning_rate: float = 0.1):
    """Return an untrained XGBClassifier with the given hyperparameters.

    XGBoost is a gradient-boosted decision tree ensemble. It builds trees
    sequentially where each new tree corrects the errors of the previous ones.
    predict_proba gives us per-class probabilities — that's where the
    confidence score (max probability) comes from.
    """
    return xgb.XGBClassifier(
        max_depth=max_depth,
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        num_class=len(DISPATCH_LEVELS),   # 3 dispatch levels
        objective="multi:softprob",        # output full probability distribution, not just winner
        eval_metric="mlogloss",            # multi-class log loss for evaluation
        random_state=42,
    )


def save_model(model: xgb.XGBClassifier, model_dir: str):
    """Save model to model_dir/model.xgb — SageMaker tars this directory into model.tar.gz."""
    os.makedirs(model_dir, exist_ok=True)
    model_path = os.path.join(model_dir, "model.xgb")
    model.save_model(model_path)

    # Save feature names alongside the model so the endpoint can validate input order.
    meta_path = os.path.join(model_dir, "feature_meta.json")
    with open(meta_path, "w") as f:
        json.dump({"feature_names": FEATURE_NAMES, "dispatch_levels": DISPATCH_LEVELS}, f)

    return model_path


def load_model(model_dir: str) -> xgb.XGBClassifier:
    """Load model from model_dir — called by the SageMaker endpoint container at startup."""
    model = xgb.XGBClassifier()
    model.load_model(os.path.join(model_dir, "model.xgb"))
    return model


def predict(model: xgb.XGBClassifier, features: list) -> dict:
    """Run inference and return recommendation + confidence.

    Args:
        features: list of floats in FEATURE_NAMES order (use features.extract_features)

    Returns:
        dict with:
          dispatch_level (int 0-2)
          recommendation (str "LOCAL" | "MUTUAL_AID" | "AERIAL")
          confidence (float 0-1) — probability of the winning class
          probabilities (dict) — full class probability breakdown
    """
    import numpy as np

    # XGBoost expects a 2D array — one row per sample.
    X = np.array([features])
    probs = model.predict_proba(X)[0]   # shape (3,) — one prob per dispatch level

    dispatch_level = int(np.argmax(probs))
    confidence = float(probs[dispatch_level])

    return {
        "dispatch_level": dispatch_level,
        "recommendation": DISPATCH_LEVELS[dispatch_level],
        "confidence": confidence,
        # Full breakdown for transparency in the dispatch panel (#28).
        "probabilities": {DISPATCH_LEVELS[i]: float(p) for i, p in enumerate(probs)},
    }
