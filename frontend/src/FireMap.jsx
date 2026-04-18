import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef, useState } from 'react'
import { fetchActiveFires, buildAlertZones } from './api/fires'
import { buildEvacRoutes, routeSummaryForFire } from './api/evacRoutes'

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

export default function FireMap({ selectedFire, onSelectFire }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const stationsRef = useRef(null)
  const firesRef = useRef(null)
  const zonesRef = useRef(null)
  const routesRef = useRef(null)
  const destsRef = useRef(null)
  const didInitTheme = useRef(false)
  const [error, setError] = useState(null)
  const [theme, setTheme] = useState('light')
  // Selection is lifted to App so the dispatch panel can read it. We derive
  // the id locally for the evac filter; click handlers report the full feature
  // back up via onSelectFire.
  const selectedFireId = selectedFire?.properties?.fire_id ?? null
  const selectedFireIdRef = useRef(null)
  selectedFireIdRef.current = selectedFireId

  // Mapbox filter expression that matches a single fire_id (or matches nothing
  // when null is passed). Used to scope evac route + destination visibility.
  const filterForSelected = (id) =>
    id ? ['==', ['get', 'fire_id'], id] : ['==', ['get', 'fire_id'], '__none__']

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

  // Fire perimeter fill + outline. Polygon footprint is scaled by acres burned
  // (see api/fires.js). Color interpolates by containment (red→green).
  const addFiresLayer = (map, data) => {
    for (const id of ['fires-fill', 'fires-outline']) {
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
  }

  // Alert-zone fill + dashed outline drawn UNDER the fire footprint so the
  // smaller burned area stays readable on top. Stable amber so it reads as
  // "warning halo" regardless of containment color.
  const addAlertZonesLayer = (map, data) => {
    for (const id of ['alert-zones-fill', 'alert-zones-outline']) {
      if (map.getLayer(id)) map.removeLayer(id)
    }
    if (map.getSource('alert-zones')) map.removeSource('alert-zones')

    map.addSource('alert-zones', { type: 'geojson', data })
    // Insert beneath fires-fill so the burned footprint sits visibly on top.
    const beforeId = map.getLayer('fires-fill') ? 'fires-fill' : 'fire-stations-circle'
    map.addLayer({
      id: 'alert-zones-fill',
      type: 'fill',
      source: 'alert-zones',
      paint: { 'fill-color': '#ffaa00', 'fill-opacity': 0.12 },
    }, beforeId)
    map.addLayer({
      id: 'alert-zones-outline',
      type: 'line',
      source: 'alert-zones',
      paint: {
        'line-color': '#ff7700',
        'line-width': 1.5,
        'line-opacity': 0.7,
        'line-dasharray': [2, 2],
      },
    }, beforeId)
  }

  // Evac route polylines drawn ABOVE the alert-zone halo so the corridor reads
  // clearly, but BELOW stations so trucks stay clickable. Two stacked line
  // layers (dark casing + bright fill) give a road-style appearance that
  // contrasts against both light and dark basemaps.
  const addEvacLayers = (map, routes, destinations) => {
    for (const id of ['evac-route-casing', 'evac-route-line', 'evac-dest-circle', 'evac-dest-label']) {
      if (map.getLayer(id)) map.removeLayer(id)
    }
    for (const id of ['evac-routes', 'evac-destinations']) {
      if (map.getSource(id)) map.removeSource(id)
    }

    map.addSource('evac-routes', { type: 'geojson', data: routes })
    map.addSource('evac-destinations', { type: 'geojson', data: destinations })

    const beforeId = map.getLayer('fire-stations-circle') ? 'fire-stations-circle' : undefined
    // All four evac layers honor the same per-fire filter so showing or hiding
    // a route is a single-state toggle, not four parallel updates.
    const filter = filterForSelected(selectedFireIdRef.current)
    map.addLayer({
      id: 'evac-route-casing',
      type: 'line',
      source: 'evac-routes',
      filter,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#0a3d62', 'line-width': 6, 'line-opacity': 0.9 },
    }, beforeId)
    // Line color reflects worst-case traffic on the route. Cyan = clear,
    // amber/red telegraph "this evac corridor is already congested."
    map.addLayer({
      id: 'evac-route-line',
      type: 'line',
      source: 'evac-routes',
      filter,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-width': 3,
        'line-opacity': 0.95,
        'line-color': [
          'match', ['get', 'traffic_severity'],
          'severe', '#ff2200',
          'heavy', '#ff8800',
          'moderate', '#ffdd33',
          /* low / unknown */ '#00d2ff',
        ],
      },
    }, beforeId)
    map.addLayer({
      id: 'evac-dest-circle',
      type: 'circle',
      source: 'evac-destinations',
      filter,
      paint: {
        'circle-radius': 8,
        'circle-color': '#22cc44',
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 2,
      },
    }, beforeId)
    map.addLayer({
      id: 'evac-dest-label',
      type: 'symbol',
      source: 'evac-destinations',
      filter,
      layout: {
        'text-field': ['get', 'name'],
        'text-size': 12,
        'text-offset': [0, 1.4],
        'text-anchor': 'top',
        'text-allow-overlap': false,
      },
      paint: {
        'text-color': '#0a3d62',
        'text-halo-color': '#ffffff',
        'text-halo-width': 2,
      },
    })
  }

  const refreshFires = async (map) => {
    try {
      const fires = await fetchActiveFires()
      const zones = buildAlertZones(fires)
      firesRef.current = fires
      zonesRef.current = zones
      if (map.isStyleLoaded()) {
        const fireSrc = map.getSource('active-fires')
        if (fireSrc) fireSrc.setData(fires)
        else addFiresLayer(map, fires)
        const zoneSrc = map.getSource('alert-zones')
        if (zoneSrc) zoneSrc.setData(zones)
        else addAlertZonesLayer(map, zones)
      }

      // Evac routes are async per fire and shouldn't block the fires render.
      // Fire-and-forget; layer updates as soon as routes resolve.
      buildEvacRoutes(fires).then(({ routes, destinations }) => {
        routesRef.current = routes
        destsRef.current = destinations
        console.log(`[evac] ${routes.features.length}/${fires.features.length} routes resolved`)
        const apply = () => {
          const rSrc = map.getSource('evac-routes')
          const dSrc = map.getSource('evac-destinations')
          if (rSrc && dSrc) {
            rSrc.setData(routes)
            dSrc.setData(destinations)
          } else {
            addEvacLayers(map, routes, destinations)
          }
        }
        // Style may still be settling on first load — defer until idle if so.
        if (map.isStyleLoaded()) apply()
        else map.once('idle', apply)
      }).catch((e) => console.warn('evac routes failed:', e.message))
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
    for (const layer of ['fires-fill']) {
      map.on('mouseenter', layer, showFirePopup)
      map.on('mouseleave', layer, hideFirePopup)
    }

    // Alert-zone popup — population at risk + nearest evac route. These are
    // client-side stubs today (api/fires.js) and will be replaced by #9's
    // enrichment + Location Service routing once that pipeline lands.
    const zonePopup = new mapboxgl.Popup({ closeButton: false, offset: 8 })
    map.on('mouseenter', 'alert-zones-fill', (e) => {
      const fireHit = map.queryRenderedFeatures(e.point, { layers: ['fires-fill'] })
      if (fireHit.length) return  // fire popup wins when hovering the burned area
      map.getCanvas().style.cursor = 'pointer'
      const p = e.features[0].properties
      const route = routeSummaryForFire(p.fire_id)
      const trafficLabel = {
        severe: '🔴 severe traffic',
        heavy: '🟠 heavy traffic',
        moderate: '🟡 moderate traffic',
        low: '🟢 clear',
        unknown: 'traffic unknown',
      }[route?.traffic_severity] || ''
      const evacLine = route
        ? `Evac route: <strong>${route.destination}</strong> — ` +
          `${route.distance_km.toFixed(0)} km · ${Math.round(route.duration_min)} min<br/>` +
          `<span style="color:#444;font-size:12px">${trafficLabel} (live)</span>`
        : `Evac route: ${p.evacuation_route} (computing…)`
      zonePopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<strong>Alert zone — ${p.name || 'fire'}</strong><br/>` +
          `Radius: ${Number(p.alert_radius_km).toFixed(1)} km<br/>` +
          `Population at risk: ~${Number(p.population_at_risk).toLocaleString()}<br/>` +
          `${evacLine}<br/>` +
          `<span style="color:#666;font-size:11px">Stub data — #9 enrichment pending</span>`
        )
        .addTo(map)
    })
    map.on('mouseleave', 'alert-zones-fill', () => {
      map.getCanvas().style.cursor = ''
      zonePopup.remove()
    })

    // Click a fire footprint to toggle its evac route. Click again on the same
    // fire (or any empty space) to hide it. Single-fire selection only.
    map.on('click', 'fires-fill', (e) => {
      const f = e.features[0]
      if (!f?.properties?.fire_id) return
      e.originalEvent.__fireClick = true   // suppress the empty-space deselect below
      // queryRenderedFeatures returns a flat clone — find the matching feature
      // in firesRef so the panel gets the full property bag (centroid, etc.)
      const full = firesRef.current?.features?.find(
        (x) => x.properties.fire_id === f.properties.fire_id,
      ) || f
      onSelectFire((prev) => (prev?.properties?.fire_id === full.properties.fire_id ? null : full))
    })
    map.on('click', (e) => {
      if (e.originalEvent.__fireClick) return
      onSelectFire(null)
    })

    return () => {
      clearInterval(interval)
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Push the per-fire selection into the four evac layer filters whenever the
  // user clicks a different fire. Layers may not exist yet on first selection
  // (routes haven't resolved) — addEvacLayers reads the same ref to apply the
  // current filter at creation time.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const filter = filterForSelected(selectedFireId)
    for (const id of ['evac-route-casing', 'evac-route-line', 'evac-dest-circle', 'evac-dest-label']) {
      if (map.getLayer(id)) map.setFilter(id, filter)
    }
  }, [selectedFireId])

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
      if (zonesRef.current) addAlertZonesLayer(map, zonesRef.current)
      if (routesRef.current && destsRef.current) {
        addEvacLayers(map, routesRef.current, destsRef.current)
      }
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
