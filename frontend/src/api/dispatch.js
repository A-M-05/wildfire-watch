// Dispatch + advisory data — STUBBED.
//
// In the shipped pipeline this comes from DynamoDB (resources table written by
// #22 alert sender) and the safety-gated Bedrock advisory (#21). Until #30
// wires the API Gateway + WebSocket, the panel reads from this generator,
// which produces deterministic, fire-specific stubs from the live fire props
// and the static fire-stations GeoJSON.

const STATIONS_URL = '/data/fire_stations.geojson'
const EARTH_RADIUS_KM = 6371

let stationsCache = null
async function loadStations() {
  if (stationsCache) return stationsCache
  const res = await fetch(STATIONS_URL, { cache: 'force-cache' })
  if (!res.ok) throw new Error(`stations fetch ${res.status}`)
  stationsCache = await res.json()
  return stationsCache
}

function haversineKm(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b[1] - a[1])
  const dLon = toRad(b[0] - a[0])
  const lat1 = toRad(a[1])
  const lat2 = toRad(b[1])
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h))
}

// Deterministic hash → 0..1, so the same fire always produces the same stubs.
// Avoids dispatched units flickering between renders.
function hash01(str) {
  let h = 2166136261
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619)
  return ((h >>> 0) % 10000) / 10000
}

// Confidence is weighted by containment (a contained fire is a clearer call
// for the model) plus a small per-fire jitter. The 0.65 threshold is the
// CLAUDE.md safety contract — anything below routes to human review.
function fakeConfidence(fire) {
  const p = fire.properties || {}
  const containmentBoost = Math.min(1, (p.containment_pct ?? 0) / 100) * 0.2
  const jitter = (hash01(p.fire_id || p.name || '') - 0.5) * 0.25
  return Math.max(0.3, Math.min(0.98, 0.72 + containmentBoost + jitter))
}

// Templated advisory — three tone tiers so the panel reflects the fire's
// severity. Real advisories will come from the Bedrock prompt (#14) post-
// Guardrails (#16) post-safety-gate (#21).
function fakeAdvisory(fire) {
  const p = fire.properties || {}
  const acres = p.acres_burned ?? 0
  const contained = p.containment_pct ?? 0
  const name = p.name || 'this incident'
  if (contained >= 100) {
    return (
      `${name} is fully contained. No active evacuation in effect; mop-up crews ` +
      `remain on scene through the cold-trail check, then standby resources are ` +
      `released. Air monitoring continues for 24h.`
    )
  }
  if (contained >= 70) {
    return (
      `Mop-up phase recommended for ${name}. Maintain perimeter watch with current ` +
      `engines; release surge units back to home stations. Continue air monitoring ` +
      `for 24h post-containment.`
    )
  }
  if (acres > 10000 || contained < 20) {
    return (
      `EXTREME risk profile for ${name}. Recommend full strike-team mobilization, ` +
      `pre-position air tankers at the nearest ANG base, and stage shelters in the ` +
      `downwind alert zone. Coordinate with CHP for highway closures along the evac ` +
      `corridor.`
    )
  }
  return (
    `Active suppression for ${name}. Dispatch nearest engine companies and one ` +
    `Type 1 hand crew. Stage second-wave resources within the alert radius and ` +
    `prepare evacuation messaging for residents in the threatened zone.`
  )
}

// Picks 2-4 nearest stations and assigns deterministic unit counts + ETAs.
// Status mirrors what the alert sender (#22) would post back to DynamoDB.
async function fakeDispatchedUnits(fire) {
  const p = fire.properties || {}
  const center = p.centroid
  if (!center) return []
  const data = await loadStations()
  const candidates = (data.features || [])
    .map((s) => ({
      station_id: s.properties.station_id,
      name: s.properties.name,
      county: s.properties.county,
      available: s.properties.available !== false,
      distance_km: haversineKm(center, s.geometry.coordinates),
    }))
    .sort((a, b) => a.distance_km - b.distance_km)

  // Larger fires pull more stations.
  const acres = p.acres_burned ?? 0
  const stationCount = acres > 20000 ? 4 : acres > 5000 ? 3 : 2
  return candidates.slice(0, stationCount).map((s, i) => {
    const seed = hash01(`${p.fire_id}-${s.station_id}`)
    // Average emergency response speed in CA backcountry: ~60 km/h.
    const etaMin = Math.round((s.distance_km / 60) * 60 + 4 + seed * 6)
    const units = Math.max(1, Math.round(2 + seed * 3))
    // Closest unit is en-route; subsequent waves staged.
    const status = i === 0 ? 'en route' : i === 1 ? 'dispatched' : 'staged'
    return {
      station_id: s.station_id,
      name: s.name,
      county: s.county,
      distance_km: s.distance_km,
      units,
      eta_min: etaMin,
      status,
      available: s.available,
    }
  })
}

// Audit hash chain — each alert gets a deterministic SHA-like fingerprint so
// residents can see proof the safety gate ran. Real chain is written by the
// safety-gate Lambda (#21) into the wildfire-watch-audit DynamoDB table; this
// stub just renders something hash-looking from the fire_id.
function fakeAuditHash(fire) {
  const seed = (fire.properties?.fire_id || fire.properties?.name || 'unknown') + '|audit'
  let h = 2166136261
  const out = []
  for (let i = 0; i < 8; i++) {
    for (let j = 0; j < seed.length; j++) h = Math.imul(h ^ (seed.charCodeAt(j) + i), 16777619)
    out.push((h >>> 0).toString(16).padStart(8, '0'))
  }
  return '0x' + out.join('').slice(0, 40)
}

// Mock alert-sent timestamp — stable per fire so the UI doesn't flicker.
// Real timestamp comes from the alert sender (#22) when sns.publish returns.
function fakeAlertSentAt(fire) {
  const seed = hash01(fire.properties?.fire_id || '')
  // Push the timestamp 2-30 minutes into the past so "X min ago" reads well.
  const minutesAgo = Math.round(2 + seed * 28)
  return new Date(Date.now() - minutesAgo * 60_000).toISOString()
}

// Public entry — returns everything both panel views need for a given fire.
// One async call so the panel can show a single loading state.
export async function fetchDispatchData(fire) {
  if (!fire) return null
  const units = await fakeDispatchedUnits(fire)
  const population = fire.properties?.population_at_risk ?? 0
  return {
    fire_id: fire.properties.fire_id,
    confidence: fakeConfidence(fire),
    advisory: fakeAdvisory(fire),
    dispatched_units: units,
    // Resident-facing fields. Alerts go to ~85-95% of at-risk residents
    // (the rest haven't registered for SMS yet).
    alerts_sent: Math.round(population * (0.85 + hash01(fire.properties.fire_id || '') * 0.1)),
    alert_sent_at: fakeAlertSentAt(fire),
    audit_hash: fakeAuditHash(fire),
    generated_at: new Date().toISOString(),
  }
}
