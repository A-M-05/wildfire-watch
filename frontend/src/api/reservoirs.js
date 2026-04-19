// Reservoir water-level lookup for fire-spread risk context — issue #24.
//
// Backed by frontend/public/data/reservoirs.json, a snapshot from CDEC sensor 15
// produced by scripts/fetch_reservoirs.py. We snapshot rather than fetch live
// because (a) CDEC doesn't set CORS headers, and (b) reservoir levels change
// slowly enough that a per-demo refresh is plenty fresh.
//
// Drought bands are calibrated for SoCal storage reservoirs, where 100% is the
// gross pool. <50% means the operator has eaten most of the conservation pool —
// shorthand for "if a fire breaks out near here, the closest water source is
// already drawn down." 50-80% is the normal operating band by late summer.

const DATA_URL = '/data/reservoirs.json'
const EARTH_RADIUS_KM = 6371

let cachedPromise = null

function haversineKm(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(lat2 - lat1)
  const dLon = toRad(lon2 - lon1)
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(a))
}

// Module-level promise so React's StrictMode double-mount doesn't double-fetch
// and so every fire popup that opens during a session shares one network call.
export function loadReservoirs() {
  if (!cachedPromise) {
    cachedPromise = fetch(DATA_URL, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`reservoirs.json HTTP ${res.status}`)
        return res.json()
      })
      .then((payload) => payload.reservoirs || [])
      .catch((err) => {
        // Don't poison the cache — let the next caller retry the fetch.
        cachedPromise = null
        throw err
      })
  }
  return cachedPromise
}

// Bands are intentionally generous — the chip is a *cue* for the dispatcher,
// not a hydrology assessment. If a reservoir shows up as "severe" on a fire
// popup, that's a "go double-check the closest hydrant capacity" signal.
export function droughtSeverity(pct) {
  if (pct == null || Number.isNaN(pct)) return 'unknown'
  if (pct < 50) return 'severe'
  if (pct < 80) return 'moderate'
  return 'normal'
}

// `lat`/`lon` are the fire centroid. Returns `null` when the snapshot is empty
// (e.g., if every CDEC station was lagging when the fetcher ran). Callers
// should treat null as "no chip" rather than rendering an empty state.
export async function nearestReservoir(lat, lon) {
  if (lat == null || lon == null) return null
  const reservoirs = await loadReservoirs()
  if (!reservoirs.length) return null
  let best = null
  for (const r of reservoirs) {
    const distance_km = haversineKm(lat, lon, r.lat, r.lon)
    if (!best || distance_km < best.distance_km) {
      best = {
        name: r.name,
        station: r.station,
        pct_capacity: r.pct_capacity,
        storage_af: r.storage_af,
        gross_pool_af: r.gross_pool_af,
        distance_km,
        drought_severity: droughtSeverity(r.pct_capacity),
      }
    }
  }
  return best
}
