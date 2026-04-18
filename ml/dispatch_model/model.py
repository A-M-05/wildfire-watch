"""
XGBoost spread prediction model — build, save, load, predict.

Predicts fire spread rate (km²/hr) and projected area (km²) from weather
and terrain inputs. Dispatch recommendation is derived rule-based from
the spread rate so the decision is fully auditable.
"""

import os
import json
import xgboost as xgb
from features import FEATURE_NAMES, TARGET_NAMES, spread_to_dispatch, spread_to_confidence


def build_model(max_depth: int = 6, n_estimators: int = 200, learning_rate: float = 0.05):
    """Return an untrained XGBRegressor.

    XGBoost regression builds trees that minimize squared error on the
    continuous spread rate target, rather than class probabilities.
    Lower learning_rate + more trees = better generalization on tabular data.
    """
    return xgb.XGBRegressor(
        max_depth=max_depth,
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        objective="reg:squarederror",
        eval_metric="rmse",
        random_state=42,
        n_jobs=-1,
    )


def save_model(spread_model, area_model, model_dir: str):
    """Save both models to model_dir — SageMaker tars this into model.tar.gz."""
    os.makedirs(model_dir, exist_ok=True)
    spread_model.save_model(os.path.join(model_dir, "spread_model.xgb"))
    area_model.save_model(os.path.join(model_dir, "area_model.xgb"))

    meta = {"feature_names": FEATURE_NAMES, "target_names": TARGET_NAMES}
    with open(os.path.join(model_dir, "feature_meta.json"), "w") as f:
        json.dump(meta, f)


def load_models(model_dir: str):
    """Load both models — called by the SageMaker endpoint at startup."""
    spread_model = xgb.XGBRegressor()
    spread_model.load_model(os.path.join(model_dir, "spread_model.xgb"))
    area_model = xgb.XGBRegressor()
    area_model.load_model(os.path.join(model_dir, "area_model.xgb"))
    return spread_model, area_model


def predict(spread_model, area_model, features: list) -> dict:
    """Run inference and return spread predictions + derived dispatch recommendation.

    Returns:
        spread_rate_km2_per_hr: predicted fire growth rate
        projected_area_km2: estimated area burned in next 30 minutes
        recommendation: LOCAL | MUTUAL_AID | AERIAL (rule-based from spread rate)
        dispatch_level: 0 | 1 | 2
        confidence: 0-1 score (low near dispatch thresholds, high when clear)
    """
    import numpy as np

    X = np.array([features])
    spread_rate = float(spread_model.predict(X)[0])
    projected_area = float(area_model.predict(X)[0])

    # Clamp to physically meaningful range
    spread_rate = max(0.0, spread_rate)
    projected_area = max(0.0, projected_area)

    recommendation, dispatch_level = spread_to_dispatch(spread_rate)
    confidence = spread_to_confidence(spread_rate, projected_area)

    return {
        "spread_rate_km2_per_hr": round(spread_rate, 3),
        "projected_area_km2": round(projected_area, 3),
        "recommendation": recommendation,
        "dispatch_level": dispatch_level,
        "confidence": confidence,
    }
