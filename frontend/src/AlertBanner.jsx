import { useEffect, useState } from 'react'

// Top-of-screen banner that announces SMS alert dispatches. In the wired
// pipeline this fires off WebSocket "alert_sent" events (#30); for now App
// triggers it directly when the highest-confidence fire would have alerted.
// 8s auto-dismiss + manual close — long enough to read, short enough that
// stale banners don't clutter a long demo session.

const AUTO_DISMISS_MS = 8_000

export default function AlertBanner({ alert, onDismiss, theme = 'light' }) {
  const [visible, setVisible] = useState(false)

  // Slide-in animation hook — flip to visible the next frame so the CSS
  // transition catches the state change.
  useEffect(() => {
    if (!alert) {
      setVisible(false)
      return
    }
    const raf = requestAnimationFrame(() => setVisible(true))
    const dismiss = setTimeout(() => {
      setVisible(false)
      setTimeout(onDismiss, 250)
    }, AUTO_DISMISS_MS)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(dismiss)
    }
  }, [alert, onDismiss])

  if (!alert) return null

  const dark = theme === 'dark'
  const handleClose = () => {
    setVisible(false)
    setTimeout(onDismiss, 250)
  }

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: 'fixed', top: visible ? 0 : -80, left: 0, right: 0, zIndex: 40,
        transition: 'top 0.25s ease',
        display: 'flex', justifyContent: 'center', pointerEvents: 'none',
      }}
    >
      <div style={{
        margin: 12, maxWidth: 540, pointerEvents: 'auto',
        background: dark ? 'rgba(255, 51, 34, 0.95)' : 'rgba(255, 51, 34, 0.97)',
        color: '#fff',
        padding: '10px 14px 10px 16px', borderRadius: 8,
        boxShadow: '0 6px 20px rgba(0, 0, 0, 0.35)',
        display: 'flex', alignItems: 'center', gap: 12,
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        fontSize: 13, lineHeight: 1.4,
      }}>
        <span aria-hidden="true" style={{ fontSize: 18 }}>🔔</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, letterSpacing: 0.2 }}>SMS alerts dispatched</div>
          <div style={{ opacity: 0.95 }}>
            <strong>{alert.fire_name}</strong> · {alert.alerts_sent.toLocaleString()} resident
            {alert.alerts_sent === 1 ? '' : 's'} notified · audit {alert.audit_hash.slice(0, 10)}…
          </div>
        </div>
        <button onClick={handleClose} aria-label="Dismiss"
          style={{
            background: 'transparent', border: 'none', color: '#fff',
            fontSize: 22, lineHeight: 1, cursor: 'pointer', padding: 0,
            width: 24, height: 24, opacity: 0.9,
          }}>×</button>
      </div>
    </div>
  )
}
