import { useState } from 'react'
import FireMap from './FireMap'
import DispatchPanel from './DispatchPanel'

export default function App() {
  // Selection + theme are owned at the app level so the map and the side
  // panel stay in sync. FireMap reports clicks; the panel renders the
  // selected fire and recolors its chrome to match the active basemap theme.
  const [selectedFire, setSelectedFire] = useState(null)
  const [theme, setTheme] = useState('light')
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
    </>
  )
}
