import { useEffect, useRef, useState } from 'react'
import FireMap from './FireMap'
import DispatchPanel from './DispatchPanel'
import RegisterModal from './RegisterModal'
import StatusPill from './StatusPill'
import AlertBanner from './AlertBanner'
import { fetchDispatchData } from './api/dispatch'

export default function App() {
  // Selection + theme are owned at the app level so the map and the side
  // panel stay in sync. FireMap reports clicks; the panel renders the
  // selected fire and recolors its chrome to match the active basemap theme.
  const [selectedFire, setSelectedFire] = useState(null)
  const [theme, setTheme] = useState('light')
  const [registerOpen, setRegisterOpen] = useState(false)
  const [fireCount, setFireCount] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [activeAlert, setActiveAlert] = useState(null)
  const announcedRef = useRef(new Set())

  // When fires arrive, surface the highest-confidence "alert sent" event as a
  // banner. Once #30 wires the WebSocket this driver is replaced by an
  // alert_sent message handler — same setActiveAlert call.
  const handleFiresLoaded = async (collection) => {
    const fires = collection?.features || []
    setFireCount(fires.length)
    setLastUpdated(Date.now())
    if (!fires.length) return
    // Pick the most populous at-risk fire and check whether it would have
    // alerted (confidence >= 0.65). One banner per fire per session.
    const ranked = [...fires].sort(
      (a, b) => (b.properties.population_at_risk || 0) - (a.properties.population_at_risk || 0),
    )
    for (const fire of ranked) {
      const id = fire.properties?.fire_id
      if (!id || announcedRef.current.has(id)) continue
      const data = await fetchDispatchData(fire)
      if (data && data.confidence >= 0.65 && data.alerts_sent > 0) {
        announcedRef.current.add(id)
        setActiveAlert({
          fire_id: id,
          fire_name: fire.properties.name,
          alerts_sent: data.alerts_sent,
          audit_hash: data.audit_hash,
        })
        return
      }
    }
  }

  const dark = theme === 'dark'
  return (
    <>
      <FireMap
        selectedFire={selectedFire}
        onSelectFire={setSelectedFire}
        theme={theme}
        onThemeChange={setTheme}
        onFiresLoaded={handleFiresLoaded}
        onAlertSent={(msg) => {
          // Real `alert_sent` from the WebSocket — supersedes the simulated
          // driver in handleFiresLoaded for any fire it covers.
          announcedRef.current.add(msg.fire_id)
          setActiveAlert({
            fire_id: msg.fire_id,
            fire_name: msg.fire_name,
            alerts_sent: msg.alerts_sent,
            audit_hash: msg.audit_hash,
          })
        }}
      />
      <StatusPill fireCount={fireCount} lastUpdated={lastUpdated} theme={theme} />
      <AlertBanner alert={activeAlert} onDismiss={() => setActiveAlert(null)} theme={theme} />
      <DispatchPanel
        fire={selectedFire}
        onClose={() => setSelectedFire(null)}
        theme={theme}
      />
      <button
        onClick={() => setRegisterOpen(true)}
        style={{
          position: 'absolute', top: 12, left: 60, zIndex: 6,
          padding: '8px 14px', height: 36,
          background: '#ff5533', color: '#fff',
          border: 'none', borderRadius: 999, cursor: 'pointer',
          fontSize: 13, fontWeight: 600, letterSpacing: 0.3,
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          boxShadow: dark ? '0 2px 8px rgba(0,0,0,0.5)' : '0 2px 8px rgba(255,85,51,0.35)',
          display: 'flex', alignItems: 'center', gap: 6,
        }}
      >
        <BellIcon />
        Get SMS alerts
      </button>
      <RegisterModal
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        theme={theme}
      />
    </>
  )
}

function BellIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true">
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  )
}
