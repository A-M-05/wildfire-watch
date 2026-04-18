import { useEffect, useState } from 'react'
import { fetchDispatchData } from './api/dispatch'

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

// Dispatcher view tone — kept for the AI-safety track demo.
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

function timeAgo(iso) {
  const min = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 60_000))
  if (min < 60) return `${min} min ago`
  const hr = Math.round(min / 60)
  return `${hr}h ago`
}

export default function DispatchPanel({ fire, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState('resident')

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
      <div style={emptyChipStyle}>
        <span style={{ fontSize: 14 }}>📍</span>
        <span>Click a fire for details</span>
      </div>
    )
  }

  const p = fire.properties

  return (
    <aside style={panelStyle}>
      <header style={headerStyle}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 11, opacity: 0.6, letterSpacing: 1 }}>
            {view === 'resident' ? 'COMMUNITY ALERT' : 'DISPATCH PANEL'}
          </div>
          <h2 style={{ margin: '4px 0 0', fontSize: 18, color: '#111' }}>{p.name}</h2>
          {p.location && <div style={{ fontSize: 12, color: '#555' }}>{p.location}</div>}
        </div>
        <button onClick={onClose} style={closeBtnStyle} aria-label="Close panel">×</button>
      </header>

      <div style={tabBarStyle}>
        <Tab active={view === 'resident'} onClick={() => setView('resident')}>Resident</Tab>
        <Tab active={view === 'dispatcher'} onClick={() => setView('dispatcher')}>Dispatcher</Tab>
      </div>

      {loading && <div style={loadingStyle}>Loading…</div>}

      {view === 'resident' ? (
        <ResidentView fire={fire} data={data} />
      ) : (
        <DispatcherView fire={fire} data={data} />
      )}

      <footer style={footerStyle}>
        Stub data — wire to DynamoDB + WebSocket in #30
      </footer>
    </aside>
  )
}

function ResidentView({ fire, data }) {
  const p = fire.properties
  const tone = data ? safetyTone(data.confidence) : null
  return (
    <>
      <Section title="Are you in danger?">
        <KV k="Distance to fire" v="—" hint="Enable location" />
        <KV k="Alert radius" v={`${(p.alert_radius_km || 0).toFixed(1)} km`} />
        {p.spread_rate_km2_per_hr ? (
          <KV k="Fire spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
        <KV k="Containment" v={`${p.containment_pct ?? 0}%`} />
      </Section>

      <Section title="What to do">
        {p.evacuation_route ? (
          <div style={evacBoxStyle}>
            <div style={{ fontSize: 11, color: '#666', marginBottom: 4 }}>EVACUATION ROUTE</div>
            <div style={{ fontSize: 14, color: '#111', fontWeight: 600 }}>{p.evacuation_route}</div>
            <div style={{ fontSize: 12, color: '#555', marginTop: 4 }}>
              Tap the fire on the map to see the live route and current traffic.
            </div>
          </div>
        ) : (
          <div style={{ fontSize: 13, color: '#555' }}>
            Stay tuned for evacuation guidance from local officials.
          </div>
        )}
      </Section>

      {data && (
        <>
          <Section title="Alert status">
            <div style={{ ...badgeStyle, background: tone.color }}>{tone.label}</div>
            <div style={{ ...gateNoteStyle, borderLeft: `3px solid ${tone.color}` }}>
              <div style={gateNoteReasonStyle}>{tone.reason}</div>
              <div style={gateNoteActionStyle}>{tone.action}</div>
            </div>
            {data.confidence >= 0.65 && (
              <div style={alertMetaStyle}>
                <KV k="Residents notified" v={data.alerts_sent.toLocaleString()} />
                <KV k="Sent" v={timeAgo(data.alert_sent_at)} />
              </div>
            )}
          </Section>

          <Section title="What the AI is saying">
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, color: '#222' }}>
              {data.advisory}
            </p>
            <div style={auditChipStyle} title={data.audit_hash}>
              ✓ Verified · audit {data.audit_hash.slice(0, 10)}…
            </div>
          </Section>
        </>
      )}
    </>
  )
}

function DispatcherView({ fire, data }) {
  const p = fire.properties
  const tone = data ? dispatcherTone(data.confidence) : null
  return (
    <>
      <Section title="Incident">
        <KV k="County" v={p.county || '—'} />
        <KV k="Containment" v={`${p.containment_pct ?? 0}%`} />
        <KV k="Acres burned" v={Number(p.acres_burned || 0).toLocaleString()} />
        {p.spread_rate_km2_per_hr ? (
          <KV k="Spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
      </Section>

      {data && (
        <>
          <Section title="AI Advisory">
            <div style={{ ...badgeStyle, background: tone.color }}>
              {tone.label} · {(data.confidence * 100).toFixed(0)}%
            </div>
            <div style={{ ...gateNoteStyle, borderLeft: `3px solid ${tone.color}` }}>
              <div style={gateNoteReasonStyle}>{tone.reason}</div>
              <div style={gateNoteActionStyle}>{tone.action}</div>
            </div>
            <p style={{ margin: '10px 0 0', fontSize: 13, lineHeight: 1.5, color: '#222' }}>
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
    </>
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

function KV({ k, v, hint }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '3px 0' }}>
      <span style={{ color: '#666' }}>{k}</span>
      <span style={{ color: '#111', fontWeight: 500 }}>
        {v}{hint ? <span style={{ color: '#aaa', fontWeight: 400, marginLeft: 6 }}>({hint})</span> : null}
      </span>
    </div>
  )
}

function Tab({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        ...tabStyle,
        color: active ? '#111' : '#888',
        borderBottom: active ? '2px solid #ff5533' : '2px solid transparent',
      }}
    >
      {children}
    </button>
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
const tabBarStyle = {
  display: 'flex', borderBottom: '1px solid #e5e5e5',
  background: '#fafafa',
}
const tabStyle = {
  flex: 1, background: 'transparent', border: 'none',
  padding: '10px 0', fontSize: 12, fontWeight: 600, letterSpacing: 0.5,
  textTransform: 'uppercase', cursor: 'pointer',
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
const emptyChipStyle = {
  position: 'absolute', top: 12, right: 12, zIndex: 5,
  background: 'rgba(255, 255, 255, 0.92)', color: '#444',
  padding: '6px 12px', borderRadius: 999,
  fontSize: 12, fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  boxShadow: '0 2px 6px rgba(0, 0, 0, 0.15)',
  display: 'flex', alignItems: 'center', gap: 6,
  pointerEvents: 'none',
}
const loadingStyle = { padding: '12px 16px', fontSize: 12, color: '#888' }
const gateNoteStyle = {
  background: '#fafafa', padding: '8px 10px', borderRadius: 3,
  marginBottom: 4, fontSize: 12, lineHeight: 1.45,
}
const gateNoteReasonStyle = { color: '#222', fontWeight: 600, marginBottom: 3 }
const gateNoteActionStyle = { color: '#555' }
const evacBoxStyle = {
  background: '#f5f9ff', border: '1px solid #d6e6ff',
  padding: '10px 12px', borderRadius: 4,
}
const alertMetaStyle = { marginTop: 8 }
const auditChipStyle = {
  marginTop: 10, fontSize: 11, color: '#0e8a4e',
  fontFamily: 'ui-monospace, Menlo, monospace',
}
const footerStyle = {
  padding: '10px 16px', fontSize: 10, color: '#aaa',
  textAlign: 'center', letterSpacing: 0.3,
}
