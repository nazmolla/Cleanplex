import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Monitor, SkipForward, Clock, Wifi, WifiOff } from 'lucide-react'

interface Session {
  session_key: string
  user: string
  title: string
  media_type: string
  position_ms: number
  duration_ms: number
  client: string
  is_controllable: boolean
  filtering_enabled: boolean
  thumb_url: string
}

interface SkipEvent {
  time: string
  user: string
  title: string
  position_ms: number
  client: string
}

interface ScannerStatus {
  queue_size: number
  current_scan: string | null
  current_title: string | null
  current_progress: number
  paused: boolean
}

function msToTime(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  return `${m}:${String(sec).padStart(2, '0')}`
}

export default function Dashboard() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [events, setEvents] = useState<SkipEvent[]>([])
  const [scanner, setScanner] = useState<ScannerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [skipLoadingKey, setSkipLoadingKey] = useState<string | null>(null)
  const [skipScanLoading, setSkipScanLoading] = useState(false)

  const refresh = async () => {
    try {
      const [s, e, sc] = await Promise.all([
        api.get<{ sessions: Session[] }>('/api/sessions'),
        api.get<{ events: SkipEvent[] }>('/api/sessions/events'),
        api.get<ScannerStatus>('/api/sessions/scanner-status'),
      ])
      setSessions(s.sessions)
      setEvents(e.events)
      setScanner(sc)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [])

  const skipNow = async (sessionKey: string) => {
    try {
      setSkipLoadingKey(sessionKey)
      await api.post(`/api/sessions/${sessionKey}/skip`)
      await refresh()
    } catch (err: any) {
      alert(`Skip failed: ${err.message || 'Unknown error'}`)
    } finally {
      setSkipLoadingKey(null)
    }
  }

  const skipCurrentScan = async () => {
    try {
      setSkipScanLoading(true)
      await api.post('/api/scan/skip-current')
      await refresh()
    } catch (err: any) {
      alert(`Skip scan failed: ${err.message || 'Unknown error'}`)
    } finally {
      setSkipScanLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-100">Dashboard</h1>

      {/* Scanner status bar */}
      {scanner && (
        <div className="bg-plex-card border border-plex-border rounded-xl p-4 space-y-3">
          <div className="flex items-center gap-4 text-sm">
            <div className={`w-2 h-2 rounded-full flex-shrink-0 ${scanner.paused ? 'bg-yellow-500' : 'bg-green-500 animate-pulse'}`} />
            <span className="text-gray-400">
              Scanner:{' '}
              <span className={scanner.paused ? 'text-yellow-400' : 'text-green-400'}>
                {scanner.paused ? 'Paused (outside scan window)' : 'Active'}
              </span>
            </span>
            {scanner.queue_size > 0 && (
              <span className="text-gray-500 ml-auto">Queue: <span className="text-gray-300">{scanner.queue_size}</span></span>
            )}
          </div>
          {scanner.current_title && (
            <div>
              <div className="flex items-center justify-between text-xs mb-1.5">
                <span className="text-plex-orange flex items-center gap-1.5">
                  <span className="animate-pulse">●</span> Scanning
                </span>
                <span className="text-gray-300 truncate mx-3 flex-1">{scanner.current_title}</span>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className="text-gray-400">{Math.round(scanner.current_progress * 100)}%</span>
                  <button
                    className="btn-outline text-xs px-2 py-0.5 disabled:opacity-50"
                    onClick={skipCurrentScan}
                    disabled={skipScanLoading}
                    title="Skip this title and move to the next"
                  >
                    {skipScanLoading ? 'Skipping...' : 'Skip'}
                  </button>
                </div>
              </div>
              <div className="h-1.5 bg-plex-border rounded-full overflow-hidden">
                <div
                  className="h-full bg-plex-orange rounded-full transition-all duration-1000"
                  style={{ width: `${scanner.current_progress * 100}%` }}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {/* Active sessions */}
      <section>
        <h2 className="text-lg font-semibold text-gray-200 mb-3 flex items-center gap-2">
          <Monitor size={18} className="text-plex-orange" />
          Active Streams
        </h2>
        {loading ? (
          <div className="text-gray-500 text-sm">Loading...</div>
        ) : sessions.length === 0 ? (
          <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500">
            No active streams
          </div>
        ) : (
          <div className="grid gap-4">
            {sessions.map(s => (
              <div key={s.session_key} className="bg-plex-card border border-plex-border rounded-xl p-4 flex gap-4">
                {s.thumb_url && (
                  <img
                    src={s.thumb_url}
                    alt=""
                    className="w-16 h-24 object-cover rounded-lg flex-shrink-0 bg-plex-border"
                    onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-start justify-between gap-2">
                    <p className="font-medium text-gray-100 truncate">{s.title}</p>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {s.is_controllable
                        ? <span className="flex items-center gap-1 text-xs text-green-400 bg-green-400/10 px-2 py-0.5 rounded-full"><Wifi size={11} />Controllable</span>
                        : <span className="flex items-center gap-1 text-xs text-gray-500 bg-white/5 px-2 py-0.5 rounded-full"><WifiOff size={11} />Not controllable</span>
                      }
                      {s.filtering_enabled
                        ? <span className="text-xs text-plex-orange bg-plex-orange/10 px-2 py-0.5 rounded-full">Filtering ON</span>
                        : <span className="text-xs text-gray-500 bg-white/5 px-2 py-0.5 rounded-full">Filtering OFF</span>
                      }
                    </div>
                  </div>
                  <p className="text-sm text-gray-400 mt-1">{s.user} · {s.client}</p>
                  <div className="mt-2">
                    <div className="flex justify-between text-xs text-gray-500 mb-1">
                      <span>{msToTime(s.position_ms)}</span>
                      <span>{msToTime(s.duration_ms)}</span>
                    </div>
                    <div className="h-1.5 bg-plex-border rounded-full overflow-hidden">
                      <div
                        className="h-full bg-plex-orange rounded-full transition-all"
                        style={{ width: `${s.duration_ms ? (s.position_ms / s.duration_ms) * 100 : 0}%` }}
                      />
                    </div>
                  </div>
                  <div className="mt-3 flex justify-end">
                    <button
                      className="btn-outline text-xs px-3 py-1.5 disabled:opacity-50"
                      disabled={!s.is_controllable || skipLoadingKey === s.session_key}
                      onClick={() => skipNow(s.session_key)}
                      title={s.is_controllable ? 'Skip current title segment' : 'Session is not controllable'}
                    >
                      {skipLoadingKey === s.session_key ? 'Skipping...' : 'Skip'}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Recent skip events */}
      <section>
        <h2 className="text-lg font-semibold text-gray-200 mb-3 flex items-center gap-2">
          <SkipForward size={18} className="text-plex-orange" />
          Recent Skips
        </h2>
        {events.length === 0 ? (
          <div className="bg-plex-card border border-plex-border rounded-xl p-6 text-center text-gray-500 text-sm">
            No skips yet
          </div>
        ) : (
          <div className="bg-plex-card border border-plex-border rounded-xl divide-y divide-plex-border">
            {events.slice(0, 10).map((ev, i) => (
              <div key={i} className="px-4 py-3 flex items-center gap-3 text-sm">
                <Clock size={14} className="text-gray-600 flex-shrink-0" />
                <span className="text-gray-500 w-20 flex-shrink-0">{ev.time.slice(11)}</span>
                <span className="text-gray-300 truncate flex-1">{ev.title}</span>
                <span className="text-gray-500 text-xs flex-shrink-0">{ev.user} · {ev.client}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
