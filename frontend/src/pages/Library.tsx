import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'
import { Film, Tv, ChevronRight, RotateCcw, Zap, Moon, RefreshCw } from 'lucide-react'

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
  thumb_url: string
  segment_count: number
  content_rating: string
  media_type: string
  year?: number | null
  ignored: boolean
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

const SORT_OPTIONS = ['title', 'date-added', 'year', 'year-release'] as const
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
  show: string
  seasons: SeasonGroup[]
  episodes: Title[]
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
  const [scannerStatus, setScannerStatus] = useState<ScannerStatus | null>(null)
  const [selectedGuids, setSelectedGuids] = useState<string[]>([])
  const [expandedShows, setExpandedShows] = useState<Set<string>>(new Set())
  const [expandedSeasons, setExpandedSeasons] = useState<Set<string>>(new Set())

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/libraries').then(d => setLibraries(d.libraries))
  }, [])

  // Poll scanner status every 3s
  useEffect(() => {
    const poll = () =>
      api.get<ScannerStatus>('/api/sessions/scanner-status')
        .then(setScannerStatus)
        .catch(() => {})
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  // Auto-refresh titles when something is scanning
  useEffect(() => {
    if (!selected || !scannerStatus || scannerStatus.active_scans.length === 0) return
    const id = setInterval(async () => {
      try {
        const d = await api.get<{ titles: Title[] }>(`/api/libraries/${selected.id}/titles`)
        setTitles(d.titles)
      } catch {}
    }, 5000)
    return () => clearInterval(id)
  }, [selected, scannerStatus])

  const loadTitles = useCallback(async (libId: string) => {
    const d = await api.get<{ titles: Title[] }>(`/api/libraries/${libId}/titles`)
    return d.titles
  }, [])

  const selectLibrary = async (lib: Library) => {
    setSelected(lib)
    setFilter('')
    setRatingFilter('all')
    setStatusFilter('all')
    setSelectedGuids([])
    setLoadingTitles(true)
    try {
      const titles = await loadTitles(lib.id)
      setTitles(titles)
      // Always sync in background to ensure we're up-to-date
      api.post(`/api/libraries/${lib.id}/sync`)
        .then(async () => {
          // Reload titles after sync completes
          const updated = await loadTitles(lib.id)
          setTitles(updated)
        })
        .catch(err => console.warn('Library sync failed:', err.message))
    } finally {
      setLoadingTitles(false)
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
    try {
      for (const guid of selectedGuids) {
        await api.post('/api/scan/title', { plex_guid: guid, now, library_id: selected.id })
      }
      setSelectedGuids([])
      setTitles(await loadTitles(selected.id))
    } catch (err: any) {
      alert(`Failed to scan selected titles: ${err.message || 'Unknown error'}`)
    }
  }

  const toggleIgnored = async (guid: string, currentIgnored: boolean, label?: string) => {
    try {
      await api.post(`/api/scan/title/${encodeURIComponent(guid)}/ignore`, { ignored: !currentIgnored })
      setTitles(await loadTitles(selected!.id))
    } catch (err: any) {
      alert(`Failed to update ignore status${label ? ` for ${label}` : ''}: ${err.message || 'Unknown error'}`)
    }
  }

  const setIgnoredForGuids = async (guids: string[], ignored: boolean, label?: string) => {
    if (!selected || guids.length === 0) return
    try {
      for (const guid of guids) {
        await api.post(`/api/scan/title/${encodeURIComponent(guid)}/ignore`, { ignored })
      }
      setSelectedGuids(prev => prev.filter(g => !guids.includes(g)))
      setTitles(await loadTitles(selected.id))
    } catch (err: any) {
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
        // Note: created_at not available, using index as fallback
        // Backend should return created_at for proper sorting
        aVal = a.plex_guid
        bVal = b.plex_guid
        break
      case 'year':
        aVal = a.year ?? 0
        bVal = b.year ?? 0
        break
      case 'year-release':
        aVal = a.year ?? 0
        bVal = b.year ?? 0
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

    const showMap = new Map<string, Map<string, Title[]>>()
    for (const t of sorted) {
      if (t.media_type !== 'episode') continue
      const parsed = parseEpisodeTitle(t.title)
      if (!showMap.has(parsed.show)) {
        showMap.set(parsed.show, new Map())
      }
      const seasonMap = showMap.get(parsed.show)!
      if (!seasonMap.has(parsed.season)) {
        seasonMap.set(parsed.season, [])
      }
      seasonMap.get(parsed.season)!.push(t)
    }

    return Array.from(showMap.entries())
      .map(([show, seasonMap]) => {
        const seasons = Array.from(seasonMap.entries())
          .map(([season, episodes]) => ({
            season,
            episodes: [...episodes].sort((a, b) => a.title.localeCompare(b.title)),
          }))
          .sort((a, b) => a.season.localeCompare(b.season))
        return {
          show,
          seasons,
          episodes: seasons.flatMap(s => s.episodes),
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
      {/* Library list */}
      <div className="w-52 flex-shrink-0">
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
      <div className="flex-1 min-w-0">
        {!selected ? (
          <div className="flex items-center justify-center h-64 text-gray-600">
            Select a library to browse titles
          </div>
        ) : (
          <>
            {/* Header row */}
            <div className="flex items-center justify-between mb-3 gap-3">
              <h2 className="text-xl font-semibold text-gray-100 truncate">{selected.title}</h2>
              <div className="flex gap-2 flex-shrink-0">
                <button
                  onClick={refreshTitles}
                  disabled={refreshing}
                  title="Refresh"
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
            <div className="flex gap-2 mb-4">
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
              </select>
              <button
                onClick={() => setSortDesc(!sortDesc)}
                className="px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
                title={sortDesc ? 'Descending' : 'Ascending'}
              >
                {sortDesc ? '↓' : '↑'}
              </button>
            </div>

            {/* Multi-select actions */}
            <div className="mb-3 flex items-center gap-2 flex-wrap">
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
            {loadingTitles ? (
              <div className="text-gray-500 text-sm">Loading...</div>
            ) : filtered.length === 0 ? (
              <div className="text-gray-600 text-sm">No titles found</div>
            ) : isTvLibrary ? (
              <div className="space-y-3">
                {showGroups.map(group => {
                  const allIgnored = group.episodes.length > 0 && group.episodes.every(ep => ep.ignored)
                  const someIgnored = group.episodes.some(ep => ep.ignored)
                  const showOpen = expandedShows.has(group.show)
                  return (
                    <div key={group.show} className="bg-plex-card border border-plex-border rounded-xl p-3">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => toggleShowExpanded(group.show)}
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
                          <span className="text-xs bg-yellow-500/15 text-yellow-400 px-2 py-0.5 rounded-full">Ignored</span>
                        ) : someIgnored ? (
                          <span className="text-xs bg-yellow-500/10 text-yellow-500 px-2 py-0.5 rounded-full">Partially Ignored</span>
                        ) : null}
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
                            const seasonKey = `${group.show}__${season.season}`
                            const seasonOpen = expandedSeasons.has(seasonKey)
                            return (
                              <div key={seasonKey} className="border border-plex-border rounded-lg p-2">
                                <button
                                  onClick={() => toggleSeasonExpanded(group.show, season.season)}
                                  className="w-full flex items-center gap-2 text-left"
                                >
                                  <ChevronRight size={13} className={seasonOpen ? 'rotate-90 transition-transform text-gray-400' : 'transition-transform text-gray-400'} />
                                  <span className="text-xs text-gray-300 font-medium">{season.season}</span>
                                  <span className="text-xs text-gray-500 ml-auto">
                                    {season.episodes.length} episode{season.episodes.length !== 1 ? 's' : ''}
                                  </span>
                                </button>

                                {seasonOpen && (
                                  <div className="mt-2 space-y-2">
                                    {season.episodes.map(title => {
                                      const parsed = parseEpisodeTitle(title.title)
                                      return (
                                        <div
                                          key={title.plex_guid}
                                          className={`border rounded-lg p-2 flex items-center gap-2 ${
                                            title.ignored
                                              ? 'border-yellow-500/30 bg-yellow-500/5'
                                              : 'border-plex-border bg-black/10'
                                          }`}
                                        >
                                          <input
                                            type="checkbox"
                                            checked={selectedGuids.includes(title.plex_guid)}
                                            onChange={() => toggleSelected(title.plex_guid)}
                                            className="w-4 h-4 accent-plex-orange flex-shrink-0"
                                          />
                                          <div className="flex-1 min-w-0">
                                            <p className="text-sm text-gray-100 truncate">
                                              {parsed.episode}
                                              {title.ignored && <span className="ml-2 text-yellow-400 text-xs font-medium">IGNORED</span>}
                                            </p>
                                            <div className="flex items-center gap-2 mt-1">
                                              <StatusBadge status={title.status} progress={title.progress} />
                                              {title.segment_count > 0 && (
                                                <span className="text-xs text-gray-500">{title.segment_count} segment{title.segment_count !== 1 ? 's' : ''}</span>
                                              )}
                                            </div>
                                          </div>
                                          <div className="flex items-center gap-1.5 flex-shrink-0">
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
                    className={`rounded-xl p-3 flex items-center gap-3 ${
                      title.ignored
                        ? 'bg-yellow-500/5 border border-yellow-500/30'
                        : 'bg-plex-card border border-plex-border'
                    }`}
                  >
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
                        className="w-10 h-14 object-cover rounded bg-plex-border flex-shrink-0"
                        onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                      />
                    ) : (
                      <div className="w-10 h-14 bg-plex-border rounded flex-shrink-0" />
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
                        {title.segment_count > 0 && (
                          <span className="text-xs text-gray-500">{title.segment_count} segment{title.segment_count !== 1 ? 's' : ''}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 flex-shrink-0">
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
                        {title.ignored ? '✗' : '○'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
