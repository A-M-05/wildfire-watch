#!/usr/bin/env python3
"""Fetch CA reservoir storage from CDEC and snapshot to a static JSON file.

CDEC (cdec.water.ca.gov) is the canonical source for California reservoir
levels — USGS only covers stream gages, not reservoirs. Each station
publishes daily storage in acre-feet (sensor 15); we divide by the gross
pool capacity to get % full.

Run before a demo to refresh `frontend/public/data/reservoirs.json`. The
frontend reads that snapshot rather than calling CDEC live because (a)
CDEC doesn't set CORS headers, and (b) reservoir levels change slowly
enough that a snapshot per demo is plenty fresh.

Usage:
    python scripts/fetch_reservoirs.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# CDEC station code → display metadata. Lat/lon used to find nearest reservoir
# to a fire. Gross pool capacity in acre-feet from CDEC station definitions.
# Codes verified against cdec.water.ca.gov/dynamicapp/staMeta — Big Bear and
# Silverwood are MWD-operated and not in the public CDEC sensor 15 feed, so
# they're omitted (we'll add an MWD source in a follow-up if it matters).
RESERVOIRS = [
    {"station": "CAS", "name": "Castaic Lake",   "lat": 34.5239, "lon": -118.6131, "gross_pool_af": 323702},
    {"station": "PYM", "name": "Pyramid Lake",   "lat": 34.6502, "lon": -118.7481, "gross_pool_af": 171196},
    {"station": "PRR", "name": "Lake Perris",    "lat": 33.8556, "lon": -117.1683, "gross_pool_af": 131452},
    {"station": "CCH", "name": "Cachuma Lake",   "lat": 34.5828, "lon": -119.9750, "gross_pool_af": 193305},
]

# Sensor 15 = reservoir storage (acre-feet, daily timestep).
CDEC_URL = "https://cdec.water.ca.gov/dynamicapp/req/JSONDataServlet"

OUT_PATH = Path(__file__).resolve().parents[1] / "frontend" / "public" / "data" / "reservoirs.json"


def fetch_storage(station: str) -> float | None:
    """Return the most recent daily storage (acre-feet) for a CDEC station."""
    # Pull a broad ~120-day window. CDEC has multi-week lag on some stations,
    # and we'd rather show a slightly older real value than fall back to nothing.
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=120)
    params = {
        "Stations": station,
        "SensorNums": 15,
        "dur_code": "D",
        "Start": start.isoformat(),
        "End": end.isoformat(),
    }
    try:
        resp = requests.get(CDEC_URL, params=params, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        print(f"  WARN {station}: fetch failed — {exc}", file=sys.stderr)
        return None

    # CDEC returns rows oldest-first; walk backwards to the latest non-null value.
    # -9999 is the sentinel for "no data this day" — common for the latest few
    # rows when an upstream telemetry hop hasn't reported yet.
    for row in reversed(rows):
        value = row.get("value")
        if value is None or value in ("---", "BRT", "ART", -9999, "-9999"):
            continue
        try:
            v = float(value)
            if v < 0:
                continue
            return v
        except (TypeError, ValueError):
            continue
    return None


def main() -> int:
    snapshot = []
    for r in RESERVOIRS:
        storage_af = fetch_storage(r["station"])
        if storage_af is None:
            print(f"  skip {r['station']} ({r['name']}) — no recent value", file=sys.stderr)
            continue
        pct = round(100.0 * storage_af / r["gross_pool_af"], 1)
        snapshot.append({
            "station": r["station"],
            "name": r["name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "storage_af": int(storage_af),
            "gross_pool_af": r["gross_pool_af"],
            "pct_capacity": pct,
        })
        print(f"  {r['station']:4s} {r['name']:20s} {pct:5.1f}% ({int(storage_af):,} of {r['gross_pool_af']:,} AF)")

    if not snapshot:
        print("ERROR: no reservoir data fetched — refusing to overwrite snapshot", file=sys.stderr)
        return 1

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "California Data Exchange Center (cdec.water.ca.gov), sensor 15",
        "reservoirs": snapshot,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nWrote {len(snapshot)} reservoirs to {OUT_PATH.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
