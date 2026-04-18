// Resident registration — STUBBED.
//
// In production this calls POST /residents/register through API Gateway
// authenticated by Cognito (functions/alert/register.py is the backend).
// Until #30 wires API Gateway and Cognito, the form posts to this stub
// which validates the same way the Lambda does and pretends to succeed.

import mapboxgl from 'mapbox-gl'

const E164_RE = /^\+[1-9]\d{1,14}$/

// Mirrors functions/alert/register.py validation so the form catches the
// same bad input the backend would reject — no point shipping a request
// the API will 400 anyway.
export function validateRegistration({ phone, address, lat, lon, alert_radius_km }) {
  const errors = {}
  if (!phone) errors.phone = 'Phone number is required.'
  else if (!E164_RE.test(phone)) errors.phone = 'Use E.164 format (e.g. +15551234567).'

  const hasAddress = address && address.trim().length > 0
  const hasLatLon = lat != null && lon != null
  if (!hasAddress && !hasLatLon) errors.address = 'Address (or coordinates) is required.'

  if (alert_radius_km != null) {
    const r = Number(alert_radius_km)
    if (!Number.isFinite(r) || r <= 0) errors.alert_radius_km = 'Radius must be a positive number.'
    else if (r > 100) errors.alert_radius_km = 'Radius must be ≤ 100 km.'
  }
  return errors
}

// Mapbox geocoding — converts a free-form address to lat/lon client-side
// so we skip the server's Location Service round-trip. California-biased
// (proximity hint at the state center) since this is a CA-only demo.
export async function geocodeAddress(address) {
  if (!mapboxgl.accessToken) throw new Error('Mapbox token not configured')
  const url =
    `https://api.mapbox.com/geocoding/v5/mapbox.places/` +
    `${encodeURIComponent(address)}.json` +
    `?access_token=${mapboxgl.accessToken}` +
    `&country=us&proximity=-119.5,37.0&limit=1`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`geocode failed (${res.status})`)
  const data = await res.json()
  const f = data.features?.[0]
  if (!f) throw new Error('No match for that address.')
  return {
    lat: f.center[1],
    lon: f.center[0],
    place_name: f.place_name,
  }
}

// Stub registration call. Returns the same shape register.py returns:
//   { resident_id, alert_radius_km, created_at }
// Real implementation: POST {API_GATEWAY_URL}/residents/register with the
// Cognito JWT in Authorization. Body = { phone, lat, lon, alert_radius_km }.
export async function registerResident(payload) {
  // Simulate latency so the loading state is visible — real API ~200-400ms.
  await new Promise((r) => setTimeout(r, 600))
  // Bare-bounds validation; the form already ran the full check.
  if (!payload?.phone || (payload.lat == null && !payload.address)) {
    throw new Error('Missing required fields.')
  }
  return {
    resident_id: `stub-${Math.random().toString(36).slice(2, 10)}`,
    alert_radius_km: payload.alert_radius_km ?? 10,
    created_at: new Date().toISOString(),
  }
}
