import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { SkipForward, Clock, ChevronLeft, ChevronRight, AlertTriangle } from 'lucide-react'

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
  return new Date(createdAt.replace(' ', 'T') + 'Z').toLocaleString()
}

// Date inputs are local calendar days. Send local midnight as an ISO instant so the
// server's UTC comparison lines up with the day the viewer picked; the end bound is
// the following midnight because the API treats it as exclusive.
function dayStart(day: string, offsetDays = 0): string {
  const d = new Date(`${day}T00:00:00`)
  d.setDate(d.getDate() + offsetDays)
  return d.toISOString()
}

export default function RecentSkips() {
  const [data, setData] = useState<HistoryResponse>({ events: [], total: 0 })
  const [page, setPage] = useState(0)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [error, setError] = useState('')

  // Refetch whenever the page or date window changes, and every 5s while mounted
  // so new skips appear like the rest of the dashboard. Each tick aborts the last.
  useEffect(() => {
    let controller = new AbortController()
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(page * PAGE_SIZE),
    })
    if (from) params.set('start', dayStart(from))
    if (to) params.set('end', dayStart(to, 1))

    const load = async () => {
      controller.abort()
      controller = new AbortController()
      try {
        const d = await api.get<HistoryResponse>(`/api/analytics/skips/history?${params}`, {
          signal: controller.signal,
        })
        setData(d)
        setError('')
      } catch (err: any) {
        if (err.name !== 'AbortError') setError('Could not load skip history.')
      }
    }

    load()
    const id = setInterval(load, 5000)
    return () => {
      clearInterval(id)
      controller.abort()
    }
  }, [page, from, to])

  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))
  const setRange = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setter(e.target.value)
    setPage(0)
  }

  return (
    <section>
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <h2 className="text-lg font-semibold text-gray-200 flex items-center gap-2">
          <SkipForward size={18} className="text-plex-orange" />
          Recent Skips
          <span className="text-sm font-normal text-gray-500">({data.total})</span>
        </h2>
        <div className="flex items-center gap-2 text-xs text-gray-400 ml-auto">
          <label className="flex items-center gap-1">
            From
            <input
              type="date"
              aria-label="From date"
              value={from}
              max={to || undefined}
              onChange={setRange(setFrom)}
              className="bg-plex-card border border-plex-border rounded px-2 py-1 text-gray-200"
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
              className="bg-plex-card border border-plex-border rounded px-2 py-1 text-gray-200"
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
        <div className="bg-plex-card border border-plex-border rounded-xl p-6 text-center text-gray-500 text-sm">
          {from || to ? 'No skips in this date range' : 'No skips yet'}
        </div>
      ) : (
        <div className="bg-plex-card border border-plex-border rounded-xl divide-y divide-plex-border">
          {data.events.map(ev => (
            <div key={ev.id} className="px-4 py-3 flex items-center gap-3 text-sm">
              <Clock size={14} className="text-gray-600 flex-shrink-0" />
              <span className="text-gray-500 w-44 flex-shrink-0">{formatTime(ev.created_at)}</span>
              <span className="text-gray-300 truncate flex-1">{ev.title}</span>
              <span className="text-xs text-gray-400 flex-shrink-0">
                {ev.action}
                {ev.category ? ` · ${ev.category}` : ''}
              </span>
              {!ev.success && (
                <span title="The client did not confirm this action" className="flex-shrink-0">
                  <AlertTriangle size={14} className="text-yellow-500" />
                </span>
              )}
              <span className="text-gray-500 text-xs flex-shrink-0">
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
