# Skill: Frontend (React + Mapbox GL JS)

Read this before writing any React component or Mapbox integration.

## Setup

```bash
cd frontend
npx create-react-app . --template cra-template
npm install mapbox-gl @aws-amplify/ui-react aws-amplify
```

## Mapbox initialization pattern

```jsx
// src/FireMap.jsx
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef } from 'react'

mapboxgl.accessToken = process.env.REACT_APP_MAPBOX_TOKEN

export default function FireMap() {
  const mapContainer = useRef(null)
  const map = useRef(null)

  useEffect(() => {
    if (map.current) return
    map.current = new mapboxgl.Map({
      container: mapContainer.current,
      style: 'mapbox://styles/mapbox/dark-v11',
      center: [-119.5, 37.0],  // California
      zoom: 6
    })

    map.current.on('load', () => {
      loadFireStations()
      loadActiveFirePerimeters()
    })
  }, [])

  return <div ref={mapContainer} style={{ width: '100%', height: '100vh' }} />
}
```

## Adding a GeoJSON source + layer

```jsx
// Fire perimeters
map.current.addSource('fire-perimeters', {
  type: 'geojson',
  data: { type: 'FeatureCollection', features: [] }
})

map.current.addLayer({
  id: 'fire-perimeters-fill',
  type: 'fill',
  source: 'fire-perimeters',
  paint: {
    'fill-color': [
      'interpolate', ['linear'],
      ['get', 'containment_pct'],
      0, '#ff2200',
      25, '#ff6600',
      50, '#ffaa00',
      100, '#22cc44'
    ],
    'fill-opacity': 0.6
  }
})
```

## Updating GeoJSON dynamically

```jsx
// When new fire data arrives
map.current.getSource('fire-perimeters').setData({
  type: 'FeatureCollection',
  features: fires.map(fire => ({
    type: 'Feature',
    geometry: JSON.parse(fire.perimeter_geojson),
    properties: {
      fire_id: fire.fire_id,
      containment_pct: fire.containment_pct
    }
  }))
})
```

## API calls (Amplify + Cognito auth)

```jsx
import { Amplify } from 'aws-amplify'
import { get } from 'aws-amplify/api'

// Fetch active fires
async function fetchActiveFires() {
  const { body } = await get({ apiName: 'wildfireAPI', path: '/fires/active' })
  return body.json()
}
```

## WebSocket connection

```jsx
import { useEffect, useState } from 'react'

export function useFireSocket() {
  const [lastMessage, setLastMessage] = useState(null)
  const ws = useRef(null)

  useEffect(() => {
    ws.current = new WebSocket(process.env.REACT_APP_WS_URL)
    ws.current.onmessage = (e) => setLastMessage(JSON.parse(e.data))
    return () => ws.current?.close()
  }, [])

  return lastMessage
}
```

## SafetyBadge color coding

- Confidence ≥ 0.65: green (`#22c55e`)
- Confidence 0.4–0.65: amber (`#f59e0b`)
- Confidence < 0.4: red (`#ef4444`)
- Guardrails passed: show checkmark in green
- Guardrails blocked: show warning in red, do not show advisory text

## Environment variables (frontend/.env)

```
REACT_APP_MAPBOX_TOKEN=pk.xxx
REACT_APP_API_URL=https://xxx.execute-api.us-east-1.amazonaws.com/prod
REACT_APP_WS_URL=wss://xxx.execute-api.us-east-1.amazonaws.com/prod
REACT_APP_COGNITO_USER_POOL_ID=us-east-1_xxx
REACT_APP_COGNITO_CLIENT_ID=xxx
```
