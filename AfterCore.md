# After Core — Feature Extensions

## Accessibility & Congestion Risk Model

A secondary risk layer that adjusts dispatch scores and alert urgency based on **how reachable** a fire location actually is — accounting for road congestion and fire department access constraints.

### Why it matters

A fire with `risk_score = 0.7` in a dense suburb with gridlocked roads is functionally more dangerous than a `risk_score = 0.8` fire on an open highway. The base enrichment model doesn't capture this. Dispatch confidence should degrade when access is poor.

### Inputs

| Input | Source | Notes |
|---|---|---|
| Road network + real-time congestion | HERE / TomTom Traffic API or AWS Location Service Routes | Travel time from nearest station to fire coords |
| Road type / width | OpenStreetMap via Overpass API | Single-lane, unpaved, or gated roads cap apparatus size |
| Active evacuation routes | CAL FIRE / CALOES feed | Conflicting evac traffic on same roads as apparatus |
| Fire station apparatus type | `data/fire_stations.geojson` (extend) | Ladder vs. engine — narrow roads block ladder trucks |
| Historical response times | CAL FIRE incident database | Baseline expected vs. congestion-adjusted ETA |

### Model design

**Step 1 — ETA scoring**

For each candidate station, compute:
```
congestion_eta_min = routing_api(station_coords → fire_coords, departure_now)
baseline_eta_min   = straight_line_km / avg_speed_kmh * 60
access_penalty     = congestion_eta_min / baseline_eta_min   # 1.0 = no penalty
```

**Step 2 — Road accessibility flag**

Check OSM road attributes along the route:
- `road_width_m < 4.5` → `narrow_road = True` (blocks ladder trucks)
- `surface = unpaved` → `unpaved = True` (caps speed to 30 km/h)
- Route overlaps active evac route → `evac_conflict = True`

**Step 3 — Adjusted dispatch score**

```python
def adjust_for_access(station, fire_event):
    penalty = station.congestion_eta_min / station.baseline_eta_min
    if station.narrow_road and station.apparatus == "LADDER":
        penalty *= 1.5      # ladder can't use this road at all
    if station.evac_conflict:
        penalty *= 1.3      # competing traffic on evac route
    return station.base_score / penalty
```

Stations are re-ranked by `adjusted_score` before SageMaker dispatch recommendation.

**Step 4 — Risk score bump**

If the top-ranked station has `access_penalty > 1.5` (i.e. >50% slower than baseline), bump the event's `risk_score` by `+0.1` to account for degraded response capability. This can push marginal events over the `0.6` dispatch threshold.

### Enriched event additions

```json
{
  "nearest_stations": [
    {
      "station_id": "string",
      "distance_km": float,
      "available": bool,
      "eta_min_baseline": float,
      "eta_min_congested": float,
      "access_penalty": float,
      "narrow_road": bool,
      "evac_conflict": bool,
      "adjusted_score": float
    }
  ],
  "access_constrained": bool
}
```

### Integration points

- **Enrichment Lambda** (`functions/enrich/handler.py`) — add access scoring after SageMaker call, before EventBridge emit
- **Dispatch panel** (`frontend/src/DispatchPanel.jsx`) — show ETA and access flags per station; red badge if `access_constrained = true`
- **SMS advisory** — if `access_constrained`, Bedrock prompt should include delayed response caveat: *"Response time may be affected by road conditions."*
- **QLDB audit** — log `access_penalty` per station so post-incident review can assess whether congestion contributed to outcome

### Confidence gate interaction

If `access_constrained = true` AND model confidence is already borderline (0.65–0.75), lower the auto-approve threshold to 0.75 for that event — force human review when both confidence and access are uncertain.
