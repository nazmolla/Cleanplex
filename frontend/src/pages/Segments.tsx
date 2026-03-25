import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Film, Tv, ChevronRight, ChevronDown, Trash2, AlertTriangle, SkipForward, Play } from 'lucide-react'

interface Library {
  id: string
  title: string
  type: string
}

interface Title {
  plex_guid: string
  title: string
  status: string
  finished_at?: string | null
  thumb_url: string
  segment_count: number
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

// For TV shows, parse episode info from title
interface EpisodeGroup {
  episodeKey: string // e.g., "S01E01" or full episode title
  episodeTitle: string
  segments: Segment[]
  isExpanded: boolean
}

function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

function formatFinishedAt(value?: string | null): string {
  if (!value) return ''
  const dt = new Date(value)
  if (Number.isNaN(dt.getTime())) return ''
  return dt.toLocaleString()
}

function renderLabels(labels?: string): React.ReactNode {
  if (!labels || !labels.trim()) return null
  const labelArray = labels.split(',').filter(l => l.trim())
  if (labelArray.length === 0) return null
  
  return (
    <div className="flex flex-wrap gap-1 mt-2">
      {labelArray.map(label => {
        const cleanLabel = label.trim()
        const shortLabel = cleanLabel
          .replace('_EXPOSED', '')
          .replace('FEMALE_', 'F ')
          .replace('MALE_', 'M ')
          .replace('GENITALIA', 'Gen.')
          .replace('BREAST', 'Breast')
          .replace('_', ' ')
        return (
          <span
            key={cleanLabel}
            className="text-xs px-2 py-1 rounded bg-red-500/20 text-red-300 border border-red-500/30"
            title={cleanLabel}
          >
            {shortLabel}
          </span>
        )
      })}
    </div>
  )
}

export default function Segments() {
  const [libraries, setLibraries] = useState<Library[]>([])
  const [selectedLib, setSelectedLib] = useState<Library | null>(null)
  const [titles, setTitles] = useState<Title[]>([])
  const [selectedTitle, setSelectedTitle] = useState<Title | null>(null)
  const [segments, setSegments] = useState<Segment[]>([])
  const [loadingTitles, setLoadingTitles] = useState(false)
  const [loadingSegs, setLoadingSegs] = useState(false)
  const [deleting, setDeleting] = useState<Record<number, boolean>>({})
  const [deletingAll, setDeletingAll] = useState(false)
  const [jumping, setJumping] = useState<Record<number, boolean>>({})
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false)
  const [expandedEpisodes, setExpandedEpisodes] = useState<Set<string>>(new Set())
  const [scannerStatus, setScannerStatus] = useState<ScannerStatus | null>(null)
  const previewVideoRef = useRef<HTMLVideoElement | null>(null)

  // Parse episode info from segment title (e.g., "Show – S01E05 – Title")
  const parseEpisodeKey = (title: string): string => {
    const match = title.match(/S\d+E\d+/)
    return match ? match[0] : title
  }

  // Group segments by episode for TV shows
  const groupSegmentsByEpisode = (): EpisodeGroup[] => {
    const groups: Record<string, { episodeKey: string; segments: Segment[] }> = {}
    
    segments.forEach(seg => {
      const episodeKey = parseEpisodeKey(seg.title)
      if (!groups[episodeKey]) {
        groups[episodeKey] = { episodeKey, segments: [] }
      }
      groups[episodeKey].segments.push(seg)
    })

    return Object.values(groups)
      .sort((a, b) => a.episodeKey.localeCompare(b.episodeKey))
      .map(g => ({
        episodeKey: g.episodeKey,
        episodeTitle: g.segments[0]?.title || g.episodeKey,
        segments: g.segments,
        isExpanded: expandedEpisodes.has(g.episodeKey)
      }))
  }

  const toggleEpisodeExpanded = (episodeKey: string) => {
    const newSet = new Set(expandedEpisodes)
    if (newSet.has(episodeKey)) {
      newSet.delete(episodeKey)
    } else {
      newSet.add(episodeKey)
    }
    setExpandedEpisodes(newSet)
  }

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/libraries').then(d => setLibraries(d.libraries))
  }, [])

  // Poll scanner status every 3s so Segments page mirrors live scan activity.
  useEffect(() => {
    const poll = () =>
      api.get<ScannerStatus>('/api/sessions/scanner-status')
        .then(setScannerStatus)
        .catch(() => {})
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  const selectLib = async (lib: Library) => {
    setSelectedLib(lib)
    setSelectedTitle(null)
    setSegments([])
    setLoadingTitles(true)
    try {
      const d = await api.get<{ titles: Title[] }>(`/api/libraries/${lib.id}/titles`)
      // Only show titles that currently have at least one segment.
      setTitles(d.titles.filter(t => t.segment_count > 0))
    } finally {
      setLoadingTitles(false)
    }
  }

  const selectTitle = async (title: Title) => {
    setSelectedTitle(title)
    setExpandedEpisodes(new Set())
    setLoadingSegs(true)
    try {
      const d = await api.get<{ segments: Segment[] }>(`/api/titles/${encodeURIComponent(title.plex_guid)}/segments`)
      setSegments(d.segments)
    } finally {
      setLoadingSegs(false)
    }
  }

  const deleteSegment = async (id: number) => {
    setDeleting(d => ({ ...d, [id]: true }))
    try {
      await api.delete(`/api/segments/${id}`)
      if (selectedTitle && selectedLib) {
        const d = await api.get<{ titles: Title[] }>(`/api/libraries/${selectedLib.id}/titles`)
        const visibleTitles = d.titles.filter(t => t.segment_count > 0)
        setTitles(visibleTitles)

        const stillVisible = visibleTitles.some(t => t.plex_guid === selectedTitle.plex_guid)
        if (!stillVisible) {
          setSelectedTitle(null)
          setSegments([])
        } else {
          const segData = await api.get<{ segments: Segment[] }>(`/api/titles/${encodeURIComponent(selectedTitle.plex_guid)}/segments`)
          setSegments(segData.segments)
        }
      }
    } finally {
      setDeleting(d => ({ ...d, [id]: false }))
    }
  }

  const deleteAllSegments = async () => {
    if (!selectedTitle) return
    setDeletingAll(true)
    try {
      await api.delete(`/api/titles/${selectedTitle.plex_guid}/segments`)
      setSegments([])
      if (selectedLib) {
        const d = await api.get<{ titles: Title[] }>(`/api/libraries/${selectedLib.id}/titles`)
        setTitles(d.titles.filter(t => t.segment_count > 0))
      }
      setSelectedTitle(null)
      setConfirmDeleteAll(false)
    } finally {
      setDeletingAll(false)
    }
  }

  const jumpToSegment = async (id: number) => {
    setJumping(j => ({ ...j, [id]: true }))
    try {
      const d = await api.post<{ ok: boolean; client: string; user: string; seek_to_ms: number }>(`/api/segments/${id}/jump`)
      if (d.ok) {
        alert(`Jumped playback to segment on ${d.client} (${d.user}) at ${msToTimecode(d.seek_to_ms)}.`)
      }
    } catch (err: any) {
      alert(err?.message || 'Could not jump to segment. Start this title in Plex first.')
    } finally {
      setJumping(j => ({ ...j, [id]: false }))
    }
  }

  const openPreview = (seg: Segment) => {
    setPreviewSeg(seg)
  }

  const closePreview = () => {
    setPreviewSeg(null)
  }

  return (
    <div className="flex gap-4 h-full overflow-hidden">
      {/* Library tree - independent scroll */}
      <div className="w-44 flex-shrink-0 overflow-y-auto">
        <h1 className="text-xl font-bold text-gray-100 mb-3">Segments</h1>
        <div className="space-y-0.5 pr-2">
          {libraries.map(lib => (
            <div key={lib.id}>
              <button
                onClick={() => selectLib(lib)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left transition-colors ${
                  selectedLib?.id === lib.id ? 'bg-plex-orange/20 text-plex-orange' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
                }`}
              >
                {lib.type === 'movie' ? <Film size={13} /> : <Tv size={13} />}
                <span className="truncate">{lib.title}</span>
                <ChevronRight size={11} className="ml-auto opacity-40" />
              </button>

              {selectedLib?.id === lib.id && (
                <div className="ml-4 mt-0.5 space-y-0.5">
                  {loadingTitles ? (
                    <div className="text-xs text-gray-600 px-2 py-1">Loading...</div>
                  ) : titles.length === 0 ? (
                    <div className="text-xs text-gray-600 px-2 py-1">No scanned titles</div>
                  ) : (
                    titles.map(t => (
                      <button
                        key={t.plex_guid}
                        onClick={() => selectTitle(t)}
                        className={`w-full flex items-center gap-1.5 px-2 py-1.5 rounded text-xs text-left transition-colors ${
                          selectedTitle?.plex_guid === t.plex_guid
                            ? 'bg-white/10 text-gray-100'
                            : 'text-gray-500 hover:text-gray-200 hover:bg-white/5'
                        }`}
                      >
                        <span className="truncate flex-1">{t.title.split(' – ')[0]}</span>
                        <span className="flex-shrink-0 text-gray-600">{t.segment_count}</span>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Segments detail panel - independent scroll */}
      <div className="flex-1 min-w-0 overflow-y-auto">
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

        {!selectedTitle ? (
          <div className="flex items-center justify-center h-64 text-gray-600 text-sm">
            Select a title to review its segments
          </div>
        ) : (
          <>
            <div className="flex items-center gap-3 mb-4 justify-between">
              <div className="flex items-center gap-3">
                {selectedTitle.thumb_url && (
                  <img
                    src={selectedTitle.thumb_url}
                    alt=""
                    className="w-10 h-14 object-cover rounded bg-plex-border"
                    onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                )}
                <div>
                  <h2 className="text-lg font-semibold text-gray-100">{selectedTitle.title}</h2>
                  <p className="text-sm text-gray-500">{segments.length} segment{segments.length !== 1 ? 's' : ''} detected</p>
                  {selectedTitle.finished_at && (
                    <p className="text-xs text-gray-500 mt-0.5">Scan finished: {formatFinishedAt(selectedTitle.finished_at)}</p>
                  )}
                </div>
              </div>
              {segments.length > 0 && (
                <button
                  onClick={() => setConfirmDeleteAll(true)}
                  disabled={deletingAll}
                  className="px-3 py-1.5 text-xs bg-red-500/20 border border-red-500/30 rounded-lg text-red-400 hover:bg-red-500/30 transition-colors disabled:opacity-40 flex-shrink-0"
                >
                  Delete All
                </button>
              )}
            </div>

            {loadingSegs ? (
              <div className="text-gray-500 text-sm">Loading segments...</div>
            ) : segments.length === 0 ? (
              <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500 text-sm">
                No segments found for this title
              </div>
            ) : selectedLib?.type === 'show' ? (
              // TV show view — grouped by episode
              <div className="space-y-2">
                {groupSegmentsByEpisode().map(episode => (
                  <div key={episode.episodeKey}>
                    {/* Episode header */}
                    <button
                      onClick={() => toggleEpisodeExpanded(episode.episodeKey)}
                      className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-plex-card border border-plex-border text-left hover:border-plex-orange/50 transition-colors"
                    >
                      {episode.isExpanded ? (
                        <ChevronDown size={16} className="text-plex-orange flex-shrink-0" />
                      ) : (
                        <ChevronRight size={16} className="text-gray-600 flex-shrink-0" />
                      )}
                      <span className="font-mono text-sm font-semibold text-plex-orange">{episode.episodeKey}</span>
                      <span className="text-sm text-gray-400 truncate">— {episode.episodeTitle.split(' – ').slice(1).join(' – ')}</span>
                      <span className="ml-auto text-xs text-gray-500 flex-shrink-0">{episode.segments.length} segment{episode.segments.length !== 1 ? 's' : ''}</span>
                    </button>

                    {/* Episode segments */}
                    {episode.isExpanded && (
                      <div className="mt-2 ml-4 space-y-2 pb-2">
                        {episode.segments.map(seg => (
                          <div key={seg.id} className="bg-plex-card border border-plex-border rounded-xl overflow-hidden flex gap-0">
                            {/* Thumbnail */}
                            <div className="w-40 flex-shrink-0 bg-black relative">
                              {seg.has_thumbnail ? (
                                <img
                                  src={seg.thumbnail_url}
                                  alt="Flagged frame"
                                  className="w-full h-full object-cover"
                                  style={{ minHeight: '90px' }}
                                />
                              ) : (
                                <div className="w-full h-24 flex items-center justify-center text-gray-700">
                                  <AlertTriangle size={20} />
                                </div>
                              )}
                              <div className="absolute bottom-1 left-1 bg-black/70 text-xs text-gray-300 px-1.5 py-0.5 rounded">
                                {Math.round(seg.confidence * 100)}%
                              </div>
                            </div>

                            {/* Info */}
                            <div className="flex-1 p-4 flex items-center justify-between gap-4">
                              <div>
                                <div className="flex items-center gap-3 mb-1">
                                  <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.start_ms)}</span>
                                  <span className="text-gray-600">→</span>
                                  <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.end_ms)}</span>
                                  <span className="text-xs text-gray-600">
                                    ({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)
                                  </span>
                                </div>
                                <p className="text-xs text-gray-500">
                                  Detected {new Date(seg.created_at).toLocaleDateString()}
                                </p>
                                {renderLabels(seg.labels)}
                              </div>
                              <div className="flex items-center gap-2">
                                <button
                                  onClick={() => openPreview(seg)}
                                  title="Preview this segment in-app"
                                  className="p-2 text-gray-600 hover:text-green-400 hover:bg-green-400/10 rounded-lg transition-colors flex-shrink-0"
                                >
                                  <Play size={16} />
                                </button>
                                <button
                                  onClick={() => jumpToSegment(seg.id)}
                                  disabled={jumping[seg.id]}
                                  title="Jump active Plex playback for this title to this segment"
                                  className="p-2 text-gray-600 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors disabled:opacity-40 flex-shrink-0"
                                >
                                  <SkipForward size={16} />
                                </button>
                                <button
                                  onClick={() => deleteSegment(seg.id)}
                                  disabled={deleting[seg.id]}
                                  title="Remove this segment (false positive)"
                                  className="p-2 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-40 flex-shrink-0"
                                >
                                  <Trash2 size={16} />
                                </button>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              // Movie view — flat list
              <div className="grid gap-3">
                {segments.map(seg => (
                  <div key={seg.id} className="bg-plex-card border border-plex-border rounded-xl overflow-hidden flex gap-0">
                    {/* Thumbnail */}
                    <div className="w-40 flex-shrink-0 bg-black relative">
                      {seg.has_thumbnail ? (
                        <img
                          src={seg.thumbnail_url}
                          alt="Flagged frame"
                          className="w-full h-full object-cover"
                          style={{ minHeight: '90px' }}
                        />
                      ) : (
                        <div className="w-full h-24 flex items-center justify-center text-gray-700">
                          <AlertTriangle size={20} />
                        </div>
                      )}
                      <div className="absolute bottom-1 left-1 bg-black/70 text-xs text-gray-300 px-1.5 py-0.5 rounded">
                        {Math.round(seg.confidence * 100)}%
                      </div>
                    </div>

                    {/* Info */}
                    <div className="flex-1 p-4 flex items-center justify-between gap-4">
                      <div>
                        <div className="flex items-center gap-3 mb-1">
                          <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.start_ms)}</span>
                          <span className="text-gray-600">→</span>
                          <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.end_ms)}</span>
                          <span className="text-xs text-gray-600">
                            ({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)
                          </span>
                        </div>
                        <p className="text-xs text-gray-500">
                          Detected {new Date(seg.created_at).toLocaleDateString()}
                        </p>
                        {renderLabels(seg.labels)}
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => openPreview(seg)}
                          title="Preview this segment in-app"
                          className="p-2 text-gray-600 hover:text-green-400 hover:bg-green-400/10 rounded-lg transition-colors flex-shrink-0"
                        >
                          <Play size={16} />
                        </button>
                        <button
                          onClick={() => jumpToSegment(seg.id)}
                          disabled={jumping[seg.id]}
                          title="Jump active Plex playback for this title to this segment"
                          className="p-2 text-gray-600 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors disabled:opacity-40 flex-shrink-0"
                        >
                          <SkipForward size={16} />
                        </button>
                        <button
                          onClick={() => deleteSegment(seg.id)}
                          disabled={deleting[seg.id]}
                          title="Remove this segment (false positive)"
                          className="p-2 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-40 flex-shrink-0"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {confirmDeleteAll && selectedTitle && (
          <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
            <div className="w-full max-w-sm bg-plex-card border border-plex-border rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-plex-border">
                <h3 className="text-sm font-semibold text-gray-100">Delete All Segments?</h3>
              </div>
              <div className="p-4">
                <p className="text-sm text-gray-300 mb-4">
                  Are you sure you want to delete all {segments.length} segment{segments.length !== 1 ? 's' : ''} for <strong>{selectedTitle.title}</strong>? This cannot be undone.
                </p>
                <div className="flex gap-2 justify-end">
                  <button
                    onClick={() => setConfirmDeleteAll(false)}
                    disabled={deletingAll}
                    className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors disabled:opacity-40"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={deleteAllSegments}
                    disabled={deletingAll}
                    className="px-3 py-1.5 text-xs bg-red-500/20 border border-red-500/30 rounded-lg text-red-400 hover:bg-red-500/30 transition-colors disabled:opacity-40"
                  >
                    {deletingAll ? 'Deleting...' : 'Delete All'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {previewSeg && (
          <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
            <div className="w-full max-w-4xl bg-plex-card border border-plex-border rounded-xl overflow-hidden">
              <div className="flex items-center justify-between px-4 py-3 border-b border-plex-border">
                <div>
                  <h3 className="text-sm font-semibold text-gray-100">Segment Preview</h3>
                  <p className="text-xs text-gray-500">
                    {msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}
                  </p>
                </div>
                <button
                  onClick={closePreview}
                  className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
                >
                  Close
                </button>
              </div>

              <div className="p-4">
                <video
                  ref={previewVideoRef}
                  controls
                  autoPlay
                  className="w-full rounded-lg bg-black max-h-[70vh]"
                  src={`/api/segments/${previewSeg.id}/stream`}
                  onLoadedMetadata={e => {
                    const el = e.currentTarget
                    el.currentTime = previewSeg.start_ms / 1000
                  }}
                  onTimeUpdate={e => {
                    const el = e.currentTarget
                    if (el.currentTime >= previewSeg.end_ms / 1000) {
                      el.pause()
                    }
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
    </div>
  )
}
