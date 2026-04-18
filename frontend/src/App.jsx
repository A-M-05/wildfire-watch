import { useState } from 'react'
import FireMap from './FireMap'
import DispatchPanel from './DispatchPanel'
import RegisterModal from './RegisterModal'

export default function App() {
  // Selection + theme are owned at the app level so the map and the side
  // panel stay in sync. FireMap reports clicks; the panel renders the
  // selected fire and recolors its chrome to match the active basemap theme.
  const [selectedFire, setSelectedFire] = useState(null)
  const [theme, setTheme] = useState('light')
  const [registerOpen, setRegisterOpen] = useState(false)
  const dark = theme === 'dark'
  return (
    <>
      <FireMap
        selectedFire={selectedFire}
        onSelectFire={setSelectedFire}
        theme={theme}
        onThemeChange={setTheme}
      />
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
