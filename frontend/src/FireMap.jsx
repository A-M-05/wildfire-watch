import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { useEffect, useRef, useState } from 'react'
import { fetchActiveFires, buildAlertZones } from './api/fires'
import { buildEvacRoutes, routeSummaryForFire } from './api/evacRoutes'
import { nearestReservoir, loadReservoirs, droughtSeverity } from './api/reservoirs'
import { useFireWebSocket } from './api/websocket'

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

// Mirrors WW_CONFIDENCE_THRESHOLD from CLAUDE.md — at or above this the safety
// gate auto-dispatches, below it Step Functions pauses for a human reviewer.
// Keep these in sync; the badge color is the user-facing read of that gate.
const CONFIDENCE_THRESHOLD = 0.65

const RESERVOIR_TONES = {
  severe:   { bg: '#c62828', label: 'drought-elevated spread risk' },
  moderate: { bg: '#ef6c00', label: 'below-average storage' },
  normal:   { bg: '#2e7d32', label: 'normal' },
  unknown:  { bg: '#666',    label: 'unknown' },
}

function reservoirChipHTML(r) {
  const tone = RESERVOIR_TONES[r.drought_severity] || RESERVOIR_TONES.unknown
  return (
    `<span style="display:inline-block;background:${tone.bg};color:#fff;` +
    `padding:1px 6px;border-radius:8px;font-size:11px;font-weight:600">` +
    `${r.pct_capacity}% capacity · ${tone.label}</span>`
  )
}

function modelBadgeHTML(p) {
  // Records from CAL FIRE direct-fetch (pre-#105) won't carry these — show
  // nothing rather than a misleading "—" so the popup stays accurate.
  const risk = p.risk_score
  const conf = p.confidence
  if (risk == null && conf == null) return null
  const parts = []
  if (risk != null) parts.push(`Risk: ${Number(risk).toFixed(2)}`)
  if (conf != null) {
    const auto = Number(conf) >= CONFIDENCE_THRESHOLD
    const label = auto ? 'auto-dispatch' : 'human review pending'
    const bg = auto ? '#1f9d55' : '#d97706'
    parts.push(
      `<span style="display:inline-block;background:${bg};color:#fff;` +
      `padding:1px 6px;border-radius:8px;font-size:11px;font-weight:600;` +
      `margin-left:4px">${Number(conf).toFixed(2)} · ${label}</span>`,
    )
  }
  return parts.join(' ')
}

export default function FireMap({ selectedFire, onSelectFire, theme, onThemeChange, onFiresLoaded, onAlertSent }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const stationsRef = useRef(null)
  const firesRef = useRef(null)
  const zonesRef = useRef(null)
  const routesRef = useRef(null)
  const destsRef = useRef(null)
  const reservoirsRef = useRef(null)
  const popupRef = useRef(null)
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
  // generateId lets us drive hover styling via setFeatureState.
  const addStationsLayer = (map, data) => {
    if (map.getLayer('fire-stations-circle')) map.removeLayer('fire-stations-circle')
    if (map.getSource('fire-stations')) map.removeSource('fire-stations')

    map.addSource('fire-stations', { type: 'geojson', data, generateId: true })
    map.addLayer({
      id: 'fire-stations-circle',
      type: 'circle',
      source: 'fire-stations',
      paint: {
        // Smaller deltas + longer duration — the previous 7→10 jump landed
        // hard. 7→8.5 is enough to read as "this one" without snapping.
        'circle-radius': [
          'case', ['boolean', ['feature-state', 'hover'], false], 8.5, 7,
        ],
        'circle-color': '#22cc44',
        'circle-stroke-width': [
          'case', ['boolean', ['feature-state', 'hover'], false], 2.5, 1.5,
        ],
        'circle-stroke-color': '#ffffff',
        'circle-radius-transition': { duration: 320 },
        'circle-stroke-width-transition': { duration: 320 },
      },
    })
  }

  // Reservoir dots — a separate circle layer so the dispatcher can see *where*
  // the closest water source is, not just its name in the popup. Color is fixed
  // blue (the chip in the click popup carries the drought severity color).
  const addReservoirsLayer = (map, data) => {
    if (map.getLayer('reservoirs-circle')) map.removeLayer('reservoirs-circle')
    if (map.getSource('reservoirs')) map.removeSource('reservoirs')
    map.addSource('reservoirs', { type: 'geojson', data, generateId: true })
    map.addLayer({
      id: 'reservoirs-circle',
      type: 'circle',
      source: 'reservoirs',
      paint: {
        'circle-radius': [
          'case', ['boolean', ['feature-state', 'hover'], false], 9.5, 8,
        ],
        'circle-color': '#1e88e5',
        'circle-stroke-width': [
          'case', ['boolean', ['feature-state', 'hover'], false], 2.5, 1.5,
        ],
        'circle-stroke-color': '#ffffff',
        'circle-radius-transition': { duration: 320 },
        'circle-stroke-width-transition': { duration: 320 },
      },
    })
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
      // Reservoirs are best-effort — if the snapshot is missing the rest of
      // the map should still work. Conversion to a FeatureCollection happens
      // here (not in api/reservoirs.js) so that module stays focused on the
      // nearest-fire lookup the dispatcher panel uses.
      try {
        const reservoirs = await loadReservoirs()
        const data = {
          type: 'FeatureCollection',
          features: reservoirs.map((r) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [r.lon, r.lat] },
            properties: {
              ...r,
              drought_severity: droughtSeverity(r.pct_capacity),
            },
          })),
        }
        reservoirsRef.current = data
        addReservoirsLayer(map, data)
      } catch (e) {
        console.warn('reservoir layer skipped:', e.message)
      }
      await refreshFires(map)
    })

    const interval = setInterval(() => refreshFires(map), FIRE_REFRESH_MS)

    // Single shared popup — keeps "only one popup at a time" trivial. Each
    // click handler swaps content + position; the empty-space click closes it.
    // className is themed via popupRef so the basemap-theme effect can flip it.
    const popup = new mapboxgl.Popup({ closeButton: true, closeOnClick: false, offset: 10 })
    popupRef.current = popup
    if (theme === 'dark') popup.addClassName('ww-popup-dark')

    // Hover state per layer — drives the feature-state-based paint expressions
    // (radius pop on circles, opacity bump on fills) so you can *see* what's
    // under the cursor. Tracked per layer so a fire under a reservoir doesn't
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
      { layer: 'fires-fill',          source: 'active-fires' },
      { layer: 'alert-zones-fill',    source: 'alert-zones' },
      { layer: 'fire-stations-circle', source: 'fire-stations' },
      { layer: 'reservoirs-circle',   source: 'reservoirs' },
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

    const firePopupHTML = (p) => {
      const lines = [
        `<strong>${p.name || 'Unnamed fire'}</strong>`,
        p.location ? `${p.location}` : null,
        p.county ? `County: ${p.county}` : null,
        `Containment: ${p.containment_pct}%`,
        p.acres_burned ? `Acres burned: ${Number(p.acres_burned).toLocaleString()}` : null,
        p.spread_rate_km2_per_hr ? `Spread: ${p.spread_rate_km2_per_hr} km²/hr` : null,
        modelBadgeHTML(p),
        `<span style="color:#666;font-size:11px">Updated ${formatRelative(p.last_updated)}</span>`,
      ].filter(Boolean)
      return lines.join('<br/>')
    }

    const zonePopupHTML = (p) => {
      const route = routeSummaryForFire(p.fire_id)
      const trafficLabel = {
        severe: '🔴 severe traffic',
        heavy: '🟠 heavy traffic',
        moderate: '🟡 moderate traffic',
        low: '🟢 clear',
        unknown: 'traffic unknown',
      }[route?.traffic_severity] || ''
      const destLabel = route?.destination_type === 'shelter'
        ? `🏠 ${route.destination}`        // live FEMA-listed shelter
        : route?.destination               // metro fallback
      const shelterMeta = route?.destination_type === 'shelter'
        ? [
            route.destination_capacity ? `cap ${route.destination_capacity}` : null,
            route.destination_pet_friendly ? '🐾 pets OK' : null,
          ].filter(Boolean).join(' · ')
        : ''
      const evacLine = route
        ? `Evac route: <strong>${destLabel}</strong> — ` +
          `${route.distance_km.toFixed(0)} km · ${Math.round(route.duration_min)} min<br/>` +
          (shelterMeta ? `<span style="color:#444;font-size:12px">${shelterMeta}</span><br/>` : '') +
          `<span style="color:#444;font-size:12px">${trafficLabel} (live)</span>`
        : `Evac route: computing…`
      return (
        `<strong>Alert zone — ${p.name || 'fire'}</strong><br/>` +
        `Radius: ${Number(p.alert_radius_km).toFixed(1)} km<br/>` +
        `Population at risk: ~${Number(p.population_at_risk).toLocaleString()}<br/>` +
        `${evacLine}`
      )
    }

    const stationPopupHTML = (p) => (
      `<strong>${p.name}</strong><br/>` +
      `${p.station_id}<br/>` +
      `Units: ${p.units}`
    )

    const reservoirPopupHTML = (p) => (
      `<strong>${p.name}</strong><br/>` +
      `<span style="color:#666;font-size:11px">CDEC ${p.station} · ` +
      `${Number(p.storage_af).toLocaleString()} of ${Number(p.gross_pool_af).toLocaleString()} AF</span><br/>` +
      `<div style="margin-top:6px">${reservoirChipHTML(p)}</div>`
    )

    // Click handlers — each marks the original event so the empty-space click
    // listener below can tell "click hit a layer" from "click hit nothing."
    map.on('click', 'fire-stations-circle', (e) => {
      const f = e.features[0]
      if (!f) return
      e.originalEvent.__layerClick = true
      popup._ww_fireId = null
      popup.setLngLat(f.geometry.coordinates).setHTML(stationPopupHTML(f.properties)).addTo(map)
    })

    map.on('click', 'reservoirs-circle', (e) => {
      const f = e.features[0]
      if (!f) return
      e.originalEvent.__layerClick = true
      popup._ww_fireId = null
      popup.setLngLat(f.geometry.coordinates).setHTML(reservoirPopupHTML(f.properties)).addTo(map)
    })

    map.on('click', 'alert-zones-fill', (e) => {
      // Fire footprint sits *inside* the halo, so a click at that point hits
      // both layers. Defer to the fires-fill handler below — it both selects
      // the fire and shows the fire popup.
      const fireHit = map.queryRenderedFeatures(e.point, { layers: ['fires-fill'] })
      if (fireHit.length) return
      const f = e.features[0]
      e.originalEvent.__layerClick = true
      const baseHtml = zonePopupHTML(f.properties)
      popup.setLngLat(e.lngLat).setHTML(baseHtml).addTo(map)
      // Reservoir chip is async — patch the popup once the lookup resolves.
      const fullFire = firesRef.current?.features?.find(
        (x) => x.properties.fire_id === f.properties.fire_id,
      )
      const center = fullFire?.properties?.centroid
      if (!center) return
      const fireIdAtClick = f.properties.fire_id
      nearestReservoir(center[1], center[0]).then((r) => {
        // Bail if the user has since closed the popup or clicked a different
        // feature — otherwise we'd splat reservoir data onto an unrelated popup.
        if (!r || !popup.isOpen() || popup._ww_fireId !== fireIdAtClick) return
        const chip =
          `<div style="margin-top:6px;font-size:12px">` +
          `Nearest reservoir: <strong>${r.name}</strong> ` +
          `(${r.distance_km.toFixed(0)} km)<br/>` +
          `<div style="margin-top:2px">${reservoirChipHTML(r)}</div>` +
          `</div>`
        popup.setHTML(baseHtml + chip)
      }).catch(() => { /* chip is best-effort */ })
      popup._ww_fireId = fireIdAtClick
    })

    // Fire footprint click does double duty: drives the dispatch-panel
    // selection (existing behavior) AND opens the fire popup. Single click,
    // single popup, panel updates in lockstep.
    map.on('click', 'fires-fill', (e) => {
      // Reservoir / station dots can sit inside a fire footprint — defer to
      // them so the user can actually click a reservoir under a fire without
      // the fire layer hijacking the click.
      const pointHit = map.queryRenderedFeatures(e.point, {
        layers: ['reservoirs-circle', 'fire-stations-circle'],
      })
      if (pointHit.length) return
      const f = e.features[0]
      if (!f?.properties?.fire_id) return
      e.originalEvent.__fireClick = true
      e.originalEvent.__layerClick = true
      const full = firesRef.current?.features?.find(
        (x) => x.properties.fire_id === f.properties.fire_id,
      ) || f
      const sameFire = selectedFireIdRef.current === full.properties.fire_id
      onSelectFire(sameFire ? null : full)
      popup._ww_fireId = null
      if (sameFire) {
        popup.remove()
      } else {
        popup.setLngLat(e.lngLat).setHTML(firePopupHTML(full.properties)).addTo(map)
      }
    })

    // Empty-space click closes the popup AND deselects the fire. Layer clicks
    // mark the event so this only fires when the user genuinely clicked nothing.
    map.on('click', (e) => {
      if (e.originalEvent.__fireClick) return
      onSelectFire(null)
      if (e.originalEvent.__layerClick) return
      popup.remove()
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
  // Also drive the active-fires `selected` feature-state so the chosen fire's
  // outline thickens and its fill darkens — visual confirmation that the
  // panel and the map are in sync.
  const prevSelectedRef = useRef(null)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const filter = filterForSelected(selectedFireId)
    for (const id of ['evac-route-casing', 'evac-route-line', 'evac-dest-circle', 'evac-dest-label']) {
      if (map.getLayer(id)) map.setFilter(id, filter)
    }
    const prev = prevSelectedRef.current
    if (prev && prev !== selectedFireId && map.getSource('active-fires')) {
      map.setFeatureState({ source: 'active-fires', id: prev }, { selected: false })
    }
    if (selectedFireId && map.getSource('active-fires')) {
      map.setFeatureState({ source: 'active-fires', id: selectedFireId }, { selected: true })
    }
    prevSelectedRef.current = selectedFireId
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
    if (popupRef.current) {
      if (theme === 'dark') popupRef.current.addClassName('ww-popup-dark')
      else popupRef.current.removeClassName('ww-popup-dark')
    }
    map.once('style.load', () => {
      if (stationsRef.current) addStationsLayer(map, stationsRef.current)
      if (reservoirsRef.current) addReservoirsLayer(map, reservoirsRef.current)
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
