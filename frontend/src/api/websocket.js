// Live update channel from the wildfire-watch backend.
//
// Subscribes to a JSON WebSocket and routes each message to the right
// callback. Two message types are wired up today:
//
//   { type: 'fire_updated', fire: <GeoJSON Feature> }
//      → backend has new perimeter / containment / acres for one fire.
//        Frontend patches the single feature in place (no full reload).
//
//   { type: 'alert_sent', fire_id, fire_name, alerts_sent, audit_hash }
//      → safety gate ran, audit row written, SMS dispatched. App lifts
//        this into AlertBanner state.
//
// `resource_dispatched` was intentionally dropped — see #106. Live station
// availability is out of hackathon scope.
//
// Auto-reconnect with exponential backoff (1s → 30s cap) so a transient
// blip doesn't kill live updates for the rest of the demo session. If
// VITE_WS_URL isn't set, the hook is a no-op so local dev keeps working
// off the 30s polling fallback alone.

import { useEffect, useRef } from 'react'

const WS_URL = import.meta.env.VITE_WS_URL
const INITIAL_RETRY_MS = 1_000
const MAX_RETRY_MS = 30_000

export function useFireWebSocket({ onFireUpdate, onAlertSent } = {}) {
  // Keep handlers in a ref so the effect doesn't tear down/reconnect every
  // time the parent re-renders with a new closure. We want one connection
  // per page load.
  const handlersRef = useRef({ onFireUpdate, onAlertSent })
  handlersRef.current = { onFireUpdate, onAlertSent }

  useEffect(() => {
    if (!WS_URL) {
      console.info('[ws] VITE_WS_URL not set — live updates disabled')
      return
    }

    let cancelled = false
    let ws = null
    let retryTimer = null
    let retryDelay = INITIAL_RETRY_MS

    const connect = () => {
      if (cancelled) return
      ws = new WebSocket(WS_URL)

      ws.onopen = () => {
        console.info('[ws] connected', WS_URL)
        retryDelay = INITIAL_RETRY_MS
      }

      ws.onmessage = (e) => {
        let msg
        try {
          msg = JSON.parse(e.data)
        } catch {
          console.warn('[ws] dropped non-JSON payload:', e.data?.slice?.(0, 80))
          return
        }
        const h = handlersRef.current
        switch (msg.type) {
          case 'fire_updated':
            if (msg.fire) h.onFireUpdate?.(msg.fire)
            break
          case 'alert_sent':
            h.onAlertSent?.(msg)
            break
          default:
            // Forward-compatibility — log unknown types but don't error.
            console.debug('[ws] unhandled message type:', msg.type)
        }
      }

      ws.onclose = () => {
        if (cancelled) return
        retryTimer = setTimeout(connect, retryDelay)
        retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS)
      }

      // onerror always followed by onclose; let close handle the retry.
      ws.onerror = () => ws.close()
    }

    connect()

    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
      if (ws) {
        ws.onclose = null   // suppress reconnect on intentional teardown
        ws.close()
      }
    }
  }, [])
}
