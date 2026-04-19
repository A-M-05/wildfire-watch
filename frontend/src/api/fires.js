// Active fires data source.
//
// Three modes, controlled by VITE_USE_MOCK_FIRES:
//   "true"   → static demo GeoJSON in /public only (fast local dev, no API).
//   "false"  → live GET /fires only (production / staging).
//   "hybrid" → live + mock merged. Live wins on fire_id collision. Used for
//              the demo: real CA fires are typically thin and undramatic, so
//              the curated mock fires backstop the visual story even when the
//              live feed is quiet. Live failures fall back to mock-only so a
//              flaky API doesn't blank the map.
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

// Predicted fire perimeter — JS port of enrich Lambda's `predicted_perimeter`
// (functions/enrich/handler.py, Anderson 1983 / Andrews 2018). Mock fires
// arrive without server-side enrichment, so we run the same ellipse math
// locally to keep their footprints visually consistent with live fires.
const ELLIPSE_VERTICES = 32
const ELLIPSE_T_MIN_HR = 0.5
const ELLIPSE_T_MAX_HR = 24.0
const DEG_LAT_KM = 111.0

function lengthToBreadth(windMph) {
  const u = Math.max(0, windMph)
  const lb = 0.936 * Math.exp(0.2566 * u) + 0.461 * Math.exp(-0.1548 * u) - 0.397
  return Math.max(1.0, Math.min(lb, 8.0))
}

function hoursSince(detectedAt) {
  const t = Date.parse(detectedAt)
  if (Number.isNaN(t)) return 1.0
  const age = (Date.now() - t) / 3600000
  return Math.max(ELLIPSE_T_MIN_HR, Math.min(age, ELLIPSE_T_MAX_HR))
}

// Mock spread is published as area-per-hour; the ellipse expects linear km/hr.
// For a roughly circular fire, dA/dt = 2πr · dr/dt → dr/dt ≈ sqrt(dA/dt / π).
function areaRateToLinearKmHr(areaKm2PerHr) {
  return Math.sqrt(Math.max(0, areaKm2PerHr) / Math.PI)
}

function predictedPerimeter(props) {
  const spread = areaRateToLinearKmHr(props.spread_rate_km2_per_hr || 0)
  if (spread <= 0) return null
  const lat = Number(props.lat)
  const lon = Number(props.lon)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null

  const windMs = Number(props.wind_speed_ms) || 0
  const windFromDeg = Number.isFinite(Number(props.wind_direction_deg))
    ? Number(props.wind_direction_deg) : 270
  const spreadBearingRad = ((windFromDeg + 180) % 360) * Math.PI / 180

  const windMph = windMs * 2.23694
  const lb = lengthToBreadth(windMph)
  const root = Math.sqrt(Math.max(lb * lb - 1.0, 0))
  const hb = (lb + root) / Math.max(lb - root, 1e-6)

  const t = hoursSince(props.detected_at)
  const headKm = spread * t
  const backKm = headKm / hb
  const aKm = (headKm + backKm) / 2.0
  const bKm = aKm / lb
  const centerOffsetKm = (headKm - backKm) / 2.0

  const degPerKmLat = 1.0 / DEG_LAT_KM
  const degPerKmLon = 1.0 / (DEG_LAT_KM * Math.max(Math.cos(lat * Math.PI / 180), 0.01))

  const cxKm = Math.sin(spreadBearingRad) * centerOffsetKm
  const cyKm = Math.cos(spreadBearingRad) * centerOffsetKm
  const centerLon = lon + cxKm * degPerKmLon
  const centerLat = lat + cyKm * degPerKmLat

  const cosB = Math.cos(spreadBearingRad)
  const sinB = Math.sin(spreadBearingRad)
  const ring = []
  for (let i = 0; i <= ELLIPSE_VERTICES; i++) {
    const theta = (2 * Math.PI * i) / ELLIPSE_VERTICES
    const localX = bKm * Math.cos(theta)
    const localY = aKm * Math.sin(theta)
    const eastKm = localX * cosB + localY * sinB
    const northKm = -localX * sinB + localY * cosB
    ring.push([centerLon + eastKm * degPerKmLon, centerLat + northKm * degPerKmLat])
  }
  return { type: 'Polygon', coordinates: [ring] }
}

const MOCK_URL = '/data/active_fires.geojson'
const API_BASE = import.meta.env.VITE_API_URL || ''
const MODE = import.meta.env.VITE_USE_MOCK_FIRES

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
  // Mock fires (and any unenriched live fire) skip the server ellipse path,
  // so synthesize one client-side when wind + spread are present. Falls back
  // to the acres-based diamond when the inputs aren't there.
  const ellipse = predictedPerimeter(p)
  if (ellipse) {
    return {
      ...feature,
      geometry: ellipse,
      properties: { ...p, centroid: point, perimeter_source: p.perimeter_source || 'predicted' },
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

// Live + mock merged. Live wins on fire_id collision so a real fire that
// happens to share an id with a mock entry isn't shadowed. Either side
// failing is non-fatal — we keep whatever we got so a flaky API or missing
// snapshot doesn't blank the map mid-demo.
async function fetchHybrid() {
  const empty = { type: 'FeatureCollection', features: [] }
  const [live, mock] = await Promise.all([
    fetchLive().catch((e) => { console.warn('live fires failed:', e); return empty }),
    fetchMock().catch((e) => { console.warn('mock fires failed:', e); return empty }),
  ])
  const seen = new Set()
  const features = []
  for (const f of live.features) {
    const id = f.properties?.fire_id
    if (id) seen.add(id)
    features.push(f)
  }
  for (const f of mock.features) {
    const id = f.properties?.fire_id
    if (id && seen.has(id)) continue
    features.push(f)
  }
  return { type: 'FeatureCollection', features }
}

export async function fetchActiveFires() {
  if (MODE === 'hybrid') return fetchHybrid()
  return MODE === 'true' ? fetchMock() : fetchLive()
}
