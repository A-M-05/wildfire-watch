import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef, useState } from 'react'
import { fetchActiveFires } from './api/fires'

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN

const STATIONS_URL = '/data/fire_stations.geojson'
const FIRE_REFRESH_MS = 30_000

function formatRelative(iso) {
  if (!iso) return 'unknown'
  const diffMs = Date.now() - new Date(iso).getTime()
  if (diffMs < 0) return 'just now'
  const mins = Math.floor(diffMs / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

const STYLES = {
  light: 'mapbox://styles/mapbox/light-v11',
  dark: 'mapbox://styles/mapbox/dark-v11',
}

export default function FireMap() {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const stationsRef = useRef(null)
  const firesRef = useRef(null)
  const didInitTheme = useRef(false)
  const [error, setError] = useState(null)
  const [theme, setTheme] = useState('light')

  // Adds the stations source + circle layer. Called on first load and after
  // every setStyle() — changing the basemap wipes user-added sources/layers.
  const addStationsLayer = (map, data) => {
    if (map.getLayer('fire-stations-circle')) map.removeLayer('fire-stations-circle')
    if (map.getSource('fire-stations')) map.removeSource('fire-stations')

    map.addSource('fire-stations', { type: 'geojson', data })
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
  }

  // Fire perimeter fill + outline (for polygons) and circle (for points).
  // CAL FIRE live data is point-only; mock demo data is polygons. One source,
  // two layer types filtered by geometry. Color interpolates by containment.
  const addFiresLayer = (map, data) => {
    for (const id of ['fires-fill', 'fires-outline', 'fires-circle']) {
      if (map.getLayer(id)) map.removeLayer(id)
    }
    if (map.getSource('active-fires')) map.removeSource('active-fires')

    const containmentColor = [
      'interpolate', ['linear'], ['get', 'containment_pct'],
      0,   '#ff2200',
      25,  '#ff6600',
      50,  '#ffaa00',
      75,  '#ffdd33',
      100, '#22cc44',
    ]

    map.addSource('active-fires', { type: 'geojson', data })
    map.addLayer({
      id: 'fires-fill',
      type: 'fill',
      source: 'active-fires',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': containmentColor, 'fill-opacity': 0.55 },
    }, 'fire-stations-circle')
    map.addLayer({
      id: 'fires-outline',
      type: 'line',
      source: 'active-fires',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'line-color': '#000', 'line-width': 1.2, 'line-opacity': 0.4 },
    }, 'fire-stations-circle')
    map.addLayer({
      id: 'fires-circle',
      type: 'circle',
      source: 'active-fires',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['zoom'],
          5, 6,
          10, 14,
        ],
        'circle-color': containmentColor,
        'circle-opacity': 0.75,
        'circle-stroke-width': 2,
        'circle-stroke-color': '#000',
      },
    }, 'fire-stations-circle')
  }

  const refreshFires = async (map) => {
    try {
      const data = await fetchActiveFires()
      firesRef.current = data
      if (map.isStyleLoaded()) {
        const src = map.getSource('active-fires')
        if (src) src.setData(data)
        else addFiresLayer(map, data)
      }
    } catch (e) {
      setError(`fire load failed: ${e.message}`)
    }
  }

  useEffect(() => {
    if (mapRef.current) return
    if (!mapboxgl.accessToken) {
      setError('VITE_MAPBOX_TOKEN is not set — see frontend/.env.example')
      return
    }

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: STYLES[theme],
      center: [-119.5, 37.0],
      zoom: 6,
    })
    mapRef.current = map

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

    map.on('load', async () => {
      try {
        const res = await fetch(STATIONS_URL)
        if (!res.ok) throw new Error(`failed to load stations (${res.status})`)
        const data = await res.json()
        stationsRef.current = data
        addStationsLayer(map, data)
      } catch (e) {
        setError(`station load failed: ${e.message}`)
      }
      await refreshFires(map)
    })

    const interval = setInterval(() => refreshFires(map), FIRE_REFRESH_MS)

    const firePopup = new mapboxgl.Popup({ closeButton: false, offset: 8 })
    const showFirePopup = (e) => {
      map.getCanvas().style.cursor = 'pointer'
      const f = e.features[0]
      const p = f.properties
      const lines = [
        `<strong>${p.name || 'Unnamed fire'}</strong>`,
        p.location ? `${p.location}` : null,
        p.county ? `County: ${p.county}` : null,
        `Containment: ${p.containment_pct}%`,
        p.acres_burned ? `Acres burned: ${Number(p.acres_burned).toLocaleString()}` : null,
        p.spread_rate_km2_per_hr ? `Spread: ${p.spread_rate_km2_per_hr} km²/hr` : null,
        `<span style="color:#666;font-size:11px">Updated ${formatRelative(p.last_updated)}</span>`,
      ].filter(Boolean)
      firePopup.setLngLat(e.lngLat).setHTML(lines.join('<br/>')).addTo(map)
    }
    const hideFirePopup = () => {
      map.getCanvas().style.cursor = ''
      firePopup.remove()
    }
    for (const layer of ['fires-fill', 'fires-circle']) {
      map.on('mouseenter', layer, showFirePopup)
      map.on('mouseleave', layer, hideFirePopup)
    }

    return () => {
      clearInterval(interval)
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Swap basemap on theme change; re-attach stations once the new style settles.
  // Skip the first run — the map constructor already loaded the initial theme.
  useEffect(() => {
    if (!didInitTheme.current) {
      didInitTheme.current = true
      return
    }
    const map = mapRef.current
    if (!map) return

    map.setStyle(STYLES[theme])
    map.once('style.load', () => {
      if (stationsRef.current) addStationsLayer(map, stationsRef.current)
      if (firesRef.current) addFiresLayer(map, firesRef.current)
    })
  }, [theme])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100vh' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      <button
        onClick={() => setTheme((t) => (t === 'light' ? 'dark' : 'light'))}
        style={{
          position: 'absolute', top: 12, right: 12, padding: '8px 12px',
          background: theme === 'light' ? '#1a1a1a' : '#f5f5f5',
          color: theme === 'light' ? '#f5f5f5' : '#1a1a1a',
          border: 'none', borderRadius: 4, cursor: 'pointer',
          fontFamily: 'monospace', fontSize: 13, fontWeight: 600,
          boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
        }}
      >
        {theme === 'light' ? 'Dark' : 'Light'}
      </button>
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
