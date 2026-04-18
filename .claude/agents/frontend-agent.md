# Frontend Agent

**Owns:** React app, Mapbox GL JS map, all UI components
**Issues:** #25, #26, #27, #28, #29, #30

## Responsibilities

- Build the live fire map using Mapbox GL JS
- Implement the dispatch panel showing Bedrock advisory + confidence badge
- Build resident registration form
- Wire WebSocket for real-time map updates

## File layout

```
frontend/
├── package.json
├── src/
│   ├── App.jsx                ← root, routing
│   ├── FireMap.jsx            ← Issue #25/26/27: main map component
│   ├── DispatchPanel.jsx      ← Issue #28: sidebar with advisory + confidence
│   ├── AlertBanner.jsx        ← active alert notification strip
│   ├── SafetyBadge.jsx        ← shows QLDB status + Guardrails pass/fail
│   ├── RegisterForm.jsx       ← Issue #29: resident SMS registration
│   ├── api/
│   │   ├── fires.js           ← DynamoDB REST calls
│   │   └── websocket.js       ← Issue #30: WebSocket client
│   └── index.css
└── public/
```

## Issue #25 — Base map

Use Mapbox GL JS. Initialize with:
- Center: California (-119.5, 37.0)
- Zoom: 6
- Style: `mapbox://styles/mapbox/dark-v11` (fires pop on dark)

Add fire station markers from `data/fire_stations.geojson`. Use a fire truck icon.
Show availability status as green (available) / red (deployed) dot on each marker.

## Issue #26 — Fire perimeter overlay

Layer type: `fill` for perimeters, `line` for border.

Color by containment %:
- 0–25%: `#ff2200` (full red)
- 25–50%: `#ff6600` (orange)
- 50–75%: `#ffaa00` (amber)
- 75–100%: `#22cc44` (green — mostly contained)

Data source: REST call to API Gateway `/fires/active` every 30s, or WebSocket (#30).

## Issue #27 — Risk radius overlay

For each active fire, draw a circle overlay showing the alert radius.

```javascript
map.addLayer({
  id: `risk-radius-${fire.fire_id}`,
  type: 'fill',
  source: { type: 'geojson', data: createCircle(fire.lat, fire.lon, fire.risk_radius_km) },
  paint: {
    'fill-color': '#ff4400',
    'fill-opacity': 0.15
  }
})
```

Clicking inside the risk zone shows a popup: population at risk, nearest evacuation route.

## Issue #28 — Dispatch panel

Sidebar that appears when a fire is selected on the map.

Sections:
1. **Fire info** — location, spread rate, containment %
2. **Resources dispatched** — list of stations with ETAs
3. **Advisory** — Bedrock-generated brief text
4. **Safety badge** — confidence score (color-coded), Guardrails status (pass/fail), QLDB link
5. **Human review status** — pending / approved / auto-approved

```jsx
// SafetyBadge.jsx
function SafetyBadge({ confidence, guardrailsPassed, qldbDocId }) {
  const confidenceColor = confidence >= 0.65 ? 'green' : confidence >= 0.4 ? 'amber' : 'red'
  return (
    <div className="safety-badge">
      <span style={{ color: confidenceColor }}>Confidence: {(confidence * 100).toFixed(0)}%</span>
      <span>{guardrailsPassed ? '✓ Guardrails passed' : '⚠ Guardrails blocked'}</span>
      <a href={`/audit/${qldbDocId}`}>View audit trail →</a>
    </div>
  )
}
```

## Issue #29 — Registration form

Simple form: name, address, phone number.
On submit: call `POST /residents/register` via Cognito-authenticated API Gateway.
Show confirmation: "You will receive SMS alerts for fires within 10km of your address."

## Issue #30 — WebSocket real-time updates

Connect to API Gateway WebSocket on mount. On message:
- `fire_updated` → update the fire's GeoJSON on the map
- `alert_sent` → show AlertBanner
- `resource_dispatched` → update station marker to red

```javascript
// api/websocket.js
const ws = new WebSocket(process.env.REACT_APP_WS_URL)
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data)
  dispatch({ type: msg.type, payload: msg.data })
}
```

## Verification

```bash
cd frontend && npm start
# Open http://localhost:3000
# Verify: map loads, CA fires visible, clicking fire shows dispatch panel
```
