import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef, useState } from 'react'

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN

const STATIONS_URL = '/data/fire_stations.geojson'

export default function FireMap() {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (mapRef.current) return
    if (!mapboxgl.accessToken) {
      setError('VITE_MAPBOX_TOKEN is not set — see frontend/.env.example')
      return
    }

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: 'mapbox://styles/mapbox/dark-v11',
      center: [-119.5, 37.0],
      zoom: 6,
    })
    mapRef.current = map

    map.on('load', async () => {
      try {
        const res = await fetch(STATIONS_URL)
        if (!res.ok) throw new Error(`failed to load stations (${res.status})`)
        const data = await res.json()

        map.addSource('fire-stations', { type: 'geojson', data })

        // Color-coded dots: green = available, red = deployed/unavailable.
        map.addLayer({
          id: 'fire-stations-circle',
          type: 'circle',
          source: 'fire-stations',
          paint: {
            'circle-radius': 7,
            'circle-color': [
              'case',
              ['==', ['get', 'available'], true], '#22cc44',
              '#ff3322',
            ],
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#ffffff',
          },
        })

        const popup = new mapboxgl.Popup({ closeButton: false, offset: 12 })
        map.on('mouseenter', 'fire-stations-circle', (e) => {
          map.getCanvas().style.cursor = 'pointer'
          const f = e.features[0]
          const { name, station_id, available, units } = f.properties
          popup
            .setLngLat(f.geometry.coordinates)
            .setHTML(
              `<strong>${name}</strong><br/>` +
              `${station_id}<br/>` +
              `Status: ${available === true || available === 'true' ? 'available' : 'deployed'}<br/>` +
              `Units: ${units}`
            )
            .addTo(map)
        })
        map.on('mouseleave', 'fire-stations-circle', () => {
          map.getCanvas().style.cursor = ''
          popup.remove()
        })
      } catch (e) {
        setError(`station load failed: ${e.message}`)
      }
    })

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100vh' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      {error && (
        <div style={{
          position: 'absolute', top: 12, left: 12, padding: '8px 12px',
          background: 'rgba(180, 30, 30, 0.9)', color: '#fff', borderRadius: 4,
          fontFamily: 'monospace', fontSize: 13,
        }}>
          {error}
        </div>
      )}
    </div>
  )
}
