// Active fires data source.
//
// Two modes, controlled by VITE_USE_MOCK_FIRES:
//   "true"  → static demo GeoJSON in /public (curated polygons for demo runs)
//   "false" → live fetch from CAL FIRE's public incident API (current real fires)
//
// CAL FIRE returns Point geometries with AcresBurned. We synthesize a circular
// polygon sized to the burned area so fires scale visibly on the map — a real
// perimeter would come from #7's enrichment, but acres+centroid is what the
// public feed gives us today.
const ACRES_TO_KM2 = 0.00404686
const EARTH_RADIUS_KM = 6371
const CIRCLE_VERTICES = 48
const MIN_RADIUS_KM = 0.2    // tiny floor so 0-acre fires still render as a dot
const VISUAL_SCALE = 2       // mild 2x — preserves real ratios between fires
                             // while making small ones visible at zoom 6

function acresToRadiusKm(acres) {
  const km2 = Math.max(0, acres) * ACRES_TO_KM2
  const trueRadius = Math.sqrt(km2 / Math.PI)
  return Math.max(MIN_RADIUS_KM, trueRadius * VISUAL_SCALE)
}

// Approximate circle as a 48-sided polygon on the sphere (good enough at the
// scale of a fire perimeter; ignores ellipsoid).
function circlePolygon([lon, lat], radiusKm) {
  const ring = []
  const angularDist = radiusKm / EARTH_RADIUS_KM
  const latRad = (lat * Math.PI) / 180
  const lonRad = (lon * Math.PI) / 180
  for (let i = 0; i <= CIRCLE_VERTICES; i++) {
    const bearing = (i / CIRCLE_VERTICES) * 2 * Math.PI
    const lat2 = Math.asin(
      Math.sin(latRad) * Math.cos(angularDist) +
        Math.cos(latRad) * Math.sin(angularDist) * Math.cos(bearing),
    )
    const lon2 =
      lonRad +
      Math.atan2(
        Math.sin(bearing) * Math.sin(angularDist) * Math.cos(latRad),
        Math.cos(angularDist) - Math.sin(latRad) * Math.sin(lat2),
      )
    ring.push([(lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI])
  }
  return { type: 'Polygon', coordinates: [ring] }
}

const MOCK_URL = '/data/active_fires.geojson'
const CALFIRE_URL = '/api/calfire'   // proxied in vite.config.js to bypass CORS

const USE_MOCK = import.meta.env.VITE_USE_MOCK_FIRES === 'true'

// Population-at-risk stub — proportional to acres with a 250-person floor so
// even small fires register as a non-zero alert. Real numbers come from #9's
// US Census enrichment by alert-radius polygon.
function estimatePopulationAtRisk(acres) {
  return Math.max(250, Math.round((acres || 0) * 8))
}

// Alert radius scales sub-linearly with acreage so small and large incidents
// stay distinguishable: a 200-acre brushfire gets ~5 km, a 75 k-acre megafire
// gets ~45 km. 3 km floor keeps any active fire visible as an alert zone.
function alertRadiusKm(acres) {
  return 3 + Math.sqrt(Math.max(0, acres)) * 0.15
}

function normalizeCalFireFeature(f) {
  const p = f.properties || {}
  const acres = p.AcresBurned ?? 0
  const point = f.geometry?.type === 'Point' ? f.geometry.coordinates : null
  const geometry = point
    ? circlePolygon(point, acresToRadiusKm(acres))
    : f.geometry
  return {
    type: 'Feature',
    geometry,
    properties: {
      fire_id: p.UniqueId,
      name: (p.Name || '').trim(),
      containment_pct: p.PercentContained ?? 0,
      acres_burned: acres,
      county: p.County,
      location: p.Location,
      detected_at: p.Started,
      last_updated: p.Updated,
      url: p.Url,
      // Populated client-side as a stand-in for #9 enrichment + Location Service.
      alert_radius_km: alertRadiusKm(acres),
      population_at_risk: estimatePopulationAtRisk(acres),
      // Centroid used to draw the alert-zone polygon (see buildAlertZones).
      centroid: point,
      // spread_rate isn't in the CAL FIRE feed — leave undefined; popup tolerates it
    },
  }
}

// Polygon centroid via average of ring vertices (close enough for our convex
// synth circles and the small mock diamonds; not a true area-weighted centroid).
function polygonCentroid(geometry) {
  const ring = geometry?.coordinates?.[0]
  if (!ring?.length) return null
  let sx = 0, sy = 0
  // Last vertex of a closed ring duplicates the first — skip it.
  const n = ring.length - 1
  for (let i = 0; i < n; i++) {
    sx += ring[i][0]
    sy += ring[i][1]
  }
  return [sx / n, sy / n]
}

// Derives a FeatureCollection of alert-zone circles from the fire features.
// Kept as a separate source so the zone fill can sit under the fire footprint
// without confusing layer stacking or hover targets.
export function buildAlertZones(fireCollection) {
  const features = (fireCollection?.features || [])
    .map((f) => {
      const p = f.properties || {}
      const center = p.centroid || polygonCentroid(f.geometry)
      if (!center) return null
      const radius = p.alert_radius_km || alertRadiusKm(p.acres_burned)
      return {
        type: 'Feature',
        geometry: circlePolygon(center, radius),
        properties: {
          fire_id: p.fire_id,
          name: p.name,
          population_at_risk: p.population_at_risk ?? estimatePopulationAtRisk(p.acres_burned),
          alert_radius_km: radius,
        },
      }
    })
    .filter(Boolean)
  return { type: 'FeatureCollection', features }
}

// Mock fires are stored in our normalized schema as Points + acres_burned, then
// run through the same synthesis pipeline as live (so alert radius, centroid,
// stubs, and footprint all derive from the same code path).
function normalizeMockFeature(f) {
  const p = f.properties || {}
  const point = f.geometry?.type === 'Point' ? f.geometry.coordinates : null
  const acres = p.acres_burned ?? 0
  const geometry = point
    ? circlePolygon(point, acresToRadiusKm(acres))
    : f.geometry
  return {
    type: 'Feature',
    geometry,
    properties: {
      ...p,
      acres_burned: acres,
      alert_radius_km: alertRadiusKm(acres),
      population_at_risk: p.population_at_risk ?? estimatePopulationAtRisk(acres),
      centroid: point || polygonCentroid(f.geometry),
    },
  }
}

async function fetchMock() {
  const res = await fetch(MOCK_URL, { cache: 'no-store' })
  if (!res.ok) throw new Error(`failed to fetch mock fires (${res.status})`)
  const raw = await res.json()
  return {
    type: 'FeatureCollection',
    features: (raw.features || []).map(normalizeMockFeature),
  }
}

async function fetchLive() {
  const res = await fetch(CALFIRE_URL, { cache: 'no-store' })
  if (!res.ok) throw new Error(`failed to fetch CAL FIRE (${res.status})`)
  const raw = await res.json()
  const features = (raw.features || [])
    .filter((f) => f?.properties?.IsActive)
    .map(normalizeCalFireFeature)
  return { type: 'FeatureCollection', features }
}

export async function fetchActiveFires() {
  return USE_MOCK ? fetchMock() : fetchLive()
}
