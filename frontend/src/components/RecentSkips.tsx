import { useCallback, useEffect, useRef, useState } from 'react'
import { parseDbTimestamp } from '../utils/dbTime'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import { ChevronLeft, ChevronRight, AlertTriangle } from 'lucide-react'

interface SkipEvent {
  id: number
  title: string
  username: string
  client_title: string
  category: string
  action: string
  position_ms: number
  success: number
  created_at: string
}

interface HistoryResponse {
  events: SkipEvent[]
  total: number
}

const PAGE_SIZE = 25

// created_at is SQLite's naive UTC timestamp; mark it as UTC so it renders local.
function formatTime(createdAt: string): string {
  return parseDbTimestamp(createdAt).toLocaleString()
}

// Date inputs are local calendar days. Send local midnight as an ISO instant so the
// server's UTC comparison lines up with the day the viewer picked; the end bound is
// the following midnight because the API treats it as exclusive.
function dayStart(day: string, offsetDays = 0): string {
  const d = new Date(`${day}T00:00:00`)
  d.setDate(d.getDate() + offsetDays)
  return d.toISOString()
}

// A local calendar day as YYYY-MM-DD, the value format of <input type="date">.
// Built from local fields: toISOString() would give the UTC day, which is
// tomorrow for evenings west of UTC.
function localDay(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// Default window: the last 7 days including today. Clear widens it to all history.
const DEFAULT_DAYS = 7

export default function RecentSkips() {
  const [data, setData] = useState<HistoryResponse>({ events: [], total: 0 })
  const [page, setPage] = useState(0)
  const [from, setFrom] = useState(() => localDay(-(DEFAULT_DAYS - 1)))
  const [to, setTo] = useState(() => localDay())
  const [error, setError] = useState('')

  // Each load aborts the previous one so an older page or date window never wins.
  const controllerRef = useRef<AbortController | null>(null)
  const load = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = (controllerRef.current = new AbortController())
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(page * PAGE_SIZE),
    })
    if (from) params.set('start', dayStart(from))
    if (to) params.set('end', dayStart(to, 1))
    try {
      const d = await api.get<HistoryResponse>(`/api/analytics/skips/history?${params}`, {
        signal: controller.signal,
      })
      setData(d)
      setError('')
    } catch (err: any) {
      if (err.name !== 'AbortError') setError('Could not load skip history.')
    }
  }, [page, from, to])

  // Reload when the page or date window changes.
  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load])

  // Live: a new skip or mute appears as soon as the filter engine records it.
  useLiveRefresh(['skip_recorded'], load)

  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))
  const setRange = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setter(e.target.value)
    setPage(0)
  }

  return (
    <section>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
          Recent skips
          <span className="ml-2 text-muted font-normal normal-case tabular-nums">({data.total})</span>
        </h2>
        <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400 sm:ml-auto">
          <label className="flex items-center gap-1">
            From
            <input
              type="date"
              aria-label="From date"
              value={from}
              max={to || undefined}
              onChange={setRange(setFrom)}
              className="bg-plex-card border border-plex-border rounded-lg px-2 py-1 text-gray-200"
            />
          </label>
          <label className="flex items-center gap-1">
            To
            <input
              type="date"
              aria-label="To date"
              value={to}
              min={from || undefined}
              onChange={setRange(setTo)}
              className="bg-plex-card border border-plex-border rounded-lg px-2 py-1 text-gray-200"
            />
          </label>
          {(from || to) && (
            <button
              className="btn-outline text-xs px-2 py-1"
              onClick={() => {
                setFrom('')
                setTo('')
                setPage(0)
              }}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {error && <p className="text-sm text-red-400 mb-2">{error}</p>}

      {data.events.length === 0 ? (
        <div className="bg-plex-card border border-plex-border rounded-xl px-4 py-5 text-muted text-sm">
          {from || to ? 'No skips in this date range' : 'No skips yet'}
        </div>
      ) : (
        // One grid for every row, so time, title, action and viewer line up down the list (#206).
        <div className="bg-plex-card border border-plex-border rounded-xl divide-y divide-plex-border">
          {data.events.map(ev => (
            <div
              key={ev.id}
              className="px-4 py-2.5 grid items-center gap-x-4 gap-y-0.5 text-sm grid-cols-[minmax(0,1fr)_auto] md:grid-cols-[11rem_minmax(0,1fr)_9rem_minmax(0,14rem)]"
            >
              <span className="text-muted tabular-nums order-3 md:order-none">{formatTime(ev.created_at)}</span>
              <span className="text-gray-200 truncate" title={ev.title}>{ev.title}</span>
              <span className="text-xs text-gray-400 flex items-center gap-1.5 justify-self-end md:justify-self-start">
                {ev.action}
                {ev.category ? ` · ${ev.category}` : ''}
                {!ev.success && (
                  <span title="The client did not confirm this action" className="flex-shrink-0">
                    <AlertTriangle size={14} className="text-yellow-500" aria-label="Not confirmed by the client" />
                  </span>
                )}
              </span>
              <span className="text-muted text-xs truncate order-4 md:order-none justify-self-end md:justify-self-auto md:text-right">
                {ev.username} · {ev.client_title}
              </span>
            </div>
          ))}
        </div>
      )}

      {pages > 1 && (
        <div className="flex items-center justify-end gap-2 mt-2 text-xs text-gray-400">
          <button
            className="btn-outline px-2 py-1 disabled:opacity-50"
            aria-label="Previous page"
            disabled={page === 0}
            onClick={() => setPage(p => p - 1)}
          >
            <ChevronLeft size={14} />
          </button>
          <span>
            Page {page + 1} of {pages}
          </span>
          <button
            className="btn-outline px-2 py-1 disabled:opacity-50"
            aria-label="Next page"
            disabled={page >= pages - 1}
            onClick={() => setPage(p => p + 1)}
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}
    </section>
  )
}
