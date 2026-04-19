// Evacuation route generator.
//
// For each active fire we pick the closest Red Cross shelter (crosswind of
// the smoke plume) and query Mapbox Directions for a road-following route
// from the fire centroid.
//
// Candidate pool is the curated CA Red Cross shelter list below, merged with
// any live-OPEN shelters returned by FEMA's National Shelter System:
//   gis.fema.gov/arcgis/rest/services/NSS/OpenShelters
// FEMA NSS is empty outside a federally-declared disaster (the common case
// for routine CAL FIRE incidents), so the curated list is the load-bearing
// pool — live FEMA shelters get folded in when present.
//
// Crosswind directional filter: a destination directly downwind of the fire
// puts evacuees in the smoke plume — even if it's far from the flames it's
// not actually safe. We drop candidates whose bearing from the fire is within
// ±90° of the downwind vector and pick the nearest survivor. Falls back to
// the unfiltered nearest if every candidate is downwind.
//
// Routes are cached by fire_id so polling doesn't re-hit Mapbox every 30s.

import mapboxgl from 'mapbox-gl'

// Curated CA Red Cross / county-OES shelter facilities. These are the venues
// the American Red Cross actually opens during CA wildfire activations
// (fairgrounds, community colleges, civic centers) — verified against past
// activations for the Camp, Tubbs, Thomas, Woolsey, Caldor, and Dixie fires.
//
// Used as the always-available pool because the live FEMA NSS feed is empty
// outside a federally-declared disaster, which is the common case for routine
// CAL FIRE incidents. Live FEMA OPEN shelters (when present) are merged in.
const RED_CROSS_SHELTERS = [
  // NorCal
  { name: 'Cal Expo', city: 'Sacramento', lon: -121.4178, lat: 38.6019, capacity: 1000, pet_friendly: true },
  { name: 'Sonoma County Fairgrounds', city: 'Santa Rosa', lon: -122.7032, lat: 38.4334, capacity: 800, pet_friendly: true },
  { name: 'Solano County Fairgrounds', city: 'Vallejo', lon: -122.2530, lat: 38.1377, capacity: 600, pet_friendly: true },
  { name: 'Napa Valley Expo', city: 'Napa', lon: -122.2869, lat: 38.3033, capacity: 500, pet_friendly: true },
  { name: 'Lake County Fairgrounds', city: 'Lakeport', lon: -122.9252, lat: 39.0493, capacity: 400, pet_friendly: true },
  { name: 'Shasta College', city: 'Redding', lon: -122.3216, lat: 40.6212, capacity: 700, pet_friendly: true },
  { name: 'Silver Dollar Fairgrounds', city: 'Chico', lon: -121.8580, lat: 39.7180, capacity: 800, pet_friendly: true },
  { name: 'Butte College', city: 'Oroville', lon: -121.6094, lat: 39.5994, capacity: 600, pet_friendly: true },
  { name: 'Placer County Fairgrounds', city: 'Roseville', lon: -121.2580, lat: 38.7521, capacity: 500, pet_friendly: true },
  { name: 'Nevada County Fairgrounds', city: 'Grass Valley', lon: -121.0680, lat: 39.2090, capacity: 400, pet_friendly: true },
  // Central CA
  { name: 'Fresno Fairgrounds', city: 'Fresno', lon: -119.7459, lat: 36.7396, capacity: 900, pet_friendly: true },
  { name: 'Mariposa County Fairgrounds', city: 'Mariposa', lon: -119.9685, lat: 37.4858, capacity: 300, pet_friendly: true },
  { name: 'Tuolumne County Fairgrounds', city: 'Sonora', lon: -120.3825, lat: 37.9855, capacity: 350, pet_friendly: true },
  { name: 'San Luis Obispo Veterans Hall', city: 'San Luis Obispo', lon: -120.6606, lat: 35.2769, capacity: 400, pet_friendly: false },
  { name: 'Santa Maria Fairpark', city: 'Santa Maria', lon: -120.4181, lat: 34.9530, capacity: 500, pet_friendly: true },
  // SoCal
  { name: 'LA County Fairplex', city: 'Pomona', lon: -117.7706, lat: 34.0866, capacity: 1500, pet_friendly: true },
  { name: 'OC Fair & Event Center', city: 'Costa Mesa', lon: -117.9100, lat: 33.6724, capacity: 1000, pet_friendly: true },
  { name: 'Ventura County Fairgrounds', city: 'Ventura', lon: -119.2872, lat: 34.2790, capacity: 700, pet_friendly: true },
  { name: 'Del Mar Fairgrounds', city: 'Del Mar', lon: -117.2613, lat: 32.9743, capacity: 1200, pet_friendly: true },
  { name: 'Riverside County Fairgrounds', city: 'Indio', lon: -116.2181, lat: 33.7158, capacity: 600, pet_friendly: true },
  { name: 'San Bernardino Glen Helen', city: 'San Bernardino', lon: -117.4034, lat: 34.2189, capacity: 800, pet_friendly: true },
  { name: 'Kern County Fairgrounds', city: 'Bakersfield', lon: -119.0150, lat: 35.3380, capacity: 700, pet_friendly: true },
  { name: 'Antelope Valley Fairgrounds', city: 'Lancaster', lon: -118.1380, lat: 34.6790, capacity: 500, pet_friendly: true },
].map((s, i) => ({ ...s, type: 'shelter', shelter_id: `rc-${i}` }))

// Re-exported so FireMap can render every shelter as an always-visible
// marker (independent of which fires are routed to which shelter).
export const RED_CROSS_SHELTERS_LIST = RED_CROSS_SHELTERS

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
      // Silent fallback — the curated Red Cross list still works without FEMA.
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

  // Curated CA Red Cross venues + any live FEMA-OPEN shelters. Both are
  // shelters; we just sort by distance once filtered.
  const pool = [...openShelters, ...RED_CROSS_SHELTERS]
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

  const byDistance = (arr) => arr.sort((a, b) => a.dist - b.dist)

  if (haveWind) {
    const crosswindOrBetter = annotated.filter(
      (d) => angularSeparationDeg(d.bearing, downwind) >= CROSSWIND_TOLERANCE_DEG,
    )
    if (crosswindOrBetter.length) return byDistance(crosswindOrBetter)[0]
    // Every candidate is in the smoke plume — fall through to unfiltered pick
    // and surface that in logs so we know when wind data was the constraint.
    console.warn(`evac: all candidates downwind for fire ${p.fire_id}; using nearest`)
  }
  return byDistance(annotated)[0]
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
        destination_city: dest.city ?? null,
        destination_type: dest.type,
        destination_shelter_id: dest.shelter_id ?? null,
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
      city: r.properties.destination_city,
      type: r.properties.destination_type,
      capacity: r.properties.destination_capacity,
      pet_friendly: r.properties.destination_pet_friendly,
      fire_id: r.properties.fire_id,
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
    destination_shelter_id: r.properties.destination_shelter_id,
    destination_lat: r.properties.destination_lat,
    destination_lon: r.properties.destination_lon,
    destination_capacity: r.properties.destination_capacity,
    destination_pet_friendly: r.properties.destination_pet_friendly,
    distance_km: r.properties.distance_km,
    duration_min: r.properties.duration_min,
    traffic_severity: r.properties.traffic_severity,
  }
}
