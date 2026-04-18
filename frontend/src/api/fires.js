// Active fires data source.
//
// Two modes, controlled by VITE_USE_MOCK_FIRES:
//   "true"  → static demo GeoJSON in /public (curated polygons for demo runs)
//   "false" → live fetch from CAL FIRE's public incident API (current real fires)
//
// CAL FIRE returns Point geometries; the FireMap renders points + polygons
// with the same containment color scheme. Schema is normalized below to the
// shape our map components expect.
const MOCK_URL = '/data/active_fires.geojson'
const CALFIRE_URL = '/api/calfire'   // proxied in vite.config.js to bypass CORS

const USE_MOCK = import.meta.env.VITE_USE_MOCK_FIRES === 'true'

function normalizeCalFireFeature(f) {
  const p = f.properties || {}
  return {
    type: 'Feature',
    geometry: f.geometry,   // CAL FIRE returns Point — map renders as circle
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
