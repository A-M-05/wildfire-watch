// Evacuation route generator.
//
// For each active fire we pick a "safe destination" — a major California city
// far enough from the fire to plausibly be outside the threatened area — and
// query Mapbox Directions for a road-following route from the fire centroid.
// Routes are cached by fire_id so polling doesn't re-hit the API every 30s.
//
// This is a frontend stand-in for what the dispatcher service will eventually
// compute server-side once #9's enrichment + Location Service routing land.
// The data still flows through the same map layer, so swapping the source is
// a one-line change at fetch time.

import mapboxgl from 'mapbox-gl'

// Major California cities used as evacuation targets. Chosen for population
// density (real shelters cluster around them) and statewide coverage so any
// fire has a candidate within reasonable driving distance.
const SAFE_CITIES = [
  { name: 'Los Angeles', lon: -118.2437, lat: 34.0522 },
  { name: 'San Diego', lon: -117.1611, lat: 32.7157 },
  { name: 'San Francisco', lon: -122.4194, lat: 37.7749 },
  { name: 'Sacramento', lon: -121.4944, lat: 38.5816 },
  { name: 'San Jose', lon: -121.8863, lat: 37.3382 },
  { name: 'Fresno', lon: -119.7871, lat: 36.7378 },
  { name: 'Long Beach', lon: -118.1937, lat: 33.7701 },
  { name: 'Bakersfield', lon: -119.0187, lat: 35.3733 },
  { name: 'Oakland', lon: -122.2712, lat: 37.8044 },
  { name: 'Santa Barbara', lon: -119.6982, lat: 34.4208 },
  { name: 'Ventura', lon: -119.2945, lat: 34.2746 },
  { name: 'Palm Springs', lon: -116.5453, lat: 33.8303 },
  { name: 'San Luis Obispo', lon: -120.6596, lat: 35.2828 },
  { name: 'Redding', lon: -122.3917, lat: 40.5865 },
  { name: 'Eureka', lon: -124.1637, lat: 40.8021 },
]

const EARTH_RADIUS_KM = 6371

// Haversine — needed both for picking destinations and reporting fire→safe
// distance in the popup.
function haversineKm(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b.lat - a.lat)
  const dLon = toRad(b.lon - a.lon)
  const lat1 = toRad(a.lat)
  const lat2 = toRad(b.lat)
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h))
}

// Evacuation destination must be:
//   - outside the fire's alert radius (no point routing into the threat zone)
//   - at least 25 km away (closer than that and the route is just neighborhood
//     streets, not an evacuation corridor)
// Of the candidates that qualify, pick the closest — drives shorter than 50 km
// are operationally realistic and Mapbox returns them faster.
const MIN_EVAC_KM = 25

function pickDestination(fire) {
  const p = fire.properties || {}
  const center = p.centroid
  if (!center) return null
  const fireLatLon = { lon: center[0], lat: center[1] }
  const minSafeDist = Math.max(MIN_EVAC_KM, (p.alert_radius_km || 0) + 5)
  const candidates = SAFE_CITIES
    .map((c) => ({ ...c, dist: haversineKm(fireLatLon, c) }))
    .filter((c) => c.dist >= minSafeDist)
    .sort((a, b) => a.dist - b.dist)
  return candidates[0] || null
}

// Cache key — fire_id only. Live traffic conditions evolve, so entries expire
// after 5 minutes. That's frequent enough that durations stay believable but
// infrequent enough that we're not hammering the Directions API on every poll.
const routeCache = new Map()
const CACHE_TTL_MS = 5 * 60 * 1000

// Worst congestion level seen on the route. Mapbox returns one of:
//   "low" | "moderate" | "heavy" | "severe" | "unknown"
const CONGESTION_RANK = { unknown: 0, low: 1, moderate: 2, heavy: 3, severe: 4 }
function worstCongestion(legs) {
  let worst = 'low'
  for (const leg of legs || []) {
    for (const c of leg.annotation?.congestion || []) {
      if ((CONGESTION_RANK[c] ?? 0) > (CONGESTION_RANK[worst] ?? 0)) worst = c
    }
  }
  return worst
}

async function fetchOneRoute(fire) {
  if (!mapboxgl.accessToken) return null
  const fireId = fire.properties?.fire_id
  if (!fireId) return null
  const cached = routeCache.get(fireId)
  if (cached && Date.now() - cached._cachedAt < CACHE_TTL_MS) return cached

  const dest = pickDestination(fire)
  if (!dest) return null

  const [lon1, lat1] = fire.properties.centroid
  // driving-traffic profile uses Mapbox's live traffic data — the route
  // selection avoids current congestion and the returned duration reflects
  // real-world travel time, not free-flow.
  const url =
    `https://api.mapbox.com/directions/v5/mapbox/driving-traffic/` +
    `${lon1},${lat1};${dest.lon},${dest.lat}` +
    `?geometries=geojson&overview=full&annotations=duration,congestion` +
    `&access_token=${mapboxgl.accessToken}`

  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    const route = data.routes?.[0]
    if (!route) return null
    const result = {
      type: 'Feature',
      geometry: route.geometry,
      _cachedAt: Date.now(),
      properties: {
        fire_id: fireId,
        fire_name: fire.properties.name,
        destination_name: dest.name,
        // Mapbox returns metres + seconds. duration_typical_min would be the
        // free-flow time; duration_min reflects current traffic.
        distance_km: route.distance / 1000,
        duration_min: route.duration / 60,
        traffic_severity: worstCongestion(route.legs),
        destination_lon: dest.lon,
        destination_lat: dest.lat,
      },
    }
    routeCache.set(fireId, result)
    return result
  } catch (e) {
    // Routing failures shouldn't break the rest of the map. Return null without
    // caching so the next 30s refresh retries — Mapbox blips usually clear fast
    // and we'd rather try again than hold a stale failure for the cache TTL.
    console.warn(`evac route fetch failed for ${fireId}:`, e.message)
    return null
  }
}

// Returns one FeatureCollection of route LineStrings + a parallel collection
// of destination markers. Concurrent fetch with Promise.all — Mapbox's free
// tier comfortably handles a few dozen parallel directions queries.
export async function buildEvacRoutes(fireCollection) {
  const fires = fireCollection?.features || []
  const results = await Promise.all(fires.map(fetchOneRoute))
  const routes = results.filter(Boolean)
  const destinations = routes.map((r) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [r.properties.destination_lon, r.properties.destination_lat] },
    properties: {
      name: r.properties.destination_name,
      fire_name: r.properties.fire_name,
      distance_km: r.properties.distance_km,
      duration_min: r.properties.duration_min,
    },
  }))
  return {
    routes: { type: 'FeatureCollection', features: routes },
    destinations: { type: 'FeatureCollection', features: destinations },
  }
}

// Returns route metadata for a given fire_id (for popups). Reads from the
// same cache populated by buildEvacRoutes — no extra network call.
export function routeSummaryForFire(fireId) {
  const r = routeCache.get(fireId)
  if (!r) return null
  return {
    destination: r.properties.destination_name,
    destination_lat: r.properties.destination_lat,
    destination_lon: r.properties.destination_lon,
    distance_km: r.properties.distance_km,
    duration_min: r.properties.duration_min,
    traffic_severity: r.properties.traffic_severity,
  }
}
