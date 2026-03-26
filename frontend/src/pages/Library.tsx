import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'
import { Film, Tv, ChevronRight, ChevronDown, RotateCcw, Zap, Moon, RefreshCw, ExternalLink, AlertTriangle, Trash2, SkipForward, Play } from 'lucide-react'

interface Library {
  id: string
  title: string
  type: string
}

interface Title {
  plex_guid: string
  rating_key: string
  title: string
  status: string
  progress: number
  finished_at?: string | null
  thumb_url: string
  poster_url?: string
  show_guid?: string
  show_title?: string
  show_rating_key?: string
  season_rating_key?: string
  segment_count: number
  content_rating: string
  media_type: string
  year?: number | null
  ignored: boolean
}

interface Segment {
  id: number
  plex_guid: string
  title: string
  start_ms: number
  end_ms: number
  confidence: number
  has_thumbnail: boolean
  thumbnail_url: string
  created_at: string
  labels?: string
}

interface ScannerStatus {
  queue_size: number
  current_scan: string | null
  current_title: string | null
  current_progress: number
  current_scans: string[]
  active_scans: { guid: string; title: string; progress: number; status: string }[]
  workers_configured: number
  workers_active: number
  workers_idle: number
  paused: boolean
}

const STATUS_TABS = ['all', 'pending', 'scanning', 'done', 'failed'] as const
type StatusTab = typeof STATUS_TABS[number]

const SORT_OPTIONS = ['title', 'date-added', 'year', 'year-release', 'segments'] as const
type SortOption = typeof SORT_OPTIONS[number]

interface ParsedEpisodeTitle {
  show: string
  season: string
  episode: string
}

interface SeasonGroup {
  season: string
  episodes: Title[]
}

interface ShowGroup {
  show_key: string
  show: string
  seasons: SeasonGroup[]
  episodes: Title[]
  poster_url: string
}

function parseEpisodeTitle(title: string): ParsedEpisodeTitle {
  const parts = title.split(' – ')
  if (parts.length >= 3) {
    return {
      show: parts[0].trim(),
      season: parts[1].trim(),
      episode: parts.slice(2).join(' – ').trim(),
    }
  }
  return {
    show: 'Unknown Show',
    season: 'Unknown Season',
    episode: title,
  }
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

function formatFinishedAt(value?: string | null): string {
  if (!value) return ''
  const dt = new Date(value)
  if (Number.isNaN(dt.getTime())) return ''
  return dt.toLocaleString()
}

function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

function renderLabels(labels?: string): React.ReactNode {
  if (!labels || !labels.trim()) return null
  const labelArray = labels.split(',').filter(l => l.trim())
  if (labelArray.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {labelArray.map(label => {
        const clean = label.trim()
        const short = clean.replace('_EXPOSED', '').replace('FEMALE_', 'F ').replace('MALE_', 'M ').replace('GENITALIA', 'Gen.').replace('BREAST', 'Breast').replace('_', ' ')
        return (
          <span key={clean} className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 border border-red-500/30" title={clean}>
            {short}
          </span>
        )
      })}
    </div>
  )
}

export default function Library() {
  const [libraries, setLibraries] = useState<Library[]>([])
  const [selected, setSelected] = useState<Library | null>(null)
  const [titles, setTitles] = useState<Title[]>([])
  const [loadingTitles, setLoadingTitles] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [scanning, setScanning] = useState<Record<string, boolean>>({})
  const [filter, setFilter] = useState('')
  const [ratingFilter, setRatingFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<StatusTab>('all')
  const [sortBy, setSortBy] = useState<SortOption>('date-added')
  const [sortDesc, setSortDesc] = useState(true)
  const [showIgnored, setShowIgnored] = useState(false)
  const [scannerStatus, setScannerStatus] = useState<ScannerStatus | null>(null)
  const [selectedGuids, setSelectedGuids] = useState<string[]>([])
  const [expandedShows, setExpandedShows] = useState<Set<string>>(new Set())
  const [expandedSeasons, setExpandedSeasons] = useState<Set<string>>(new Set())
  const [expandedSegments, setExpandedSegments] = useState<Set<string>>(new Set())
  const [loadedSegments, setLoadedSegments] = useState<Record<string, Segment[]>>({})
  const [loadingSegments, setLoadingSegments] = useState<Set<string>>(new Set())
  const [deletingSegs, setDeletingSegs] = useState<Record<number, boolean>>({})
  const [jumpingSegs, setJumpingSegs] = useState<Record<number, boolean>>({})
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const [machineId, setMachineId] = useState('')

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/libraries').then(d => setLibraries(d.libraries))
  }, [])

  // Fetch Plex server machine identifier for building web deep links.
  useEffect(() => {
    api.get<{ machine_identifier: string }>('/api/settings/plex-server-id')
      .then(d => setMachineId(d.machine_identifier))
      .catch(() => {})
  }, [])

  // Poll scanner status every 3s.
  // AbortController prevents stale responses from overwriting newer state.
  useEffect(() => {
    let controller = new AbortController()

    const tick = () => {
      controller.abort()
      controller = new AbortController()
      api.get<ScannerStatus>('/api/sessions/scanner-status', { signal: controller.signal })
        .then(setScannerStatus)
        .catch(() => {})
    }

    tick()
    const id = setInterval(tick, 3000)
    return () => {
      clearInterval(id)
      controller.abort()
    }
  }, [])

  // Auto-refresh titles while scanning; cancel in-flight request before each new tick.
  useEffect(() => {
    if (!selected || !scannerStatus || scannerStatus.active_scans.length === 0) return
    let controller = new AbortController()

    const tick = async () => {
      controller.abort()
      controller = new AbortController()
      try {
        const d = await api.get<{ titles: Title[] }>(
          `/api/libraries/${selected.id}/titles`,
          { signal: controller.signal },
        )
        setTitles(d.titles)
      } catch {}
    }

    const id = setInterval(tick, 5000)
    return () => {
      clearInterval(id)
      controller.abort()
    }
  }, [selected, scannerStatus])

  const loadTitles = useCallback(async (libId: string) => {
    const d = await api.get<{ titles: Title[] }>(`/api/libraries/${libId}/titles`)
    return d.titles
  }, [])

  const plexWebUrl = (ratingKey: string) =>
    machineId && ratingKey
      ? `https://app.plex.tv/desktop/#!/server/${machineId}/details?key=%2Flibrary%2Fmetadata%2F${ratingKey}`
      : ''

  const toggleSegments = async (guid: string) => {
    const isOpen = expandedSegments.has(guid)
    setExpandedSegments(prev => {
      const next = new Set(prev)
      isOpen ? next.delete(guid) : next.add(guid)
      return next
    })
    // Lazy-fetch on first expand only.
    if (!isOpen && !(guid in loadedSegments)) {
      setLoadingSegments(prev => new Set(prev).add(guid))
      try {
        const d = await api.get<{ segments: Segment[] }>(`/api/titles/${encodeURIComponent(guid)}/segments`)
        setLoadedSegments(prev => ({ ...prev, [guid]: d.segments }))
      } catch {
        setLoadedSegments(prev => ({ ...prev, [guid]: [] }))
      } finally {
        setLoadingSegments(prev => { const n = new Set(prev); n.delete(guid); return n })
      }
    }
  }

  const deleteSegmentInline = async (segId: number, guid: string) => {
    setDeletingSegs(d => ({ ...d, [segId]: true }))
    try {
      await api.delete(`/api/segments/${segId}`)
      setLoadedSegments(prev => ({ ...prev, [guid]: (prev[guid] || []).filter(s => s.id !== segId) }))
      setTitles(prev => prev.map(t =>
        t.plex_guid === guid ? { ...t, segment_count: Math.max(0, t.segment_count - 1) } : t
      ))
    } finally {
      setDeletingSegs(d => ({ ...d, [segId]: false }))
    }
  }

  const jumpToSegmentInline = async (segId: number) => {
    setJumpingSegs(j => ({ ...j, [segId]: true }))
    try {
      const d = await api.post<{ ok: boolean; client: string; user: string; seek_to_ms: number }>(`/api/segments/${segId}/jump`)
      if (d.ok) {
        alert(`Jumped playback to segment on ${d.client} (${d.user}) at ${msToTimecode(d.seek_to_ms)}.`)
      }
    } catch (err: any) {
      alert(err?.message || 'Could not jump to segment. Start this title in Plex first.')
    } finally {
      setJumpingSegs(j => ({ ...j, [segId]: false }))
    }
  }

  const renderSegmentsBox = (guid: string) => {
    if (!expandedSegments.has(guid)) return null
    const segs = loadedSegments[guid] || []
    return (
      <div className="mt-2 pt-2 border-t border-plex-border space-y-2">
        {loadingSegments.has(guid) ? (
          <p className="text-xs text-gray-500 px-1">Loading segments…</p>
        ) : segs.length === 0 ? (
          <p className="text-xs text-gray-600 px-1">No segments found</p>
        ) : segs.map(seg => (
          <div key={seg.id} className="flex gap-2 bg-plex-darker border border-plex-border rounded-lg overflow-hidden">
            <div className="w-40 flex-shrink-0 bg-black relative">
              {seg.has_thumbnail ? (
                <img src={seg.thumbnail_url} alt="Flagged frame" className="w-full h-full object-cover" style={{ minHeight: '90px', maxHeight: '135px' }} />
              ) : (
                <div className="w-full h-24 flex items-center justify-center text-gray-700"><AlertTriangle size={20} /></div>
              )}
              <div className="absolute bottom-0.5 left-0.5 bg-black/70 text-xs text-gray-300 px-1 py-0.5 rounded text-[10px]">
                {Math.round(seg.confidence * 100)}%
              </div>
            </div>
            <div className="flex-1 p-2 flex items-center justify-between gap-2 min-w-0">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-xs text-plex-orange">{msToTimecode(seg.start_ms)}</span>
                  <span className="text-gray-600 text-xs">→</span>
                  <span className="font-mono text-xs text-plex-orange">{msToTimecode(seg.end_ms)}</span>
                  <span className="text-xs text-gray-600">({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)</span>
                </div>
                {renderLabels(seg.labels)}
              </div>
              <div className="flex items-center gap-1 flex-shrink-0">
                <button
                  onClick={() => setPreviewSeg(seg)}
                  title="Preview segment video"
                  className="p-1.5 text-gray-600 hover:text-green-400 hover:bg-green-400/10 rounded transition-colors"
                >
                  <Play size={13} />
                </button>
                <button
                  onClick={() => jumpToSegmentInline(seg.id)}
                  disabled={jumpingSegs[seg.id]}
                  title="Jump active Plex playback to this segment"
                  className="p-1.5 text-gray-600 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40"
                >
                  <SkipForward size={13} />
                </button>
                <button
                  onClick={() => deleteSegmentInline(seg.id, guid)}
                  disabled={deletingSegs[seg.id]}
                  title="Remove this segment"
                  className="p-1.5 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors disabled:opacity-40"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    )
  }

  const selectLibrary = async (lib: Library) => {
    setSelected(lib)
    setFilter('')
    setRatingFilter('all')
    setStatusFilter('all')
    setShowIgnored(false)
    setSelectedGuids([])
    setLoadingTitles(true)
    try {
      // Load titles from DB only — no automatic Plex sync on select.
      // Use the "Sync from Plex" button to pull new titles explicitly.
      setTitles(await loadTitles(lib.id))
    } finally {
      setLoadingTitles(false)
    }
  }

  const syncLibraryNow = async () => {
    if (!selected) return
    setRefreshing(true)
    try {
      await api.post(`/api/libraries/${selected.id}/sync`)
      setTitles(await loadTitles(selected.id))
    } catch (err: any) {
      console.warn('Library sync failed:', err.message)
    } finally {
      setRefreshing(false)
    }
  }

  const refreshTitles = async () => {
    if (!selected) return
    setRefreshing(true)
    try {
      setTitles(await loadTitles(selected.id))
    } finally {
      setRefreshing(false)
    }
  }

  const scanTitle = async (guid: string, now: boolean) => {
    setScanning(s => ({ ...s, [guid]: true }))
    try {
      await api.post('/api/scan/title', { plex_guid: guid, now, library_id: selected?.id || null })
      setTitles(await loadTitles(selected!.id))
    } catch (err: any) {
      alert(`Failed to scan: ${err.message || 'Unknown error'}`)
    } finally {
      setScanning(s => ({ ...s, [guid]: false }))
    }
  }

  const scanLibrary = async (libId: string, now: boolean) => {
    try {
      await api.post(`/api/scan/library/${libId}`, { now })
      setTitles(await loadTitles(libId))
    } catch (err: any) {
      alert(`Failed to scan library: ${err.message || 'Unknown error'}`)
    }
  }

  const toggleSelected = (guid: string) => {
    setSelectedGuids(prev => prev.includes(guid) ? prev.filter(g => g !== guid) : [...prev, guid])
  }

  const scanSelected = async (now: boolean) => {
    if (!selected || selectedGuids.length === 0) return
    // Limit to 5 concurrent requests to avoid overwhelming the server.
    const CONCURRENCY = 5
    const errors: string[] = []
    const queue = [...selectedGuids]

    const worker = async () => {
      while (queue.length > 0) {
        const guid = queue.shift()
        if (!guid) break
        try {
          await api.post('/api/scan/title', { plex_guid: guid, now, library_id: selected.id })
        } catch (err: any) {
          errors.push(err.message || guid)
        }
      }
    }

    await Promise.all(Array.from({ length: CONCURRENCY }, worker))
    setSelectedGuids([])
    setTitles(await loadTitles(selected.id))
    if (errors.length > 0) {
      alert(`${errors.length} title(s) failed to enqueue:\n${errors.slice(0, 5).join('\n')}`)
    }
  }

  const toggleIgnored = async (guid: string, currentIgnored: boolean, label?: string) => {
    if (!selected) return
    const nextIgnored = !currentIgnored
    setTitles(prev => prev.map(t => (t.plex_guid === guid ? { ...t, ignored: nextIgnored } : t)))
    try {
      await api.post(`/api/scan/title/${encodeURIComponent(guid)}/ignore`, { ignored: nextIgnored })
      setTitles(await loadTitles(selected!.id))
    } catch (err: any) {
      setTitles(prev => prev.map(t => (t.plex_guid === guid ? { ...t, ignored: currentIgnored } : t)))
      alert(`Failed to update ignore status${label ? ` for ${label}` : ''}: ${err.message || 'Unknown error'}`)
    }
  }

  const setIgnoredForGuids = async (guids: string[], ignored: boolean, label?: string) => {
    if (!selected || guids.length === 0) return
    const guidSet = new Set(guids)
    const prevIgnoredByGuid = new Map(
      titles
        .filter(t => guidSet.has(t.plex_guid))
        .map(t => [t.plex_guid, t.ignored]),
    )

    setTitles(prev => prev.map(t => (guidSet.has(t.plex_guid) ? { ...t, ignored } : t)))

    try {
      for (const guid of guids) {
        await api.post(`/api/scan/title/${encodeURIComponent(guid)}/ignore`, { ignored })
      }
      setSelectedGuids(prev => prev.filter(g => !guids.includes(g)))
      setTitles(await loadTitles(selected.id))
    } catch (err: any) {
      setTitles(prev => prev.map(t => {
        if (!guidSet.has(t.plex_guid)) return t
        const previousIgnored = prevIgnoredByGuid.get(t.plex_guid)
        return typeof previousIgnored === 'boolean' ? { ...t, ignored: previousIgnored } : t
      }))
      alert(`Failed to update ignore status${label ? ` for ${label}` : ''}: ${err.message || 'Unknown error'}`)
    }
  }

  const availableRatings = Array.from(new Set(titles.map(t => t.content_rating).filter(Boolean))).sort()

  const counts: Record<string, number> = { all: titles.length }
  for (const t of titles) counts[t.status] = (counts[t.status] ?? 0) + 1

  const filtered = titles.filter(t => {
    if (statusFilter !== 'all' && t.status !== statusFilter) return false
    if (filter && !t.title.toLowerCase().includes(filter.toLowerCase())) return false
    if (ratingFilter !== 'all' && t.content_rating !== ratingFilter) return false
    if (!showIgnored && t.ignored) return false
    return true
  })

  const sorted = [...filtered].sort((a, b) => {
    let aVal: any, bVal: any
    
    switch (sortBy) {
      case 'title':
        aVal = a.title.toLowerCase()
        bVal = b.title.toLowerCase()
        break
      case 'date-added':
        // rating_key is a monotonically increasing integer in Plex;
        // higher = more recently added.
        aVal = parseInt(a.rating_key, 10) || 0
        bVal = parseInt(b.rating_key, 10) || 0
        break
      case 'year':
        aVal = a.year ?? 0
        bVal = b.year ?? 0
        break
      case 'year-release':
        aVal = a.year ?? 0
        bVal = b.year ?? 0
        break
      case 'segments':
        aVal = a.segment_count
        bVal = b.segment_count
        break
      default:
        aVal = a.title.toLowerCase()
        bVal = b.title.toLowerCase()
    }

    if (aVal < bVal) return sortDesc ? 1 : -1
    if (aVal > bVal) return sortDesc ? -1 : 1
    return 0
  })

  const isTvLibrary = selected?.type === 'show' || sorted.some(t => t.media_type === 'episode')

  const showGroups: ShowGroup[] = (() => {
    if (!isTvLibrary) return []

    const showMap = new Map<string, { show: string; seasons: Map<string, Title[]> }>()
    for (const t of sorted) {
      if (t.media_type !== 'episode') continue
      const parsed = parseEpisodeTitle(t.title)
      const showKey = t.show_guid || parsed.show
      const showName = t.show_title || parsed.show
      if (!showMap.has(showKey)) {
        showMap.set(showKey, { show: showName, seasons: new Map() })
      }
      const showEntry = showMap.get(showKey)!
      if (!showEntry.seasons.has(parsed.season)) {
        showEntry.seasons.set(parsed.season, [])
      }
      showEntry.seasons.get(parsed.season)!.push(t)
    }

    return Array.from(showMap.entries())
      .map(([showKey, showEntry]) => {
        const allEpisodes = Array.from(showEntry.seasons.values()).flatMap(episodes => episodes)
        const explicitPoster = allEpisodes.find(ep => !!ep.poster_url)?.poster_url ?? ''

        const seasons = Array.from(showEntry.seasons.entries())
          .map(([season, episodes]) => ({
            season,
            episodes: [...episodes].sort((a, b) => a.title.localeCompare(b.title)),
          }))
          .sort((a, b) => a.season.localeCompare(b.season))
        return {
          show_key: showKey,
          show: showEntry.show,
          seasons,
          episodes: seasons.flatMap(s => s.episodes),
          poster_url: explicitPoster,
        }
      })
      .sort((a, b) => a.show.localeCompare(b.show))
  })()

  const filteredGuids = filtered.map(t => t.plex_guid)
  const allFilteredSelected = filteredGuids.length > 0 && filteredGuids.every(g => selectedGuids.includes(g))

  const toggleShowExpanded = (show: string) => {
    setExpandedShows(prev => {
      const next = new Set(prev)
      if (next.has(show)) next.delete(show)
      else next.add(show)
      return next
    })
  }

  const toggleSeasonExpanded = (show: string, season: string) => {
    const key = `${show}__${season}`
    setExpandedSeasons(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  useEffect(() => {
    const valid = new Set(titles.map(t => t.plex_guid))
    setSelectedGuids(prev => prev.filter(g => valid.has(g)))
  }, [titles])

  return (
    <div className="flex gap-6 h-full">
      {/* Library list — desktop only */}
      <div className="hidden md:block w-52 flex-shrink-0">
        <h1 className="text-2xl font-bold text-gray-100 mb-4">Library</h1>
        <div className="space-y-1">
          {libraries.map(lib => (
            <button
              key={lib.id}
              onClick={() => selectLibrary(lib)}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-left transition-colors ${
                selected?.id === lib.id
                  ? 'bg-plex-orange/20 text-plex-orange'
                  : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
              }`}
            >
              {lib.type === 'movie' ? <Film size={16} /> : <Tv size={16} />}
              <span className="truncate">{lib.title}</span>
              <ChevronRight size={14} className="ml-auto opacity-50" />
            </button>
          ))}
        </div>
      </div>

      {/* Titles panel */}
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        {/* Mobile library selector */}
        <div className="flex md:hidden items-center gap-2 mb-4">
          <h1 className="text-xl font-bold text-gray-100">Library</h1>
          <select
            value={selected?.id ?? ''}
            onChange={e => {
              const lib = libraries.find(l => l.id === e.target.value)
              if (lib) selectLibrary(lib)
            }}
            className="flex-1 px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-200 focus:outline-none focus:border-plex-orange/60"
          >
            <option value="">Select library…</option>
            {libraries.map(lib => (
              <option key={lib.id} value={lib.id}>{lib.title}</option>
            ))}
          </select>
        </div>
        {!selected ? (
          <div className="flex items-center justify-center h-64 text-gray-600">
            Select a library to browse titles
          </div>
        ) : (
          <>
            {/* Header row */}
            <div className="flex flex-wrap items-center justify-between mb-3 gap-2">
              <h2 className="text-xl font-semibold text-gray-100 truncate">{selected.title}</h2>
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={syncLibraryNow}
                  disabled={refreshing}
                  title="Sync new titles from Plex into the database"
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-plex-orange/50 transition-colors disabled:opacity-50"
                >
                  <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /> Sync from Plex
                </button>
                <button
                  onClick={refreshTitles}
                  disabled={refreshing}
                  title="Reload titles from local database"
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors disabled:opacity-50"
                >
                  <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /> Refresh
                </button>
                <button
                  onClick={() => scanLibrary(selected.id, false)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-plex-orange/50 transition-colors"
                >
                  <Moon size={13} /> Scan Tonight
                </button>
                <button
                  onClick={() => scanLibrary(selected.id, true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-orange/20 border border-plex-orange/30 rounded-lg text-plex-orange hover:bg-plex-orange/30 transition-colors"
                >
                  <Zap size={13} /> Scan Now
                </button>
              </div>
            </div>

            {/* Scanner progress banner */}
            {scannerStatus && scannerStatus.active_scans.length > 0 && (
              <div className="mb-3 bg-plex-card border border-plex-orange/30 rounded-xl px-4 py-3">
                <div className="flex items-center justify-between text-xs mb-2 text-gray-400">
                  <span>Scanning now</span>
                  <span>{scannerStatus.workers_active}/{scannerStatus.workers_configured} workers active</span>
                </div>
                <div className="space-y-2">
                  {scannerStatus.active_scans.map(scan => (
                    <div key={scan.guid}>
                      <div className="flex items-center justify-between text-xs mb-1.5">
                        <span className="text-plex-orange font-medium flex items-center gap-1.5">
                          <span className="animate-pulse">●</span> Scanning
                        </span>
                        <span className="text-gray-300 truncate mx-3 flex-1">{scan.title}</span>
                        <span className="text-gray-400 flex-shrink-0">{Math.round(scan.progress * 100)}%</span>
                      </div>
                      <div className="h-1.5 bg-plex-border rounded-full overflow-hidden">
                        <div
                          className="h-full bg-plex-orange rounded-full transition-all duration-1000"
                          style={{ width: `${scan.progress * 100}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Status filter tabs */}
            <div className="flex gap-1 mb-3 flex-wrap">
              {STATUS_TABS.map(s => (
                <button
                  key={s}
                  onClick={() => setStatusFilter(s)}
                  className={`px-3 py-1 text-xs rounded-full transition-colors capitalize ${
                    statusFilter === s
                      ? 'bg-plex-orange text-black font-semibold'
                      : 'bg-plex-card border border-plex-border text-gray-400 hover:text-gray-200'
                  }`}
                >
                  {s === 'all' ? 'All' : s.charAt(0).toUpperCase() + s.slice(1)}
                  {counts[s] != null && (
                    <span className="ml-1 opacity-70">({counts[s] ?? 0})</span>
                  )}
                </button>
              ))}
            </div>

            {/* Text + rating filters */}
            <div className="flex flex-wrap gap-2 mb-4">
              <input
                type="text"
                placeholder="Filter titles..."
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="flex-1 px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-plex-orange/50"
              />
              {availableRatings.length > 0 && (
                <select
                  value={ratingFilter}
                  onChange={e => setRatingFilter(e.target.value)}
                  className="px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 focus:outline-none focus:border-plex-orange/50"
                >
                  <option value="all">All ratings</option>
                  {availableRatings.map(r => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              )}
              <select
                value={sortBy}
                onChange={e => setSortBy(e.target.value as SortOption)}
                className="px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 focus:outline-none focus:border-plex-orange/50"
              >
                <option value="date-added">Date Added</option>
                <option value="title">Alphabetical</option>
                <option value="year">Release Year</option>
                <option value="segments">Segments</option>
              </select>
              <button
                onClick={() => setSortDesc(!sortDesc)}
                className="px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
                title={sortDesc ? 'Descending' : 'Ascending'}
              >
                {sortDesc ? '↓' : '↑'}
              </button>
              <label className="inline-flex items-center gap-2 px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 select-none">
                <input
                  type="checkbox"
                  checked={showIgnored}
                  onChange={e => setShowIgnored(e.target.checked)}
                  className="w-4 h-4 accent-plex-orange"
                />
                Show Ignored
              </label>
            </div>

            {/* Multi-select actions */}
            <div className="mb-3 flex items-center gap-2 flex-wrap shrink-0 bg-plex-darker/95 backdrop-blur border border-plex-border shadow-lg rounded-xl px-2.5 py-2">
              <label className="inline-flex items-center gap-2 text-xs text-gray-300 bg-plex-card border border-plex-border px-2.5 py-1.5 rounded-lg">
                <input
                  type="checkbox"
                  checked={allFilteredSelected}
                  onChange={() => setSelectedGuids(allFilteredSelected ? [] : filteredGuids)}
                  className="w-4 h-4 accent-plex-orange"
                />
                Select all filtered
              </label>
              <button
                onClick={() => setSelectedGuids([])}
                disabled={selectedGuids.length === 0}
                className="px-2.5 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors disabled:opacity-40"
              >
                Clear
              </button>
              <button
                onClick={() => scanSelected(false)}
                disabled={selectedGuids.length === 0}
                className="px-2.5 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-plex-orange/50 transition-colors disabled:opacity-40 inline-flex items-center gap-1.5"
              >
                <Moon size={13} /> Scan Selected Tonight ({selectedGuids.length})
              </button>
              <button
                onClick={() => scanSelected(true)}
                disabled={selectedGuids.length === 0}
                className="px-2.5 py-1.5 text-xs bg-plex-orange/20 border border-plex-orange/30 rounded-lg text-plex-orange hover:bg-plex-orange/30 transition-colors disabled:opacity-40 inline-flex items-center gap-1.5"
              >
                <Zap size={13} /> Scan Selected Now ({selectedGuids.length})
              </button>
              <button
                onClick={() => setIgnoredForGuids(selectedGuids, true)}
                disabled={selectedGuids.length === 0}
                className="px-2.5 py-1.5 text-xs bg-yellow-500/20 border border-yellow-500/30 rounded-lg text-yellow-500 hover:bg-yellow-500/30 transition-colors disabled:opacity-40 inline-flex items-center gap-1.5"
              >
                Ignore Selected ({selectedGuids.length})
              </button>
              <button
                onClick={() => setIgnoredForGuids(selectedGuids, false)}
                disabled={selectedGuids.length === 0}
                className="px-2.5 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors disabled:opacity-40 inline-flex items-center gap-1.5"
              >
                ○ Un-ignore Selected ({selectedGuids.length})
              </button>
            </div>

            {/* Title list */}
            <div className="flex-1 min-h-0 overflow-y-auto pr-1 pb-6">
            {loadingTitles ? (
              <div className="text-gray-500 text-sm">Loading...</div>
            ) : filtered.length === 0 ? (
              <div className="text-gray-600 text-sm">No titles found</div>
            ) : isTvLibrary ? (
              <div className="space-y-3">
                {showGroups.map(group => {
                  const allIgnored = group.episodes.length > 0 && group.episodes.every(ep => ep.ignored)
                  const someIgnored = group.episodes.some(ep => ep.ignored)
                  const showOpen = expandedShows.has(group.show_key)
                  return (
                    <div key={group.show_key} className="bg-plex-card border border-plex-border rounded-xl p-3">
                      <div className="flex items-center gap-2">
                        {group.poster_url ? (
                          <img
                            src={group.poster_url}
                            alt={`${group.show} poster`}
                            className="w-16 h-24 object-cover rounded bg-plex-border flex-shrink-0"
                            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                          />
                        ) : (
                          <div className="w-16 h-24 bg-plex-border rounded flex-shrink-0" />
                        )}
                        <button
                          onClick={() => toggleShowExpanded(group.show_key)}
                          className="p-1 text-gray-400 hover:text-white"
                          title={showOpen ? 'Collapse show' : 'Expand show'}
                        >
                          <ChevronRight size={14} className={showOpen ? 'rotate-90 transition-transform' : 'transition-transform'} />
                        </button>
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-semibold text-gray-100 truncate">{group.show}</p>
                          <p className="text-xs text-gray-500">
                            {group.seasons.length} season{group.seasons.length !== 1 ? 's' : ''} • {group.episodes.length} episode{group.episodes.length !== 1 ? 's' : ''}
                          </p>
                        </div>
                        {allIgnored ? (
                          <span className="text-xs bg-yellow-500/20 text-yellow-300 px-2 py-0.5 rounded-full font-semibold">IGNORED SHOW</span>
                        ) : someIgnored ? (
                          <span className="text-xs bg-yellow-500/10 text-yellow-500 px-2 py-0.5 rounded-full font-medium">PARTIALLY IGNORED</span>
                        ) : null}
                        {machineId && plexWebUrl(group.episodes[0]?.show_rating_key || '') && (
                          <a href={plexWebUrl(group.episodes[0]?.show_rating_key || '')} target="_blank" rel="noopener noreferrer" title="Open show in Plex" className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors">
                            <ExternalLink size={13} />
                          </a>
                        )}
                        <button
                          onClick={() => setIgnoredForGuids(group.episodes.map(ep => ep.plex_guid), true, group.show)}
                          className="px-2 py-1 text-xs bg-yellow-500/20 border border-yellow-500/30 rounded text-yellow-500 hover:bg-yellow-500/30 transition-colors"
                          title="Ignore all episodes in this show"
                        >
                          Ignore Show
                        </button>
                        <button
                          onClick={() => setIgnoredForGuids(group.episodes.map(ep => ep.plex_guid), false, group.show)}
                          className="px-2 py-1 text-xs bg-plex-card border border-plex-border rounded text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
                          title="Un-ignore all episodes in this show"
                        >
                          Un-ignore Show
                        </button>
                      </div>

                      {showOpen && (
                        <div className="mt-3 space-y-2">
                          {group.seasons.map(season => {
                            const seasonKey = `${group.show_key}__${season.season}`
                            const seasonOpen = expandedSeasons.has(seasonKey)
                            return (
                              <div key={seasonKey} className="border border-plex-border rounded-lg p-2">
                                <button
                                  onClick={() => toggleSeasonExpanded(group.show_key, season.season)}
                                  className="w-full flex items-center gap-2 text-left"
                                >
                                  <ChevronRight size={13} className={seasonOpen ? 'rotate-90 transition-transform text-gray-400' : 'transition-transform text-gray-400'} />
                                  <span className="text-xs text-gray-300 font-medium">{season.season}</span>
                                  <span className="text-xs text-gray-500 ml-auto">
                                    {season.episodes.length} episode{season.episodes.length !== 1 ? 's' : ''}
                                  </span>
                                  {machineId && plexWebUrl(season.episodes[0]?.season_rating_key || '') && (
                                    <a href={plexWebUrl(season.episodes[0]?.season_rating_key || '')} target="_blank" rel="noopener noreferrer" title="Open season in Plex" onClick={e => e.stopPropagation()} className="p-1 text-gray-600 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors flex-shrink-0">
                                      <ExternalLink size={12} />
                                    </a>
                                  )}
                                </button>

                                {seasonOpen && (
                                  <div className="mt-2 space-y-2">
                                    {season.episodes.map(title => {
                                      const parsed = parseEpisodeTitle(title.title)
                                      return (
                                        <div
                                          key={title.plex_guid}
                                          className={`border rounded-lg p-2 ${
                                            title.ignored
                                              ? 'border-yellow-500/30 bg-yellow-500/5'
                                              : 'border-plex-border bg-black/10'
                                          }`}
                                        >
                                          <div className="flex items-center gap-2">
                                            <input
                                              type="checkbox"
                                              checked={selectedGuids.includes(title.plex_guid)}
                                              onChange={() => toggleSelected(title.plex_guid)}
                                              className="w-4 h-4 accent-plex-orange flex-shrink-0"
                                            />
                                            <div className="flex-1 min-w-0">
                                              <p className="text-sm text-gray-100 truncate">
                                                {parsed.episode}
                                                {title.ignored && <span className="ml-2 text-yellow-300 text-xs font-semibold">IGNORED</span>}
                                              </p>
                                              <div className="flex items-center gap-2 mt-1">
                                                <StatusBadge status={title.status} progress={title.progress} />
                                                {title.finished_at && (
                                                  <span className="text-xs text-gray-500">Finished {formatFinishedAt(title.finished_at)}</span>
                                                )}
                                              </div>
                                            </div>
                                            <div className="flex items-center gap-1.5 flex-shrink-0">
                                              {title.segment_count > 0 && (
                                                <button
                                                  onClick={() => toggleSegments(title.plex_guid)}
                                                  title="Toggle segments"
                                                  className="flex items-center gap-1 px-1.5 py-1 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded hover:bg-red-500/20 transition-colors"
                                                >
                                                  {expandedSegments.has(title.plex_guid) ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                                                  {title.segment_count}
                                                </button>
                                              )}
                                              {machineId && plexWebUrl(title.rating_key) && (
                                                <a href={plexWebUrl(title.rating_key)} target="_blank" rel="noopener noreferrer" title="Open in Plex" className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors">
                                                  <ExternalLink size={13} />
                                                </a>
                                              )}
                                              <button
                                                onClick={() => scanTitle(title.plex_guid, false)}
                                                disabled={scanning[title.plex_guid]}
                                                title="Scan Tonight"
                                                className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-white/5 rounded transition-colors disabled:opacity-40"
                                              >
                                                <Moon size={14} />
                                              </button>
                                              <button
                                                onClick={() => scanTitle(title.plex_guid, true)}
                                                disabled={scanning[title.plex_guid]}
                                                title="Scan Now"
                                                className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40"
                                              >
                                                <Zap size={14} />
                                              </button>
                                              <button
                                                onClick={() => toggleIgnored(title.plex_guid, title.ignored, parsed.episode)}
                                                title={title.ignored ? 'Un-ignore this episode' : 'Ignore this episode'}
                                                className={`p-1.5 rounded transition-colors ${
                                                  title.ignored
                                                    ? 'text-yellow-600 hover:text-yellow-400 hover:bg-yellow-500/10'
                                                    : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                                                }`}
                                              >
                                                {title.ignored ? 'Ignored' : 'Ignore'}
                                              </button>
                                            </div>
                                          </div>
                                          {renderSegmentsBox(title.plex_guid)}
                                        </div>
                                      )
                                    })}
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="grid gap-2">
                {sorted.map(title => (
                  <div
                    key={title.plex_guid}
                    className={`rounded-xl p-3 ${
                      title.ignored
                        ? 'bg-yellow-500/5 border border-yellow-500/30'
                        : 'bg-plex-card border border-plex-border'
                    }`}
                  >
                    <div className="flex items-center gap-3">
                      <input
                        type="checkbox"
                        checked={selectedGuids.includes(title.plex_guid)}
                        onChange={() => toggleSelected(title.plex_guid)}
                        className="w-4 h-4 accent-plex-orange flex-shrink-0"
                      />
                      {title.thumb_url ? (
                        <img
                          src={title.thumb_url}
                          alt=""
                          className="w-16 h-24 object-cover rounded bg-plex-border flex-shrink-0"
                          onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                        />
                      ) : (
                        <div className="w-16 h-24 bg-plex-border rounded flex-shrink-0" />
                      )}
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-gray-100 truncate">
                          {title.ignored && <span className="text-yellow-400 mr-1">[IGNORED]</span>}
                          {title.title}
                        </p>
                        <div className="flex items-center gap-2 mt-1">
                          <StatusBadge status={title.status} progress={title.progress} />
                          {title.media_type === 'episode' && (
                            <span className="text-xs bg-blue-500/15 text-blue-400 px-1.5 py-0.5 rounded font-medium">TV</span>
                          )}
                          {title.content_rating && (
                            <span className="text-xs text-gray-600 bg-white/5 px-1.5 py-0.5 rounded">{title.content_rating}</span>
                          )}
                          {title.finished_at && (
                            <span className="text-xs text-gray-500">Finished {formatFinishedAt(title.finished_at)}</span>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5 flex-shrink-0">
                        {title.segment_count > 0 && (
                          <button
                            onClick={() => toggleSegments(title.plex_guid)}
                            title="Toggle segments"
                            className="flex items-center gap-1 px-1.5 py-1 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded hover:bg-red-500/20 transition-colors"
                          >
                            {expandedSegments.has(title.plex_guid) ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                            {title.segment_count}
                          </button>
                        )}
                        {machineId && plexWebUrl(title.rating_key) && (
                          <a href={plexWebUrl(title.rating_key)} target="_blank" rel="noopener noreferrer" title="Open in Plex" className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors">
                            <ExternalLink size={14} />
                          </a>
                        )}
                        <button
                          onClick={() => scanTitle(title.plex_guid, false)}
                          disabled={scanning[title.plex_guid]}
                          title="Scan Tonight"
                          className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-white/5 rounded transition-colors disabled:opacity-40"
                        >
                          <Moon size={14} />
                        </button>
                        <button
                          onClick={() => scanTitle(title.plex_guid, true)}
                          disabled={scanning[title.plex_guid]}
                          title="Scan Now"
                          className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40"
                        >
                          <Zap size={14} />
                        </button>
                        <button
                          onClick={() => scanTitle(title.plex_guid, false)}
                          disabled={scanning[title.plex_guid]}
                          title="Re-scan"
                          className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-white/5 rounded transition-colors disabled:opacity-40"
                        >
                          <RotateCcw size={14} />
                        </button>
                        <button
                          onClick={() => toggleIgnored(title.plex_guid, title.ignored, title.title)}
                          title={title.ignored ? "Un-ignore this title" : "Ignore this title"}
                          className={`p-1.5 rounded transition-colors ${
                            title.ignored
                              ? 'text-yellow-600 hover:text-yellow-400 hover:bg-yellow-500/10'
                              : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                          }`}
                        >
                          {title.ignored ? 'IGNORED' : 'Ignore'}
                        </button>
                      </div>
                    </div>
                    {renderSegmentsBox(title.plex_guid)}
                  </div>
                ))}
              </div>
            )}
            </div>
          </>
        )}
      </div>

      {previewSeg && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setPreviewSeg(null)}>
          <div className="w-full max-w-4xl bg-plex-card border border-plex-border rounded-xl overflow-hidden" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-plex-border">
              <div>
                <h3 className="text-sm font-semibold text-gray-100">Segment Preview</h3>
                <p className="text-xs text-gray-500">
                  {msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}
                </p>
              </div>
              <button
                onClick={() => setPreviewSeg(null)}
                className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
              >
                Close
              </button>
            </div>
            <div className="p-4">
              <video
                key={previewSeg.id}
                controls
                autoPlay
                className="w-full rounded-lg bg-black max-h-[70vh]"
                src={`/api/segments/${previewSeg.id}/stream`}
                onLoadedMetadata={e => { e.currentTarget.currentTime = previewSeg.start_ms / 1000 }}
                onTimeUpdate={e => {
                  const el = e.currentTarget
                  if (el.currentTime >= previewSeg.end_ms / 1000) el.pause()
                }}
              />
              <p className="text-xs text-gray-500 mt-2">
                Playback uses your browser codecs. If this file does not play, use the jump button to seek in Plex instead.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
