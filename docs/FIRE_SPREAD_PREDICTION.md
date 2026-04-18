# Fire Spread Prediction — Implementation Brief

**Owner:** _TBD_
**Target:** Predict next-hour fire perimeter + spread rate for enriched fire events so the dispatch model and resident alerts can reason about *where the fire is going*, not just where it is.

## Scope

Add a `predict_spread` Lambda that consumes enriched fire events from Kinesis and emits a `spread_forecast` payload (cone polygon + rate + confidence) onto the same stream for downstream consumers (#9 dispatch model, #22 alert sender).

Output schema (added to enriched event):

```json
{
  "spread_forecast": {
    "rate_km_per_hr": float,
    "bearing_deg": float,
    "cone_geojson": "GeoJSON Polygon string",
    "horizon_minutes": 60,
    "confidence": float,
    "model_version": "spread-v1"
  }
}
```

Confidence must feed the existing 0.65 gate (CLAUDE.md rule 3). If `confidence < 0.65`, Step Functions pauses for human review before any resident alert.

## Approach — hybrid physics + ML

### Layer 1: Rothermel-style elliptical baseline (deterministic)

Simplified surface-fire spread model. Inputs from the enriched event + NOAA:

- `wind_speed_ms`, `wind_direction_deg` (already enriched)
- slope (derive from USGS DEM tile at lat/lon — cache per cell)
- fuel moisture proxy (use NOAA RH + days-since-rain; bucket low/med/high)
- fuel model (LANDFIRE 40 Scott & Burgan — pick nearest 1km cell)

Produce head/flank/back spread rates → project an ellipse over 60 min → GeoJSON polygon.

This gives an **explainable baseline** that always runs, even if the ML layer fails. Critical for the AI-safety track.

### Layer 2: SageMaker residual correction (ML)

Train a gradient-boosted regressor (XGBoost built-in SageMaker container — cheap, fast) to predict the **residual** between the Rothermel rate and observed spread from FIRMS historical detections.

- Features: wind, RH, temp, fuel model, slope, radiative_power, containment_pct, hour-of-day, month
- Label: (observed next-detection spread rate) − (Rothermel predicted rate)
- Final prediction = Rothermel rate + ML residual
- Confidence = `1 − normalized_prediction_interval_width` (quantile regression or bootstrap)

Using residuals (not raw rate) keeps the physics interpretable and means a broken model degrades to the baseline, not to garbage.

## Roadmap

### Phase 1 — Baseline only (half day)
1. `functions/ml/spread_predictor/handler.py` — Rothermel ellipse from enriched event, emit `spread_forecast` with fixed `confidence=0.5` (forces human review until ML ships).
2. Cache DEM slope + LANDFIRE fuel lookups in S3 as pre-computed 1km grid JSON — no runtime GIS calls.
3. Unit tests: known-input ellipse, zero-wind (circle), high-wind elongation.
4. Wire into enrichment Lambda output. Verify end-to-end on a demo fire.

### Phase 2 — Training data (half day)
1. Pull 2 years of FIRMS detections for CA from S3 (already used by #12).
2. Build training rows: for each detection, find same-fire detection ~60 min later, compute observed spread rate + bearing.
3. Join weather at detection time (NOAA historical) + slope/fuel grids.
4. Drop to Parquet in the ML bucket.

### Phase 3 — Model (1 day)
1. SageMaker XGBoost training job on the Parquet dataset. Target = residual.
2. Quantile regression (`reg:quantileerror` with α=0.1, 0.5, 0.9) for prediction intervals.
3. Deploy to same SageMaker endpoint pattern as #13 (separate variant or new endpoint — coordinate with #13 owner).
4. Add model call to the Lambda; fall back to baseline on any error/timeout (hard timeout 400ms).

### Phase 4 — Safety + validation (half day)
1. Log every prediction (input features + baseline + ML residual + final + confidence) to the hash-chain audit table (#17).
2. Run Clarify bias audit across geography buckets (coastal/inland/mountain) — piggyback on #18.
3. Set Model Monitor baseline (#20).
4. Confirm Step Functions gate trips correctly when confidence < 0.65 (#19).

## Hard constraints

- **No PII.** Not a concern here — no resident data touches this pipeline, but don't add any.
- **Audit first.** Spread prediction result logged to `wildfire-watch-audit` before the event is re-published to Kinesis. See CLAUDE.md rule 1 and issue #32.
- **Fallback is mandatory.** If the SageMaker endpoint is down, baseline-only must still publish (with reduced confidence so the gate catches it).
- **Budget:** runtime < 500ms per event; training job < $15.

## Open questions for the team

1. New SageMaker endpoint or multi-model endpoint with #13? (cost vs. isolation)
2. Horizon: is 60 min the right window for the demo narrative, or do we want 30 / 60 / 120 min tiers?
3. Do we surface the cone on the frontend map (issue #27 alert-zone overlay already lays groundwork) or keep it internal-only for v1?

## Suggested GitHub issues to file

- `[ml] Implement Rothermel baseline spread Lambda` — depends on #6 (enrichment), blocks ML layer
- `[ml] Build FIRMS historical spread training dataset` — depends on #2
- `[ml] Train + deploy XGBoost spread residual model` — depends on the dataset issue
- `[safety] Hash-chain audit spread predictions` — depends on #17
- `[frontend] Render spread cone on map` — depends on baseline Lambda, optional for demo

File these under the existing ML (12–15) / Safety (16–21) sections of `docs/ISSUES.md` so the dependency graph stays coherent.
