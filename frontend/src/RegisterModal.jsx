import { useEffect, useState } from 'react'
import { geocodeAddress, registerResident, validateRegistration } from './api/residents'

// Resident SMS-alert registration. Modal-based so it overlays the map without
// dragging React Router into the bundle for a single screen.
//
// Submission flow: validate → geocode address → POST to register stub. The
// geocode happens client-side via Mapbox so the backend doesn't need to call
// Location Service for users on a map already showing Mapbox tiles.

function tokens(theme) {
  const dark = theme === 'dark'
  return {
    overlayBg: 'rgba(0, 0, 0, 0.55)',
    panelBg: dark ? '#1c1c1f' : '#ffffff',
    panelBorder: dark ? '#2a2a2d' : '#e5e5e5',
    textPrimary: dark ? '#f1f1f3' : '#111',
    textSecondary: dark ? '#bdbdc2' : '#555',
    textMuted: dark ? '#9a9aa0' : '#666',
    textDim: dark ? '#86868c' : '#888',
    inputBg: dark ? '#26262a' : '#fff',
    inputBorder: dark ? '#3a3a3f' : '#d0d0d0',
    inputBorderFocus: '#ff5533',
    accent: '#ff5533',
    accentText: '#fff',
    successBg: dark ? '#143822' : '#e6f7ee',
    successText: dark ? '#7be0a3' : '#0e6b3a',
    errorText: '#ff5544',
    eyebrow: dark ? '#cfcfd4' : '#444',
  }
}

const initialForm = {
  name: '',
  phone: '',
  address: '',
  alert_radius_km: 10,
}

export default function RegisterModal({ open, onClose, theme = 'light' }) {
  const t = tokens(theme)
  const [form, setForm] = useState(initialForm)
  const [errors, setErrors] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  const [confirmation, setConfirmation] = useState(null)

  // Reset on open so a re-opened modal starts clean. Don't reset on close —
  // the closing animation would briefly flash the empty form.
  useEffect(() => {
    if (open) {
      setForm(initialForm)
      setErrors({})
      setSubmitError(null)
      setConfirmation(null)
    }
  }, [open])

  // Esc closes — standard modal expectation, and the onClick overlay close
  // alone is a trap for keyboard users.
  useEffect(() => {
    if (!open) return
    const handler = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null

  const update = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    setSubmitError(null)
    const phone = form.phone.trim()
    const address = form.address.trim()
    const alertRadius = Number(form.alert_radius_km)

    const errs = validateRegistration({ phone, address, alert_radius_km: alertRadius })
    if (Object.keys(errs).length) {
      setErrors(errs)
      return
    }
    setErrors({})
    setSubmitting(true)

    try {
      const geo = await geocodeAddress(address)
      const result = await registerResident({
        phone,
        lat: geo.lat,
        lon: geo.lon,
        alert_radius_km: alertRadius,
      })
      setConfirmation({
        radius_km: result.alert_radius_km,
        place: geo.place_name,
      })
    } catch (err) {
      setSubmitError(err.message || 'Registration failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: t.overlayBg,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 50, padding: 16,
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="register-title"
        style={{
          background: t.panelBg, color: t.textPrimary,
          width: '100%', maxWidth: 420, borderRadius: 8,
          border: `1px solid ${t.panelBorder}`,
          boxShadow: '0 12px 40px rgba(0, 0, 0, 0.45)',
          overflow: 'hidden',
        }}
      >
        <header style={{
          padding: '18px 20px 14px', borderBottom: `1px solid ${t.panelBorder}`,
          display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8,
        }}>
          <div>
            <div style={{ fontSize: 11, color: t.eyebrow, letterSpacing: 1, fontWeight: 600 }}>
              SMS ALERT SIGN-UP
            </div>
            <h2 id="register-title" style={{ margin: '4px 0 0', fontSize: 18 }}>
              Get notified when fire threatens your area
            </h2>
          </div>
          <button onClick={onClose} aria-label="Close" style={{
            background: 'transparent', border: 'none', fontSize: 24, lineHeight: 1,
            cursor: 'pointer', color: t.textMuted, padding: 0, width: 24, height: 24,
          }}>×</button>
        </header>

        {confirmation ? (
          <div style={{ padding: 20 }}>
            <div style={{
              background: t.successBg, color: t.successText,
              padding: '12px 14px', borderRadius: 6, fontSize: 13, lineHeight: 1.5,
            }}>
              <div style={{ fontWeight: 700, marginBottom: 4 }}>You're registered.</div>
              You'll receive SMS alerts for fires within{' '}
              <strong>{confirmation.radius_km} km</strong> of{' '}
              <strong>{confirmation.place}</strong>.
            </div>
            <div style={{ marginTop: 14, fontSize: 12, color: t.textSecondary, lineHeight: 1.5 }}>
              We never share your number. SMS only goes out when a fire passes our
              safety gate (Bedrock Guardrails + ≥65% confidence).
            </div>
            <button onClick={onClose} style={{
              marginTop: 16, width: '100%', padding: '10px 14px',
              background: t.accent, color: t.accentText, border: 'none',
              borderRadius: 5, fontSize: 14, fontWeight: 600, cursor: 'pointer',
            }}>
              Done
            </button>
          </div>
        ) : (
          <form onSubmit={submit} style={{ padding: 20, display: 'grid', gap: 14 }}>
            <Field label="Name (optional)" t={t}>
              <input
                value={form.name}
                onChange={update('name')}
                style={inputStyle(t)}
                placeholder="Jane Doe"
                autoComplete="name"
              />
            </Field>

            <Field label="Phone number" t={t} error={errors.phone}>
              <input
                value={form.phone}
                onChange={update('phone')}
                style={inputStyle(t, !!errors.phone)}
                placeholder="+15551234567"
                inputMode="tel"
                autoComplete="tel"
                required
              />
              <Hint t={t}>Include country code (E.164 format).</Hint>
            </Field>

            <Field label="Home address" t={t} error={errors.address}>
              <input
                value={form.address}
                onChange={update('address')}
                style={inputStyle(t, !!errors.address)}
                placeholder="123 Main St, San Francisco, CA"
                autoComplete="street-address"
                required
              />
              <Hint t={t}>Used to check whether your area is in a fire's alert radius.</Hint>
            </Field>

            <Field label="Alert radius" t={t} error={errors.alert_radius_km}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input
                  type="range" min={1} max={50} step={1}
                  value={form.alert_radius_km}
                  onChange={update('alert_radius_km')}
                  style={{ flex: 1, accentColor: t.accent }}
                />
                <span style={{
                  minWidth: 56, textAlign: 'right',
                  fontVariantNumeric: 'tabular-nums', fontWeight: 600, fontSize: 13,
                }}>
                  {form.alert_radius_km} km
                </span>
              </div>
              <Hint t={t}>You'll be notified for fires whose alert zone overlaps this radius.</Hint>
            </Field>

            {submitError && (
              <div style={{ color: t.errorText, fontSize: 13 }}>{submitError}</div>
            )}

            <button type="submit" disabled={submitting} style={{
              padding: '11px 14px',
              background: submitting ? t.textDim : t.accent,
              color: t.accentText, border: 'none',
              borderRadius: 5, fontSize: 14, fontWeight: 600,
              cursor: submitting ? 'wait' : 'pointer',
            }}>
              {submitting ? 'Registering…' : 'Sign up for alerts'}
            </button>
          </form>
        )}
      </div>
    </div>
  )
}

function Field({ label, t, error, children }) {
  return (
    <label style={{ display: 'grid', gap: 5, fontSize: 12 }}>
      <span style={{ color: t.textSecondary, fontWeight: 600, letterSpacing: 0.3 }}>{label}</span>
      {children}
      {error && <span style={{ color: '#ff5544', fontSize: 12 }}>{error}</span>}
    </label>
  )
}

function Hint({ t, children }) {
  return <span style={{ color: t.textDim, fontSize: 11 }}>{children}</span>
}

function inputStyle(t, hasError = false) {
  return {
    background: t.inputBg, color: t.textPrimary,
    border: `1px solid ${hasError ? '#ff5544' : t.inputBorder}`,
    borderRadius: 5, padding: '9px 11px', fontSize: 14,
    outline: 'none', width: '100%', boxSizing: 'border-box',
    fontFamily: 'inherit',
  }
}
