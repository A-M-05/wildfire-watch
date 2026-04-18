import { useEffect, useState } from 'react'
import { fetchDispatchData } from './api/dispatch'

// Confidence badge color follows the CLAUDE.md safety contract: <0.65 routes
// to human review (red), 0.65-0.85 is auto-dispatch but flagged (amber), and
// ≥0.85 is high-confidence auto-dispatch (green).
function confidenceTone(score) {
  if (score < 0.65) return { color: '#ff3322', label: 'HUMAN REVIEW REQUIRED' }
  if (score < 0.85) return { color: '#ffaa00', label: 'AUTO-DISPATCH (FLAGGED)' }
  return { color: '#22cc44', label: 'AUTO-DISPATCH' }
}

const STATUS_TONE = {
  'en route': '#22cc44',
  dispatched: '#ffaa00',
  staged: '#888',
}

export default function DispatchPanel({ fire, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

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

  if (!fire) {
    return (
      <aside style={panelStyle}>
        <div style={emptyStyle}>
          <div style={{ fontSize: 32, marginBottom: 8 }}>📍</div>
          <div>Click a fire on the map to see dispatch details.</div>
        </div>
      </aside>
    )
  }

  const p = fire.properties
  const tone = data ? confidenceTone(data.confidence) : null

  return (
    <aside style={panelStyle}>
      <header style={headerStyle}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 11, opacity: 0.6, letterSpacing: 1 }}>DISPATCH PANEL</div>
          <h2 style={{ margin: '4px 0 0', fontSize: 18, color: '#111' }}>{p.name}</h2>
          {p.location && <div style={{ fontSize: 12, color: '#555' }}>{p.location}</div>}
        </div>
        <button onClick={onClose} style={closeBtnStyle} aria-label="Close panel">×</button>
      </header>

      <Section title="Incident">
        <KV k="County" v={p.county || '—'} />
        <KV k="Containment" v={`${p.containment_pct ?? 0}%`} />
        <KV k="Acres burned" v={Number(p.acres_burned || 0).toLocaleString()} />
        {p.spread_rate_km2_per_hr ? (
          <KV k="Spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
      </Section>

      {loading && <div style={loadingStyle}>Loading dispatch data…</div>}

      {data && (
        <>
          <Section title="AI Advisory">
            <div style={{ ...badgeStyle, background: tone.color }}>
              {tone.label} · {(data.confidence * 100).toFixed(0)}%
            </div>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, color: '#222' }}>
              {data.advisory}
            </p>
          </Section>

          <Section title={`Dispatched resources (${data.dispatched_units.length})`}>
            {data.dispatched_units.map((u) => (
              <div key={u.station_id} style={unitRowStyle}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#111' }}>{u.name}</div>
                  <div style={{ fontSize: 11, color: '#666' }}>
                    {u.units} unit{u.units > 1 ? 's' : ''} · {u.distance_km.toFixed(1)} km · ETA {u.eta_min} min
                  </div>
                </div>
                <span style={{ ...statusPillStyle, background: STATUS_TONE[u.status] || '#888' }}>
                  {u.status}
                </span>
              </div>
            ))}
          </Section>
        </>
      )}

      <footer style={footerStyle}>
        Stub data — wire to DynamoDB + WebSocket in #30
      </footer>
    </aside>
  )
}

function Section({ title, children }) {
  return (
    <section style={sectionStyle}>
      <h3 style={sectionTitleStyle}>{title}</h3>
      {children}
    </section>
  )
}

function KV({ k, v }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '3px 0' }}>
      <span style={{ color: '#666' }}>{k}</span>
      <span style={{ color: '#111', fontWeight: 500 }}>{v}</span>
    </div>
  )
}

const panelStyle = {
  position: 'absolute',
  top: 12,
  right: 12,
  bottom: 12,
  width: 340,
  background: 'rgba(255, 255, 255, 0.97)',
  borderRadius: 6,
  boxShadow: '0 4px 16px rgba(0, 0, 0, 0.25)',
  overflowY: 'auto',
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  zIndex: 5,
}
const headerStyle = {
  padding: '14px 16px 12px',
  borderBottom: '1px solid #e5e5e5',
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  gap: 8,
}
const closeBtnStyle = {
  background: 'transparent', border: 'none', fontSize: 24, lineHeight: 1,
  cursor: 'pointer', color: '#666', padding: 0, width: 24, height: 24,
}
const sectionStyle = { padding: '12px 16px', borderBottom: '1px solid #f0f0f0' }
const sectionTitleStyle = {
  margin: '0 0 8px', fontSize: 11, fontWeight: 700, letterSpacing: 1,
  color: '#888', textTransform: 'uppercase',
}
const unitRowStyle = {
  display: 'flex', alignItems: 'center', gap: 8,
  padding: '8px 0', borderTop: '1px solid #f5f5f5',
}
const badgeStyle = {
  color: '#fff', padding: '6px 10px', borderRadius: 4,
  fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
  display: 'inline-block', marginBottom: 8,
}
const statusPillStyle = {
  fontSize: 10, fontWeight: 700, letterSpacing: 0.5, color: '#fff',
  padding: '3px 7px', borderRadius: 3, textTransform: 'uppercase', whiteSpace: 'nowrap',
}
const emptyStyle = {
  padding: 24, textAlign: 'center', color: '#666',
  fontSize: 13, marginTop: 60,
}
const loadingStyle = { padding: '12px 16px', fontSize: 12, color: '#888' }
const footerStyle = {
  padding: '10px 16px', fontSize: 10, color: '#aaa',
  textAlign: 'center', letterSpacing: 0.3,
}
