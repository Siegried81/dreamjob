/**
 * The shell's live notification furniture: the unread bell in the top bar and
 * the toasts that announce a new vacancy the moment it joins the ranked list.
 *
 * Both are fed by the shared event stream (api/liveEvents). The bell still
 * reads the count from `/api/monitoring/notifications` until the stream is
 * open, and polls it slowly whenever the stream is down, so it is right with
 * or without the live route. Notifications themselves stay on the Monitoring
 * screen; a toast is a pointer to the row, not a second inbox.
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { useLiveEvent, useLiveStatus } from '../api/liveEvents'
import Icon from './Icon'
import { usePolling } from './ui'

/** Fallback poll for the count while the stream is unavailable. */
const FALLBACK_POLL_MS = 60000
/** How long a toast stays before it leaves on its own. */
const TOAST_MS = 9000
/** Toasts beyond this are dropped oldest-first: a burst must not cover the screen. */
const MAX_TOASTS = 4

export function NotificationBell() {
  const [unread, setUnread] = useState(null)
  const live = useLiveStatus()

  // Read the count straight away and then slowly, but only until the stream
  // is open: from then on `hello` and `unread` keep it current. A failing
  // read leaves the bell hidden rather than showing a zero.
  const { data } = usePolling(
    () => api.get('/monitoring/notifications?limit=1'),
    FALLBACK_POLL_MS,
    { enabled: live !== 'open' },
  )
  useEffect(() => {
    if (typeof data?.unread === 'number') setUnread(data.unread)
  }, [data])

  // `hello` and `unread` carry the absolute count and always win; a bare
  // `notification` bumps it so the bell moves even if no `unread` follows.
  useLiveEvent('hello', (d) => typeof d?.unread === 'number' && setUnread(d.unread))
  useLiveEvent('unread', (d) => typeof d?.unread === 'number' && setUnread(d.unread))
  useLiveEvent('notification', () => setUnread((n) => (n == null ? n : n + 1)))

  if (unread == null) return null
  const label = unread === 1 ? '1 unread notification' : `${unread} unread notifications`
  return (
    <Link
      className={`topbar-bell${unread > 0 ? ' has-unread' : ''}`}
      to="/monitoring?tab=notifications"
      title={label}
      aria-label={label}
    >
      <Icon name="monitoring" />
      {unread > 0 && <span className="topbar-bell-count">{unread > 99 ? '99+' : unread}</span>}
    </Link>
  )
}

/** Where a new-vacancy toast points: the ranked row first, else the company. */
function toastLink(payload) {
  const p = payload || {}
  if (p.opportunity_id) return { to: `/opportunities/${p.opportunity_id}`, label: 'Open the opportunity' }
  if (p.company_id) return { to: `/companies/${p.company_id}`, label: 'Open the company' }
  return null
}

export function LiveToasts() {
  const [toasts, setToasts] = useState([])
  // A reconnect may replay recent events; one notification, one toast.
  const seen = useRef(new Set())
  const timers = useRef(new Map())

  function dismiss(id) {
    clearTimeout(timers.current.get(id))
    timers.current.delete(id)
    setToasts((list) => list.filter((t) => t.id !== id))
  }

  useLiveEvent('notification', (n) => {
    if (!n || n.kind !== 'new_vacancy' || n.id == null || seen.current.has(n.id)) return
    seen.current.add(n.id)
    setToasts((list) => [...list, n].slice(-MAX_TOASTS))
    timers.current.set(
      n.id,
      setTimeout(() => dismiss(n.id), TOAST_MS),
    )
  })

  useEffect(() => {
    const pending = timers.current
    return () => pending.forEach((t) => clearTimeout(t))
  }, [])

  if (!toasts.length) return null
  return (
    <div className="live-toasts" role="status" aria-live="polite">
      {toasts.map((t) => {
        const link = toastLink(t.payload)
        return (
          <div className="live-toast" key={t.id}>
            <span className="live-toast-icon">
              <Icon name="vacancy" />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="tiny muted">
                {t.payload?.added_to_ranked_list ? 'New vacancy in your ranked list' : 'New vacancy'}
              </div>
              <div className="live-toast-title">{t.title}</div>
              {link && (
                <Link className="small" to={link.to} onClick={() => dismiss(t.id)}>
                  {link.label}
                </Link>
              )}
            </div>
            <button
              className="btn btn-sm btn-ghost"
              aria-label="Dismiss"
              onClick={() => dismiss(t.id)}
            >
              ✕
            </button>
          </div>
        )
      })}
    </div>
  )
}
