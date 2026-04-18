import { useEffect, useMemo, useState } from 'react'
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
        <span style={{ fontSize: 14 }}>📍</span>
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

      <footer style={{
        padding: '10px 16px', fontSize: 10, color: t.textVeryDim,
        textAlign: 'center', letterSpacing: 0.3,
      }}>
        Stub data — wire to DynamoDB + WebSocket in #30
      </footer>
    </aside>
  )
}

function ResidentView({ fire, data, t }) {
  const p = fire.properties
  const tone = data ? safetyTone(data.confidence) : null
  return (
    <>
      <Section t={t} title="Are you in danger?">
        <KV t={t} k="Distance to fire" v="—" hint="Enable location" />
        <KV t={t} k="Alert radius" v={`${(p.alert_radius_km || 0).toFixed(1)} km`} />
        {p.spread_rate_km2_per_hr ? (
          <KV t={t} k="Fire spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
        <KV t={t} k="Containment" v={`${p.containment_pct ?? 0}%`} />
      </Section>

      <Section t={t} title="What to do">
        {p.evacuation_route ? (
          <div style={{
            background: t.evacBoxBg, border: `1px solid ${t.evacBoxBorder}`,
            padding: '10px 12px', borderRadius: 4,
          }}>
            <div style={{ fontSize: 11, color: t.textMuted, marginBottom: 4 }}>EVACUATION ROUTE</div>
            <div style={{ fontSize: 14, color: t.textPrimary, fontWeight: 600 }}>{p.evacuation_route}</div>
            <div style={{ fontSize: 12, color: t.textSecondary, marginTop: 4 }}>
              Tap the fire on the map to see the live route and current traffic.
            </div>
          </div>
        ) : (
          <div style={{ fontSize: 13, color: t.textSecondary }}>
            Stay tuned for evacuation guidance from local officials.
          </div>
        )}
      </Section>

      {data && (
        <>
          <Section t={t} title="Alert status">
            <Badge color={tone.color}>{tone.label}</Badge>
            <GateNote t={t} tone={tone} />
            {data.confidence >= 0.65 && (
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

function DispatcherView({ fire, data, t }) {
  const p = fire.properties
  const tone = data ? dispatcherTone(data.confidence) : null
  return (
    <>
      <Section t={t} title="Incident">
        <KV t={t} k="County" v={p.county || '—'} />
        <KV t={t} k="Containment" v={`${p.containment_pct ?? 0}%`} />
        <KV t={t} k="Acres burned" v={Number(p.acres_burned || 0).toLocaleString()} />
        {p.spread_rate_km2_per_hr ? (
          <KV t={t} k="Spread rate" v={`${p.spread_rate_km2_per_hr} km²/hr`} />
        ) : null}
      </Section>

      {data && (
        <>
          <Section t={t} title="AI Advisory">
            <Badge color={tone.color}>{tone.label} · {(data.confidence * 100).toFixed(0)}%</Badge>
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
