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
const CIRCLE_VERTICES = 4
const MIN_RADIUS_KM = 3      // floor so small fires are visible at statewide zoom
const VISUAL_SCALE = 4       // exaggerate footprint for demo legibility — a real
                             // 100-acre fire is ~300m across, invisible at zoom 6

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

function normalizeCalFireFeature(f) {
  const p = f.properties || {}
  const acres = p.AcresBurned ?? 0
  const geometry =
    f.geometry?.type === 'Point'
      ? circlePolygon(f.geometry.coordinates, acresToRadiusKm(acres))
      : f.geometry
  return {
    type: 'Feature',
    geometry,
    properties: {
      fire_id: p.UniqueId,
      name: (p.Name || '').trim(),
      containment_pct: p.PercentContained ?? 0,
      acres_burned: p.AcresBurned ?? 0,
      county: p.County,
      location: p.Location,
      detected_at: p.Started,
      last_updated: p.Updated,
      url: p.Url,
      // spread_rate isn't in the CAL FIRE feed — leave undefined; popup tolerates it
    },
  }
}

async function fetchMock() {
  const res = await fetch(MOCK_URL, { cache: 'no-store' })
  if (!res.ok) throw new Error(`failed to fetch mock fires (${res.status})`)
  return res.json()
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
