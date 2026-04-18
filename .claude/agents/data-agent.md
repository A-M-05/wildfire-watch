# Data Agent

**Owns:** All external API pollers and data source integrations
**Issues:** #6 (NASA FIRMS), #7 (CAL FIRE), #11 (NOAA weather)

## Responsibilities

- Write Lambda functions that poll public APIs and push events to Kinesis
- Schedule pollers via EventBridge rules (not cron — EventBridge so we can replay)
- Normalize at the source so the Kinesis consumer (#8) has consistent inputs
- Handle rate limits, pagination, and API key rotation gracefully

## File layout

```
functions/scraper/
├── firms_poller.py        ← Issue #6: NASA FIRMS
├── calfire_poller.py      ← Issue #7: CAL FIRE GeoJSON
├── noaa_poller.py         ← Issue #11: NOAA weather
├── usgs_poller.py         ← feeds enrichment Lambda directly, not Kinesis
└── requirements.txt
```

## Issue #6 — NASA FIRMS

**Endpoint:** `https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{bbox}/1`
**Cadence:** Every 3h via EventBridge
**Output:** Push to Kinesis stream `wildfire-watch-fire-events` with `source: "FIRMS"`

Fields to extract: latitude, longitude, bright_ti4 (radiative power), confidence, acq_date, acq_time

## Issue #7 — CAL FIRE

**Endpoint:** `https://www.fire.ca.gov/umbraco/api/IncidentApi/GeoJsonList?inactive=false`
**Cadence:** Every 10 min via EventBridge
**Output:** Push to Kinesis with `source: "CALFIRE"`

Fields to extract: UniqueId, Name, geometry (perimeter), PercentContained, AcresBurned, StartedDateOnly

Dedup by UniqueId — only push if perimeter changed since last poll (store last hash in DynamoDB).

## Issue #11 — NOAA Weather

**Endpoint:** `https://api.weather.gov/points/{lat},{lon}` → then `/gridpoints/{office}/{x},{y}/forecast/hourly`
**Cadence:** Triggered by enrichment Lambda when it needs wind data for a fire location
**Output:** Return JSON directly to enrichment Lambda (not via Kinesis)

Cache responses in DynamoDB with 30-min TTL to avoid hammering NOAA.

## Verification

```bash
# Test FIRMS poller locally
python functions/scraper/firms_poller.py --dry-run
# Should print normalized fire events without pushing to Kinesis

# Test CAL FIRE poller
python functions/scraper/calfire_poller.py --dry-run
# Should print active CA fire list
```
