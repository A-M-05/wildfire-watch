// Evacuation route generator.
//
// For each active fire we pick a "safe destination" and query Mapbox
// Directions for a road-following route from the fire centroid. Two upgrades
// vs. the original "nearest big city" logic:
//
//   1. Live shelter feed: pull currently-OPEN shelters from FEMA's National
//      Shelter System (synced nightly with the American Red Cross DB). Adds
//      real activated shelters to the candidate pool when a disaster is live;
//      silently no-ops when nothing is open (the common case for CA).
//      Endpoint: gis.fema.gov/arcgis/rest/services/NSS/OpenShelters
//
//   2. Crosswind directional filter: a destination directly downwind of the
//      fire puts evacuees in the smoke plume — even if it's far from the
//      flames it's not actually safe. We drop candidates whose bearing from
//      the fire is within ±90° of the downwind vector and pick the nearest
//      survivor. Falls back to the unfiltered nearest if every candidate is
//      downwind (rare, only happens for very narrow candidate pools).
//
// Routes are cached by fire_id so polling doesn't re-hit Mapbox every 30s.

import mapboxgl from 'mapbox-gl'

// Always-present base destinations — major California metros with shelter
// infrastructure. Used when the live shelter feed has no openings (typical
// state outside an active disaster) or when crosswind filtering eliminates
// all live shelters.
const SAFE_DESTINATIONS = [
  { name: 'Los Angeles', lon: -118.2437, lat: 34.0522, type: 'metro' },
  { name: 'San Diego', lon: -117.1611, lat: 32.7157, type: 'metro' },
  { name: 'San Francisco', lon: -122.4194, lat: 37.7749, type: 'metro' },
  { name: 'Sacramento', lon: -121.4944, lat: 38.5816, type: 'metro' },
  { name: 'San Jose', lon: -121.8863, lat: 37.3382, type: 'metro' },
  { name: 'Fresno', lon: -119.7871, lat: 36.7378, type: 'metro' },
  { name: 'Long Beach', lon: -118.1937, lat: 33.7701, type: 'metro' },
  { name: 'Bakersfield', lon: -119.0187, lat: 35.3733, type: 'metro' },
  { name: 'Oakland', lon: -122.2712, lat: 37.8044, type: 'metro' },
  { name: 'Santa Barbara', lon: -119.6982, lat: 34.4208, type: 'metro' },
  { name: 'Ventura', lon: -119.2945, lat: 34.2746, type: 'metro' },
  { name: 'Palm Springs', lon: -116.5453, lat: 33.8303, type: 'metro' },
  { name: 'San Luis Obispo', lon: -120.6596, lat: 35.2828, type: 'metro' },
  { name: 'Redding', lon: -122.3917, lat: 40.5865, type: 'metro' },
  { name: 'Eureka', lon: -124.1637, lat: 40.8021, type: 'metro' },
]

const EARTH_RADIUS_KM = 6371

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

// Initial bearing from a→b in degrees clockwise from north (compass convention).
function bearingDeg(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const φ1 = toRad(a.lat)
  const φ2 = toRad(b.lat)
  const Δλ = toRad(b.lon - a.lon)
  const y = Math.sin(Δλ) * Math.cos(φ2)
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ)
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360
}

// Smallest angular separation between two bearings, in [0, 180].
function angularSeparationDeg(a, b) {
  const d = Math.abs(a - b) % 360
  return d > 180 ? 360 - d : d
}

// ---------------------------------------------------------------------------
// FEMA National Shelter System — live OPEN shelters
// ---------------------------------------------------------------------------
//
// The endpoint mirrors the American Red Cross shelter DB nightly with 20-min
// updates throughout the day. Schema returns Point features per shelter with
// status, capacity, and ADA/pet flags we surface in the popup.
const FEMA_SHELTERS_URL =
  'https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/MapServer/0/query' +
  '?where=' + encodeURIComponent("state='CA' AND shelter_status='OPEN'") +
  '&outFields=' + encodeURIComponent(
    'shelter_name,city,evacuation_capacity,total_population,pet_accommodations_code,wheelchair_accessible',
  ) +
  '&f=geojson'

const SHELTER_TTL_MS = 10 * 60 * 1000   // 10-min refresh — matches FEMA's update cadence
let _shelterCache = { features: [], fetchedAt: 0 }
let _shelterInflight = null

async function loadOpenShelters() {
  if (Date.now() - _shelterCache.fetchedAt < SHELTER_TTL_MS) return _shelterCache.features
  if (_shelterInflight) return _shelterInflight
  _shelterInflight = (async () => {
    try {
      const res = await fetch(FEMA_SHELTERS_URL, { cache: 'no-store' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const features = (data.features || [])
        .map((f) => {
          const [lon, lat] = f.geometry?.coordinates || []
          const p = f.properties || {}
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
          return {
            name: p.shelter_name || 'Open shelter',
            city: p.city,
            lon,
            lat,
            type: 'shelter',
            capacity: p.evacuation_capacity,
            current_population: p.total_population,
            pet_friendly: p.pet_accommodations_code && p.pet_accommodations_code !== 'NONE',
            wheelchair_accessible: p.wheelchair_accessible === 'YES',
          }
        })
        .filter(Boolean)
      _shelterCache = { features, fetchedAt: Date.now() }
      return features
    } catch (e) {
      // Silent fallback — the metro-area list still works without live shelters.
      console.warn('FEMA shelter fetch failed:', e.message)
      _shelterCache = { features: [], fetchedAt: Date.now() }
      return []
    } finally {
      _shelterInflight = null
    }
  })()
  return _shelterInflight
}

// ---------------------------------------------------------------------------
// Destination selection
// ---------------------------------------------------------------------------

// A destination must be:
//   - outside the fire's alert radius (no point routing into the threat zone)
//   - at least MIN_EVAC_KM away (closer than that and the route is just
//     neighborhood streets, not an evacuation corridor)
//   - >CROSSWIND_TOLERANCE_DEG off the downwind vector (smoke plume safety)
const MIN_EVAC_KM = 25
// Drop candidates whose bearing from the fire is within ±90° of downwind.
// A wider tolerance (e.g. 120°) is safer but eliminates more candidates and
// can leave us with only very-far destinations; 90° is the conventional
// crosswind-or-better threshold used in wildfire evac guidance.
const CROSSWIND_TOLERANCE_DEG = 90

function pickDestination(fire, openShelters) {
  const p = fire.properties || {}
  const center = p.centroid
  if (!center) return null
  const fireLatLon = { lon: center[0], lat: center[1] }
  const minSafeDist = Math.max(MIN_EVAC_KM, (p.alert_radius_km || 0) + 5)

  // Live shelters get priority — they're real beds with real capacity. Metros
  // are the always-available fallback. We score both pools the same way once
  // they're merged so a closer metro can still beat a farther shelter.
  const pool = [...openShelters, ...SAFE_DESTINATIONS]
  const annotated = pool
    .map((d) => ({
      ...d,
      dist: haversineKm(fireLatLon, d),
      bearing: bearingDeg(fireLatLon, d),
    }))
    .filter((d) => d.dist >= minSafeDist)

  if (!annotated.length) return null

  // Wind direction is the bearing the wind is COMING FROM (NOAA convention).
  // Smoke plume travels in the opposite direction — that's the bearing we want
  // evacuees to avoid.
  const windFrom = Number(p.wind_direction_deg)
  const haveWind = Number.isFinite(windFrom)
  const downwind = haveWind ? (windFrom + 180) % 360 : null

  // Live shelters first when both pools have viable candidates — beats sending
  // people to a metro 100 km away when there's an open shelter at 40 km.
  const sortBySafetyThenDistance = (arr) =>
    arr.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'shelter' ? -1 : 1
      return a.dist - b.dist
    })

  if (haveWind) {
    const crosswindOrBetter = annotated.filter(
      (d) => angularSeparationDeg(d.bearing, downwind) >= CROSSWIND_TOLERANCE_DEG,
    )
    if (crosswindOrBetter.length) return sortBySafetyThenDistance(crosswindOrBetter)[0]
    // Every candidate is in the smoke plume — fall through to unfiltered pick
    // and surface that in logs so we know when wind data was the constraint.
    console.warn(`evac: all candidates downwind for fire ${p.fire_id}; using nearest`)
  }
  return sortBySafetyThenDistance(annotated)[0]
}

// ---------------------------------------------------------------------------
// Mapbox routing
// ---------------------------------------------------------------------------

const routeCache = new Map()
const CACHE_TTL_MS = 5 * 60 * 1000

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

async function fetchOneRoute(fire, openShelters) {
  if (!mapboxgl.accessToken) return null
  const fireId = fire.properties?.fire_id
  if (!fireId) return null
  const cached = routeCache.get(fireId)
  if (cached && Date.now() - cached._cachedAt < CACHE_TTL_MS) return cached

  // Fully-contained fires don't need an evac route — drawing one would imply
  // an active threat that no longer exists. Cache a contained sentinel so
  // routeSummaryForFire can surface a status badge instead of the route line.
  const containment = Number(fire.properties?.containment_pct ?? 0)
  if (containment >= 100) {
    routeCache.set(fireId, { _cachedAt: Date.now(), _contained: true })
    return null
  }

  const dest = pickDestination(fire, openShelters)
  if (!dest) return null

  const [lon1, lat1] = fire.properties.centroid
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
        destination_type: dest.type,
        destination_capacity: dest.capacity ?? null,
        destination_pet_friendly: dest.pet_friendly ?? null,
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
    console.warn(`evac route fetch failed for ${fireId}:`, e.message)
    return null
  }
}

// Returns one FeatureCollection of route LineStrings + a parallel collection
// of destination markers. Loads the live shelter feed once per call (cached
// internally) and passes it to every per-fire route picker.
export async function buildEvacRoutes(fireCollection) {
  const fires = fireCollection?.features || []
  const openShelters = await loadOpenShelters()
  const results = await Promise.all(fires.map((f) => fetchOneRoute(f, openShelters)))
  const routes = results.filter(Boolean)
  const destinations = routes.map((r) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [r.properties.destination_lon, r.properties.destination_lat] },
    properties: {
      name: r.properties.destination_name,
      type: r.properties.destination_type,
      capacity: r.properties.destination_capacity,
      pet_friendly: r.properties.destination_pet_friendly,
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

export function routeSummaryForFire(fireId) {
  const r = routeCache.get(fireId)
  if (!r) return null
  if (r._contained) return { contained: true }
  return {
    destination: r.properties.destination_name,
    destination_type: r.properties.destination_type,
    destination_lat: r.properties.destination_lat,
    destination_lon: r.properties.destination_lon,
    destination_capacity: r.properties.destination_capacity,
    destination_pet_friendly: r.properties.destination_pet_friendly,
    distance_km: r.properties.distance_km,
    duration_min: r.properties.duration_min,
    traffic_severity: r.properties.traffic_severity,
  }
}
