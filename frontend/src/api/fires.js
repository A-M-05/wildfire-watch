// Active fires data source.
//
// Two modes, controlled by VITE_USE_MOCK_FIRES:
//   "true"  → static demo GeoJSON in /public (curated polygons for demo runs)
//   "false" → live fetch from the backend GET /fires endpoint, which returns
//             enriched, normalized fire records straight from DynamoDB. Risk
//             score, population at risk, alert radius, etc. all come from the
//             enrichment Lambda (#9) — no client-side estimation anymore.
//
// Records may carry a real perimeter polygon (`geometry.type === 'Polygon'`)
// or just a centroid (`geometry.type === 'Point'`) when the upstream source
// — typically CAL FIRE — only published acres + lat/lon. For the Point case
// we synthesize a small footprint so the fire stays visible on the map.

const ACRES_TO_KM2 = 0.00404686
const EARTH_RADIUS_KM = 6371
const CIRCLE_VERTICES = 4
const MIN_RADIUS_KM = 0.2    // tiny floor so 0-acre fires still render as a dot
const VISUAL_SCALE = 2       // mild 2x — preserves real ratios between fires
                             // while making small ones visible at zoom 6

function acresToRadiusKm(acres) {
  const km2 = Math.max(0, acres) * ACRES_TO_KM2
  const trueRadius = Math.sqrt(km2 / Math.PI)
  return Math.max(MIN_RADIUS_KM, trueRadius * VISUAL_SCALE)
}

// 4-sided diamond on the sphere — coarser than a true circle but reads as a
// stylized fire footprint at zoom 6 and renders cheaply across many fires.
// Bumping CIRCLE_VERTICES higher has caused render regressions at small radii;
// keep it at 4 unless you've verified the change end-to-end on the map.
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
const API_BASE = import.meta.env.VITE_API_URL || ''
const USE_MOCK = import.meta.env.VITE_USE_MOCK_FIRES === 'true'

// Normalize the API base so callers can pass with or without a trailing slash.
// CDK's RestApi.url output ends in /, manual entries usually don't.
function firesUrl() {
  if (!API_BASE) throw new Error('VITE_API_URL is not set')
  return `${API_BASE.replace(/\/$/, '')}/fires`
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

// Records arrive with the schema in CLAUDE.md. Geometry is either a real
// Polygon perimeter or a Point centroid; for Points we synthesize a footprint
// from acres so they render at the same scale as polygon fires.
function ensurePolygonGeometry(feature) {
  const p = feature.properties || {}
  const point = feature.geometry?.type === 'Point' ? feature.geometry.coordinates : null
  if (!point) {
    // Already a Polygon — keep it as-is and derive centroid for the alert zone.
    return {
      ...feature,
      properties: { ...p, centroid: p.centroid || polygonCentroid(feature.geometry) },
    }
  }
  return {
    ...feature,
    geometry: circlePolygon(point, acresToRadiusKm(p.acres_burned ?? 0)),
    properties: { ...p, centroid: point },
  }
}

// Derives a FeatureCollection of alert-zone circles from the fire features.
// Kept as a separate source so the zone fill can sit under the fire footprint
// without confusing layer stacking or hover targets.
export function buildAlertZones(fireCollection) {
  const features = (fireCollection?.features || [])
    .map((f) => {
      const p = f.properties || {}
      const center = p.centroid || polygonCentroid(f.geometry)
      const radius = p.alert_radius_km
      if (!center || !radius) return null
      return {
        type: 'Feature',
        geometry: circlePolygon(center, radius),
        properties: {
          fire_id: p.fire_id,
          name: p.name,
          population_at_risk: p.population_at_risk,
          alert_radius_km: radius,
        },
      }
    })
    .filter(Boolean)
  return { type: 'FeatureCollection', features }
}

async function fetchMock() {
  const res = await fetch(MOCK_URL, { cache: 'no-store' })
  if (!res.ok) throw new Error(`failed to fetch mock fires (${res.status})`)
  const raw = await res.json()
  return {
    type: 'FeatureCollection',
    features: (raw.features || []).map(ensurePolygonGeometry),
  }
}

async function fetchLive() {
  const res = await fetch(firesUrl(), { cache: 'no-store' })
  if (!res.ok) throw new Error(`failed to fetch /fires (${res.status})`)
  const raw = await res.json()
  return {
    type: 'FeatureCollection',
    features: (raw.features || []).map(ensurePolygonGeometry),
  }
}

export async function fetchActiveFires() {
  return USE_MOCK ? fetchMock() : fetchLive()
}
