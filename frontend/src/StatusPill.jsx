import { useEffect, useState } from 'react'

// Dashboard heartbeat — sits top-center showing the live fire count and the
// time since the last refresh. Re-renders every 15s so the "updated X ago"
// stays current without thrashing.

function timeAgo(ts) {
  if (!ts) return 'never'
  const sec = Math.floor((Date.now() - ts) / 1000)
  if (sec < 60) return 'just now'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  return `${hr}h ago`
}

export default function StatusPill({ fireCount, lastUpdated, theme = 'light' }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 15_000)
    return () => clearInterval(id)
  }, [])

  const dark = theme === 'dark'
  if (fireCount == null) return null

  return (
    <div style={{
      position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
      zIndex: 6,
      background: dark ? 'rgba(28, 28, 30, 0.92)' : 'rgba(255, 255, 255, 0.94)',
      color: dark ? '#e5e5e9' : '#222',
      padding: '7px 14px', borderRadius: 999,
      fontSize: 12, fontWeight: 500, letterSpacing: 0.2,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      boxShadow: dark ? '0 2px 8px rgba(0,0,0,0.5)' : '0 2px 8px rgba(0,0,0,0.18)',
      display: 'flex', alignItems: 'center', gap: 10, pointerEvents: 'none',
    }}>
      <span style={{
        display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
        background: fireCount > 0 ? '#ff3322' : '#22cc44',
        boxShadow: fireCount > 0 ? '0 0 0 0 rgba(255, 51, 34, 0.6)' : 'none',
        animation: fireCount > 0 ? 'pulse 2s ease-out infinite' : 'none',
      }} />
      <span><strong>{fireCount}</strong> active fire{fireCount === 1 ? '' : 's'}</span>
      <span style={{ color: dark ? '#888' : '#888' }}>·</span>
      <span style={{ color: dark ? '#aaa' : '#777' }}>updated {timeAgo(lastUpdated)}</span>
      <style>{`
        @keyframes pulse {
          0%   { box-shadow: 0 0 0 0 rgba(255, 51, 34, 0.6); }
          70%  { box-shadow: 0 0 0 8px rgba(255, 51, 34, 0); }
          100% { box-shadow: 0 0 0 0 rgba(255, 51, 34, 0); }
        }
      `}</style>
    </div>
  )
}
