import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Zap, Moon, RefreshCw, ExternalLink, Trash2, Eye, EyeOff, X, Search, ChevronRight, RotateCcw, SlidersHorizontal,
  Film, Tv, Layers, ArrowDown, ArrowUp, DownloadCloud,
} from 'lucide-react'
import { api } from '../api/client'
import { useLiveEvent, useLiveRefresh, withScanProgress } from '../api/events'
import { normalizeRating, ratingAllowed } from '../utils/ratings'
import { parseDbTimestamp } from '../utils/dbTime'
import Modal from '../components/Modal'
import { toast } from '../components/Toast'
import ScanContextMenu, { ScanTarget } from '../components/ScanContextMenu'
import SegmentsView from '../components/browse/SegmentsView'
import TitleSegments from '../components/browse/TitleSegments'
import BrowseRow, { COL, CountCell, GroupStatus, IgnoredBadge, RowImage, StatusBadge, scannedCell } from '../components/browse/BrowseRow'
import { CastSort, castSuggestionsFor, FilterChips, FilterPanel, NO_TITLE_FILTERS, TitleFilters, titleFiltersActive } from '../components/browse/FilterPanel'
import SafeImg from '../components/browse/SafeImg'
import { SyncAllBanner, SyncProgress, TitleMarkerPanel, TitleRow } from '../components/browse/markers'
import {
  Library, Title, ScannerStatus, Scope, scopeLevel, inScope, parseEpisodeTitle, showKeyOf, showNameOf,
  seasonSortKey, failureText, SegmentFilters, NO_SEGMENT_FILTERS, titleNameParts, plexThumb,
} from '../components/browse/model'

type SortOption = 'date-added' | 'scanned' | 'title' | 'year' | 'segments'
type View = 'titles' | 'segments' | 'markers'
const VIEWS: { id: View; label: string }[] = [
  { id: 'titles', label: 'Titles' },
  { id: 'segments', label: 'Segments' },
  { id: 'markers', label: 'Markers' },
]

// Rows rendered at first and added per "Show more": each row holds a picture.
const PAGE = 100
// Parallel requests for an action over many titles (scan, ignore, delete segments, marker sync).
const CONCURRENCY = 5

/** A show or season: the titles under it that pass the filters. */
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

type Row =
  | { kind: 'library'; lib: Library; titles: Title[] }
  | { kind: 'group'; group: Group }
  | { kind: 'title'; title: Title }

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

const sum = (titles: Title[], f: (t: Title) => number) => titles.reduce((n, t) => n + f(t), 0)
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
const fpOf = (t: Title) => t.false_positive_count ?? 0

export default function Browse() {
  const [params, setParams] = useSearchParams()
  const [libraries, setLibraries] = useState<Library[]>([])
  const [titles, setTitles] = useState<Title[]>([])
  const [actorPhotos, setActorPhotos] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [scannerStatus, setScannerStatus] = useState<ScannerStatus | null>(null)
  const [machineId, setMachineId] = useState('')
  const [plexBaseUrl, setPlexBaseUrl] = useState('')
  // Ratings the scanner accepts; empty means every rating is scanned.
  const [scanRatings, setScanRatings] = useState<string[]>([])

  const [tf, setTf] = useState<TitleFilters>(NO_TITLE_FILTERS)
  const patchTf = useCallback((patch: Partial<TitleFilters>) => setTf(prev => ({ ...prev, ...patch })), [])
  const [sf, setSf] = useState<SegmentFilters>(NO_SEGMENT_FILTERS)
  const [segOptions, setSegOptions] = useState<{ labels: string[]; origins: string[] }>({ labels: [], origins: [] })
  const onSegOptions = useCallback((labels: string[], origins: string[]) => setSegOptions({ labels, origins }), [])
  const [filtersOpen, setFiltersOpen] = useState(false)
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
  const level = scopeLevel(scope)
  const requestedView = (VIEWS.find(v => v.id === params.get('view'))?.id ?? 'titles') as View
  // A single title has no titles under it: its page opens on its segments.
  const view: View = level === 'title' && requestedView === 'titles' ? 'segments' : requestedView

  const go = useCallback((next: Scope, nextView?: View) => {
    const p: Record<string, string> = {}
    if (next.lib) p.lib = next.lib
    if (next.show) p.show = next.show
    if (next.season) p.season = next.season
    if (next.guid) p.title = next.guid
    const v = nextView ?? requestedView
    if (v !== 'titles') p.view = v
    setParams(p)
    setSelected(new Set())
    setLimit(PAGE)
  }, [setParams, requestedView])

  // ── Loading ───────────────────────────────────────────────────────────────────
  const fetchLibrary = useCallback(async (libId: string, signal?: AbortSignal): Promise<Title[]> => {
    const d = await api.get<{ titles: Omit<Title, 'library_id'>[]; actor_photos?: Record<string, string> }>(`/api/libraries/${libId}/titles`, { signal })
    if (d.actor_photos) setActorPhotos(prev => ({ ...prev, ...d.actor_photos }))
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
  const query = tf.name.trim().toLowerCase()

  const queuedGuids = useMemo(() => new Set((scannerStatus?.queue_items ?? []).map(q => q.guid)), [scannerStatus?.queue_items])

  // Every filter except the status, so each status choice's count matches what it lists.
  const passes = useCallback((t: Title) => {
    // A rating outside Scan ratings is out of scope at every status, like an ignored title (#216);
    // the Show ignored and out-of-scope checkbox lists them. A scan in progress or a queued title
    // (a single-title Scan now forces any rating, #214) stays listed.
    if (!ratingAllowed(t.content_rating, scanRatings) && !tf.showIgnored && scope.guid !== t.plex_guid
        && t.status !== 'scanning' && !queuedGuids.has(t.plex_guid)) return false
    // The full title is "<show> – <season> – <episode>" for an episode, so a show or season name matches too.
    if (query && !t.title.toLowerCase().includes(query)) return false
    if (tf.actors.length) {
      const cast = t.cast ?? []
      if (tf.actorMatch === 'all' ? !tf.actors.every(a => cast.includes(a)) : !tf.actors.some(a => cast.includes(a))) return false
    }
    if (tf.rating !== 'all' && normalizeRating(t.content_rating) !== tf.rating) return false
    if (tf.yearFrom || tf.yearTo) {
      // Titles Plex has no year for cannot be inside a range.
      if (!t.year || (tf.yearFrom && t.year < Number(tf.yearFrom)) || (tf.yearTo && t.year > Number(tf.yearTo))) return false
    }
    if (tf.scanFrom || tf.scanTo) {
      // finished_at is "YYYY-MM-DD HH:MM:SS"; a never-scanned title has no scan date.
      const d = (t.finished_at ?? '').slice(0, 10)
      if (!d || (tf.scanFrom && d < tf.scanFrom) || (tf.scanTo && d > tf.scanTo)) return false
    }
    // An ignored title is hidden unless asked for, or it is the title being looked at.
    // A scan in progress is always listed, so the Scanning count matches the scan line (#212).
    if (t.ignored && !tf.showIgnored && scope.guid !== t.plex_guid && t.status !== 'scanning') return false
    return true
  }, [scanRatings, query, tf, scope.guid, queuedGuids])

  const visible = useMemo(() => titles.filter(passes), [titles, passes])
  const matchedAll = useMemo(() => visible.filter(t => tf.status === 'all' || t.status === tf.status), [visible, tf.status])
  const matched = useMemo(() => matchedAll.filter(t => inScope(t, scope)), [matchedAll, scope])
  const statusCounts = useMemo(() => {
    const c: Record<string, number> = { all: 0 }
    for (const t of visible) {
      if (!inScope(t, scope)) continue
      c.all++
      c[t.status] = (c[t.status] ?? 0) + 1
    }
    return c
  }, [visible, scope])

  const scopeTitles = useMemo(() => titles.filter(t => inScope(t, scope)), [titles, scope])
  // Ratings offered by the rating filter: normalized ("eg/16" is R, #219) and, unless out-of-scope
  // titles are shown, only ratings inside Scan ratings, since the rest are not listed anyway.
  const availableRatings = useMemo(() => Array.from(new Set(scopeTitles
    .filter(t => tf.showIgnored || ratingAllowed(t.content_rating, scanRatings))
    .map(t => normalizeRating(t.content_rating)).filter(Boolean))).sort(), [scopeTitles, scanRatings, tf.showIgnored])
  // Suggestions for the cast filter (#227), ordered by castSort: actors in two or more titles of
  // this level by title count or by name, or every actor whose titles have segments by segment total.
  const [castSort, setCastSort] = useState<CastSort>('titles')
  const castSuggestions = useMemo(() => castSuggestionsFor(scopeTitles, castSort), [scopeTitles, castSort])

  // While a filter narrows the list, the list shows the matches directly instead of the levels (#208).
  const flat = titleFiltersActive(tf)
  const panelFilterCount = [tf.status !== 'all', tf.actors.length > 0, tf.rating !== 'all', !!(tf.yearFrom || tf.yearTo), !!(tf.scanFrom || tf.scanTo), tf.showIgnored].filter(Boolean).length
    + (view === 'segments' ? [sf.state !== 'all', !!sf.origin, sf.labels.size > 0].filter(Boolean).length : 0)
  const clearFilters = () => { setTf(NO_TITLE_FILTERS); setSf(NO_SEGMENT_FILTERS) }

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

  // A group sorts by the best value among its titles: newest scan, newest addition, total segments.
  const groupSortValue = (g: Group): number | string => {
    if (sortBy === 'title') return g.label.toLowerCase()
    if (sortBy === 'segments') return sum(g.titles, t => t.segment_count)
    const vals = g.titles.map(sortValue).filter(v => typeof v === 'number' && Number.isFinite(v)) as number[]
    return vals.length ? Math.max(...vals) : (sortDesc ? -Infinity : Infinity)
  }

  const sortDirLabel = sortBy === 'title' ? (sortDesc ? 'Z to A' : 'A to Z')
    : sortBy === 'segments' ? (sortDesc ? 'Most first' : 'Fewest first')
    : (sortDesc ? 'Newest first' : 'Oldest first')

  // ── Rows: the children of the current level ───────────────────────────────────
  const makeGroup = (kind: 'show' | 'season', eps: Title[]): Group => {
    const first = eps[0]
    const parsed = parseEpisodeTitle(first.title)
    const showName = showNameOf(first)
    const seasonKey = eps.find(e => e.season_rating_key)?.season_rating_key
    const showPoster = eps.find(e => !!e.poster_url)?.poster_url ?? ''
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
      posterUrl: kind === 'season' && seasonKey ? plexThumb(seasonKey) : showPoster,
      plexKey: (kind === 'season' ? seasonKey : eps.find(e => e.show_rating_key)?.show_rating_key) ?? '',
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

  const rows: Row[] = (() => {
    if (level === 'title') return []
    if (flat) {
      // Each match appears once: inside a show (or season) whose own name matches the search,
      // otherwise as its own row with its show and season as context.
      const childKind: 'show' | 'season' | null = level === 'all' || level === 'library' ? 'show' : level === 'show' ? 'season' : null
      let groups: Group[] = []
      if (query && childKind) {
        const named = sorted.filter(t => t.media_type === 'episode' && (childKind === 'show'
          ? showNameOf(t).toLowerCase().includes(query)
          : parseEpisodeTitle(t.title).season.toLowerCase().includes(query)))
        groups = groupBy(childKind, named)
      }
      const inGroup = new Set(groups.flatMap(g => g.titles.map(t => t.plex_guid)))
      return [
        ...groups.map(group => ({ kind: 'group' as const, group })),
        ...sorted.filter(t => !inGroup.has(t.plex_guid)).map(title => ({ kind: 'title' as const, title })),
      ]
    }
    if (level === 'all') return libraries.map(lib => ({ kind: 'library' as const, lib, titles: matchedAll.filter(t => t.library_id === lib.id) }))
    const episodes = sorted.filter(t => t.media_type === 'episode')
    if (level === 'library') {
      return [
        ...groupBy('show', episodes).map(group => ({ kind: 'group' as const, group })),
        ...sorted.filter(t => t.media_type !== 'episode').map(title => ({ kind: 'title' as const, title })),
      ]
    }
    if (level === 'show') return groupBy('season', episodes).map(group => ({ kind: 'group' as const, group }))
    return sorted.map(title => ({ kind: 'title' as const, title }))
  })()

  // The group the scope itself names (show or season level), for the header.
  const scopeGroup: Group | null = (level === 'show' || level === 'season') && scopeTitles.length > 0
    ? { ...makeGroup(level, scopeTitles), titles: matched }
    : null
  const scopeLib = scope.lib ? libById.get(scope.lib) : undefined
  const scopeTitle = level === 'title' ? titles.find(t => t.plex_guid === scope.guid) : undefined

  /** How a title is named in rows and segment headings at this level. */
  const nameOf = useCallback((t: Title) => {
    const parts = titleNameParts(t, level, flat)
    // At the top level a match can come from any library; say which.
    if (level === 'all' && libraries.length > 1) {
      const lib = libById.get(t.library_id)?.title
      // Last, so the show and season stay readable when the line is cut short.
      if (lib) parts.context = [parts.context, lib].filter(Boolean).join(' · ')
    }
    return parts
  }, [level, flat, libraries.length, libById])

  // ── Markers: counts per title, loaded for the libraries in scope when the view opens ──
  const [markerCounts, setMarkerCounts] = useState<Map<string, number>>(new Map())
  const [markerNotCached, setMarkerNotCached] = useState<Set<string>>(new Set())
  const [markerSync, setMarkerSync] = useState<SyncProgress | null>(null)
  const markerLoaded = useRef<Set<string>>(new Set())
  const loadMarkerCounts = useCallback(async (libIds: string[]) => {
    await Promise.all(libIds.map(async libId => {
      try {
        const d = await api.get<{ titles: TitleRow[]; cached: boolean }>(`/api/markers/libraries/${libId}/titles`)
        markerLoaded.current.add(libId)
        setMarkerNotCached(prev => {
          const n = new Set(prev)
          d.cached === false ? n.add(libId) : n.delete(libId)
          return n
        })
        // Keyed by rating key and by guid: the marker cache and the scan jobs can disagree on either one.
        setMarkerCounts(prev => {
          const n = new Map(prev)
          for (const t of d.titles || []) { n.set(t.rating_key, t.marker_count); n.set(t.plex_guid, t.marker_count) }
          return n
        })
      } catch {
        // Counts stay unknown (shown as 0); the next markers_changed event or view switch retries.
      }
    }))
  }, [])
  const libsInScope = scopeTitle ? [scopeTitle.library_id] : scope.lib ? [scope.lib] : libraries.map(l => l.id)
  const libsKey = libsInScope.join(',')
  useEffect(() => {
    if (view !== 'markers') return
    const missing = libsInScope.filter(id => !markerLoaded.current.has(id))
    if (missing.length) loadMarkerCounts(missing)
  }, [view, libsKey, loadMarkerCounts])
  useLiveRefresh(['markers_changed'], () => {
    if (markerLoaded.current.size) loadMarkerCounts(Array.from(markerLoaded.current))
  }, { debounceMs: 1000, fallbackMs: 0 })
  const markerCount = (t: Title) => markerCounts.get(t.rating_key) ?? markerCounts.get(t.plex_guid) ?? 0
  const onMarkerCountChange = useCallback((key: string, count: number) => {
    setMarkerCounts(prev => new Map(prev).set(key, count))
  }, [])

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
    const withSegs = list.filter(t => t.segment_count > 0 || fpOf(t) > 0)
    if (withSegs.length === 0) return
    if (skipDeleteConfirm) { runDelete(label, withSegs); return }
    setPendingDelete({ label, guids: withSegs.map(t => t.plex_guid), count: sum(withSegs, t => t.segment_count + fpOf(t)), busy: false })
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

  // Pulls markers from Plex for every listed title, five at a time with live progress.
  const syncMarkers = async (list: Title[]) => {
    if (list.length === 0 || markerSync && !markerSync.finished) return
    let done = 0, failed = 0, totalMarkers = 0
    setMarkerSync({ done: 0, total: list.length, failed: 0, totalMarkers: 0, finished: false })
    await pool(list, async t => {
      try {
        const r = await api.post<{ count: number }>(`/api/markers/titles/${t.rating_key}/sync`, { plex_guid: t.plex_guid })
        totalMarkers += r.count ?? 0
      } catch {
        failed++
      } finally {
        done++
        setMarkerSync({ done, total: list.length, failed, totalMarkers, finished: false })
      }
    })
    await loadMarkerCounts(Array.from(new Set(list.map(t => t.library_id))))
    setMarkerSync({ done, total: list.length, failed, totalMarkers, finished: true })
  }
  // Reloads a library's marker title list from Plex (the marker cache), then its counts.
  const refreshMarkerList = (libId: string) => withBusy(`mrefresh:${libId}`, async () => {
    try {
      await api.post(`/api/markers/libraries/${libId}/refresh`)
      await loadMarkerCounts([libId])
    } catch (err: any) {
      toast.error(`Could not reload the marker list from Plex: ${err.message || 'Unknown error'}`)
    }
  })

  // Right-click rescan menu (#137): the listed titles that have segments, or the whole level.
  const openScanMenu = (e: React.MouseEvent, s: Scope) => {
    e.preventDefault()
    if (s.guid) {
      const t = titles.find(x => x.plex_guid === s.guid)
      if (!t) return
      setScanMenu({ x: e.clientX, y: e.clientY, label: nameOf(t).name, guids: [t.plex_guid] })
      return
    }
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
    const allIn = guids.length > 0 && guids.every(g => next.has(g))
    for (const g of guids) allIn ? next.delete(g) : next.add(g)
    return next
  })
  const selectedTitles = matched.filter(t => selected.has(t.plex_guid))

  // ── Breadcrumb ────────────────────────────────────────────────────────────────
  const pathOf = (t: Title): Scope => (t.media_type === 'episode'
    ? { lib: t.library_id, show: showKeyOf(t), season: parseEpisodeTitle(t.title).season }
    : { lib: t.library_id })
  const path: Scope = scopeTitle ? pathOf(scopeTitle) : scope
  const crumbs: { label: string; scope: Scope }[] = [{ label: 'All libraries', scope: {} }]
  {
    const anyInPath = scopeTitle ?? scopeTitles[0]
    if (path.lib) crumbs.push({ label: libById.get(path.lib)?.title ?? 'Library', scope: { lib: path.lib } })
    if (path.show) crumbs.push({ label: anyInPath ? showNameOf(anyInPath) : 'Show', scope: { lib: path.lib, show: path.show } })
    if (path.season) crumbs.push({ label: path.season, scope: { lib: path.lib, show: path.show, season: path.season } })
    if (scopeTitle) crumbs.push({ label: nameOf(scopeTitle).name, scope })
  }
  const heading = crumbs[crumbs.length - 1].label

  // ── Rendering helpers ─────────────────────────────────────────────────────────
  const PlexLink = ({ ratingKey, label }: { ratingKey?: string; label: string }) => {
    const url = plexWebUrl(ratingKey)
    if (!url) return null
    return (
      <a href={url} target="_blank" rel="noopener noreferrer" title={label} aria-label={label} className="p-1.5 text-gray-400 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors">
        <ExternalLink size={15} />
      </a>
    )
  }

  const titleActions = (t: Title, name: string) => (
    <>
      <ActionIcon label="Scan tonight" onClick={() => scanTitles([t], false)} disabled={busy[`scan:${t.plex_guid}`]}><Moon size={15} /></ActionIcon>
      <ActionIcon label="Scan now" onClick={() => scanTitles([t], true)} disabled={busy[`scan:${t.plex_guid}`]} tone="orange"><Zap size={15} /></ActionIcon>
      <ActionIcon label={t.ignored ? `Un-ignore ${name}` : `Ignore ${name}`} onClick={() => setIgnored([t], !t.ignored, name)} tone="yellow">
        {t.ignored ? <Eye size={15} /> : <EyeOff size={15} />}
      </ActionIcon>
      <PlexLink ratingKey={t.rating_key} label="Open in Plex" />
      {(t.segment_count > 0 || fpOf(t) > 0) && (
        <ActionIcon label={`Delete all segments for ${name}`} onClick={() => deleteSegments(name, [t])} tone="red"><Trash2 size={15} /></ActionIcon>
      )}
    </>
  )

  const groupActions = (g: Group) => {
    const what = g.kind === 'season' ? g.season : 'whole show'
    const all = titles.filter(t => inScope(t, { lib: g.libId, show: g.showKey, season: g.season || undefined }))
    return (
      <>
        <ActionIcon label={`Scan ${what} tonight`} onClick={() => scanGroup(g, false)}><Moon size={15} /></ActionIcon>
        <ActionIcon label={`Scan ${what} now`} onClick={() => scanGroup(g, true)} tone="orange"><Zap size={15} /></ActionIcon>
        <ActionIcon label={`Ignore all episodes in this ${g.kind}`} onClick={() => setIgnored(all, true, g.label)} tone="yellow"><EyeOff size={15} /></ActionIcon>
        <ActionIcon label={`Un-ignore all episodes in this ${g.kind}`} onClick={() => setIgnored(all, false, g.label)}><Eye size={15} /></ActionIcon>
        <PlexLink ratingKey={g.plexKey} label={`Open ${g.kind} in Plex`} />
        {sum(g.titles, t => t.segment_count + fpOf(t)) > 0 && (
          <ActionIcon label={`Delete all segments for this ${g.kind}`} onClick={() => deleteSegments(g.kind === 'season' ? `${g.showName} – ${g.season}` : g.showName, g.titles)} tone="red"><Trash2 size={15} /></ActionIcon>
        )}
      </>
    )
  }

  const libraryActions = (lib: Library) => (
    <>
      <ActionIcon label="Scan library tonight" onClick={() => scanLibrary(lib, false)}><Moon size={15} /></ActionIcon>
      <ActionIcon label="Scan library now" onClick={() => scanLibrary(lib, true)} tone="orange"><Zap size={15} /></ActionIcon>
      <ActionIcon label="Sync from Plex: pick up titles added, removed or changed in Plex" onClick={() => syncLibrary(lib)} disabled={busy[`sync:${lib.id}`]}>
        <RefreshCw size={15} className={busy[`sync:${lib.id}`] ? 'animate-spin' : ''} />
      </ActionIcon>
      <ActionIcon label="Retry failed: queue every failed title in this library again and clear ignored ones from the list" onClick={() => retryFailed(lib)} disabled={busy[`retry:${lib.id}`]}>
        <RotateCcw size={15} />
      </ActionIcon>
    </>
  )

  // Marker actions, added to the header in the Markers view.
  const markerActions = (list: Title[], libId?: string) => (
    <>
      {libId && (
        <ActionIcon label="Reload the marker title list from Plex" onClick={() => refreshMarkerList(libId)} disabled={busy[`mrefresh:${libId}`]}>
          <RefreshCw size={15} className={busy[`mrefresh:${libId}`] ? 'animate-spin' : ''} />
        </ActionIcon>
      )}
      <ActionIcon label={`Pull markers from Plex for ${plural(list.length, 'listed title')}`} onClick={() => syncMarkers(list)} disabled={!!markerSync && !markerSync.finished}>
        <DownloadCloud size={15} />
      </ActionIcon>
    </>
  )

  const countFor = (list: Title[]) => view === 'markers'
    ? <CountCell count={sum(list, markerCount)} unit="markers" />
    : <CountCell count={sum(list, t => t.segment_count)} falsePositives={sum(list, fpOf)} unit="segments" />

  const titleImage = (t: Title): RowImage => (t.media_type === 'episode'
    ? { kind: 'still', src: t.thumb_url }
    : { kind: 'poster', src: t.poster_url || t.thumb_url })

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggleExpanded = (guid: string) => setExpanded(prev => {
    const next = new Set(prev)
    if (next.has(guid)) next.delete(guid)
    else next.add(guid)
    return next
  })

  const renderRow = (row: Row) => {
    if (row.kind === 'library') {
      const { lib, titles: list } = row
      const shows = new Set(list.filter(t => t.media_type === 'episode').map(showKeyOf)).size
      return (
        <BrowseRow
          key={`l:${lib.id}`}
          image={{ kind: 'icon', icon: lib.type === 'movie' ? 'movie' : 'show' }}
          name={lib.title}
          context={lib.type === 'show' ? `${plural(shows, 'show')} · ${plural(list.length, 'episode')}` : plural(list.length, 'title')}
          status={<GroupStatus titles={list} />}
          scanned={scannedCell(list)}
          count={countFor(list)}
          actions={libraryActions(lib)}
          checked={list.length > 0 && list.every(t => selected.has(t.plex_guid))}
          onCheck={() => toggleGuids(list.map(t => t.plex_guid))}
          onOpen={() => go({ lib: lib.id })}
          onContextMenu={e => openScanMenu(e, { lib: lib.id })}
        />
      )
    }
    if (row.kind === 'group') {
      const g = row.group
      const s: Scope = { lib: g.libId, show: g.showKey, season: g.season || undefined }
      const seasons = new Set(g.titles.map(t => parseEpisodeTitle(t.title).season)).size
      const context = g.kind === 'show'
        ? [level === 'all' && libraries.length > 1 ? libById.get(g.libId)?.title : '', plural(seasons, 'season'), plural(g.titles.length, 'episode')].filter(Boolean).join(' · ')
        : [flat || level !== 'show' ? g.showName : '', plural(g.titles.length, 'episode')].filter(Boolean).join(' · ')
      return (
        <BrowseRow
          key={`g:${g.libId}:${g.showKey}:${g.season}`}
          image={{ kind: 'poster', src: g.posterUrl }}
          name={g.label}
          context={context}
          badges={<IgnoredBadge titles={g.titles} />}
          status={<GroupStatus titles={g.titles} />}
          scanned={scannedCell(g.titles)}
          count={countFor(g.titles)}
          actions={groupActions(g)}
          checked={g.titles.length > 0 && g.titles.every(t => selected.has(t.plex_guid))}
          onCheck={() => toggleGuids(g.titles.map(t => t.plex_guid))}
          onOpen={() => go(s)}
          onContextMenu={e => openScanMenu(e, s)}
          ignored={g.titles.length > 0 && g.titles.every(t => t.ignored)}
        />
      )
    }
    const t = row.title
    const { name, context } = nameOf(t)
    const open = expanded.has(t.plex_guid)
    return (
      <div key={t.plex_guid}>
      <BrowseRow
        image={titleImage(t)}
        name={name}
        context={context}
        // The reason a title failed, so a Failed title never stands unexplained (#190).
        note={t.status === 'failed' && t.error_msg ? <p className="text-xs text-red-400/80 truncate" title={t.error_msg}>{failureText(t.error_msg)}</p> : undefined}
        badges={t.ignored ? <span className="text-xs bg-yellow-500/15 text-yellow-300 px-2 py-0.5 rounded-full flex-shrink-0">Ignored</span> : undefined}
        status={<StatusBadge status={t.status} progress={t.progress} />}
        scanned={scannedCell([t])}
        count={countFor([t])}
        actions={titleActions(t, name)}
        checked={selected.has(t.plex_guid)}
        onCheck={() => toggleGuids([t.plex_guid])}
        // A title expands into its segments in place instead of opening its own page (#209).
        onOpen={() => toggleExpanded(t.plex_guid)}
        expanded={open}
        onContextMenu={e => openScanMenu(e, { guid: t.plex_guid })}
        ignored={t.ignored}
      />
      {open && <TitleSegments title={t} onChanged={x => reloadLibrary(x.library_id)} />}
      </div>
    )
  }

  // The list header names what the rows are, e.g. "63 shows · 11 movies" or "12 episodes".
  const countText = (() => {
    const n = { library: 0, show: 0, season: 0, movie: 0, episode: 0 }
    for (const r of rows) {
      if (r.kind === 'library') n.library++
      else if (r.kind === 'group') n[r.group.kind]++
      else n[r.title.media_type === 'episode' ? 'episode' : 'movie']++
    }
    const parts = [
      n.library && `${n.library} ${n.library === 1 ? 'library' : 'libraries'}`,
      n.show && plural(n.show, 'show'),
      n.season && plural(n.season, 'season'),
      n.movie && plural(n.movie, 'movie'),
      n.episode && plural(n.episode, 'episode'),
    ].filter(Boolean)
    return (flat ? 'Matches: ' : '') + parts.join(' · ')
  })()

  const headerImage: RowImage | null = scopeTitle ? titleImage(scopeTitle)
    : scopeGroup ? { kind: 'poster', src: scopeGroup.posterUrl }
    : null

  // What the level holds, counted like the list: hidden (ignored, unscannable) titles are not in it.
  const headerContext = (() => {
    if (scopeTitle) return titleNameParts(scopeTitle, 'all', true).context
    const shown = visible.filter(t => inScope(t, scope))
    const eps = shown.filter(t => t.media_type === 'episode')
    const seasons = new Set(eps.map(t => `${showKeyOf(t)}:${parseEpisodeTitle(t.title).season}`)).size
    const shows = new Set(eps.map(showKeyOf)).size
    const movies = shown.length - eps.length
    const parts: string[] = []
    if (level !== 'show' && level !== 'season' && shows) parts.push(plural(shows, 'show'))
    if (level === 'show') parts.push(plural(seasons, 'season'))
    if (eps.length) parts.push(plural(eps.length, 'episode'))
    if (movies) parts.push(plural(movies, eps.length ? 'movie' : 'title'))
    return parts.join(' · ') || 'Nothing here'
  })()

  const inputCls = 'px-2 py-1.5 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-200 placeholder-muted focus:border-plex-orange/50'
  const views = level === 'title' ? VIEWS.filter(v => v.id !== 'titles') : VIEWS
  const showTitleFilters = level !== 'title'
  const filtersButton = showTitleFilters || view === 'segments'
  const sidebarCount = (libId?: string) => libId ? matchedAll.filter(t => t.library_id === libId).length : matchedAll.length
  const activeLib = path.lib

  return (
    <div className="flex gap-5 h-full">
      {/* Libraries: the only list beside the main one, so a show appears in one place (#208). */}
      <aside className="hidden md:flex flex-col w-52 flex-shrink-0 min-h-0">
        <h1 className="text-2xl font-bold text-gray-100 mb-3">Browse</h1>
        <nav aria-label="Libraries" className="space-y-0.5 overflow-y-auto">
          {[{ id: '', title: 'All libraries', type: 'all' }, ...libraries].map(lib => {
            const active = lib.id ? activeLib === lib.id : !activeLib
            const Icon = lib.type === 'all' ? Layers : lib.type === 'movie' ? Film : Tv
            return (
              <button
                key={lib.id || 'all'}
                onClick={() => go(lib.id ? { lib: lib.id } : {})}
                aria-current={active ? 'true' : undefined}
                onContextMenu={lib.id ? e => openScanMenu(e, { lib: lib.id }) : undefined}
                className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-left transition-colors ${active ? 'bg-plex-orange/20 text-plex-orange' : 'text-gray-300 hover:text-gray-100 hover:bg-white/5'}`}
              >
                <Icon size={16} className="flex-shrink-0" />
                <span className="truncate">{lib.title}</span>
                <span className="ml-auto text-xs tabular-nums text-muted">{sidebarCount(lib.id || undefined)}</span>
              </button>
            )
          })}
        </nav>
      </aside>

      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        <h1 className="md:hidden text-xl font-bold text-gray-100 mb-2">Browse</h1>

        {/* Header: where you are, what it holds, and what can be done to all of it */}
        <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-1 text-xs text-muted mb-1 min-h-[1rem]">
          {crumbs.slice(0, -1).map((c, i) => (
            <span key={i} className="flex items-center gap-1">
              <button onClick={() => go(c.scope)} className="hover:text-gray-100">{c.label}</button>
              <ChevronRight size={11} />
            </span>
          ))}
        </nav>
        <div className="flex items-center gap-3 mb-3 min-w-0">
          {headerImage && headerImage.kind !== 'icon' && headerImage.src && (
            <SafeImg src={headerImage.src} className={`${headerImage.kind === 'still' ? 'w-28 h-16' : 'w-11 h-16'} rounded bg-plex-border flex-shrink-0`} />
          )}
          <div className="min-w-0 mr-auto">
            <div className="flex items-center gap-2 min-w-0">
              <h2 className="text-xl font-semibold text-gray-100 truncate">{heading}</h2>
              {scopeTitle && <StatusBadge status={scopeTitle.status} progress={scopeTitle.progress} />}
              {scopeTitle?.ignored && <span className="text-xs bg-yellow-500/15 text-yellow-300 px-2 py-0.5 rounded-full">Ignored</span>}
              {scopeGroup && <IgnoredBadge titles={scopeTitles} />}
            </div>
            <p className="text-xs text-muted truncate">
              {headerContext}
              {scopeTitle?.finished_at && ` · Scanned ${scannedCell([scopeTitle]).full}`}
            </p>
            {scopeTitle?.status === 'failed' && scopeTitle.error_msg && (
              <p className="text-xs text-red-400/80 break-words" title={scopeTitle.error_msg}>{failureText(scopeTitle.error_msg)}</p>
            )}
          </div>
          <div className="flex items-center gap-0.5 flex-shrink-0">
            {view === 'markers' && level !== 'title' && markerActions(matched, scope.lib)}
            {/* With titles selected, the header's scan buttons scan the selection, never the whole
                library or show: they are the buttons people reach for first (#215). */}
            {selectedTitles.length > 0 && !scopeTitle ? (
              <>
                <span className="text-xs text-plex-orange mr-1 whitespace-nowrap">{plural(selectedTitles.length, 'selected title')}</span>
                <ActionIcon label={`Scan ${plural(selectedTitles.length, 'selected title')} tonight`} onClick={() => { scanTitles(selectedTitles, false); setSelected(new Set()) }}><Moon size={15} /></ActionIcon>
                <ActionIcon label={`Scan ${plural(selectedTitles.length, 'selected title')} now`} onClick={() => { scanTitles(selectedTitles, true); setSelected(new Set()) }} tone="orange"><Zap size={15} /></ActionIcon>
              </>
            ) : (
              <>
                {level === 'library' && scopeLib && libraryActions(scopeLib)}
                {scopeGroup && groupActions(scopeGroup)}
                {scopeTitle && titleActions(scopeTitle, heading)}
              </>
            )}
          </div>
        </div>

        {/* Toolbar: one row. The name search is always here; the other filters open below it. */}
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <div role="tablist" aria-label="Show" className="flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-plex-border">
            {views.map(v => (
              <button
                key={v.id}
                role="tab"
                aria-selected={view === v.id}
                onClick={() => go(scope, v.id)}
                className={`px-3 py-1 text-sm rounded-md transition-colors ${view === v.id ? 'bg-plex-orange text-black font-semibold' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'}`}
              >
                {v.label}
              </button>
            ))}
          </div>
          {showTitleFilters && (
            <div className="relative flex-1 min-w-[12rem]">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
              <input type="search" placeholder="Search movies, shows, seasons, episodes…" aria-label="Filter by name" value={tf.name} onChange={e => patchTf({ name: e.target.value })} className={`${inputCls} w-full pl-8`} />
            </div>
          )}
          {filtersButton && (
            <button
              onClick={() => setFiltersOpen(o => !o)}
              aria-expanded={filtersOpen}
              aria-controls="browse-filters"
              className={`flex items-center gap-1.5 ${inputCls} ${filtersOpen || panelFilterCount ? 'border-plex-orange/50 text-plex-orange' : 'hover:text-white'}`}
            >
              <SlidersHorizontal size={14} />
              Filters
              {panelFilterCount > 0 && <span className="text-xs tabular-nums bg-plex-orange text-black rounded-full px-1.5">{panelFilterCount}</span>}
            </button>
          )}
          {showTitleFilters && (
            <div className="flex items-stretch">
              <select value={sortBy} aria-label="Sort by" onChange={e => setSortBy(e.target.value as SortOption)} className={`${inputCls} rounded-r-none`}>
                <option value="date-added">Date added</option>
                <option value="scanned">Scan date</option>
                <option value="title">Name</option>
                <option value="year">Release year</option>
                <option value="segments">Segments</option>
              </select>
              <button onClick={() => setSortDesc(!sortDesc)} aria-label={`Sort direction: ${sortDirLabel}`} title="Reverse the order" className={`${inputCls} -ml-px rounded-l-none flex items-center gap-1 whitespace-nowrap hover:text-white`}>
                {sortDesc ? <ArrowDown size={13} /> : <ArrowUp size={13} />}
                {sortDirLabel}
              </button>
            </div>
          )}
        </div>

        {filtersOpen && filtersButton ? (
          <FilterPanel
            titles={tf}
            onTitles={patchTf}
            statusCounts={statusCounts}
            ratings={availableRatings}
            castSuggestions={castSuggestions}
            castSort={castSort}
            onCastSort={setCastSort}
            actorPhotos={actorPhotos}
            showTitleSection={showTitleFilters}
            segments={sf}
            onSegments={setSf}
            segmentLabels={segOptions.labels}
            segmentOrigins={segOptions.origins}
            showSegmentSection={view === 'segments'}
          />
        ) : (
          <FilterChips titles={showTitleFilters ? { ...tf, name: '' } : NO_TITLE_FILTERS} onTitles={patchTf} segments={sf} onSegments={setSf} showSegments={view === 'segments'} onClearAll={clearFilters} />
        )}

        {/* One thin line for running scans (#110), and the result of the last action. */}
        {(notice || (scannerStatus && scannerStatus.active_scans.length > 0)) && (
          <div className="mb-2 flex items-center gap-3 text-xs text-muted whitespace-nowrap overflow-hidden">
            {scannerStatus && scannerStatus.active_scans.length > 0 && (
              <span className="flex items-center gap-3 min-w-0" title={scannerStatus.active_scans.map(s => `${s.title} ${Math.round(s.progress * 100)}%`).join('\n')}>
                <span className="text-plex-orange animate-pulse flex-shrink-0">●</span>
                {scannerStatus.active_scans.map(scan => (
                  <span key={scan.guid} className="flex items-baseline gap-1 min-w-0">
                    <span className="text-gray-300 truncate max-w-[14rem]">{scan.title}</span>
                    <span className="text-plex-orange flex-shrink-0">{Math.round(scan.progress * 100)}%</span>
                  </span>
                ))}
              </span>
            )}
            {notice && (
              <span className="flex items-center gap-1 ml-auto min-w-0" role="status">
                <span className="truncate text-gray-300">{notice}</span>
                <button onClick={() => setNotice('')} aria-label="Dismiss" className="text-gray-400 hover:text-white flex-shrink-0"><X size={12} /></button>
              </span>
            )}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden pr-1 pb-6">
          {loading ? (
            <div className="text-muted text-sm">Loading…</div>
          ) : level === 'title' && !scopeTitle ? (
            <div className="text-muted text-sm">This title is no longer in the library. <button onClick={() => go({})} className="text-plex-orange hover:underline">Back to all libraries</button></div>
          ) : level === 'title' && scopeTitle ? (
            view === 'markers' ? (
              <div className="space-y-3">
                <p className="text-sm text-muted">Drag the handles to move an intro or credits marker, then save it to Plex.</p>
                <TitleMarkerPanel
                  key={scopeTitle.plex_guid}
                  defaultOpen
                  title={{
                    plex_guid: scopeTitle.plex_guid, title: scopeTitle.title, rating_key: scopeTitle.rating_key, media_type: scopeTitle.media_type,
                    show_guid: scopeTitle.show_guid ?? '', show_title: scopeTitle.show_title ?? '', show_rating_key: scopeTitle.show_rating_key ?? '',
                    content_rating: scopeTitle.content_rating, year: scopeTitle.year ?? null, thumb_url: scopeTitle.thumb_url,
                    poster_url: scopeTitle.poster_url ?? '', marker_count: markerCount(scopeTitle),
                  }}
                  episodeLabel={heading}
                  onMarkerCountChange={onMarkerCountChange}
                />
              </div>
            ) : (
              <SegmentsView titles={[scopeTitle]} single nameOf={nameOf} onOpenTitle={t => go({ guid: t.plex_guid })} onImported={t => reloadLibrary(t.library_id)} filters={sf} onOptions={onSegOptions} />
            )
          ) : view === 'segments' ? (
            <SegmentsView titles={sorted} single={false} nameOf={nameOf} onOpenTitle={t => go({ guid: t.plex_guid })} onImported={t => reloadLibrary(t.library_id)} filters={sf} onOptions={onSegOptions} />
          ) : (
            <>
              {view === 'markers' && (
                <>
                  {markerSync && <SyncAllBanner progress={markerSync} onDismiss={() => setMarkerSync(null)} />}
                  {libsInScope.filter(id => markerNotCached.has(id)).map(id => (
                    <p key={id} className="mb-2 text-sm text-muted">
                      The marker list of {libById.get(id)?.title ?? 'this library'} has not been loaded from Plex yet.{' '}
                      <button onClick={() => refreshMarkerList(id)} className="text-plex-orange hover:underline">Load it now</button>
                    </p>
                  ))}
                </>
              )}
              {rows.length === 0 ? (
                <div className="text-muted text-sm py-8 text-center">Nothing matches here. Go up a level or clear a filter.</div>
              ) : (
                <div className="rounded-xl border border-plex-border bg-plex-card overflow-hidden">
                  {/* Column header; becomes the bulk-action bar while something is selected. */}
                  <div className="sticky top-0 z-10 flex items-center gap-3 px-3 py-2 bg-plex-dark border-b border-plex-border text-[11px] uppercase tracking-wide text-muted">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={() => setSelected(allSelected ? new Set() : new Set(matchedGuids))}
                      title="Select all filtered titles"
                      aria-label="Select all filtered titles"
                      className={`${COL.check} h-4 accent-plex-orange`}
                    />
                    {selectedTitles.length > 0 ? (
                      <div className="flex-1 flex flex-wrap items-center gap-1 normal-case tracking-normal">
                        <span className="text-sm text-gray-200 mr-2">{plural(selectedTitles.length, 'title')} selected</span>
                        <ActionIcon label="Scan selected tonight" onClick={() => { scanTitles(selectedTitles, false); setSelected(new Set()) }}><Moon size={15} /></ActionIcon>
                        <ActionIcon label="Scan selected now" onClick={() => { scanTitles(selectedTitles, true); setSelected(new Set()) }} tone="orange"><Zap size={15} /></ActionIcon>
                        <ActionIcon label="Ignore selected" onClick={() => setIgnored(selectedTitles, true)} tone="yellow"><EyeOff size={15} /></ActionIcon>
                        <ActionIcon label="Un-ignore selected" onClick={() => setIgnored(selectedTitles, false)}><Eye size={15} /></ActionIcon>
                        {view === 'markers' && (
                          <ActionIcon label="Pull markers from Plex for the selected titles" onClick={() => syncMarkers(selectedTitles)}><DownloadCloud size={15} /></ActionIcon>
                        )}
                        <ActionIcon label="Delete segments of selected" onClick={() => deleteSegments(plural(selectedTitles.length, 'selected title'), selectedTitles)} tone="red"><Trash2 size={15} /></ActionIcon>
                        <button onClick={() => setSelected(new Set())} className="ml-auto text-xs text-gray-400 hover:text-white">Clear selection</button>
                      </div>
                    ) : (
                      <>
                        <span className={COL.image} />
                        <span className={COL.name}>{countText}</span>
                        <span className={COL.status}>Status</span>
                        <span className={COL.scanned}>Scanned</span>
                        <span className={COL.count}>{view === 'markers' ? 'Markers' : 'Segments'}</span>
                        {/* Room for the row actions and chevron, so the columns above line up. */}
                        <span className={COL.actions} />
                        <span className="w-4 flex-shrink-0" />
                      </>
                    )}
                  </div>
                  {rows.slice(0, limit).map(renderRow)}
                </div>
              )}
              {rows.length > limit && (
                <div className="text-center pt-3">
                  <button onClick={() => setLimit(l => l + PAGE)} className="btn-outline">Show more ({rows.length - limit} left)</button>
                </div>
              )}
            </>
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

