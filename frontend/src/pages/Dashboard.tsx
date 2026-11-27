import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { toast } from '../components/Toast'
import { useLiveEvent, useLiveRefresh, withScanProgress } from '../api/events'
import RecentSkips from '../components/RecentSkips'
import { WifiOff, ChevronRight, Film, Tv, Zap } from 'lucide-react'
import { useT, type TranslationKey } from '../i18n'

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
const FILTERING_BADGES: Record<Session['filtering_state'], { textKey: TranslationKey; cls: string; titleKey: TranslationKey }> = {
  active: { textKey: 'filter_state_active', cls: 'text-hayaa-gold bg-hayaa-gold/10', titleKey: 'filter_state_active_title' },
  clear: { textKey: 'filter_state_clear', cls: 'text-green-400 bg-green-400/10', titleKey: 'filter_state_clear_title' },
  ignored: { textKey: 'filter_state_ignored', cls: NOT_CUT, titleKey: 'filter_state_ignored_title' },
  library_excluded: { textKey: 'filter_state_library_excluded', cls: NOT_CUT, titleKey: 'filter_state_library_excluded_title' },
  rating_skipped: { textKey: 'filter_state_rating_skipped', cls: NOT_CUT, titleKey: 'filter_state_rating_skipped_title' },
  not_scanned: { textKey: 'filter_state_not_scanned', cls: 'text-yellow-400 bg-yellow-400/10', titleKey: 'filter_state_not_scanned_title' },
  off: { textKey: 'filter_state_off', cls: NOT_CUT, titleKey: 'filter_state_off_title' },
}

function FilteringBadge({ state }: { state: Session['filtering_state'] }) {
  const { t } = useT()
  const b = FILTERING_BADGES[state] ?? FILTERING_BADGES.not_scanned
  return <span title={t(b.titleKey)} className={`text-xs px-2 py-0.5 rounded-full ${b.cls}`}>{t(b.textKey)}</span>
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
  const { t } = useT()
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
      toast.error(t('dash_skip_error', { error: err.message || t('common_unknown_error') }))
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
      toast.error(t('dash_skip_scan_error', { error: err.message || t('common_unknown_error') }))
    } finally {
      setSkipScanLoadingGuid(null)
    }
  }

  const forcedCount = (scanner?.queue_items ?? []).filter(i => i.force).length
  const scannerIdle = !!scanner && !scanner.paused && scanner.active_scans.length === 0 && scanner.queue_size === 0

  return (
    <div className="space-y-8 max-w-6xl">
      <h1 className="text-2xl font-bold text-gray-100">{t('dash_title')}</h1>

      {/* Now playing comes first (#206): during playback it is the reason to open this page. */}
      <section aria-labelledby="dash-playing">
        <h2 id="dash-playing" className={SECTION_HEADING}>
          {t('dash_now_playing')}
          {sessions.length > 0 && <span className="ms-2 text-muted font-normal normal-case tabular-nums">{sessions.length}</span>}
        </h2>
        {loading ? (
          <div className="text-muted text-sm">{t('common_loading')}</div>
        ) : sessions.length === 0 ? (
          <div className="bg-hayaa-card border border-hayaa-border rounded-xl px-4 py-5 text-sm text-muted">
            {t('dash_no_active_streams')}
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {sessions.map(s => (
              <div key={s.session_key} className="bg-hayaa-card border border-hayaa-border rounded-xl p-4 flex gap-4">
                {s.thumb_url && (
                  <img
                    src={s.thumb_url}
                    alt=""
                    className="w-16 h-24 object-cover rounded-lg flex-shrink-0 bg-hayaa-border"
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
                        title={t('dash_not_controllable_title')}
                        className="flex items-center gap-1 text-xs text-gray-400 bg-white/5 px-2 py-0.5 rounded-full"
                      >
                        <WifiOff size={11} />{t('dash_not_controllable')}
                      </span>
                    )}
                  </div>
                  <div className="mt-auto pt-3 flex items-center gap-3">
                    <span className="text-xs text-muted tabular-nums" dir="ltr">{msToTime(s.position_ms)}</span>
                    <div className="flex-1 h-1.5 bg-hayaa-border rounded-full overflow-hidden">
                      <div
                        className="h-full bg-hayaa-gold rounded-full transition-all"
                        style={{ width: `${s.duration_ms ? (s.position_ms / s.duration_ms) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="text-xs text-muted tabular-nums" dir="ltr">{msToTime(s.duration_ms)}</span>
                    <button
                      className="btn-outline text-xs px-3 py-1"
                      disabled={!s.is_controllable || skipLoadingKey === s.session_key}
                      onClick={() => skipNow(s.session_key)}
                      title={s.is_controllable ? t('dash_skip_title') : t('dash_skip_disabled_title')}
                    >
                      {skipLoadingKey === s.session_key ? t('dash_skipping') : t('dash_skip')}
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
          <h2 id="dash-scanner" className={SECTION_HEADING}>{t('dash_scanner')}</h2>
          <div className="bg-hayaa-card border border-hayaa-border rounded-xl">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
              <span className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${scanner.paused ? 'bg-yellow-500' : scannerIdle ? 'bg-faint' : 'bg-green-500 animate-pulse'}`} />
                <span className={`font-medium ${scanner.paused ? 'text-yellow-400' : scannerIdle ? 'text-gray-300' : 'text-green-400'}`}>
                  {scanner.paused ? t('dash_scanner_paused') : scannerIdle ? t('dash_scanner_idle') : t('dash_scanner_active')}
                </span>
              </span>
              <span className="text-muted sm:ms-auto">
                <span className="text-gray-300 tabular-nums">{scanner.workers_active}/{scanner.workers_configured}</span> {t('dash_workers_busy')}
                {scanner.workers_target && scanner.workers_target !== scanner.workers_configured && (
                  <> ({t('dash_workers_changing')} <span className="text-gray-300 tabular-nums">{scanner.workers_target}</span>)</>
                )}
              </span>
            </div>

            {scanner.active_scans.length > 0 && (
              <div className="border-t border-hayaa-border px-4 py-3 space-y-3">
                {scanner.active_scans.map(scan => (
                  <div key={scan.guid} className="flex items-center gap-3 text-sm">
                    <span className="text-gray-200 truncate w-1/3 min-w-0" title={scan.title}>{scan.title}</span>
                    <div className="flex-1 h-1.5 bg-hayaa-border rounded-full overflow-hidden">
                      <div
                        className="h-full bg-hayaa-gold rounded-full transition-all duration-1000"
                        style={{ width: `${scan.progress * 100}%` }}
                      />
                    </div>
                    <span className="text-xs text-gray-400 tabular-nums w-9 text-end">{Math.round(scan.progress * 100)}%</span>
                    <button
                      className="btn-outline text-xs px-2 py-0.5"
                      onClick={() => skipCurrentScan(scan.guid)}
                      disabled={skipScanLoadingGuid === scan.guid}
                      title={t('dash_skip_scan_title')}
                    >
                      {skipScanLoadingGuid === scan.guid ? t('dash_skipping') : t('dash_skip')}
                    </button>
                  </div>
                ))}
              </div>
            )}

            {scanner.queue_size > 0 && (
              <div className="border-t border-hayaa-border">
                <button
                  className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-gray-300 hover:text-white hover:bg-white/5 transition-colors rounded-b-xl"
                  onClick={() => setQueueExpanded(e => !e)}
                  aria-expanded={queueExpanded}
                >
                  <ChevronRight size={15} className={`text-faint transition-transform ${queueExpanded ? 'rotate-90' : 'rtl:-scale-x-100'}`} />
                  <span className="tabular-nums">{t('dash_queued_count', { count: scanner.queue_size })}</span>
                  {forcedCount > 0 && <span className="text-hayaa-gold">{t('dash_forced_count', { count: forcedCount })}</span>}
                </button>
                {queueExpanded && (
                  <div className="divide-y divide-hayaa-border border-t border-hayaa-border max-h-96 overflow-y-auto">
                    {scanner.queue_items.map(item => (
                      <div key={item.guid} className="flex items-center gap-3 px-4 py-2 text-sm">
                        <span className="text-muted w-8 text-end flex-shrink-0 tabular-nums">{item.position}</span>
                        {item.force
                          ? <Zap size={13} className="text-hayaa-gold flex-shrink-0" />
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
