import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Zap, Moon, RefreshCw, ExternalLink, Trash2, Eye, EyeOff, X, ThumbsDown, Search, Users, ChevronRight, RotateCcw,
} from 'lucide-react'
import { api } from '../api/client'
import { useLiveEvent, useLiveRefresh, withScanProgress } from '../api/events'
import { ratingAllowed } from '../utils/ratings'
import { parseDbTimestamp } from '../utils/dbTime'
import Modal from '../components/Modal'
import { toast } from '../components/Toast'
import ScanContextMenu, { ScanTarget } from '../components/ScanContextMenu'
import ScopeTree, { TreeLibrary } from '../components/browse/ScopeTree'
import SegmentsView from '../components/browse/SegmentsView'
import Markers from './Markers'
import {
  Library, Title, ScannerStatus, Scope, scopeLevel, inScope, parseEpisodeTitle, showKeyOf, showNameOf,
  seasonSortKey, formatFinishedAt, failureText,
} from '../components/browse/model'

const STATUS_TABS = ['all', 'pending', 'scanning', 'done', 'failed'] as const
type StatusTab = typeof STATUS_TABS[number]
type SortOption = 'date-added' | 'scanned' | 'title' | 'year' | 'segments'
type View = 'titles' | 'segments' | 'markers'
const VIEWS: { id: View; label: string }[] = [
  { id: 'titles', label: 'Titles' },
  { id: 'segments', label: 'Segments' },
  { id: 'markers', label: 'Markers' },
]

// Title rows rendered at first and added per "Show more": each row holds a poster.
const PAGE = 100
// Parallel requests for an action over many titles (scan, ignore, delete segments).
const CONCURRENCY = 5

/** A show or season as one row or one level: the matching episodes under it. */
interface Group {
  kind: 'show' | 'season'
  libId: string
  showKey: string
  showName: string
  /** Plex guid of the show, and its name as it appears in episode titles: what POST /api/scan/show needs. */
  showGuid: string
  showTitleInEpisodes: string
  season: string
  label: string
  titles: Title[]
  posterUrl: string
  plexKey: string
}

async function pool<T>(items: T[], run: (item: T) => Promise<unknown>): Promise<string[]> {
  const errors: string[] = []
  const queue = [...items]
  const worker = async () => {
    while (queue.length > 0) {
      const item = queue.shift() as T
      try { await run(item) } catch (err: any) { errors.push(err?.message || String(item)) }
    }
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, worker))
  return errors
}

/** Icon-only action button; the label doubles as tooltip and accessible name. */
function ActionIcon({ label, onClick, disabled, tone, children }: {
  label: string
  onClick: () => void
  disabled?: boolean
  tone?: 'orange' | 'yellow' | 'red'
  children: React.ReactNode
}) {
  const hover = tone === 'orange' ? 'text-gray-400 hover:text-plex-orange hover:bg-plex-orange/15'
    : tone === 'yellow' ? 'text-gray-400 hover:text-yellow-400 hover:bg-yellow-500/15'
    : tone === 'red' ? 'text-gray-400 hover:text-red-400 hover:bg-red-400/10'
    : 'text-gray-400 hover:text-white hover:bg-white/5'
  return (
    <button onClick={onClick} disabled={disabled} title={label} aria-label={label} className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${hover}`}>
      {children}
    </button>
  )
}

function StatusBadge({ status, progress }: { status: string; progress: number }) {
  switch (status) {
    case 'done':
      return <span className="text-xs bg-green-500/15 text-green-400 px-2 py-0.5 rounded-full">Done</span>
    case 'failed':
      return <span className="text-xs bg-red-500/15 text-red-400 px-2 py-0.5 rounded-full">Failed</span>
    case 'scanning':
      return (
        <span className="text-xs bg-plex-orange/15 text-plex-orange px-2 py-0.5 rounded-full flex items-center gap-1">
          <span className="animate-pulse">●</span> {Math.round(progress * 100)}%
        </span>
      )
    default:
      return <span className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full">Pending</span>
  }
}

function IgnoredBadge({ titles }: { titles: Title[] }) {
  if (titles.length === 0 || !titles.some(t => t.ignored)) return null
  const all = titles.every(t => t.ignored)
  return <span className={`text-xs text-yellow-300 px-2 py-0.5 rounded-full ${all ? 'bg-yellow-500/20' : 'bg-yellow-500/10'}`}>{all ? 'Ignored' : 'Partly ignored'}</span>
}

const sum = (titles: Title[], f: (t: Title) => number) => titles.reduce((n, t) => n + f(t), 0)
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

export default function Browse() {
  const [params, setParams] = useSearchParams()
  const [libraries, setLibraries] = useState<Library[]>([])
  const [titles, setTitles] = useState<Title[]>([])
  const [loading, setLoading] = useState(true)
  const [scannerStatus, setScannerStatus] = useState<ScannerStatus | null>(null)
  const [machineId, setMachineId] = useState('')
  const [plexBaseUrl, setPlexBaseUrl] = useState('')
  // Ratings the scanner accepts; empty means every rating is scanned.
  const [scanRatings, setScanRatings] = useState<string[]>([])

  const [filter, setFilter] = useState('')
  const [castFilter, setCastFilter] = useState('')
  const [ratingFilter, setRatingFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState<StatusTab>('all')
  const [yearFrom, setYearFrom] = useState('')
  const [yearTo, setYearTo] = useState('')
  // Inclusive YYYY-MM-DD bounds on the scan date.
  const [scanFrom, setScanFrom] = useState('')
  const [scanTo, setScanTo] = useState('')
  const [showIgnored, setShowIgnored] = useState(false)
  const [sortBy, setSortBy] = useState<SortOption>('date-added')
  const [sortDesc, setSortDesc] = useState(true)

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [notice, setNotice] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [scanMenu, setScanMenu] = useState<ScanTarget | null>(null)
  const [pendingDelete, setPendingDelete] = useState<{ label: string; guids: string[]; count: number; busy: boolean } | null>(null)
  const [skipDeleteConfirm, setSkipDeleteConfirm] = useState(() => {
    // Storage can be unavailable (private mode, blocked site data); the confirmation then always shows (#117).
    try { return localStorage.getItem('skipDeleteConfirm') === 'true' } catch { return false }
  })

  // ── Scope and view live in the URL, so a level can be linked to and Back works ──
  const scope: Scope = useMemo(() => ({
    lib: params.get('lib') || undefined,
    show: params.get('show') || undefined,
    season: params.get('season') || undefined,
    guid: params.get('title') || undefined,
  }), [params])
  const view = (VIEWS.find(v => v.id === params.get('view'))?.id ?? 'titles') as View
  const level = scopeLevel(scope)

  const go = useCallback((next: Scope, nextView?: View) => {
    const p: Record<string, string> = {}
    if (next.lib) p.lib = next.lib
    if (next.show) p.show = next.show
    if (next.season) p.season = next.season
    if (next.guid) p.title = next.guid
    const v = nextView ?? view
    if (v !== 'titles') p.view = v
    setParams(p)
    setSelected(new Set())
    setLimit(PAGE)
  }, [setParams, view])

  // ── Loading ───────────────────────────────────────────────────────────────────
  const fetchLibrary = useCallback(async (libId: string, signal?: AbortSignal): Promise<Title[]> => {
    const d = await api.get<{ titles: Omit<Title, 'library_id'>[] }>(`/api/libraries/${libId}/titles`, { signal })
    return d.titles.map(t => ({ ...t, library_id: libId }))
  }, [])

  // One controller per library: a newer reload of a library aborts its older one, so a stale
  // reply never overwrites a newer one, and reloads of different libraries do not cancel each other.
  const ctl = useRef<Record<string, AbortController>>({})
  const reloadLibrary = useCallback(async (libId: string) => {
    ctl.current[libId]?.abort()
    const controller = (ctl.current[libId] = new AbortController())
    try {
      const fresh = await fetchLibrary(libId, controller.signal)
      setTitles(prev => [...prev.filter(t => t.library_id !== libId), ...fresh])
    } catch {
      // Aborted or failed: the next event or the fallback timer reloads.
    }
  }, [fetchLibrary])

  const librariesRef = useRef(libraries)
  librariesRef.current = libraries
  const reloadAll = useCallback(() => {
    for (const lib of librariesRef.current) reloadLibrary(lib.id)
  }, [reloadLibrary])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const d = await api.get<{ libraries: Library[] }>('/api/libraries')
        if (cancelled) return
        setLibraries(d.libraries)
        // One request per library (a handful), in parallel; titles are never fetched one by one.
        const lists = await Promise.all(d.libraries.map(lib => fetchLibrary(lib.id).catch(() => [] as Title[])))
        if (!cancelled) setTitles(lists.flat())
      } catch (err: any) {
        if (!cancelled) toast.error(`Could not load the libraries: ${err?.message || 'Unknown error'}`)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    const controllers = ctl.current
    return () => {
      cancelled = true
      for (const c of Object.values(controllers)) c.abort()
    }
  }, [fetchLibrary])

  // Plex server id and base URL for web deep links; the scan rating list for hiding unscannable titles.
  useEffect(() => {
    api.get<{ machine_identifier: string }>('/api/settings/plex-server-id').then(d => setMachineId(d.machine_identifier)).catch(() => {})
    api.get<Record<string, string>>('/api/settings')
      .then(d => {
        setPlexBaseUrl((d.plex_url || '').replace(/\/$/, ''))
        try { setScanRatings(JSON.parse(d.scan_ratings || '[]')) } catch { setScanRatings([]) }
      })
      .catch(() => {})
  }, [])

  const scannerCtl = useRef<AbortController | null>(null)
  const loadScanner = useCallback(() => {
    scannerCtl.current?.abort()
    const controller = (scannerCtl.current = new AbortController())
    api.get<ScannerStatus>('/api/sessions/scanner-status', { signal: controller.signal }).then(setScannerStatus).catch(() => {})
  }, [])
  useEffect(() => {
    loadScanner()
    return () => scannerCtl.current?.abort()
  }, [loadScanner])

  // Live (#89): the scan line follows the scanner; a library reloads when a scan starts or ends,
  // its titles change, or segments change for one of its titles. Events naming a title reload only
  // that title's library; the rest (and a resync) reload them all. Progress is patched in place.
  useLiveRefresh(['scan_started', 'scan_finished', 'scanner_changed'], loadScanner, { fallbackMs: 30_000 })
  const libOfGuid = useRef<Map<string, string>>(new Map())
  libOfGuid.current = useMemo(() => new Map(titles.map(t => [t.plex_guid, t.library_id])), [titles])
  // Libraries named by events since the last reload; null = reload all.
  const dirty = useRef<Set<string> | null>(new Set())
  useLiveRefresh(['scan_started', 'scan_finished', 'scanner_changed', 'titles_changed', 'segments_changed'], () => {
    const libs = dirty.current
    dirty.current = new Set()
    if (libs === null || libs.size === 0) reloadAll()
    else libs.forEach(reloadLibrary)
  }, {
    debounceMs: 500,
    // The safety-net reload fetches every library, so it runs rarely.
    fallbackMs: 300_000,
    relevant: (_type, d) => {
      const lib = d.library_id || (d.guid && libOfGuid.current.get(d.guid))
      if (lib && dirty.current) dirty.current.add(lib)
      else dirty.current = null
      return true
    },
  })
  useLiveEvent(['scan_progress'], (_, d) => {
    setScannerStatus(s => withScanProgress(s, d))
    setTitles(ts => (ts.some(t => t.plex_guid === d.guid) ? ts.map(t => (t.plex_guid === d.guid ? { ...t, progress: d.progress } : t)) : ts))
  })

  // Selection never outlives the titles it names.
  useEffect(() => {
    const valid = new Set(titles.map(t => t.plex_guid))
    setSelected(prev => (Array.from(prev).every(g => valid.has(g)) ? prev : new Set(Array.from(prev).filter(g => valid.has(g)))))
  }, [titles])

  // ── Filtering ─────────────────────────────────────────────────────────────────
  const libById = useMemo(() => new Map(libraries.map(l => [l.id, l])), [libraries])

  // Every filter except the level and the status tab, so each tab's count matches what it lists.
  const passes = useCallback((t: Title) => {
    // A pending title whose rating the scanner excludes will never be scanned, so it is
    // not listed at all; already-scanned ones keep their results visible.
    if (t.status === 'pending' && !ratingAllowed(t.content_rating, scanRatings)) return false
    if (filter && !t.title.toLowerCase().includes(filter.toLowerCase())) return false
    if (castFilter && !(t.cast ?? []).some(a => a.toLowerCase().includes(castFilter.toLowerCase()))) return false
    if (ratingFilter !== 'all' && t.content_rating !== ratingFilter) return false
    if (yearFrom || yearTo) {
      // Titles Plex has no year for cannot be inside a range.
      if (!t.year || (yearFrom && t.year < Number(yearFrom)) || (yearTo && t.year > Number(yearTo))) return false
    }
    if (scanFrom || scanTo) {
      // finished_at is "YYYY-MM-DD HH:MM:SS"; a never-scanned title has no scan date.
      const d = (t.finished_at ?? '').slice(0, 10)
      if (!d || (scanFrom && d < scanFrom) || (scanTo && d > scanTo)) return false
    }
    // An ignored title is hidden unless asked for, or it is the title being looked at.
    if (t.ignored && !showIgnored && scope.guid !== t.plex_guid) return false
    return true
  }, [scanRatings, filter, castFilter, ratingFilter, yearFrom, yearTo, scanFrom, scanTo, showIgnored, scope.guid])

  const visible = useMemo(() => titles.filter(passes), [titles, passes])
  const matchedAll = useMemo(() => visible.filter(t => statusFilter === 'all' || t.status === statusFilter), [visible, statusFilter])
  const inScopeVisible = useMemo(() => visible.filter(t => inScope(t, scope)), [visible, scope])
  const matched = useMemo(() => matchedAll.filter(t => inScope(t, scope)), [matchedAll, scope])

  const counts: Record<string, number> = { all: inScopeVisible.length }
  for (const t of inScopeVisible) counts[t.status] = (counts[t.status] ?? 0) + 1

  const scopeTitles = useMemo(() => titles.filter(t => inScope(t, scope)), [titles, scope])
  const availableRatings = useMemo(() => Array.from(new Set(scopeTitles.map(t => t.content_rating).filter(Boolean))).sort(), [scopeTitles])
  // Actors in two or more titles of this level, most frequent first, as suggestions for the cast filter.
  const castSuggestions = useMemo(() => {
    const n = new Map<string, number>()
    for (const t of scopeTitles) for (const a of t.cast ?? []) n.set(a, (n.get(a) ?? 0) + 1)
    return [...n].filter(([, c]) => c >= 2).sort((a, b) => b[1] - a[1]).slice(0, 2000).map(([a]) => a)
  }, [scopeTitles])

  const filtersActive = !!(filter || castFilter || ratingFilter !== 'all' || statusFilter !== 'all' || yearFrom || yearTo || scanFrom || scanTo)
  const clearFilters = () => {
    setFilter(''); setCastFilter(''); setRatingFilter('all'); setStatusFilter('all')
    setYearFrom(''); setYearTo(''); setScanFrom(''); setScanTo('')
  }

  // ── Tree: structure from every title, counts from the ones that pass the filters ──
  const tree: TreeLibrary[] = useMemo(() => {
    const count = new Map<string, number>()
    const bump = (k: string) => count.set(k, (count.get(k) ?? 0) + 1)
    for (const t of matchedAll) {
      bump(`l:${t.library_id}`)
      if (t.media_type === 'episode') {
        const sk = showKeyOf(t)
        bump(`s:${t.library_id}:${sk}`)
        bump(`n:${t.library_id}:${sk}:${parseEpisodeTitle(t.title).season}`)
      }
    }
    const shows = new Map<string, Map<string, { name: string; seasons: Set<string> }>>()
    for (const t of titles) {
      if (t.media_type !== 'episode') continue
      if (!shows.has(t.library_id)) shows.set(t.library_id, new Map())
      const byShow = shows.get(t.library_id)!
      const sk = showKeyOf(t)
      if (!byShow.has(sk)) byShow.set(sk, { name: showNameOf(t), seasons: new Set() })
      byShow.get(sk)!.seasons.add(parseEpisodeTitle(t.title).season)
    }
    return libraries.map(lib => ({
      lib,
      count: count.get(`l:${lib.id}`) ?? 0,
      shows: Array.from(shows.get(lib.id) ?? [])
        .map(([key, s]) => ({
          key,
          name: s.name,
          count: count.get(`s:${lib.id}:${key}`) ?? 0,
          seasons: Array.from(s.seasons)
            .sort((a, b) => seasonSortKey(a) - seasonSortKey(b) || a.localeCompare(b))
            .map(season => ({ season, count: count.get(`n:${lib.id}:${key}:${season}`) ?? 0 })),
        }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
  }, [libraries, titles, matchedAll])

  // ── Sorting ───────────────────────────────────────────────────────────────────
  const sortValue = useCallback((t: Title): number | string => {
    switch (sortBy) {
      // rating_key is a monotonically increasing integer in Plex; higher = more recently added.
      case 'date-added': return parseInt(t.rating_key, 10) || 0
      case 'year': return t.year ?? 0
      case 'segments': return t.segment_count
      // Titles never scanned sort to the end regardless of direction.
      case 'scanned': return t.finished_at ? parseDbTimestamp(t.finished_at).getTime() : (sortDesc ? -Infinity : Infinity)
      default: return t.title.toLowerCase()
    }
  }, [sortBy, sortDesc])
  const cmp = useCallback((a: number | string, b: number | string) => (a < b ? (sortDesc ? 1 : -1) : a > b ? (sortDesc ? -1 : 1) : 0), [sortDesc])
  const sorted = useMemo(() => [...matched].sort((a, b) => cmp(sortValue(a), sortValue(b))), [matched, cmp, sortValue])

  // A group sorts by the best value among its episodes: newest scan, newest addition, total segments.
  const groupSortValue = (g: Group): number | string => {
    if (sortBy === 'title') return g.label.toLowerCase()
    if (sortBy === 'segments') return sum(g.titles, t => t.segment_count)
    const vals = g.titles.map(sortValue).filter(v => typeof v === 'number' && Number.isFinite(v)) as number[]
    return vals.length ? Math.max(...vals) : (sortDesc ? -Infinity : Infinity)
  }

  const sortDirLabel = sortBy === 'title' ? (sortDesc ? 'Z to A' : 'A to Z')
    : sortBy === 'segments' ? (sortDesc ? 'Most first' : 'Fewest first')
    : (sortDesc ? 'Newest first' : 'Oldest first')

  // ── The level below the scope: what the Titles view lists ─────────────────────
  const makeGroup = (kind: 'show' | 'season', eps: Title[]): Group => {
    const first = eps[0]
    const parsed = parseEpisodeTitle(first.title)
    const showName = showNameOf(first)
    return {
      kind,
      libId: first.library_id,
      showKey: showKeyOf(first),
      showName,
      showGuid: first.show_guid ?? '',
      showTitleInEpisodes: parsed.show,
      season: kind === 'season' ? parsed.season : '',
      label: kind === 'season' ? parsed.season : showName,
      titles: eps,
      posterUrl: eps.find(e => !!e.poster_url)?.poster_url ?? '',
      plexKey: (kind === 'season' ? eps.find(e => e.season_rating_key)?.season_rating_key : eps.find(e => e.show_rating_key)?.show_rating_key) ?? '',
    }
  }
  const groupBy = (kind: 'show' | 'season', eps: Title[]): Group[] => {
    const m = new Map<string, Title[]>()
    for (const t of eps) {
      const k = kind === 'show' ? `${t.library_id}:${showKeyOf(t)}` : parseEpisodeTitle(t.title).season
      if (!m.has(k)) m.set(k, [])
      m.get(k)!.push(t)
    }
    const groups = Array.from(m.values()).map(list => makeGroup(kind, list))
    return kind === 'season'
      ? groups.sort((a, b) => seasonSortKey(a.season) - seasonSortKey(b.season) || a.season.localeCompare(b.season))
      : groups.sort((a, b) => cmp(groupSortValue(a), groupSortValue(b)))
  }

  const episodes = sorted.filter(t => t.media_type === 'episode')
  const rowGroups: Group[] = level === 'library' ? groupBy('show', episodes) : level === 'show' ? groupBy('season', episodes) : []
  const rowTitles: Title[] = level === 'all' ? [] : level === 'library' ? sorted.filter(t => t.media_type !== 'episode') : level === 'show' ? [] : sorted

  // The group the scope itself names (show or season level), for the header's actions.
  const scopeGroup: Group | null = (level === 'show' || level === 'season') && scopeTitles.length > 0
    ? { ...makeGroup(level, scopeTitles), titles: matched }
    : null
  const scopeLib = scope.lib ? libById.get(scope.lib) : undefined
  const scopeTitle = level === 'title' ? titles.find(t => t.plex_guid === scope.guid) : undefined

  // ── Actions ───────────────────────────────────────────────────────────────────
  const plexWebUrl = (ratingKey?: string) =>
    machineId && ratingKey && plexBaseUrl
      ? `${plexBaseUrl}/web/index.html#!/server/${machineId}/details?key=%2Flibrary%2Fmetadata%2F${ratingKey}`
      : ''

  const withBusy = async (key: string, run: () => Promise<void>) => {
    setBusy(b => ({ ...b, [key]: true }))
    try { await run() } finally { setBusy(b => ({ ...b, [key]: false })) }
  }
  const reloadLibsOf = (list: Title[]) => Promise.all(Array.from(new Set(list.map(t => t.library_id))).map(reloadLibrary))

  const scanTitles = (list: Title[], now: boolean) => withBusy(`scan:${list.map(t => t.plex_guid).join(',')}`, async () => {
    const errors = await pool(list, t => api.post('/api/scan/title', { plex_guid: t.plex_guid, now, library_id: t.library_id }))
    await reloadLibsOf(list)
    if (errors.length > 0) toast.error(`${plural(errors.length, 'title')} could not be queued:\n${errors.slice(0, 5).join('\n')}`)
    else if (list.length > 1) setNotice(`${plural(list.length, 'title')} queued ${now ? 'to scan now' : 'for tonight'}`)
  })

  // Queues a whole show or one season through POST /api/scan/show (#187).
  const scanGroup = async (g: Group, now: boolean) => {
    const label = g.kind === 'season' ? `${g.showName} · ${g.season}` : g.showName
    try {
      const r = await api.post<{ queued: number; already_scanning: number }>('/api/scan/show', {
        show_guid: g.showGuid, show_title: g.showTitleInEpisodes, season: g.season, library_id: g.libId, now,
      })
      const scanning = r.already_scanning ? ` (${r.already_scanning} already scanning)` : ''
      setNotice(`${label}: ${plural(r.queued, 'episode')} queued ${now ? 'to scan now' : 'for tonight'}${scanning}`)
      await reloadLibrary(g.libId)
    } catch (err: any) {
      setNotice(`Could not queue ${label}: ${err.message || 'unknown error'}`)
    }
  }

  const scanLibrary = async (lib: Library, now: boolean) => {
    try {
      await api.post(`/api/scan/library/${lib.id}`, { now })
      setNotice(`${lib.title}: queued ${now ? 'to scan now' : 'for tonight'}`)
      await reloadLibrary(lib.id)
    } catch (err: any) {
      toast.error(`Could not queue the library: ${err.message || 'Unknown error'}`)
    }
  }

  const syncLibrary = (lib: Library) => withBusy(`sync:${lib.id}`, async () => {
    try {
      await api.post(`/api/libraries/${lib.id}/sync`)
      await reloadLibrary(lib.id)
    } catch (err: any) {
      toast.error(`Could not sync ${lib.title} from Plex: ${err.message || 'Unknown error'}`)
    }
  })

  // Retries every failed title of the library through the scheduled queue and takes ignored ones
  // off the Failed list (#150); the server counts what it did.
  const retryFailed = (lib: Library) => withBusy(`retry:${lib.id}`, async () => {
    try {
      const r = await api.post<{ queued: number; ignored_cleared: number; skipped_ineligible: number }>('/api/scan/rescan', { scope: 'failed', library_id: lib.id })
      const parts = [`${plural(r.queued, 'failed title')} queued for another try`]
      if (r.ignored_cleared) parts.push(`${r.ignored_cleared} ignored cleared from the list`)
      if (r.skipped_ineligible) parts.push(`${r.skipped_ineligible} outside your rating filter`)
      setNotice(parts.join(' · '))
      await reloadLibrary(lib.id)
    } catch (err: any) {
      setNotice(`Could not retry the failed titles: ${err.message || 'unknown error'}`)
    }
  })

  const setIgnored = async (list: Title[], ignored: boolean, label?: string) => {
    if (list.length === 0) return
    const guids = new Set(list.map(t => t.plex_guid))
    // Optimistic: the reload below settles it either way.
    setTitles(prev => prev.map(t => (guids.has(t.plex_guid) ? { ...t, ignored } : t)))
    const errors = await pool(list, t => api.post(`/api/scan/title/${encodeURIComponent(t.plex_guid)}/ignore`, { ignored }))
    setSelected(prev => new Set(Array.from(prev).filter(g => !guids.has(g))))
    await reloadLibsOf(list)
    if (errors.length > 0) toast.error(`Could not change ignore${label ? ` for ${label}` : ''}: ${errors[0]}`)
  }

  const runDelete = async (label: string, list: Title[]) => {
    const errors = await pool(list, t => api.delete(`/api/titles/${encodeURIComponent(t.plex_guid)}/segments`))
    await reloadLibsOf(list)
    if (errors.length > 0) toast.error(`Could not delete every segment of ${label}: ${errors[0]}`)
    else toast.success(`Deleted the segments of ${label}.`)
  }
  const deleteSegments = (label: string, list: Title[]) => {
    const withSegs = list.filter(t => t.segment_count > 0 || (t.false_positive_count ?? 0) > 0)
    if (withSegs.length === 0) return
    if (skipDeleteConfirm) { runDelete(label, withSegs); return }
    setPendingDelete({ label, guids: withSegs.map(t => t.plex_guid), count: sum(withSegs, t => t.segment_count + (t.false_positive_count ?? 0)), busy: false })
  }
  const confirmDelete = async () => {
    if (!pendingDelete) return
    setPendingDelete({ ...pendingDelete, busy: true })
    const guids = new Set(pendingDelete.guids)
    await runDelete(pendingDelete.label, titles.filter(t => guids.has(t.plex_guid)))
    setPendingDelete(null)
  }
  const toggleSkipConfirm = (on: boolean) => {
    setSkipDeleteConfirm(on)
    try { localStorage.setItem('skipDeleteConfirm', String(on)) } catch { /* not remembered across reloads */ }
  }

  // Right-click rescan menu (#137): the listed titles that have segments, or the whole level.
  const openScanMenu = (e: React.MouseEvent, s: Scope) => {
    e.preventDefault()
    const listed = matchedAll.filter(t => inScope(t, s) && t.segment_count > 0)
    const lib = s.lib ? libById.get(s.lib) : undefined
    const first = titles.find(t => inScope(t, s))
    let label = lib?.title ?? ''
    let whole: ScanTarget['whole']
    if (s.show && first) {
      const parsed = parseEpisodeTitle(first.title)
      label = s.season ? `${showNameOf(first)} – ${s.season}` : showNameOf(first)
      whole = { kind: s.season ? 'season' : 'show', showGuid: first.show_guid ?? '', showTitle: parsed.show, season: s.season ?? '', libraryId: s.lib }
    } else if (s.lib) {
      whole = { kind: 'library', libraryId: s.lib }
    }
    setScanMenu({ x: e.clientX, y: e.clientY, label, guids: listed.map(t => t.plex_guid), whole })
  }
  // One batch request per menu action, however many episodes a show has.
  const rescan = async (target: ScanTarget, now: boolean, whole = false) => {
    try {
      const w = whole ? target.whole : undefined
      let r: { queued: number }
      if (w?.kind === 'library') {
        // Scheduled only, through the bulk endpoint: every finished or failed title of the library.
        r = await api.post<{ queued: number }>('/api/scan/rescan', { scope: 'whole_library', library_id: w.libraryId })
      } else if (w) {
        r = await api.post<{ queued: number }>('/api/scan/show', {
          show_guid: w.showGuid ?? '', show_title: w.showTitle ?? '', season: w.season ?? '', library_id: w.libraryId ?? null, now,
        })
      } else {
        r = await api.post<{ queued: number }>('/api/scan/titles', { plex_guids: target.guids, now })
      }
      setNotice(`${target.label}${w ? ` (whole ${w.kind})` : ''}: ${plural(r.queued, 'title')} queued ${now && w?.kind !== 'library' ? 'to scan now' : 'for tonight'}`)
    } catch (err: any) {
      setNotice(`Could not queue ${target.label}: ${err.message || 'unknown error'}`)
    }
  }

  // ── Selection ─────────────────────────────────────────────────────────────────
  const matchedGuids = matched.map(t => t.plex_guid)
  const allSelected = matchedGuids.length > 0 && matchedGuids.every(g => selected.has(g))
  const toggleGuids = (guids: string[]) => setSelected(prev => {
    const next = new Set(prev)
    const allIn = guids.every(g => next.has(g))
    for (const g of guids) allIn ? next.delete(g) : next.add(g)
    return next
  })
  const selectedTitles = matched.filter(t => selected.has(t.plex_guid))

  // ── Labels ────────────────────────────────────────────────────────────────────
  /** A title's name relative to the level: the episode name inside a season, the full title higher up. */
  const titleLabel = (t: Title) => {
    if (t.media_type !== 'episode') return t.title
    const p = parseEpisodeTitle(t.title)
    if (level === 'season') return p.episode
    if (level === 'show') return `${p.season} – ${p.episode}`
    return t.title
  }
  const scopeOfTitle = (t: Title): Scope => ({ guid: t.plex_guid })
  // The path above a title, for its breadcrumb.
  const pathOf = (t: Title): Scope => (t.media_type === 'episode'
    ? { lib: t.library_id, show: showKeyOf(t), season: parseEpisodeTitle(t.title).season }
    : { lib: t.library_id })

  const crumbs: { label: string; scope: Scope }[] = [{ label: 'All libraries', scope: {} }]
  {
    const path: Scope = scopeTitle ? pathOf(scopeTitle) : scope
    const anyInPath = scopeTitle ?? scopeTitles[0]
    if (path.lib) crumbs.push({ label: libById.get(path.lib)?.title ?? 'Library', scope: { lib: path.lib } })
    if (path.show) crumbs.push({ label: anyInPath ? showNameOf(anyInPath) : 'Show', scope: { lib: path.lib, show: path.show } })
    if (path.season) crumbs.push({ label: path.season, scope: { lib: path.lib, show: path.show, season: path.season } })
    if (scopeTitle) crumbs.push({ label: scopeTitle.media_type === 'episode' ? parseEpisodeTitle(scopeTitle.title).episode : scopeTitle.title, scope })
  }
  const heading = crumbs[crumbs.length - 1].label

  // ── Rendering helpers ─────────────────────────────────────────────────────────
  const PlexLink = ({ ratingKey, label }: { ratingKey?: string; label: string }) => {
    const url = plexWebUrl(ratingKey)
    if (!url) return null
    return (
      <a href={url} target="_blank" rel="noopener noreferrer" title={label} aria-label={label} className="p-1.5 text-gray-400 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors">
        <ExternalLink size={14} />
      </a>
    )
  }

  const titleActions = (t: Title, name: string) => (
    <>
      {(t.segment_count > 0 || (t.false_positive_count ?? 0) > 0) && (
        <ActionIcon label={`Delete all segments for ${name}`} onClick={() => deleteSegments(name, [t])} tone="red"><Trash2 size={14} /></ActionIcon>
      )}
      <PlexLink ratingKey={t.rating_key} label="Open in Plex" />
      <ActionIcon label="Scan tonight" onClick={() => scanTitles([t], false)} disabled={busy[`scan:${t.plex_guid}`]}><Moon size={14} /></ActionIcon>
      <ActionIcon label="Scan now" onClick={() => scanTitles([t], true)} disabled={busy[`scan:${t.plex_guid}`]} tone="orange"><Zap size={14} /></ActionIcon>
      <ActionIcon label={t.ignored ? `Un-ignore ${name}` : `Ignore ${name}`} onClick={() => setIgnored([t], !t.ignored, name)} tone="yellow">
        {t.ignored ? <Eye size={14} /> : <EyeOff size={14} />}
      </ActionIcon>
    </>
  )

  const groupActions = (g: Group) => {
    const what = g.kind === 'season' ? g.season : 'whole show'
    const all = titles.filter(t => inScope(t, { lib: g.libId, show: g.showKey, season: g.season || undefined }))
    return (
      <>
        {sum(g.titles, t => t.segment_count + (t.false_positive_count ?? 0)) > 0 && (
          <ActionIcon label={`Delete all segments for this ${g.kind}`} onClick={() => deleteSegments(g.kind === 'season' ? `${g.showName} – ${g.season}` : g.showName, g.titles)} tone="red"><Trash2 size={14} /></ActionIcon>
        )}
        <PlexLink ratingKey={g.plexKey} label={`Open ${g.kind} in Plex`} />
        <ActionIcon label={`Scan ${what} tonight`} onClick={() => scanGroup(g, false)}><Moon size={14} /></ActionIcon>
        <ActionIcon label={`Scan ${what} now`} onClick={() => scanGroup(g, true)} tone="orange"><Zap size={14} /></ActionIcon>
        <ActionIcon label={`Ignore all episodes in this ${g.kind}`} onClick={() => setIgnored(all, true, g.label)} tone="yellow"><EyeOff size={14} /></ActionIcon>
        <ActionIcon label={`Un-ignore all episodes in this ${g.kind}`} onClick={() => setIgnored(all, false, g.label)}><Eye size={14} /></ActionIcon>
      </>
    )
  }

  const libraryActions = (lib: Library) => (
    <>
      <ActionIcon label="Sync from Plex: pick up titles added, removed or changed in Plex" onClick={() => syncLibrary(lib)} disabled={busy[`sync:${lib.id}`]}>
        <RefreshCw size={14} className={busy[`sync:${lib.id}`] ? 'animate-spin' : ''} />
      </ActionIcon>
      <ActionIcon label="Retry failed: queue every failed title in this library again and clear ignored ones from the list" onClick={() => retryFailed(lib)} disabled={busy[`retry:${lib.id}`]}>
        <RotateCcw size={14} />
      </ActionIcon>
      <ActionIcon label="Scan library tonight" onClick={() => scanLibrary(lib, false)}><Moon size={14} /></ActionIcon>
      <ActionIcon label="Scan library now" onClick={() => scanLibrary(lib, true)} tone="orange"><Zap size={14} /></ActionIcon>
    </>
  )

  const segmentCount = (list: Title[]) => {
    const n = sum(list, t => t.segment_count)
    const fp = sum(list, t => t.false_positive_count ?? 0)
    if (n === 0 && fp === 0) return null
    return (
      <span className="flex items-center gap-1 text-xs">
        <span className={n > 0 ? 'text-red-400 bg-red-500/10 border border-red-500/20 px-1.5 py-0.5 rounded font-medium' : 'text-gray-400 bg-white/5 border border-plex-border px-1.5 py-0.5 rounded'}>
          {plural(n, 'segment')}
        </span>
        {fp > 0 && <span className="flex items-center gap-0.5 text-amber-300" title={`${fp} marked false positive`}><ThumbsDown size={10} /> {fp}</span>}
      </span>
    )
  }

  const checkbox = (guids: string[], label: string) => (
    <input
      type="checkbox"
      checked={guids.length > 0 && guids.every(g => selected.has(g))}
      onChange={() => toggleGuids(guids)}
      aria-label={`Select ${label}`}
      className="w-4 h-4 accent-plex-orange flex-shrink-0"
    />
  )

  const poster = (src: string | undefined, wide = false) => {
    const cls = wide ? 'w-20 h-12' : 'w-12 h-[4.5rem]'
    return src
      ? <img src={src} alt="" loading="lazy" className={`${cls} object-cover rounded bg-plex-border flex-shrink-0`} onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
      : <div className={`${cls} bg-plex-border/50 rounded flex-shrink-0`} />
  }

  const titleRow = (t: Title) => {
    const name = titleLabel(t)
    const isEpisode = t.media_type === 'episode'
    return (
      <div key={t.plex_guid} className={`flex items-center gap-3 rounded-xl p-2.5 border ${t.ignored ? 'bg-yellow-500/5 border-yellow-500/30' : 'bg-plex-card border-plex-border'}`}>
        {checkbox([t.plex_guid], name)}
        {poster(isEpisode ? t.thumb_url : (t.poster_url || t.thumb_url), isEpisode)}
        <div className="flex-1 min-w-0">
          <button onClick={() => go(scopeOfTitle(t), 'segments')} className="block max-w-full text-sm font-medium text-gray-100 truncate text-left hover:text-plex-orange" title="Open this title's segments">
            {name}
          </button>
          <div className="flex flex-wrap items-center gap-2 mt-1">
            <StatusBadge status={t.status} progress={t.progress} />
            {t.ignored && <span className="text-xs bg-yellow-500/15 text-yellow-300 px-2 py-0.5 rounded-full">Ignored</span>}
            {t.year ? <span className="text-xs text-muted">{t.year}</span> : null}
            {t.content_rating && <span className="text-xs text-gray-400 bg-white/5 px-1.5 py-0.5 rounded">{t.content_rating}</span>}
            {segmentCount([t])}
            {t.finished_at && <span className="text-xs text-muted">Scanned {formatFinishedAt(t.finished_at)}</span>}
          </div>
          {/* The reason a title failed, so a Failed title never stands unexplained (#190). */}
          {t.status === 'failed' && t.error_msg && <p className="text-xs text-red-400/80 mt-1 break-words" title={t.error_msg}>{failureText(t.error_msg)}</p>}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-0.5 flex-shrink-0">{titleActions(t, name)}</div>
      </div>
    )
  }

  const groupRow = (g: Group) => {
    const s: Scope = { lib: g.libId, show: g.showKey, season: g.season || undefined }
    const seasons = new Set(g.titles.map(t => parseEpisodeTitle(t.title).season)).size
    return (
      <div key={`${g.libId}:${g.showKey}:${g.season}`} className="flex items-center gap-3 rounded-xl p-2.5 border bg-plex-card border-plex-border" onContextMenu={e => openScanMenu(e, s)}>
        {checkbox(g.titles.map(t => t.plex_guid), g.label)}
        {g.kind === 'show' && poster(g.posterUrl)}
        <div className="flex-1 min-w-0">
          <button onClick={() => go(s)} className="flex items-center gap-1 max-w-full text-sm font-semibold text-gray-100 text-left hover:text-plex-orange">
            <span className="truncate">{g.label}</span>
            <ChevronRight size={14} className="flex-shrink-0 text-gray-400" />
          </button>
          <div className="flex flex-wrap items-center gap-2 mt-1 text-xs text-muted">
            {g.kind === 'show' && <span>{plural(seasons, 'season')}</span>}
            <span>{plural(g.titles.length, 'episode')}</span>
            {segmentCount(g.titles)}
            <IgnoredBadge titles={g.titles} />
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-0.5 flex-shrink-0">{groupActions(g)}</div>
      </div>
    )
  }

  const libraryRow = (lib: Library) => {
    const list = matchedAll.filter(t => t.library_id === lib.id)
    return (
      <div key={lib.id} className="flex items-center gap-3 rounded-xl p-2.5 border bg-plex-card border-plex-border" onContextMenu={e => openScanMenu(e, { lib: lib.id })}>
        {checkbox(list.map(t => t.plex_guid), lib.title)}
        <div className="flex-1 min-w-0">
          <button onClick={() => go({ lib: lib.id })} className="flex items-center gap-1 max-w-full text-sm font-semibold text-gray-100 text-left hover:text-plex-orange">
            <span className="truncate">{lib.title}</span>
            <ChevronRight size={14} className="flex-shrink-0 text-gray-400" />
          </button>
          <div className="flex flex-wrap items-center gap-2 mt-1 text-xs text-muted">
            <span>{plural(list.length, lib.type === 'show' ? 'episode' : 'title')}</span>
            {segmentCount(list)}
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-0.5 flex-shrink-0">{libraryActions(lib)}</div>
      </div>
    )
  }

  const inputCls = 'px-2 py-1.5 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-200 placeholder-muted focus:border-plex-orange/50'
  const rowCount = level === 'all' ? libraries.length : rowGroups.length + rowTitles.length
  const markerLibs = libraries.filter(l => (scopeTitle ? scopeTitle.library_id === l.id : !scope.lib || scope.lib === l.id))
  const matchedKeys = useMemo(() => new Set(matched.flatMap(t => [t.rating_key, t.plex_guid])), [matched])

  return (
    <div className="flex gap-5 h-full">
      {/* Level tree: desktop only; on a phone the breadcrumb and the rows do the navigating. */}
      <aside className="hidden md:flex flex-col w-60 flex-shrink-0 min-h-0">
        <h1 className="text-2xl font-bold text-gray-100 mb-3">Browse</h1>
        <div className="flex-1 min-h-0 overflow-y-auto pr-1 pb-6">
          <ScopeTree tree={tree} total={matchedAll.length} scope={scopeTitle ? pathOf(scopeTitle) : scope} onScope={s => go(s)} onMenu={openScanMenu} />
        </div>
      </aside>

      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        <h1 className="md:hidden text-xl font-bold text-gray-100 mb-2">Browse</h1>

        {/* Breadcrumb */}
        <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-1 text-xs text-muted mb-1">
          {crumbs.slice(0, -1).map((c, i) => (
            <span key={i} className="flex items-center gap-1">
              <button onClick={() => go(c.scope)} className="hover:text-gray-100">{c.label}</button>
              <ChevronRight size={11} />
            </span>
          ))}
        </nav>

        {/* Header: the level's name, bulk actions while titles are selected, the level's own actions */}
        <div className="flex items-center gap-1 mb-2 min-w-0">
          <h2 className="text-xl font-semibold text-gray-100 truncate">{heading}</h2>
          <div className="flex items-center gap-2 ml-2 mr-auto min-w-0">
            {scopeTitle && <StatusBadge status={scopeTitle.status} progress={scopeTitle.progress} />}
            {scopeTitle?.ignored && <span className="text-xs bg-yellow-500/15 text-yellow-300 px-2 py-0.5 rounded-full">Ignored</span>}
            {scopeTitle?.content_rating && <span className="text-xs text-gray-400 bg-white/5 px-1.5 py-0.5 rounded">{scopeTitle.content_rating}</span>}
            {scopeTitle?.finished_at && <span className="text-xs text-muted truncate">Scanned {formatFinishedAt(scopeTitle.finished_at)}</span>}
            {scopeGroup && <IgnoredBadge titles={scopeTitles} />}
          </div>
          {selectedTitles.length > 0 && (
            <div className="flex items-center gap-0.5 mr-2 pl-2.5 pr-0.5 bg-plex-card border border-plex-border rounded-lg">
              <span className="text-xs text-gray-400 mr-1 whitespace-nowrap">{selectedTitles.length} selected</span>
              <ActionIcon label="Scan selected tonight" onClick={() => { scanTitles(selectedTitles, false); setSelected(new Set()) }}><Moon size={15} /></ActionIcon>
              <ActionIcon label="Scan selected now" onClick={() => { scanTitles(selectedTitles, true); setSelected(new Set()) }} tone="orange"><Zap size={15} /></ActionIcon>
              <ActionIcon label="Ignore selected" onClick={() => setIgnored(selectedTitles, true)} tone="yellow"><EyeOff size={15} /></ActionIcon>
              <ActionIcon label="Un-ignore selected" onClick={() => setIgnored(selectedTitles, false)}><Eye size={15} /></ActionIcon>
              <ActionIcon label="Delete segments of selected" onClick={() => deleteSegments(plural(selectedTitles.length, 'selected title'), selectedTitles)} tone="red"><Trash2 size={15} /></ActionIcon>
              <ActionIcon label="Clear selection" onClick={() => setSelected(new Set())}><X size={15} /></ActionIcon>
            </div>
          )}
          <div className="flex items-center gap-0.5 flex-shrink-0">
            {level === 'library' && scopeLib && libraryActions(scopeLib)}
            {scopeGroup && groupActions(scopeGroup)}
            {scopeTitle && titleActions(scopeTitle, heading)}
          </div>
        </div>
        {scopeTitle?.status === 'failed' && scopeTitle.error_msg && (
          <p className="text-xs text-red-400/80 mb-2 break-words" title={scopeTitle.error_msg}>{failureText(scopeTitle.error_msg)}</p>
        )}

        {/* One thin line for running scans (#110); the per-scan bars are on the Dashboard. */}
        {scannerStatus && scannerStatus.active_scans.length > 0 && (
          <div className="mb-2 flex items-center gap-3 text-xs text-muted whitespace-nowrap overflow-hidden" title={scannerStatus.active_scans.map(s => `${s.title} ${Math.round(s.progress * 100)}%`).join('\n')}>
            <span className="text-plex-orange animate-pulse flex-shrink-0">●</span>
            {scannerStatus.active_scans.map(scan => (
              <span key={scan.guid} className="flex items-baseline gap-1 min-w-0">
                <span className="text-gray-300 truncate max-w-[14rem]">{scan.title}</span>
                <span className="text-plex-orange flex-shrink-0">{Math.round(scan.progress * 100)}%</span>
              </span>
            ))}
          </div>
        )}

        {/* Filter bar: applies at every level and to all three views. Hidden for a single title. */}
        {level !== 'title' && (
          <div className="mb-3 rounded-xl border border-plex-border bg-plex-card/40 p-2.5 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative flex-1 min-w-[10rem]">
                <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
                <input type="text" placeholder="Name: show, season, episode or movie…" aria-label="Filter by name" value={filter} onChange={e => setFilter(e.target.value)} className={`${inputCls} w-full pl-8`} />
              </div>
              <div className="relative w-44">
                <Users size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
                <input type="text" list="cast-suggestions" placeholder="Actor…" aria-label="Filter by actor" value={castFilter} onChange={e => setCastFilter(e.target.value)} className={`${inputCls} w-full pl-8`} />
              </div>
              <datalist id="cast-suggestions">{castSuggestions.map(a => <option key={a} value={a} />)}</datalist>
              {availableRatings.length > 0 && (
                <select value={ratingFilter} aria-label="Filter by rating" onChange={e => setRatingFilter(e.target.value)} className={inputCls}>
                  <option value="all">All ratings</option>
                  {availableRatings.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              )}
              <label className="flex items-center gap-1.5 text-xs text-muted">
                Released
                <input type="number" placeholder="From" aria-label="Released from year" value={yearFrom} onChange={e => setYearFrom(e.target.value)} className={`${inputCls} w-20`} />
                <span>–</span>
                <input type="number" placeholder="To" aria-label="Released to year" value={yearTo} onChange={e => setYearTo(e.target.value)} className={`${inputCls} w-20`} />
              </label>
              <label className="flex items-center gap-1.5 text-xs text-muted">
                Scanned
                <input type="date" aria-label="Scanned from" value={scanFrom} onChange={e => setScanFrom(e.target.value)} className={`${inputCls} w-36`} />
                <span>–</span>
                <input type="date" aria-label="Scanned to" value={scanTo} onChange={e => setScanTo(e.target.value)} className={`${inputCls} w-36`} />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <input type="checkbox" checked={allSelected} onChange={() => setSelected(allSelected ? new Set() : new Set(matchedGuids))} title="Select all filtered titles" aria-label="Select all filtered titles" className="w-4 h-4 accent-plex-orange flex-shrink-0" />
              {/* Segmented status control: scrolls sideways on a narrow screen rather than wrapping. */}
              <div className="flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-plex-border overflow-x-auto max-w-full">
                {STATUS_TABS.map(s => (
                  <button
                    key={s}
                    onClick={() => setStatusFilter(s)}
                    aria-pressed={statusFilter === s}
                    className={`px-3 py-1 text-xs rounded-md transition-colors whitespace-nowrap ${statusFilter === s ? 'bg-plex-orange text-black font-semibold' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'}`}
                  >
                    {s === 'all' ? 'All' : s.charAt(0).toUpperCase() + s.slice(1)}
                    <span className={`ml-1.5 tabular-nums ${statusFilter === s ? 'opacity-70' : 'text-muted'}`}>{counts[s] ?? 0}</span>
                  </button>
                ))}
              </div>
              <button
                onClick={() => setShowIgnored(!showIgnored)}
                aria-pressed={showIgnored}
                title={showIgnored ? 'Hide ignored titles' : 'Show ignored titles'}
                aria-label="Show ignored titles"
                className={`p-1.5 border rounded-lg transition-colors ${showIgnored ? 'bg-yellow-500/15 border-yellow-500/30 text-yellow-400' : 'bg-plex-card border-plex-border text-gray-400 hover:text-gray-200'}`}
              >
                {showIgnored ? <Eye size={15} /> : <EyeOff size={15} />}
              </button>
              {filtersActive && <button onClick={clearFilters} className="text-xs text-gray-400 hover:text-white">Clear filters</button>}
              <span className="ml-auto text-xs text-muted tabular-nums whitespace-nowrap">{plural(matched.length, 'title')}</span>
              {/* Sort and its direction are one joined control; the direction says what it does. */}
              <div className="flex items-stretch">
                <select value={sortBy} aria-label="Sort by" onChange={e => setSortBy(e.target.value as SortOption)} className={`${inputCls} rounded-r-none`}>
                  <option value="date-added">Date added</option>
                  <option value="scanned">Scan date</option>
                  <option value="title">Name</option>
                  <option value="year">Release year</option>
                  <option value="segments">Segments</option>
                </select>
                <button onClick={() => setSortDesc(!sortDesc)} aria-label={`Sort direction: ${sortDirLabel}`} className={`${inputCls} -ml-px rounded-l-none whitespace-nowrap hover:text-white hover:border-gray-500`}>
                  {sortDirLabel}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* View switch */}
        <div className="flex items-center gap-3 mb-2">
          <div role="tablist" aria-label="Show" className="flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-plex-border">
            {VIEWS.map(v => (
              <button
                key={v.id}
                role="tab"
                aria-selected={view === v.id}
                onClick={() => go(scope, v.id)}
                className={`px-3 py-1 text-xs rounded-md transition-colors ${view === v.id ? 'bg-plex-orange text-black font-semibold' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'}`}
              >
                {v.label}
              </button>
            ))}
          </div>
          {notice && (
            <p className="flex items-center gap-2 text-xs text-gray-400 min-w-0" role="status">
              <span className="truncate">{notice}</span>
              <button onClick={() => setNotice('')} aria-label="Dismiss" className="text-gray-400 hover:text-white flex-shrink-0"><X size={12} /></button>
            </p>
          )}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden pr-1 pb-6">
          {loading ? (
            <div className="text-muted text-sm">Loading…</div>
          ) : level === 'title' && !scopeTitle ? (
            <div className="text-muted text-sm">This title is no longer in the library. <button onClick={() => go({})} className="text-plex-orange hover:underline">Back to all libraries</button></div>
          ) : view === 'titles' ? (
            rowCount === 0 ? (
              <div className="text-muted text-sm">No titles match here. Pick a higher level or clear a filter.</div>
            ) : (
              <div className="space-y-2">
                {level === 'all' && libraries.map(libraryRow)}
                {rowGroups.slice(0, limit).map(groupRow)}
                {rowTitles.slice(0, Math.max(0, limit - rowGroups.length)).map(titleRow)}
                {rowCount > limit && level !== 'all' && (
                  <div className="text-center pt-1">
                    <button onClick={() => setLimit(l => l + PAGE)} className="btn-outline">Show more ({rowCount - limit} left)</button>
                  </div>
                )}
              </div>
            )
          ) : view === 'segments' ? (
            <SegmentsView
              titles={sorted}
              single={level === 'title'}
              titleLabel={titleLabel}
              onOpenTitle={t => go(scopeOfTitle(t), 'segments')}
              onImported={t => reloadLibrary(t.library_id)}
            />
          ) : (
            <div className="space-y-6">
              <p className="text-sm text-muted">Drag the handles to move an intro or credits marker, then save it to Plex.</p>
              {markerLibs.map(lib => (
                <Markers
                  key={lib.id}
                  libraryId={lib.id}
                  libraryType={lib.type}
                  libraryTitle={markerLibs.length > 1 ? lib.title : undefined}
                  // Unfiltered at library level: the marker cache can list titles Cleanplex has no scan job for.
                  allowed={level === 'library' && !filtersActive && showIgnored ? undefined : matchedKeys}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {pendingDelete && (
        <Modal
          title="Delete segments?"
          size="sm"
          onClose={() => setPendingDelete(null)}
          dismissible={!pendingDelete.busy}
          footer={
            <>
              <button data-autofocus onClick={() => setPendingDelete(null)} disabled={pendingDelete.busy} className="btn-outline">Cancel</button>
              <button onClick={confirmDelete} disabled={pendingDelete.busy} className="btn-danger">{pendingDelete.busy ? 'Deleting…' : 'Delete'}</button>
            </>
          }
        >
          <p className="text-sm text-gray-300 mb-4">
            All {plural(pendingDelete.count, 'segment')} for <strong>{pendingDelete.label}</strong>, including the ones marked as false positives, will be deleted. This cannot be undone.
          </p>
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input type="checkbox" checked={skipDeleteConfirm} onChange={e => toggleSkipConfirm(e.target.checked)} className="w-4 h-4 accent-plex-orange" />
            <span className="text-xs text-gray-400">Don't ask again</span>
          </label>
        </Modal>
      )}
      {scanMenu && <ScanContextMenu target={scanMenu} onScan={rescan} onClose={() => setScanMenu(null)} />}
    </div>
  )
}
