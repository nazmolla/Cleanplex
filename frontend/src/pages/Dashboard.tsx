import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { toast } from '../components/Toast'
import { useLiveEvent, useLiveRefresh, withScanProgress } from '../api/events'
import RecentSkips from '../components/RecentSkips'
import { WifiOff, ChevronRight, Film, Tv, Zap } from 'lucide-react'

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
  // Whether this stream is being cut for this viewer, and why not when it is not (#192)
  filtering_state: 'off' | 'active' | 'clear' | 'ignored' | 'library_excluded' | 'rating_skipped' | 'not_scanned'
  thumb_url: string
}

interface QueueItem {
  position: number
  guid: string
  title: string
  media_type: string
  content_rating: string
  force: boolean
}

interface ScannerStatus {
  queue_size: number
  current_scan: string | null
  current_title: string | null
  current_progress: number
  current_scans: string[]
  active_scans: { guid: string; title: string; progress: number; status: string }[]
  workers_configured: number
  workers_target?: number
  workers_active: number
  workers_idle: number
  paused: boolean
  queue_items: QueueItem[]
}

const NOT_CUT = 'text-gray-400 bg-white/5'
const FILTERING_BADGES: Record<Session['filtering_state'], { text: string; cls: string; title: string }> = {
  active: { text: 'Being cut', cls: 'text-plex-orange bg-plex-orange/10', title: 'This title has segments that are skipped for this viewer.' },
  clear: { text: 'Not cut: nothing to cut', cls: 'text-green-400 bg-green-400/10', title: 'Scanned: nothing in this title needs cutting at this viewer\'s levels.' },
  ignored: { text: 'Not cut: title ignored', cls: NOT_CUT, title: 'This title is marked ignored, so it has no segments.' },
  library_excluded: { text: 'Not cut: library excluded', cls: NOT_CUT, title: 'This title\'s library is excluded from scanning.' },
  rating_skipped: { text: 'Not cut: rating not scanned', cls: NOT_CUT, title: 'This title\'s content rating is not in the scanned ratings.' },
  not_scanned: { text: 'Not cut: not scanned yet', cls: 'text-yellow-400 bg-yellow-400/10', title: 'This title is waiting to be scanned, so nothing is being cut yet.' },
  off: { text: 'Not cut: filtering off', cls: NOT_CUT, title: 'Filtering is switched off for this viewer.' },
}

function FilteringBadge({ state }: { state: Session['filtering_state'] }) {
  const b = FILTERING_BADGES[state] ?? FILTERING_BADGES.not_scanned
  return <span title={b.title} className={`text-xs px-2 py-0.5 rounded-full ${b.cls}`}>{b.text}</span>
}

function msToTime(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  return `${m}:${String(sec).padStart(2, '0')}`
}

// Section label, the same style CollapsibleSection uses on the other pages.
const SECTION_HEADING = 'text-sm font-semibold text-gray-400 uppercase tracking-wide mb-3'

export default function Dashboard() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [scanner, setScanner] = useState<ScannerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [skipLoadingKey, setSkipLoadingKey] = useState<string | null>(null)
  const [skipScanLoadingGuid, setSkipScanLoadingGuid] = useState<string | null>(null)
  const [queueExpanded, setQueueExpanded] = useState(false)

  // refresh is called ad-hoc by skipNow/skipCurrentScan — reuses the same fetch logic
  // without a signal since it's a one-shot, user-triggered call.
  const refresh = async () => {
    try {
      const [s, sc] = await Promise.all([
        api.get<{ sessions: Session[] }>('/api/sessions'),
        api.get<ScannerStatus>('/api/sessions/scanner-status'),
      ])
      setSessions(s.sessions)
      setScanner(sc)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  // Each load aborts the previous one, so a slow response can never overwrite a
  // newer one.
  const controllerRef = useRef<AbortController | null>(null)
  const load = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = (controllerRef.current = new AbortController())
    try {
      const [s, sc] = await Promise.all([
        api.get<{ sessions: Session[] }>('/api/sessions', { signal: controller.signal }),
        api.get<ScannerStatus>('/api/sessions/scanner-status', { signal: controller.signal }),
      ])
      setSessions(s.sessions)
      setScanner(sc)
    } catch {
      // Ignore — includes intentional abort
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load])

  // Live: streams starting, stopping or moving, and the scanner starting, finishing
  // or being paused. Progress is patched in place rather than refetched.
  useLiveRefresh(['sessions_changed', 'scan_started', 'scan_finished', 'scanner_changed'], load, { fallbackMs: 30_000 })
  useLiveEvent(['scan_progress'], (_, d) => setScanner(s => withScanProgress(s, d)))

  const skipNow = async (sessionKey: string) => {
    try {
      setSkipLoadingKey(sessionKey)
      await api.post(`/api/sessions/${sessionKey}/skip`)
      await refresh()
    } catch (err: any) {
      toast.error(`Could not skip: ${err.message || 'Unknown error'}`)
    } finally {
      setSkipLoadingKey(null)
    }
  }

  const skipCurrentScan = async (guid?: string) => {
    try {
      const target = guid || scanner?.current_scan || null
      if (!target) return
      setSkipScanLoadingGuid(target)
      await api.post('/api/scan/skip-current', { plex_guid: target })
      await refresh()
    } catch (err: any) {
      toast.error(`Could not skip this scan: ${err.message || 'Unknown error'}`)
    } finally {
      setSkipScanLoadingGuid(null)
    }
  }

  const forcedCount = (scanner?.queue_items ?? []).filter(i => i.force).length
  const scannerIdle = !!scanner && !scanner.paused && scanner.active_scans.length === 0 && scanner.queue_size === 0

  return (
    <div className="space-y-8 max-w-6xl">
      <h1 className="text-2xl font-bold text-gray-100">Dashboard</h1>

      {/* Now playing comes first (#206): during playback it is the reason to open this page. */}
      <section aria-labelledby="dash-playing">
        <h2 id="dash-playing" className={SECTION_HEADING}>
          Now playing
          {sessions.length > 0 && <span className="ml-2 text-muted font-normal normal-case tabular-nums">{sessions.length}</span>}
        </h2>
        {loading ? (
          <div className="text-muted text-sm">Loading…</div>
        ) : sessions.length === 0 ? (
          <div className="bg-plex-card border border-plex-border rounded-xl px-4 py-5 text-sm text-muted">
            No active streams
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
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
                <div className="flex-1 min-w-0 flex flex-col">
                  <p className="font-medium text-gray-100 truncate" title={s.title}>{s.title}</p>
                  <p className="text-sm text-gray-400 mt-0.5 truncate">{s.user} · {s.client}</p>
                  <div className="flex flex-wrap items-center gap-1.5 mt-2">
                    <FilteringBadge state={s.filtering_state} />
                    {!s.is_controllable && (
                      <span
                        title="Cleanplex cannot seek or mute this client, so it cannot skip here"
                        className="flex items-center gap-1 text-xs text-gray-400 bg-white/5 px-2 py-0.5 rounded-full"
                      >
                        <WifiOff size={11} />Not controllable
                      </span>
                    )}
                  </div>
                  <div className="mt-auto pt-3 flex items-center gap-3">
                    <span className="text-xs text-muted tabular-nums">{msToTime(s.position_ms)}</span>
                    <div className="flex-1 h-1.5 bg-plex-border rounded-full overflow-hidden">
                      <div
                        className="h-full bg-plex-orange rounded-full transition-all"
                        style={{ width: `${s.duration_ms ? (s.position_ms / s.duration_ms) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="text-xs text-muted tabular-nums">{msToTime(s.duration_ms)}</span>
                    <button
                      className="btn-outline text-xs px-3 py-1"
                      disabled={!s.is_controllable || skipLoadingKey === s.session_key}
                      onClick={() => skipNow(s.session_key)}
                      title={s.is_controllable ? 'Skip the segment playing now' : 'This client cannot be controlled'}
                    >
                      {skipLoadingKey === s.session_key ? 'Skipping…' : 'Skip'}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Scanner: state, running scans and the queue are one thing, so they are one card (#206). */}
      {scanner && (
        <section aria-labelledby="dash-scanner">
          <h2 id="dash-scanner" className={SECTION_HEADING}>Scanner</h2>
          <div className="bg-plex-card border border-plex-border rounded-xl">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
              <span className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${scanner.paused ? 'bg-yellow-500' : scannerIdle ? 'bg-faint' : 'bg-green-500 animate-pulse'}`} />
                <span className={`font-medium ${scanner.paused ? 'text-yellow-400' : scannerIdle ? 'text-gray-300' : 'text-green-400'}`}>
                  {scanner.paused ? 'Paused' : scannerIdle ? 'Idle' : 'Active'}
                </span>
              </span>
              <span className="text-muted sm:ml-auto">
                <span className="text-gray-300 tabular-nums">{scanner.workers_active}/{scanner.workers_configured}</span> workers busy
                {scanner.workers_target && scanner.workers_target !== scanner.workers_configured && (
                  <> (changing to <span className="text-gray-300 tabular-nums">{scanner.workers_target}</span>)</>
                )}
              </span>
            </div>

            {scanner.active_scans.length > 0 && (
              <div className="border-t border-plex-border px-4 py-3 space-y-3">
                {scanner.active_scans.map(scan => (
                  <div key={scan.guid} className="flex items-center gap-3 text-sm">
                    <span className="text-gray-200 truncate w-1/3 min-w-0" title={scan.title}>{scan.title}</span>
                    <div className="flex-1 h-1.5 bg-plex-border rounded-full overflow-hidden">
                      <div
                        className="h-full bg-plex-orange rounded-full transition-all duration-1000"
                        style={{ width: `${scan.progress * 100}%` }}
                      />
                    </div>
                    <span className="text-xs text-gray-400 tabular-nums w-9 text-right">{Math.round(scan.progress * 100)}%</span>
                    <button
                      className="btn-outline text-xs px-2 py-0.5"
                      onClick={() => skipCurrentScan(scan.guid)}
                      disabled={skipScanLoadingGuid === scan.guid}
                      title="Stop scanning this title and move to the next"
                    >
                      {skipScanLoadingGuid === scan.guid ? 'Skipping…' : 'Skip'}
                    </button>
                  </div>
                ))}
              </div>
            )}

            {scanner.queue_size > 0 && (
              <div className="border-t border-plex-border">
                <button
                  className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-gray-300 hover:text-white hover:bg-white/5 transition-colors rounded-b-xl"
                  onClick={() => setQueueExpanded(e => !e)}
                  aria-expanded={queueExpanded}
                >
                  <ChevronRight size={15} className={`text-faint transition-transform ${queueExpanded ? 'rotate-90' : ''}`} />
                  <span><span className="tabular-nums">{scanner.queue_size}</span> queued</span>
                  {forcedCount > 0 && <span className="text-plex-orange">· {forcedCount} forced</span>}
                </button>
                {queueExpanded && (
                  <div className="divide-y divide-plex-border border-t border-plex-border max-h-96 overflow-y-auto">
                    {scanner.queue_items.map(item => (
                      <div key={item.guid} className="flex items-center gap-3 px-4 py-2 text-sm">
                        <span className="text-muted w-8 text-right flex-shrink-0 tabular-nums">{item.position}</span>
                        {item.force
                          ? <Zap size={13} className="text-plex-orange flex-shrink-0" />
                          : item.media_type === 'episode'
                            ? <Tv size={13} className="text-faint flex-shrink-0" />
                            : <Film size={13} className="text-faint flex-shrink-0" />
                        }
                        <span className="flex-1 text-gray-300 truncate">{item.title}</span>
                        {item.content_rating && (
                          <span className="text-xs text-muted flex-shrink-0">{item.content_rating}</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </section>
      )}

      <RecentSkips />
    </div>
  )
}
