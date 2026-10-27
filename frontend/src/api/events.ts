import { useEffect, useRef } from 'react'

/**
 * Live updates from the backend (#89). One EventSource on /api/events is shared by
 * the whole app; each section subscribes to the event types it shows and refetches
 * itself when one arrives. Events only say what changed (see cleanplex/events.py).
 */

export type LiveEventType =
  | 'scan_started'
  | 'scan_progress'
  | 'scan_finished'
  | 'scanner_changed'
  | 'segments_changed'
  | 'titles_changed'
  | 'skip_recorded'
  | 'sessions_changed'
  | 'settings_changed'
  | 'users_changed'
  | 'markers_changed'

// Sent when the stream reconnects or the server dropped this tab's backlog: events
// may have been missed, so every listener refetches regardless of its types.
export const RESYNC = 'resync'

const ALL_TYPES: LiveEventType[] = [
  'scan_started', 'scan_progress', 'scan_finished', 'scanner_changed', 'segments_changed',
  'titles_changed', 'skip_recorded', 'sessions_changed', 'settings_changed', 'users_changed',
  'markers_changed',
]

type Handler = (type: LiveEventType | typeof RESYNC, data: Record<string, any>) => void
interface Listener { types: Set<string>; handler: Handler }

const listeners = new Set<Listener>()
let source: EventSource | null = null
let openedBefore = false

function dispatch(type: LiveEventType | typeof RESYNC, data: Record<string, any>) {
  for (const l of listeners) {
    if (type === RESYNC || l.types.has(type)) l.handler(type, data)
  }
}

function connect() {
  // jsdom and very old browsers have no EventSource; pages then rely on their
  // fallback refresh alone.
  if (source || typeof EventSource === 'undefined') return
  source = new EventSource('/api/events')
  source.onopen = () => {
    if (openedBefore) dispatch(RESYNC, {})
    openedBefore = true
  }
  for (const type of [...ALL_TYPES, RESYNC] as const) {
    source.addEventListener(type, e => {
      let data: Record<string, any> = {}
      try { data = JSON.parse((e as MessageEvent).data || '{}') } catch { /* keep {} */ }
      dispatch(type, data)
    })
  }
}

/** Subscribe to some event types; returns an unsubscribe function. */
export function subscribeLive(types: LiveEventType[], handler: Handler): () => void {
  const listener: Listener = { types: new Set(types), handler }
  listeners.add(listener)
  connect()
  return () => {
    listeners.delete(listener)
    // Last subscriber gone: close the stream so an idle tab holds no connection.
    if (listeners.size === 0 && source) {
      source.close()
      source = null
      openedBefore = false
    }
  }
}

/** Call `handler` for every matching event while the component is mounted. */
export function useLiveEvent(types: LiveEventType[], handler: Handler) {
  const ref = useRef(handler)
  ref.current = handler
  const key = types.join(',')
  // Re-subscribes only when the set of types changes; the handler is read through
  // a ref so a new closure every render does not churn the subscription.
  useEffect(() => subscribeLive(key.split(',') as LiveEventType[], (t, d) => ref.current(t, d)), [key])
}

interface RefreshOptions {
  /** Ignore events this section does not show, e.g. another library's titles. */
  relevant?: (type: LiveEventType, data: Record<string, any>) => boolean
  /** Coalesce bursts (a scan writing many segments) into one refetch. */
  debounceMs?: number
  /** Safety-net refetch in case the stream is down; 0 disables it. */
  fallbackMs?: number
}

/**
 * Refetch a section when the backend reports a relevant change, after a resync,
 * and on a slow fallback timer. `refresh` should cancel its own in-flight request.
 */
export function useLiveRefresh(types: LiveEventType[], refresh: () => void, opts: RefreshOptions = {}) {
  const { debounceMs = 250, fallbackMs = 60_000 } = opts
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  const relevantRef = useRef(opts.relevant)
  relevantRef.current = opts.relevant
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useLiveEvent(types, (type, data) => {
    if (type !== RESYNC && relevantRef.current && !relevantRef.current(type, data)) return
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      timer.current = null
      refreshRef.current()
    }, debounceMs)
  })

  // Fallback timer: runs until unmount (the terminal condition), cleared below.
  useEffect(() => {
    if (!fallbackMs) return
    const id = setInterval(() => refreshRef.current(), fallbackMs)
    return () => {
      clearInterval(id)
      if (timer.current) clearTimeout(timer.current)
    }
  }, [fallbackMs])
}

/** Patch one active scan's progress from a scan_progress event without refetching. */
export function withScanProgress<T extends { active_scans: { guid: string; progress: number }[] }>(
  status: T | null,
  data: Record<string, any>,
): T | null {
  if (!status) return status
  return {
    ...status,
    active_scans: status.active_scans.map(a => (a.guid === data.guid ? { ...a, progress: data.progress } : a)),
  }
}
