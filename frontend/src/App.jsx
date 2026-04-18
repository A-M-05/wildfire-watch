import { useState } from 'react'
import FireMap from './FireMap'
import DispatchPanel from './DispatchPanel'

export default function App() {
  // Selection is owned at the app level so the map and the side panel stay in
  // sync. FireMap reports clicks, the panel renders the selected fire's data.
  const [selectedFire, setSelectedFire] = useState(null)
  return (
    <>
      <FireMap selectedFire={selectedFire} onSelectFire={setSelectedFire} />
      <DispatchPanel fire={selectedFire} onClose={() => setSelectedFire(null)} />
    </>
  )
}
