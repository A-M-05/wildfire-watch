import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef, useState } from 'react'
import { fetchActiveFires, buildAlertZones } from './api/fires'
import { buildEvacRoutes, routeSummaryForFire, RED_CROSS_SHELTERS_LIST } from './api/evacRoutes'
import { fetchDispatchData } from './api/dispatch'
import { useFireWebSocket } from './api/websocket'

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN

const STATIONS_URL = '/data/fire_stations.geojson'
const FIRE_REFRESH_MS = 30_000

const STYLES = {
  light: 'mapbox://styles/mapbox/light-v11',
  dark: 'mapbox://styles/mapbox/dark-v11',
}

export default function FireMap({ selectedFire, onSelectFire, theme, onThemeChange, onFiresLoaded, onAlertSent }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const stationsRef = useRef(null)
  const firesRef = useRef(null)
  const zonesRef = useRef(null)
  const routesRef = useRef(null)
  const popupRef = useRef(null)
  const hoverPopupRef = useRef(null)
  // Tracks the station_ids currently flipped to {dispatched:true} so we can
  // clear them before applying the next selection's set.
  const prevDispatchedRef = useRef(new Set())
  // Monotonic token discarded by stale dispatch fetches.
  const dispatchTokenRef = useRef(0)
  const didInitTheme = useRef(false)
  const [error, setError] = useState(null)
  const setTheme = (next) => {
    onThemeChange((prev) => (typeof next === 'function' ? next(prev) : next))
  }
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
  // promoteId so we can flip a `dispatched` state by station_id when the
  // selected fire's dispatch units come back.
  const addStationsLayer = (map, data) => {
    if (map.getLayer('fire-stations-circle')) map.removeLayer('fire-stations-circle')
    if (map.getSource('fire-stations')) map.removeSource('fire-stations')

    map.addSource('fire-stations', { type: 'geojson', data, promoteId: 'station_id' })
    map.addLayer({
      id: 'fire-stations-circle',
      type: 'circle',
      source: 'fire-stations',
      paint: {
        // Dispatched stations pop a bit larger; hover bumps them more.
        'circle-radius': [
          'case',
          ['boolean', ['feature-state', 'hover'], false], 9.5,
          ['boolean', ['feature-state', 'dispatched'], false], 8.5,
          7,
        ],
        // Color encodes availability: bright green when free, muted gray
        // when committed to another incident. Dispatched-to-this-fire
        // stations get a stronger green to signal "actively responding".
        'circle-color': [
          'case',
          ['boolean', ['feature-state', 'dispatched'], false], '#1f9d55',
          ['boolean', ['get', 'available'], true], '#22cc44',
          '#9e9e9e',
        ],
        'circle-stroke-width': [
          'case',
          ['boolean', ['feature-state', 'dispatched'], false], 3,
          ['boolean', ['feature-state', 'hover'], false], 2.5,
          1.5,
        ],
        'circle-stroke-color': [
          'case',
          ['boolean', ['feature-state', 'dispatched'], false], '#0a3d62',
          '#ffffff',
        ],
        'circle-radius-transition': { duration: 320 },
        'circle-stroke-width-transition': { duration: 320 },
      },
    })
  }

  // Always-visible Red Cross shelter layer — three stacked Mapbox layers
  // (no DOM markers — those lag on zoom and jump on hover):
  //   1. red-cross-halo      pulsing ring around the designated shelter
  //   2. red-cross-anchor    small red circle as the always-visible anchor
  //                          (so the shelter is visible even if the font
  //                          glyphs haven't loaded yet)
  //   3. red-cross-marker    bold "+" text symbol drawing the cross shape
  // Designated highlight comes from setFeatureState on the source.
  const addRedCrossLayer = (map) => {
    for (const id of ['red-cross-halo', 'red-cross-anchor', 'red-cross-marker']) {
      if (map.getLayer(id)) map.removeLayer(id)
    }
    if (map.getSource('red-cross-shelters')) map.removeSource('red-cross-shelters')

    const data = {
      type: 'FeatureCollection',
      features: RED_CROSS_SHELTERS_LIST.map((s) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        properties: {
          shelter_id: s.shelter_id,
          name: s.name,
          city: s.city,
          capacity: s.capacity,
          pet_friendly: s.pet_friendly,
        },
      })),
    }
    map.addSource('red-cross-shelters', { type: 'geojson', data, promoteId: 'shelter_id' })
    // Pulsing halo only paints when the shelter is the designated evac for
    // the currently selected fire. Driven by the pulse interval in the main
    // effect (toggles radius; the 900ms transition smooths it).
    // Halo radius/opacity are driven per-frame by the rAF pulse loop; no CSS
    // transitions here (they'd fight the rAF updates and cause stutter).
    map.addLayer({
      id: 'red-cross-halo',
      type: 'circle',
      source: 'red-cross-shelters',
      paint: {
        'circle-radius': [
          'case', ['boolean', ['feature-state', 'designated'], false], 22, 0,
        ],
        'circle-color': '#c8102e',
        'circle-opacity': 0.18,
        'circle-stroke-color': '#c8102e',
        'circle-stroke-width': 1.5,
        'circle-stroke-opacity': [
          'case', ['boolean', ['feature-state', 'designated'], false], 0.6, 0,
        ],
      },
    })
    // Always-visible anchor circle. Even if the "+" symbol layer fails to
    // render (font/glyph issue), this dot still shows the shelter location.
    map.addLayer({
      id: 'red-cross-anchor',
      type: 'circle',
      source: 'red-cross-shelters',
      paint: {
        'circle-radius': [
          'case',
          ['boolean', ['feature-state', 'designated'], false], 11,
          ['boolean', ['feature-state', 'hover'], false], 10,
          8,
        ],
        'circle-color': '#c8102e',
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 2,
        'circle-radius-transition': { duration: 220 },
      },
    })
    // Cross rendered as a bold white "+" on top of the red anchor.
    map.addLayer({
      id: 'red-cross-marker',
      type: 'symbol',
      source: 'red-cross-shelters',
      layout: {
        'text-field': '+',
        'text-font': ['DIN Pro Bold', 'Arial Unicode MS Bold'],
        'text-size': [
          'case',
          ['boolean', ['feature-state', 'designated'], false], 22,
          ['boolean', ['feature-state', 'hover'], false], 20,
          16,
        ],
        'text-allow-overlap': true,
        'text-ignore-placement': true,
        'text-anchor': 'center',
        'text-offset': [0, 0.05],
      },
      paint: {
        'text-color': '#ffffff',
      },
    })
  }

  // Thin straight lines from each dispatched station to the selected fire's
  // centroid. Source is replaced wholesale on each selection change — empty
  // FeatureCollection when nothing's selected. Drawn ABOVE alert-zones-fill
  // so the tether is visible against the halo, but BELOW fire-stations-circle
  // so the station dot still owns the click.
  const addDispatchTethersLayer = (map) => {
    if (map.getLayer('dispatch-tethers')) map.removeLayer('dispatch-tethers')
    if (map.getSource('dispatch-tethers')) map.removeSource('dispatch-tethers')
    map.addSource('dispatch-tethers', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    })
    const beforeId = map.getLayer('fire-stations-circle') ? 'fire-stations-circle' : undefined
    map.addLayer({
      id: 'dispatch-tethers',
      type: 'line',
      source: 'dispatch-tethers',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#1f9d55',
        'line-width': 2.2,
        'line-opacity': 0.9,
        // Initial pattern — replaced every ~70ms by the rAF loop to march.
        'line-dasharray': [0, 4, 3],
      },
    }, beforeId)
  }

  // Fire perimeter fill + outline. Polygon footprint is scaled by acres burned
  // (see api/fires.js). Color interpolates by containment (red→green).
  // Hover darkens the fill; selection thickens the outline.
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

    // promoteId so feature-state can be set by fire_id directly — needed for
    // the selected-state hookup driven from the dispatch panel selection.
    map.addSource('active-fires', { type: 'geojson', data, promoteId: 'fire_id' })
    map.addLayer({
      id: 'fires-fill',
      type: 'fill',
      source: 'active-fires',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: {
        'fill-color': containmentColor,
        'fill-opacity': [
          'case',
          ['boolean', ['feature-state', 'selected'], false], 0.85,
          ['boolean', ['feature-state', 'hover'], false], 0.75,
          0.55,
        ],
        'fill-opacity-transition': { duration: 180 },
      },
    }, 'fire-stations-circle')
    map.addLayer({
      id: 'fires-outline',
      type: 'line',
      source: 'active-fires',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: {
        'line-color': '#000',
        'line-width': [
          'case',
          ['boolean', ['feature-state', 'selected'], false], 3,
          ['boolean', ['feature-state', 'hover'], false], 2,
          1.2,
        ],
        'line-opacity': [
          'case',
          ['boolean', ['feature-state', 'selected'], false], 0.9,
          ['boolean', ['feature-state', 'hover'], false], 0.7,
          0.4,
        ],
        'line-width-transition': { duration: 180 },
        'line-opacity-transition': { duration: 180 },
      },
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

    map.addSource('alert-zones', { type: 'geojson', data, promoteId: 'fire_id' })
    // Insert beneath fires-fill so the burned footprint sits visibly on top.
    const beforeId = map.getLayer('fires-fill') ? 'fires-fill' : 'fire-stations-circle'
    map.addLayer({
      id: 'alert-zones-fill',
      type: 'fill',
      source: 'alert-zones',
      paint: {
        'fill-color': '#ffaa00',
        'fill-opacity': [
          'case', ['boolean', ['feature-state', 'hover'], false], 0.25, 0.12,
        ],
        'fill-opacity-transition': { duration: 180 },
      },
    }, beforeId)
    map.addLayer({
      id: 'alert-zones-outline',
      type: 'line',
      source: 'alert-zones',
      paint: {
        'line-color': '#ff7700',
        'line-width': [
          'case', ['boolean', ['feature-state', 'hover'], false], 2.5, 1.5,
        ],
        'line-opacity': [
          'case', ['boolean', ['feature-state', 'hover'], false], 1, 0.7,
        ],
        'line-dasharray': [2, 2],
        'line-width-transition': { duration: 180 },
        'line-opacity-transition': { duration: 180 },
      },
    }, beforeId)
  }

  // Evac route polylines drawn ABOVE the alert-zone halo so the corridor reads
  // clearly, but BELOW stations and the always-visible Red Cross markers so
  // those stay clickable. Two stacked line layers (dark casing + bright fill)
  // give a road-style appearance that contrasts against both basemaps.
  // The route's terminus marker comes from the always-visible
  // `red-cross-shelters` layer with feature-state.designated set, so we no
  // longer paint a per-route shelter circle here.
  const addEvacLayers = (map, routes) => {
    for (const id of ['evac-route-casing', 'evac-route-line']) {
      if (map.getLayer(id)) map.removeLayer(id)
    }
    if (map.getSource('evac-routes')) map.removeSource('evac-routes')

    map.addSource('evac-routes', { type: 'geojson', data: routes })

    // DOM markers (mapboxgl.Marker) sit above ALL Mapbox layers anyway, so we
    // just need to insert routes above the halo / under the stations.
    const beforeId =
      map.getLayer('red-cross-halo') ? 'red-cross-halo'
      : map.getLayer('fire-stations-circle') ? 'fire-stations-circle'
      : undefined
    // Both line layers honor the same per-fire filter so showing or hiding
    // a route is a single-state toggle.
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
  }

  const refreshFires = async (map) => {
    try {
      const fires = await fetchActiveFires()
      const zones = buildAlertZones(fires)
      firesRef.current = fires
      zonesRef.current = zones
      if (onFiresLoaded) onFiresLoaded(fires)
      // Style may report not-loaded mid-flight (Mapbox quirk after addLayer
      // calls). Defer the paint to the next idle tick rather than dropping
      // the data — otherwise fires only appear on the second 30s refresh.
      const applyFires = () => {
        const fireSrc = map.getSource('active-fires')
        if (fireSrc) fireSrc.setData(fires)
        else addFiresLayer(map, fires)
        const zoneSrc = map.getSource('alert-zones')
        if (zoneSrc) zoneSrc.setData(zones)
        else addAlertZonesLayer(map, zones)
      }
      if (map.isStyleLoaded()) applyFires()
      else map.once('idle', applyFires)

      // Evac routes are async per fire and shouldn't block the fires render.
      // Fire-and-forget; layer updates as soon as routes resolve.
      buildEvacRoutes(fires).then(({ routes }) => {
        routesRef.current = routes
        console.log(`[evac] ${routes.features.length}/${fires.features.length} routes resolved`)
        const apply = () => {
          const rSrc = map.getSource('evac-routes')
          if (rSrc) rSrc.setData(routes)
          else addEvacLayers(map, routes)
          // A user may have selected a fire BEFORE its route resolved — in
          // which case the selection effect ran with no route to point at and
          // didn't designate any shelter. Re-run the designation now.
          const sel = selectedFireIdRef.current
          if (sel && map.getSource('red-cross-shelters')) {
            const route = routeSummaryForFire(sel)
            const nextDesignated = route?.destination_shelter_id ?? null
            if (nextDesignated) {
              map.setFeatureState(
                { source: 'red-cross-shelters', id: nextDesignated },
                { designated: true },
              )
              prevDesignatedRef.current = nextDesignated
            }
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

  // Live patch from a `fire_updated` WebSocket message — replace the matching
  // feature in firesRef (or append if it's a brand-new fire), recompute alert
  // zones from the patched collection, and push both back to Mapbox without a
  // full reload. Falls through silently if the map isn't ready yet (the next
  // 30s polling refresh will pick it up).
  const patchFire = (feature) => {
    const id = feature?.properties?.fire_id
    const map = mapRef.current
    if (!id || !map) return
    const current = firesRef.current?.features || []
    const idx = current.findIndex((f) => f.properties?.fire_id === id)
    const nextFeatures = idx >= 0
      ? current.map((f, i) => (i === idx ? feature : f))
      : [...current, feature]
    const fires = { type: 'FeatureCollection', features: nextFeatures }
    const zones = buildAlertZones(fires)
    firesRef.current = fires
    zonesRef.current = zones
    if (onFiresLoaded) onFiresLoaded(fires)
    const fireSrc = map.getSource('active-fires')
    const zoneSrc = map.getSource('alert-zones')
    if (fireSrc) fireSrc.setData(fires)
    if (zoneSrc) zoneSrc.setData(zones)
  }

  useFireWebSocket({
    onFireUpdate: patchFire,
    onAlertSent: (msg) => onAlertSent?.(msg),
  })

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
      // Always-visible Red Cross shelter layer — independent of which fires
      // are routed where. Designated highlighting is driven later via
      // setFeatureState in the selection effect.
      addRedCrossLayer(map)
      // Empty-on-init tethers source; populated when a fire is selected.
      addDispatchTethersLayer(map)
      await refreshFires(map)
    })

    const interval = setInterval(() => refreshFires(map), FIRE_REFRESH_MS)

    // Per-frame animations driven by a single rAF loop:
    //   - Red Cross halo: sine-wave pulse (radius grows / opacity fades).
    //   - Dispatch tethers: marching-ants dash that flows station → fire.
    // (One rAF instead of two so the browser only schedules one paint per
    // frame for these animated layers.)
    const PULSE_PERIOD_MS = 1800
    const PULSE_RADIUS_MIN = 18
    const PULSE_RADIUS_MAX = 34
    const PULSE_OPACITY_MIN = 0.05
    const PULSE_OPACITY_MAX = 0.32
    // Canonical Mapbox marching-ants dash sequence — each step shifts the dash
    // forward by 0.5 units. Cycling through gives a continuous flow in the
    // LineString's direction. Tether features are built [station, fire] so
    // forward motion = "from station toward the fire" (the dispatch direction).
    const DASH_SEQUENCE = [
      [0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5],
      [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0],
      [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5],
      [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5],
    ]
    const DASH_STEP_MS = 70  // smaller = faster march
    const pulseStart = performance.now()
    let pulseRaf = null
    let lastDashStep = -1
    const tick = (now) => {
      pulseRaf = requestAnimationFrame(tick)
      // Halo pulse
      if (map.getLayer('red-cross-halo')) {
        const phase = (Math.sin(((now - pulseStart) / PULSE_PERIOD_MS) * Math.PI * 2) + 1) / 2
        const radius = PULSE_RADIUS_MIN + phase * (PULSE_RADIUS_MAX - PULSE_RADIUS_MIN)
        const opacity = PULSE_OPACITY_MAX - phase * (PULSE_OPACITY_MAX - PULSE_OPACITY_MIN)
        map.setPaintProperty('red-cross-halo', 'circle-radius', [
          'case', ['boolean', ['feature-state', 'designated'], false], radius, 0,
        ])
        map.setPaintProperty('red-cross-halo', 'circle-opacity', [
          'case', ['boolean', ['feature-state', 'designated'], false], opacity, 0,
        ])
      }
      // Dispatch tether marching ants (skip the setPaintProperty when the step
      // hasn't changed — Mapbox would still re-validate the value otherwise).
      if (map.getLayer('dispatch-tethers')) {
        const step = Math.floor(now / DASH_STEP_MS) % DASH_SEQUENCE.length
        if (step !== lastDashStep) {
          map.setPaintProperty('dispatch-tethers', 'line-dasharray', DASH_SEQUENCE[step])
          lastDashStep = step
        }
      }
    }
    pulseRaf = requestAnimationFrame(tick)

    // Single shared popup — keeps "only one popup at a time" trivial. Each
    // click handler swaps content + position; the empty-space click closes it.
    // className is themed via popupRef so the basemap-theme effect can flip it.
    const popup = new mapboxgl.Popup({ closeButton: true, closeOnClick: false, offset: 10 })
    popupRef.current = popup
    if (theme === 'dark') popup.addClassName('ww-popup-dark')

    // Hover state per layer — drives the feature-state-based paint expressions
    // (radius pop on circles, opacity bump on fills) so you can *see* what's
    // under the cursor. Tracked per layer so a fire under a station doesn't
    // leave the fire stuck in hover when the cursor moves to the dot.
    const hovered = { /* layer → { source, id } */ }
    const setHover = (layer, sourceId, featureId) => {
      const prev = hovered[layer]
      if (prev && prev.id === featureId) return
      if (prev) map.setFeatureState({ source: prev.source, id: prev.id }, { hover: false })
      map.setFeatureState({ source: sourceId, id: featureId }, { hover: true })
      hovered[layer] = { source: sourceId, id: featureId }
    }
    const clearHover = (layer) => {
      const prev = hovered[layer]
      if (!prev) return
      map.setFeatureState({ source: prev.source, id: prev.id }, { hover: false })
      hovered[layer] = null
    }

    const HOVER_LAYERS = [
      { layer: 'fires-fill',           source: 'active-fires' },
      { layer: 'alert-zones-fill',     source: 'alert-zones' },
      { layer: 'fire-stations-circle', source: 'fire-stations' },
      { layer: 'red-cross-anchor',     source: 'red-cross-shelters' },
    ]
    for (const { layer, source } of HOVER_LAYERS) {
      map.on('mousemove', layer, (e) => {
        map.getCanvas().style.cursor = 'pointer'
        const f = e.features[0]
        if (f?.id != null) setHover(layer, source, f.id)
      })
      map.on('mouseleave', layer, () => {
        map.getCanvas().style.cursor = ''
        clearHover(layer)
      })
    }

    // Hover popup — small, no close button, never blocks clicks. Distinct from
    // the click-driven `popup` (used for fire-station chips) so we can have
    // both onscreen if needed.
    const hoverPopup = new mapboxgl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 10,
    })
    hoverPopupRef.current = hoverPopup
    if (theme === 'dark') hoverPopup.addClassName('ww-popup-dark')

    const containmentTone = (pct) => {
      if (pct >= 100) return { color: '#1f9d55', label: 'fully contained' }
      if (pct >= 70) return { color: '#1f9d55', label: 'mop-up phase' }
      if (pct >= 30) return { color: '#d97706', label: 'partial containment' }
      return { color: '#c62828', label: 'active suppression' }
    }

    // One-line summary used by the fire and alert-zone hover popups. Kept
    // tight so it reads as a tooltip, not a panel.
    const fireHoverHTML = (p) => {
      const pct = Number(p.containment_pct ?? 0)
      const tone = containmentTone(pct)
      const acresLine = p.acres_burned
        ? `${Number(p.acres_burned).toLocaleString()} acres · `
        : ''
      return (
        `<strong>${p.name || 'Unnamed fire'}</strong><br/>` +
        `<span style="font-size:12px;color:#444">${acresLine}` +
        `<span style="color:${tone.color};font-weight:600">${pct}% contained</span>` +
        ` · ${tone.label}</span>`
      )
    }

    const stationPopupHTML = (p) => {
      const available = p.available !== false
      const color = available ? '#1f9d55' : '#777'
      const label = available ? 'Available' : 'Committed — unavailable'
      return (
        `<strong>${p.name}</strong><br/>` +
        `${p.station_id}<br/>` +
        `Units: ${p.units}<br/>` +
        `Availability: <span style="color:${color};font-weight:600">${label}</span>`
      )
    }

    const shelterHoverHTML = (p) => {
      const meta = [
        p.capacity ? `cap ${Number(p.capacity).toLocaleString()}` : null,
        p.pet_friendly ? '🐾 pets OK' : null,
      ].filter(Boolean).join(' · ')
      // If a fire is currently selected and *this* shelter is its designated
      // evac, append the live distance / ETA / traffic on the second line.
      const route = selectedFireIdRef.current
        ? routeSummaryForFire(selectedFireIdRef.current)
        : null
      const designated = route && route.destination_shelter_id === p.shelter_id
      const routeLine = designated
        ? `<span style="font-size:12px;color:#1b5e20;font-weight:600">` +
          `Designated evac · ${Number(route.distance_km).toFixed(0)} km · ` +
          `${Math.round(route.duration_min)} min</span><br/>`
        : ''
      return (
        `<strong style="color:#c8102e">✚ ${p.name}</strong>` +
        (p.city ? ` <span style="color:#666;font-size:11px">${p.city}</span>` : '') +
        `<br/>` +
        routeLine +
        (meta ? `<span style="font-size:11px;color:#444">${meta}</span>` : '')
      )
    }

    // Helper: open hover popup for a fire/halo feature, but only if the fire
    // is not already the selected one (selecting a fire opens the dispatch
    // panel, which carries everything the hover bubble would say).
    const showFireHover = (lngLat, fireId, props) => {
      if (selectedFireIdRef.current === fireId) {
        hoverPopup.remove()
        return
      }
      hoverPopup.setLngLat(lngLat).setHTML(fireHoverHTML(props)).addTo(map)
    }

    // Hover popups — fires + their alert halos share one bubble. Looking up
    // the fire's properties from firesRef ensures the halo (which only has
    // a thin set of zone props) gets the same name + acreage + containment.
    map.on('mousemove', 'fires-fill', (e) => {
      const p = e.features[0]?.properties
      if (!p?.fire_id) return
      showFireHover(e.lngLat, p.fire_id, p)
    })
    map.on('mouseleave', 'fires-fill', () => hoverPopup.remove())

    map.on('mousemove', 'alert-zones-fill', (e) => {
      const p = e.features[0]?.properties
      if (!p?.fire_id) return
      // Defer to the fire footprint when both are under the cursor — its
      // popup is already showing and we'd just be duplicating it.
      const onFire = map.queryRenderedFeatures(e.point, {
        layers: ['fires-fill', 'fire-stations-circle', 'red-cross-anchor'],
      })
      if (onFire.length) return
      const full = firesRef.current?.features?.find(
        (x) => x.properties.fire_id === p.fire_id,
      )
      showFireHover(e.lngLat, p.fire_id, full?.properties || p)
    })
    map.on('mouseleave', 'alert-zones-fill', () => hoverPopup.remove())

    // Shelter hover label — always-visible layer; route/ETA only appears in
    // the popup when the hovered shelter is the designated evac for the
    // currently selected fire (logic lives inside shelterHoverHTML).
    map.on('mousemove', 'red-cross-anchor', (e) => {
      const f = e.features[0]
      if (!f) return
      hoverPopup.setLngLat(f.geometry.coordinates).setHTML(shelterHoverHTML(f.properties)).addTo(map)
    })
    map.on('mouseleave', 'red-cross-anchor', () => hoverPopup.remove())

    // Click handlers — each marks the original event so the empty-space click
    // listener below can tell "click hit a layer" from "click hit nothing."
    map.on('click', 'fire-stations-circle', (e) => {
      const f = e.features[0]
      if (!f) return
      e.originalEvent.__layerClick = true
      popup._ww_fireId = null
      popup.setLngLat(f.geometry.coordinates).setHTML(stationPopupHTML(f.properties)).addTo(map)
    })

    // Selecting a fire — works on both the footprint and the alert halo.
    // Opens the dispatch panel (no popup); selectedFire deselects on toggle.
    // Suppresses the hover popup since it would duplicate the panel.
    const selectFireFromFeature = (e, fireId) => {
      if (!fireId) return
      const full = firesRef.current?.features?.find(
        (x) => x.properties.fire_id === fireId,
      )
      if (!full) return
      e.originalEvent.__fireClick = true
      e.originalEvent.__layerClick = true
      const sameFire = selectedFireIdRef.current === fireId
      onSelectFire(sameFire ? null : full)
      hoverPopup.remove()
    }

    map.on('click', 'fires-fill', (e) => {
      // Station / shelter dots can sit on top of the fire — defer so they
      // remain clickable.
      const pointHit = map.queryRenderedFeatures(e.point, {
        layers: ['fire-stations-circle', 'red-cross-anchor'],
      })
      if (pointHit.length) return
      selectFireFromFeature(e, e.features[0]?.properties?.fire_id)
    })

    map.on('click', 'alert-zones-fill', (e) => {
      // Defer to fires-fill (covered by its own click handler) and to the
      // overlapping point markers, otherwise the halo would steal the click.
      const blockingHit = map.queryRenderedFeatures(e.point, {
        layers: ['fires-fill', 'fire-stations-circle', 'red-cross-anchor'],
      })
      if (blockingHit.length) return
      selectFireFromFeature(e, e.features[0]?.properties?.fire_id)
    })

    // Empty-space click closes any popup AND deselects the fire. Layer clicks
    // mark the event so this only fires when the user genuinely clicked nothing.
    map.on('click', (e) => {
      if (e.originalEvent.__fireClick) return
      onSelectFire(null)
      if (e.originalEvent.__layerClick) return
      popup.remove()
      hoverPopup.remove()
    })

    return () => {
      clearInterval(interval)
      if (pulseRaf) cancelAnimationFrame(pulseRaf)
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Selection drives three things:
  //   1. The evac route polylines are filtered to only show the selected fire's
  //      route (so the map doesn't paint every fire's corridor at once).
  //   2. The selected fire's footprint paints darker / outline thickens.
  //   3. The Red Cross shelter that's the designated evac for the selected
  //      fire gets `designated: true` feature-state, which paints the halo and
  //      enlarges the cross icon. All other shelters stay at their resting size.
  // Layers may not exist yet on first selection (routes haven't resolved) —
  // addEvacLayers reads the same ref to apply the current filter at creation
  // time, and a route-resolved tick later will trigger a re-render.
  const prevSelectedRef = useRef(null)
  const prevDesignatedRef = useRef(null)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const filter = filterForSelected(selectedFireId)
    for (const id of ['evac-route-casing', 'evac-route-line']) {
      if (map.getLayer(id)) map.setFilter(id, filter)
    }
    const prev = prevSelectedRef.current
    if (prev && prev !== selectedFireId && map.getSource('active-fires')) {
      map.setFeatureState({ source: 'active-fires', id: prev }, { selected: false })
    }
    if (selectedFireId && map.getSource('active-fires')) {
      map.setFeatureState({ source: 'active-fires', id: selectedFireId }, { selected: true })
    }

    // Designate the matching shelter (if the route has resolved by now).
    // Routes are async, so the first selection click may run before the route
    // exists — App will trigger a re-render once routes resolve, but the panel
    // selection is the same, so we listen for `sourcedata` on red-cross-shelters
    // below to retry the designation in that case.
    const prevDesignated = prevDesignatedRef.current
    if (prevDesignated && map.getSource('red-cross-shelters')) {
      map.setFeatureState(
        { source: 'red-cross-shelters', id: prevDesignated },
        { designated: false },
      )
    }
    let nextDesignated = null
    if (selectedFireId) {
      const route = routeSummaryForFire(selectedFireId)
      nextDesignated = route?.destination_shelter_id ?? null
      if (nextDesignated && map.getSource('red-cross-shelters')) {
        map.setFeatureState(
          { source: 'red-cross-shelters', id: nextDesignated },
          { designated: true },
        )
      }
    }
    prevDesignatedRef.current = nextDesignated

    // Dispatched stations: clear last selection's highlights, then fetch the
    // current selection's dispatch_units and (1) flip `dispatched` state on
    // each station and (2) draw a thin line from each station to the fire.
    // The fetch is async; if the user changes selection before it resolves we
    // bail via a token check so we don't paint stale state.
    const prevDispatched = prevDispatchedRef.current
    if (prevDispatched?.size && map.getSource('fire-stations')) {
      for (const id of prevDispatched) {
        map.setFeatureState({ source: 'fire-stations', id }, { dispatched: false })
      }
    }
    const tetherSrc = map.getSource('dispatch-tethers')
    if (tetherSrc) tetherSrc.setData({ type: 'FeatureCollection', features: [] })
    prevDispatchedRef.current = new Set()

    if (selectedFire) {
      const token = ++dispatchTokenRef.current
      fetchDispatchData(selectedFire).then((data) => {
        if (token !== dispatchTokenRef.current) return  // stale
        if (!map.getSource('fire-stations') || !map.getSource('dispatch-tethers')) return
        const center = selectedFire.properties?.centroid
        const ids = new Set()
        const lines = []
        for (const u of data?.dispatched_units || []) {
          if (!u.station_id) continue
          // Look up the station's source feature so we can (a) terminate the
          // tether on its real coords and (b) skip stations that are already
          // committed to another incident — drawing a tether would imply they
          // were available when the dispatch panel + dot color say otherwise.
          const feat = stationsRef.current?.features?.find(
            (f) => f.properties.station_id === u.station_id,
          )
          if (!feat) continue
          if (feat.properties.available === false) continue
          map.setFeatureState(
            { source: 'fire-stations', id: u.station_id },
            { dispatched: true },
          )
          ids.add(u.station_id)
          if (center) {
            lines.push({
              type: 'Feature',
              geometry: {
                type: 'LineString',
                coordinates: [feat.geometry.coordinates, center],
              },
              properties: { station_id: u.station_id },
            })
          }
        }
        prevDispatchedRef.current = ids
        map.getSource('dispatch-tethers').setData({
          type: 'FeatureCollection',
          features: lines,
        })
      }).catch((e) => console.warn('dispatch fetch failed:', e.message))
    }

    // Selection makes the dispatch panel authoritative — hide any leftover
    // hover bubble so we don't double up on info.
    if (selectedFireId) hoverPopupRef.current?.remove()
    prevSelectedRef.current = selectedFireId
  }, [selectedFireId, selectedFire])

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
    for (const ref of [popupRef, hoverPopupRef]) {
      const p = ref.current
      if (!p) continue
      if (theme === 'dark') p.addClassName('ww-popup-dark')
      else p.removeClassName('ww-popup-dark')
    }
    map.once('style.load', () => {
      if (stationsRef.current) addStationsLayer(map, stationsRef.current)
      addRedCrossLayer(map)
      addDispatchTethersLayer(map)
      if (firesRef.current) addFiresLayer(map, firesRef.current)
      if (zonesRef.current) addAlertZonesLayer(map, zonesRef.current)
      if (routesRef.current) addEvacLayers(map, routesRef.current)
    })
  }, [theme])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100vh' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      <button
        onClick={() => setTheme((t) => (t === 'light' ? 'dark' : 'light'))}
        aria-label={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
        title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
        style={{
          position: 'absolute', top: 12, left: 12, width: 36, height: 36,
          background: theme === 'light' ? '#1a1a1a' : '#f5f5f5',
          color: theme === 'light' ? '#f5f5f5' : '#1a1a1a',
          border: 'none', borderRadius: '50%', cursor: 'pointer',
          padding: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          boxShadow: '0 2px 6px rgba(0,0,0,0.25)',
          transition: 'transform 0.25s ease',
          zIndex: 6,
        }}
        onMouseEnter={(e) => (e.currentTarget.style.transform = 'rotate(20deg)')}
        onMouseLeave={(e) => (e.currentTarget.style.transform = 'rotate(0)')}
      >
        {theme === 'light' ? <MoonIcon /> : <SunIcon />}
      </button>
      {error && (
        <div style={{
          position: 'absolute', top: 56, left: 12, padding: '8px 12px',
          background: 'rgba(180, 30, 30, 0.9)', color: '#fff', borderRadius: 4,
          fontFamily: 'monospace', fontSize: 13,
        }}>
          {error}
        </div>
      )}
    </div>
  )
}

function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"
      aria-hidden="true">
      <path d="M20.5 14.3a8 8 0 1 1-10.8-10.8 8 8 0 0 0 10.8 10.8z" />
    </svg>
  )
}
