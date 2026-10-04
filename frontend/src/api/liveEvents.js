/**
 * The live event stream (GET /api/events/stream), shared by the whole tab.
 *
 * One EventSource per tab, however many components listen: the stream is a
 * long-lived connection the backend holds open per subscriber, so a bell, a
 * toast host and a page each opening their own would triple the server's
 * work for the same events. Components subscribe to an event type; the
 * connection opens with the first subscriber and closes shortly after the
 * last one leaves.
 *
 * The stream is a convenience, never a dependency. EventSource retries on its
 * own after a dropped connection, but a 401 or a non-stream response closes
 * it for good, and a backend without the route would make the browser retry
 * forever. So consecutive failures are counted: after a few, the stream goes
 * quiet and only tries again every few minutes, and the status flips to
 * "off" so callers can fall back to the polling they already had.
 */

import { useEffect, useRef, useState } from 'react'

const URL_STREAM = '/api/events/stream'

/** Backoff for the reconnects the browser will not do itself (closed stream). */
const RETRY_BASE_MS = 2000
const RETRY_MAX_MS = 30000
/** Consecutive failures without a successful open before the stream gives up. */
const MAX_FAILURES = 5
/** After giving up, one quiet attempt this often - a restarted backend comes back. */
const SLOW_RETRY_MS = 5 * 60 * 1000
/** Grace before closing an unused stream, so a route change does not reconnect. */
const IDLE_CLOSE_MS = 1500

const listeners = new Map() // event type -> Set of handlers
const statusListeners = new Set()
let subscriberCount = 0

let source = null
let attached = new Set() // event types with a listener on the current source
let failures = 0
let retryTimer = null
let idleTimer = null
let status = 'idle' // idle | connecting | open | off

function setStatus(next) {
  if (next === status) return
  status = next
  for (const fn of statusListeners) fn(status)
}

function dispatch(type, event) {
  const handlers = listeners.get(type)
  if (!handlers || !handlers.size) return
  let data = null
  try {
    data = event.data ? JSON.parse(event.data) : null
  } catch {
    return // a malformed frame is dropped rather than handed to a component
  }
  for (const fn of [...handlers]) {
    try {
      fn(data)
    } catch {
      /* one broken subscriber must not starve the others */
    }
  }
}

function attach(type) {
  if (!source || attached.has(type)) return
  attached.add(type)
  source.addEventListener(type, (e) => dispatch(type, e))
}

function clearTimers() {
  clearTimeout(retryTimer)
  clearTimeout(idleTimer)
  retryTimer = null
  idleTimer = null
}

function teardown() {
  if (source) {
    source.onopen = null
    source.onerror = null
    source.close()
  }
  source = null
  attached = new Set()
}

function scheduleRetry(ms) {
  clearTimeout(retryTimer)
  retryTimer = setTimeout(() => {
    retryTimer = null
    if (subscriberCount > 0) connect()
  }, ms)
}

function connect() {
  if (source || typeof window === 'undefined' || !('EventSource' in window)) {
    if (!source) setStatus('off')
    return
  }
  setStatus(failures >= MAX_FAILURES ? 'off' : 'connecting')
  source = new EventSource(URL_STREAM, { withCredentials: true })
  for (const type of listeners.keys()) attach(type)

  source.onopen = () => {
    failures = 0
    setStatus('open')
  }

  source.onerror = () => {
    failures += 1
    // CONNECTING means the browser is already retrying a dropped connection;
    // CLOSED means it will not (401, wrong content type), so retrying is ours.
    const closed = !source || source.readyState === EventSource.CLOSED
    if (failures >= MAX_FAILURES) {
      teardown()
      setStatus('off')
      scheduleRetry(SLOW_RETRY_MS)
      return
    }
    if (closed) {
      teardown()
      setStatus('connecting')
      scheduleRetry(Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** (failures - 1)))
    }
  }
}

/**
 * Listen to one event type (`hello`, `notification`, `unread`, ...). The
 * handler receives the parsed JSON payload. Returns the unsubscribe function.
 */
export function subscribe(type, handler) {
  if (!listeners.has(type)) listeners.set(type, new Set())
  listeners.get(type).add(handler)
  attach(type)
  subscriberCount += 1
  clearTimeout(idleTimer)
  idleTimer = null
  if (!source && !retryTimer) connect()

  let done = false
  return () => {
    if (done) return
    done = true
    listeners.get(type)?.delete(handler)
    subscriberCount -= 1
    if (subscriberCount <= 0) {
      subscriberCount = 0
      clearTimeout(idleTimer)
      idleTimer = setTimeout(() => {
        idleTimer = null
        if (subscriberCount === 0) closeLiveEvents()
      }, IDLE_CLOSE_MS)
    }
  }
}

/** Follow the connection state: idle | connecting | open | off. */
export function onLiveStatus(fn) {
  statusListeners.add(fn)
  fn(status)
  return () => statusListeners.delete(fn)
}

/**
 * Close the stream now and forget its failure history. Called on sign-out,
 * so the next session starts with a fresh connection under its own cookie.
 */
export function closeLiveEvents() {
  clearTimers()
  teardown()
  failures = 0
  setStatus('idle')
}

/**
 * React binding: run `handler` for every `type` event while mounted. The
 * latest handler is used without resubscribing, so callers can pass an inline
 * closure that reads current state.
 */
export function useLiveEvent(type, handler) {
  const ref = useRef(handler)
  useEffect(() => {
    ref.current = handler
  })
  useEffect(() => subscribe(type, (data) => ref.current(data)), [type])
}

/** React binding for the connection state, for callers that fall back to polling. */
export function useLiveStatus() {
  const [value, setValue] = useState(status)
  useEffect(() => onLiveStatus(setValue), [])
  return value
}
