import { useEffect, useMemo, useState } from 'react'
import { fetchDispatchData } from './api/dispatch'
import { routeSummaryForFire } from './api/evacRoutes'
import { nearestReservoir } from './api/reservoirs'

// Builds a Google Maps directions deep link. Origin is set only when the user
// has granted geolocation; otherwise we omit it and Google Maps prompts the
// user for their starting point — better UX than guessing.
function googleMapsDirections({ origin, destLat, destLon }) {
  if (destLat == null || destLon == null) return null
  const params = new URLSearchParams({
    api: '1',
    destination: `${destLat},${destLon}`,
    travelmode: 'driving',
  })
  if (origin) params.set('origin', `${origin[1]},${origin[0]}`)
  return `https://www.google.com/maps/dir/?${params.toString()}`
}

const EARTH_RADIUS_KM = 6371

// Stored in module scope so the user only has to grant geolocation once per
// session; the second fire they click reuses the resolved coords.
let cachedPosition = null

function haversineKm(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b[1] - a[1])
  const dLon = toRad(b[0] - a[0])
  const lat1 = toRad(a[1])
  const lat2 = toRad(b[1])
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h))
}

function useUserLocation() {
  const [coords, setCoords] = useState(cachedPosition)
  const [status, setStatus] = useState(cachedPosition ? 'granted' : 'idle')
  const [error, setError] = useState(null)

  const request = () => {
    if (!('geolocation' in navigator)) {
      setStatus('unsupported')
      setError('Geolocation not supported on this device.')
      return
    }
    setStatus('requesting')
    setError(null)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const next = [pos.coords.longitude, pos.coords.latitude]
        cachedPosition = next
        setCoords(next)
        setStatus('granted')
      },
      (err) => {
        setStatus('denied')
        setError(err.message || 'Location request was denied.')
      },
      { timeout: 10_000, maximumAge: 5 * 60_000, enableHighAccuracy: false },
    )
  }

  return { coords, status, error, request }
}

// Resident-facing safety badge. Frames the CLAUDE.md confidence gate from the
// resident's perspective ("we don't bother you unless we're sure") rather than
// the dispatcher's ("Step Functions paused for review").
function safetyTone(score) {
  if (score < 0.65) {
    return {
      color: '#ff8800',
      label: 'NO ALERT SENT',
      reason: `AI confidence ${(score * 100).toFixed(0)}% is below our 65% safety threshold.`,
      action: 'No SMS goes out until a human reviews this fire — we don\'t want false alarms waking you up.',
    }
  }
  if (score < 0.85) {
    return {
      color: '#22aa66',
      label: 'ALERT SENT',
      reason: `Confidence ${(score * 100).toFixed(0)}%. Above the 65% gate, flagged for post-incident audit.`,
      action: 'SMS dispatched to residents in the alert zone. Recommend you act on the evac guidance below.',
    }
  }
  return {
    color: '#0e8a4e',
    label: 'ALERT SENT (HIGH CONFIDENCE)',
    reason: `Confidence ${(score * 100).toFixed(0)}%. Passed Bedrock Guardrails and the safety gate.`,
    action: 'SMS dispatched. This is a verified, high-confidence threat — please follow the evac guidance below.',
  }
}

function dispatcherTone(score) {
  if (score < 0.65) {
    return {
      color: '#ff3322',
      label: 'HUMAN REVIEW REQUIRED',
      reason: `Dispatch confidence ${(score * 100).toFixed(0)}% is below the 65% safety threshold.`,
      action: 'Step Functions has paused this advisory. No resources will mobilize until a dispatcher approves it.',
    }
  }
  if (score < 0.85) {
    return {
      color: '#ffaa00',
      label: 'AUTO-DISPATCH (FLAGGED)',
      reason: `Confidence ${(score * 100).toFixed(0)}% is above the 65% gate but below the 85% high-confidence band.`,
      action: 'Resources are being dispatched automatically. The advisory is flagged for post-incident audit review.',
    }
  }
  return {
    color: '#22cc44',
    label: 'AUTO-DISPATCH',
    reason: `High confidence (${(score * 100).toFixed(0)}%). Model output passed Bedrock Guardrails and the safety gate.`,
    action: 'Auto-dispatched and written to the immutable audit chain.',
  }
}

const STATUS_TONE = {
  'en route': '#22cc44',
  dispatched: '#ffaa00',
  staged: '#888',
}

// Theme tokens — keeps the rest of the file readable. Light values are the
// originals; dark values target the same role at the same contrast level
// against the dark basemap.
function tokens(theme) {
  const dark = theme === 'dark'
  return {
    panelBg: dark ? 'rgba(24, 24, 26, 0.96)' : 'rgba(255, 255, 255, 0.97)',
    panelShadow: dark ? '0 4px 16px rgba(0, 0, 0, 0.6)' : '0 4px 16px rgba(0, 0, 0, 0.25)',
    border: dark ? '#2a2a2d' : '#e5e5e5',
    borderSoft: dark ? '#222225' : '#f0f0f0',
    borderInset: dark ? '#1d1d20' : '#f5f5f5',
    textPrimary: dark ? '#f1f1f3' : '#111',
    textSecondary: dark ? '#bdbdc2' : '#555',
    textMuted: dark ? '#9a9aa0' : '#666',
    textDim: dark ? '#86868c' : '#888',
    textVeryDim: dark ? '#6c6c72' : '#aaa',
    eyebrow: dark ? '#cfcfd4' : '#444',
    tabBarBg: dark ? '#1f1f22' : '#fafafa',
    tabAccent: '#ff5533',
    gateNoteBg: dark ? '#1d1d20' : '#fafafa',
    evacBoxBg: dark ? '#1a2230' : '#f5f9ff',
    evacBoxBorder: dark ? '#2a3a55' : '#d6e6ff',
    inputBorder: dark ? '#3a3a3f' : '#d0d0d0',
    chipBg: dark ? 'rgba(28, 28, 30, 0.92)' : 'rgba(255, 255, 255, 0.92)',
    chipText: dark ? '#e0e0e4' : '#444',
    chipShadow: dark ? '0 2px 6px rgba(0, 0, 0, 0.5)' : '0 2px 6px rgba(0, 0, 0, 0.15)',
    auditGreen: dark ? '#3fcf83' : '#0e8a4e',
  }
}

function timeAgo(iso) {
  const min = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 60_000))
  if (min < 60) return `${min} min ago`
  const hr = Math.round(min / 60)
  return `${hr}h ago`
}

export default function DispatchPanel({ fire, onClose, theme = 'light' }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState('resident')
  const t = useMemo(() => tokens(theme), [theme])

  useEffect(() => {
    if (!fire) {
      setData(null)
      return
    }
    let cancelled = false
    setLoading(true)
    fetchDispatchData(fire)
      .then((d) => { if (!cancelled) setData(d) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [fire?.properties?.fire_id])

  // Less intrusive empty state — a small floating chip in the corner instead
  // of a full panel. Nudges the user without occupying a third of the map.
  if (!fire) {
    return (
      <div style={{
        position: 'absolute', top: 12, right: 12, zIndex: 5,
        background: t.chipBg, color: t.chipText,
        padding: '6px 12px', borderRadius: 999,
        fontSize: 12, fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        boxShadow: t.chipShadow,
        display: 'flex', alignItems: 'center', gap: 6,
        pointerEvents: 'none',
      }}>
        <PinIcon />
        <span>Click a fire for details</span>
      </div>
    )
  }

  const p = fire.properties

  return (
    <aside style={{
      position: 'absolute', top: 12, right: 12, bottom: 12, width: 340,
      background: t.panelBg, borderRadius: 6, boxShadow: t.panelShadow,
      overflowY: 'auto', zIndex: 5,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    }}>
      <header style={{
        padding: '14px 16px 12px', borderBottom: `1px solid ${t.border}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8,
      }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 11, color: t.eyebrow, letterSpacing: 1, fontWeight: 600 }}>
            {view === 'resident' ? 'COMMUNITY ALERT' : 'DISPATCH PANEL'}
          </div>
          <h2 style={{ margin: '4px 0 0', fontSize: 18, color: t.textPrimary }}>{p.name}</h2>
          {p.location && <div style={{ fontSize: 12, color: t.textSecondary }}>{p.location}</div>}
        </div>
        <button onClick={onClose} aria-label="Close panel" style={{
          background: 'transparent', border: 'none', fontSize: 24, lineHeight: 1,
          cursor: 'pointer', color: t.textMuted, padding: 0, width: 24, height: 24,
        }}>×</button>
      </header>

      <div style={{ display: 'flex', borderBottom: `1px solid ${t.border}`, background: t.tabBarBg }}>
        <Tab t={t} active={view === 'resident'} onClick={() => setView('resident')}>Resident</Tab>
        <Tab t={t} active={view === 'dispatcher'} onClick={() => setView('dispatcher')}>Dispatcher</Tab>
      </div>

      {loading && <div style={{ padding: '12px 16px', fontSize: 12, color: t.textDim }}>Loading…</div>}

      {view === 'resident' ? (
        <ResidentView fire={fire} data={data} t={t} />
      ) : (
        <DispatcherView fire={fire} data={data} t={t} />
      )}

    </aside>
  )
}

function ResidentView({ fire, data, t }) {
  const p = fire.properties
  // Real model confidence from #105 wins; dispatch stub is the fallback.
  const confidence = p.confidence ?? data?.confidence ?? null
  const tone = confidence != null ? safetyTone(confidence) : null
  const { coords, status, error, request } = useUserLocation()

  const distance = coords && p.centroid ? haversineKm(coords, p.centroid) : null
  const inAlertZone = distance != null && distance <= (p.alert_radius_km || 0)
  const liveRoute = routeSummaryForFire(p.fire_id)
  const mapsUrl = liveRoute
    ? googleMapsDirections({
        origin: coords,
        destLat: liveRoute.destination_lat,
        destLon: liveRoute.destination_lon,
      })
    : null

  return (
    <>
      {inAlertZone && (
        <div style={{
          background: '#ff3322', color: '#fff',
          padding: '10px 16px', fontSize: 13, fontWeight: 600,
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <span aria-hidden="true">⚠</span>
          You're inside this fire's alert zone — follow the evac route below.
        </div>
      )}
      <Section t={t} title="Are you in danger?">
        {distance != null ? (
          <KV t={t} k="Distance to fire" v={`${distance.toFixed(1)} km`} />
        ) : (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 13, padding: '3px 0' }}>
            <span style={{ color: t.textMuted }}>Distance to fire</span>
            <button
              onClick={request}
              disabled={status === 'requesting'}
              style={{
                background: 'transparent', border: `1px solid ${t.inputBorder}`,
                color: t.textPrimary, fontSize: 12, padding: '3px 8px',
                borderRadius: 4, cursor: status === 'requesting' ? 'wait' : 'pointer',
                fontFamily: 'inherit',
              }}
            >
              {status === 'requesting' ? 'Locating…' : 'Use my location'}
            </button>
          </div>
        )}
        {status === 'denied' && (
          <div style={{ fontSize: 11, color: t.textDim, marginTop: 2 }}>
            Location blocked — {error}
          </div>
        )}
        <KV t={t} k="Alert radius" v={`${(p.alert_radius_km || 0).toFixed(1)} km`} />
        {p.spread_rate_km2_per_hr ? (
          <KV t={t} k="Fire spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
        <KV t={t} k="Containment" v={`${p.containment_pct ?? 0}%`} />
      </Section>

      <Section t={t} title="What to do">
        <div style={{
          background: t.evacBoxBg, border: `1px solid ${t.evacBoxBorder}`,
          padding: '10px 12px', borderRadius: 4,
        }}>
          <div style={{ fontSize: 11, color: t.textMuted, marginBottom: 4 }}>EVACUATION ROUTE</div>
          {liveRoute ? (
            <>
              <div style={{ fontSize: 14, color: t.textPrimary, fontWeight: 600 }}>
                Evacuate to {liveRoute.destination}
              </div>
              <div style={{ fontSize: 12, color: t.textSecondary, marginTop: 4 }}>
                {liveRoute.distance_km.toFixed(0)} km · {Math.round(liveRoute.duration_min)} min in current traffic
              </div>
              <a href={mapsUrl} target="_blank" rel="noopener noreferrer"
                style={{
                  marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 6,
                  background: '#1a73e8', color: '#fff',
                  padding: '7px 12px', borderRadius: 4,
                  fontSize: 13, fontWeight: 600, textDecoration: 'none',
                }}>
                <GoogleMapsIcon />
                Open route in Google Maps
              </a>
            </>
          ) : (
            <div style={{ fontSize: 13, color: t.textSecondary }}>
              Loading live route from Mapbox… check back in a moment.
            </div>
          )}
        </div>
      </Section>

      {data && (
        <>
          <Section t={t} title="Alert status">
            <Badge color={tone.color}>{tone.label}</Badge>
            <GateNote t={t} tone={tone} />
            {confidence >= 0.65 && (
              <div style={{ marginTop: 8 }}>
                <KV t={t} k="Residents notified" v={data.alerts_sent.toLocaleString()} />
                <KV t={t} k="Sent" v={timeAgo(data.alert_sent_at)} />
              </div>
            )}
          </Section>

          <Section t={t} title="What the AI is saying">
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, color: t.textPrimary }}>
              {data.advisory}
            </p>
            <div title={data.audit_hash} style={{
              marginTop: 10, fontSize: 11, color: t.auditGreen,
              fontFamily: 'ui-monospace, Menlo, monospace',
            }}>
              ✓ Verified · audit {data.audit_hash.slice(0, 10)}…
            </div>
          </Section>
        </>
      )}
    </>
  )
}

// Drought severity → display tone for the reservoir KV value. Severe gets a
// red badge so it stands out in the dispatcher's Incident section as a "water
// supply may not back you up" cue when planning resource allocation.
const RESERVOIR_TONE = {
  severe:   { color: '#c62828', label: 'drought-elevated risk' },
  moderate: { color: '#ef6c00', label: 'below average' },
  normal:   { color: '#2e7d32', label: 'normal' },
  unknown:  { color: '#666',    label: 'unknown' },
}

function useNearestReservoir(fire) {
  const [reservoir, setReservoir] = useState(null)
  const center = fire?.properties?.centroid
  useEffect(() => {
    if (!center) {
      setReservoir(null)
      return
    }
    let cancelled = false
    nearestReservoir(center[1], center[0])
      .then((r) => { if (!cancelled) setReservoir(r) })
      .catch(() => { if (!cancelled) setReservoir(null) })
    return () => { cancelled = true }
  }, [center?.[0], center?.[1]])
  return reservoir
}

function DispatcherView({ fire, data, t }) {
  const p = fire.properties
  // Real model confidence from #105's enriched fixture / live /fires endpoint
  // wins over the dispatch-stub's synthesized one. Falls back to the stub
  // when the upstream record doesn't carry a confidence field.
  const confidence = p.confidence ?? data?.confidence ?? null
  const tone = confidence != null ? dispatcherTone(confidence) : null
  const reservoir = useNearestReservoir(fire)
  return (
    <>
      <Section t={t} title="Incident">
        <KV t={t} k="County" v={p.county || '—'} />
        <KV t={t} k="Containment" v={`${p.containment_pct ?? 0}%`} />
        <KV t={t} k="Acres burned" v={Number(p.acres_burned || 0).toLocaleString()} />
        {p.spread_rate_km2_per_hr ? (
          <KV t={t} k="Spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
        {p.population_at_risk != null ? (
          <KV t={t} k="Population at risk" v={Number(p.population_at_risk).toLocaleString()} />
        ) : null}
        {p.alert_radius_km != null ? (
          <KV t={t} k="Alert radius" v={`${Number(p.alert_radius_km).toFixed(1)} km`} />
        ) : null}
        {p.risk_score != null ? (
          <KV t={t} k="Risk score" v={Number(p.risk_score).toFixed(2)} />
        ) : null}
        {reservoir && (
          <ReservoirRow t={t} reservoir={reservoir} />
        )}
      </Section>

      {data && (
        <>
          <Section t={t} title="AI Advisory">
            <Badge color={tone.color}>{tone.label} · {(confidence * 100).toFixed(0)}%</Badge>
            <GateNote t={t} tone={tone} />
            <p style={{ margin: '10px 0 0', fontSize: 13, lineHeight: 1.5, color: t.textPrimary }}>
              {data.advisory}
            </p>
          </Section>

          <Section t={t} title={`Dispatched resources (${data.dispatched_units.length})`}>
            {data.dispatched_units.map((u) => (
              <div key={u.station_id} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 0', borderTop: `1px solid ${t.borderInset}`,
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: t.textPrimary }}>{u.name}</div>
                  <div style={{ fontSize: 11, color: t.textMuted }}>
                    {u.units} unit{u.units > 1 ? 's' : ''} · {u.distance_km.toFixed(1)} km · ETA {u.eta_min} min
                  </div>
                </div>
                <span style={{
                  fontSize: 10, fontWeight: 700, letterSpacing: 0.5, color: '#fff',
                  padding: '3px 7px', borderRadius: 3, textTransform: 'uppercase', whiteSpace: 'nowrap',
                  background: STATUS_TONE[u.status] || '#888',
                }}>
                  {u.status}
                </span>
              </div>
            ))}
          </Section>
        </>
      )}
    </>
  )
}

function Section({ t, title, children }) {
  return (
    <section style={{ padding: '12px 16px', borderBottom: `1px solid ${t.borderSoft}` }}>
      <h3 style={{
        margin: '0 0 8px', fontSize: 11, fontWeight: 700, letterSpacing: 1,
        color: t.textDim, textTransform: 'uppercase',
      }}>{title}</h3>
      {children}
    </section>
  )
}

function KV({ t, k, v, hint }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '3px 0' }}>
      <span style={{ color: t.textMuted }}>{k}</span>
      <span style={{ color: t.textPrimary, fontWeight: 500 }}>
        {v}{hint ? <span style={{ color: t.textVeryDim, fontWeight: 400, marginLeft: 6 }}>({hint})</span> : null}
      </span>
    </div>
  )
}

function Badge({ color, children }) {
  return (
    <div style={{
      color: '#fff', padding: '6px 10px', borderRadius: 4,
      fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
      display: 'inline-block', marginBottom: 8, background: color,
    }}>{children}</div>
  )
}

// Two-line variant of KV: reservoir name + distance on the value line, then a
// colored chip below it for drought severity. Drought severity gets its own
// row (rather than packing into the value) so it reads as a status, not a unit.
function ReservoirRow({ t, reservoir }) {
  const tone = RESERVOIR_TONE[reservoir.drought_severity] || RESERVOIR_TONE.unknown
  return (
    <div style={{ padding: '6px 0 4px', borderTop: `1px solid ${t.borderInset}`, marginTop: 4 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
        <span style={{ color: t.textMuted }}>Nearest reservoir</span>
        <span style={{ color: t.textPrimary, fontWeight: 500 }}>
          {reservoir.name}
          <span style={{ color: t.textVeryDim, fontWeight: 400, marginLeft: 6 }}>
            ({reservoir.distance_km.toFixed(0)} km)
          </span>
        </span>
      </div>
      <div style={{ marginTop: 4, display: 'flex', justifyContent: 'flex-end' }}>
        <span style={{
          background: tone.color, color: '#fff',
          padding: '2px 8px', borderRadius: 8,
          fontSize: 11, fontWeight: 600,
        }}>
          {reservoir.pct_capacity}% capacity · {tone.label}
        </span>
      </div>
    </div>
  )
}

function GateNote({ t, tone }) {
  return (
    <div style={{
      background: t.gateNoteBg, padding: '8px 10px', borderRadius: 3,
      marginBottom: 4, fontSize: 12, lineHeight: 1.45,
      borderLeft: `3px solid ${tone.color}`,
    }}>
      <div style={{ color: t.textPrimary, fontWeight: 600, marginBottom: 3 }}>{tone.reason}</div>
      <div style={{ color: t.textSecondary }}>{tone.action}</div>
    </div>
  )
}

function PinIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true">
      <path d="M12 22s7-7.5 7-13a7 7 0 0 0-14 0c0 5.5 7 13 7 13z" />
      <circle cx="12" cy="9" r="2.5" />
    </svg>
  )
}

function GoogleMapsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 2C7.6 2 4 5.6 4 10c0 5.5 7.3 11.5 7.6 11.7.2.2.6.2.8 0C12.7 21.5 20 15.5 20 10c0-4.4-3.6-8-8-8zm0 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z" />
    </svg>
  )
}

function Tab({ t, active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        flex: 1, background: 'transparent', border: 'none',
        padding: '10px 0', fontSize: 12, fontWeight: 600, letterSpacing: 0.5,
        textTransform: 'uppercase', cursor: 'pointer',
        color: active ? t.textPrimary : t.textDim,
        borderBottom: active ? `2px solid ${t.tabAccent}` : '2px solid transparent',
      }}
    >
      {children}
    </button>
  )
}
