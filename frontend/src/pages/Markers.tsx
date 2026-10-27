import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import {
  Film, Tv, ChevronRight, ChevronDown, RefreshCw, Play, Save,
  AlertCircle, CheckCircle, Bookmark, Loader2, Trash2, Plus, Tv2,
} from 'lucide-react'

interface Library {
  id: string
  title: string
  type: string
}

interface TitleRow {
  plex_guid: string
  title: string
  rating_key: string
  media_type: string
  show_guid: string
  show_title: string
  show_rating_key: string
  content_rating: string
  year: number | null
  thumb_url: string
  poster_url: string
  marker_count: number
}

interface PlexMarker {
  id: number
  plex_guid: string
  rating_key: string
  marker_type: string
  start_ms: number
  end_ms: number
  plex_marker_id: number | null
  final: number
  synced_at: string
}

interface SeasonGroup {
  season: string
  episodes: TitleRow[]
  total_markers: number
}

interface ShowGroup {
  show_key: string
  show_title: string
  poster_url: string
  show_rating_key: string
  seasons: SeasonGroup[]
  episodes: TitleRow[]   // flat list for filtering
  total_markers: number
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function msToTimecode(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  const frac = Math.floor((ms % 1000) / 10).toString().padStart(2, '0')
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}.${frac}`
}

function parseEpisodeTitle(title: string) {
  const parts = title.split(' – ')
  return parts.length >= 3
    ? { show: parts[0].trim(), season: parts[1].trim(), episode: parts.slice(2).join(' – ').trim() }
    : { show: 'Unknown Show', season: 'Unknown Season', episode: title }
}

// Extract leading number from season label for numeric ordering ("Series 10" → 10, "None" → 0).
function seasonSortKey(s: string): number {
  const m = s.match(/\d+/)
  return m ? parseInt(m[0], 10) : 0
}

function groupByShow(titles: TitleRow[]): ShowGroup[] {
  const showMap = new Map<string, { show_title: string; poster_url: string; show_rating_key: string; seasons: Map<string, TitleRow[]> }>()
  for (const t of titles) {
    const key = t.show_guid || t.show_rating_key || t.show_title || t.title
    const parsed = parseEpisodeTitle(t.title)
    if (!showMap.has(key)) {
      showMap.set(key, {
        show_title: t.show_title || parsed.show,
        poster_url: t.poster_url,
        show_rating_key: t.show_rating_key,
        seasons: new Map(),
      })
    }
    const entry = showMap.get(key)!
    if (!entry.seasons.has(parsed.season)) entry.seasons.set(parsed.season, [])
    entry.seasons.get(parsed.season)!.push(t)
  }

  return Array.from(showMap.entries())
    .map(([show_key, entry]) => {
      const seasons: SeasonGroup[] = Array.from(entry.seasons.entries())
        .map(([season, eps]) => ({
          season,
          episodes: eps,
          total_markers: eps.reduce((s, e) => s + e.marker_count, 0),
        }))
        .sort((a, b) => seasonSortKey(a.season) - seasonSortKey(b.season))
      const episodes = seasons.flatMap(s => s.episodes)
      return {
        show_key,
        show_title: entry.show_title,
        poster_url: entry.poster_url,
        show_rating_key: entry.show_rating_key,
        seasons,
        episodes,
        total_markers: episodes.reduce((s, e) => s + e.marker_count, 0),
      }
    })
    .sort((a, b) => a.show_title.localeCompare(b.show_title))
}

// ── Marker count badge ────────────────────────────────────────────────────────

function MarkerCountBadge({ count }: { count: number }) {
  if (count === 0) return null
  return (
    <span className="text-xs px-1.5 py-0.5 rounded bg-plex-orange/15 text-plex-orange border border-plex-orange/25 font-medium">
      {count} marker{count !== 1 ? 's' : ''}
    </span>
  )
}

// ── Marker type badge ─────────────────────────────────────────────────────────

function MarkerTypeBadge({ type }: { type: string }) {
  const isCredits = type === 'credits'
  return (
    <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
      isCredits
        ? 'bg-purple-500/20 text-purple-300 border border-purple-500/40'
        : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'
    }`}>
      {isCredits ? 'CREDITS' : 'INTRO'}
    </span>
  )
}

// ── Content rating badge ──────────────────────────────────────────────────────

function ContentRatingBadge({ rating }: { rating: string }) {
  if (!rating) return null
  return (
    <span className="text-xs px-1.5 py-0.5 rounded border border-gray-600 text-gray-400 font-mono">
      {rating}
    </span>
  )
}

// ── Poster image ──────────────────────────────────────────────────────────────

function Poster({ src, alt, className }: { src: string; alt: string; className: string }) {
  const [err, setErr] = useState(false)
  if (!src || err) return <div className={`${className} bg-plex-border/60 rounded flex-shrink-0`} />
  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      className={`${className} object-cover rounded flex-shrink-0 bg-plex-border`}
      onError={() => setErr(true)}
    />
  )
}

// ── Marker editor card ────────────────────────────────────────────────────────

const TIMELINE_BUFFER_MS = 60_000  // show ±60 s around the marker on the timeline

function MarkerEditor({
  marker,
  onUpdate,
  onDelete,
}: {
  marker: PlexMarker
  onUpdate: (m: PlexMarker) => void
  onDelete: (id: number) => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const previewCleanup = useRef<(() => void) | null>(null)

  const [startMs, setStartMs] = useState(marker.start_ms)
  const [endMs, setEndMs] = useState(marker.end_ms)
  const [duration, setDuration] = useState(0)
  const [previewing, setPreviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [saveError, setSaveError] = useState('')

  const dragging = useRef<'start' | 'end' | null>(null)
  const dirty = startMs !== marker.start_ms || endMs !== marker.end_ms

  // Zoomed window: show ±BUFFER around the marker, clamped to [0, duration].
  // Recomputed only when duration loads or the committed marker boundaries change.
  const winStart = duration > 0 ? Math.max(0, marker.start_ms - TIMELINE_BUFFER_MS) : 0
  const winEnd   = duration > 0 ? Math.min(duration, marker.end_ms + TIMELINE_BUFFER_MS) : duration
  const winLen   = winEnd - winStart || 1

  const msToWinPct = (ms: number) => Math.max(0, Math.min(100, ((ms - winStart) / winLen) * 100))
  const startPct = msToWinPct(startMs)
  const endPct   = msToWinPct(endMs)

  const handleLoadedMetadata = () => {
    if (!videoRef.current) return
    const dur = videoRef.current.duration * 1000
    setDuration(dur)
    // Auto-seek to just before the marker so the preview is immediately useful.
    videoRef.current.currentTime = Math.max(0, (marker.start_ms - 3000) / 1000)
  }

  const getTimelineMs = useCallback((e: MouseEvent | React.MouseEvent) => {
    const rect = timelineRef.current?.getBoundingClientRect()
    if (!rect) return winStart
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    return winStart + pct * winLen
  }, [winStart, winLen])

  const seekVideo = (ms: number) => {
    if (videoRef.current) videoRef.current.currentTime = ms / 1000
  }

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current || duration === 0) return
      const ms = getTimelineMs(e)
      if (dragging.current === 'start') {
        const c = Math.max(winStart, Math.min(ms, endMs - 1000))
        setStartMs(c); seekVideo(c)
      } else {
        const c = Math.min(winEnd, Math.max(ms, startMs + 1000))
        setEndMs(c); seekVideo(c)
      }
    }
    const onUp = () => { dragging.current = null }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  }, [duration, startMs, endMs, winStart, winEnd, getTimelineMs])

  const stopPreview = () => {
    const v = videoRef.current
    if (v) v.pause()
    previewCleanup.current?.()
    previewCleanup.current = null
    setPreviewing(false)
  }

  const handlePlay = () => {
    if (previewing) { stopPreview(); return }
    const v = videoRef.current
    if (!v) return
    previewCleanup.current?.()
    v.currentTime = Math.max(0, (startMs - 3000) / 1000)
    v.play()
    setPreviewing(true)
    const stopAt = (endMs + 2000) / 1000
    const check = () => {
      if (v.currentTime >= stopAt) {
        v.pause()
        v.removeEventListener('timeupdate', check)
        previewCleanup.current = null
        setPreviewing(false)
      }
    }
    v.addEventListener('timeupdate', check)
    previewCleanup.current = () => v.removeEventListener('timeupdate', check)
  }

  const handleSave = async () => {
    setSaving(true); setSaveStatus('idle'); setSaveError('')
    try {
      const res = await api.patch<{ ok: boolean; marker: PlexMarker }>(`/api/markers/${marker.id}`, { start_ms: startMs, end_ms: endMs })
      setSaveStatus('ok')
      onUpdate(res.marker)
    } catch (err: unknown) {
      setSaveStatus('error')
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!confirm(`Delete this ${marker.marker_type} marker from local DB? This does not modify Plex.`)) return
    setDeleting(true)
    try {
      await api.delete(`/api/markers/${marker.id}`)
      onDelete(marker.id)
    } catch (err: unknown) {
      setSaveStatus('error')
      setSaveError(err instanceof Error ? err.message : String(err))
      setDeleting(false)
    }
  }

  return (
    <div className="bg-black/20 border border-plex-border rounded-xl p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2 flex-wrap">
        <MarkerTypeBadge type={marker.marker_type} />
        {marker.final === 1 && (
          <span className="text-xs bg-green-500/15 text-green-400 border border-green-500/25 px-1.5 py-0.5 rounded-full">Final</span>
        )}
        <span className="text-sm font-mono text-gray-300">
          {msToTimecode(startMs)} <span className="text-gray-600">→</span> {msToTimecode(endMs)}
        </span>
        <span className="text-xs text-gray-500 ml-auto">{msToTimecode(endMs - startMs)}</span>
      </div>

      {/* Video */}
      <video
        ref={videoRef}
        src={`/api/markers/${marker.id}/stream`}
        className="w-full rounded-lg max-h-56 bg-black"
        onLoadedMetadata={handleLoadedMetadata}
        preload="metadata"
      />

      {/* Zoomed timeline — spans winStart → winEnd, not 0 → duration */}
      <div
        ref={timelineRef}
        className="relative h-7 bg-gray-800/80 rounded-lg select-none cursor-col-resize"
      >
        {/* Active region */}
        <div
          className="absolute top-0 h-full bg-plex-orange/25 border-x border-plex-orange/60"
          style={{ left: `${startPct}%`, width: `${Math.max(0, endPct - startPct)}%` }}
        />
        {/* Start handle */}
        <div
          className="absolute top-0 h-full w-2 bg-plex-orange rounded-l cursor-ew-resize hover:brightness-125 transition-all"
          style={{ left: `${startPct}%`, transform: 'translateX(-50%)' }}
          onMouseDown={e => { e.preventDefault(); dragging.current = 'start' }}
          title="Drag to adjust start"
        />
        {/* End handle */}
        <div
          className="absolute top-0 h-full w-2 bg-plex-orange rounded-r cursor-ew-resize hover:brightness-125 transition-all"
          style={{ left: `${endPct}%`, transform: 'translateX(-50%)' }}
          onMouseDown={e => { e.preventDefault(); dragging.current = 'end' }}
          title="Drag to adjust end"
        />
        {/* Window labels */}
        {duration > 0 && (
          <>
            <span className="absolute left-1 top-1/2 -translate-y-1/2 text-[10px] text-gray-600 pointer-events-none select-none">{msToTimecode(winStart)}</span>
            <span className="absolute right-1 top-1/2 -translate-y-1/2 text-[10px] text-gray-600 pointer-events-none select-none">{msToTimecode(winEnd)}</span>
          </>
        )}
      </div>
      {duration > 0 && (
        <p className="text-[10px] text-gray-600 -mt-1">
          Showing {msToTimecode(winStart)} – {msToTimecode(winEnd)} (±60 s window)
        </p>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={handlePlay}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700/80 hover:bg-gray-600 text-sm text-gray-200 transition-colors border border-plex-border"
        >
          {previewing ? <><span className="w-3 h-3 border-2 border-gray-300 rounded-sm inline-block" /> Stop</> : <><Play size={13} /> Preview</>}
        </button>
        <a
          href={`/api/markers/${marker.id}/vlc`}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700/80 hover:bg-gray-600 text-sm text-gray-200 transition-colors border border-plex-border"
          title="Open full file in VLC (requires VLC installed)"
        >
          <Tv2 size={13} /> VLC
        </a>
        <button
          onClick={handleSave}
          disabled={saving || !dirty}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-white transition-colors disabled:opacity-40 border border-plex-orange/40 bg-plex-orange/20 hover:bg-plex-orange/30"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
          {saving ? 'Saving…' : dirty ? 'Save to Plex' : 'Saved'}
        </button>
        <button
          onClick={handleDelete}
          disabled={deleting}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm text-red-400 border border-red-500/30 bg-red-500/10 hover:bg-red-500/20 transition-colors disabled:opacity-40 ml-auto"
          title="Remove from local DB (does not modify Plex)"
        >
          {deleting ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
        </button>
        {saveStatus === 'ok' && (
          <span className="flex items-center gap-1 text-green-400 text-xs">
            <CheckCircle size={12} /> Saved to Plex
          </span>
        )}
        {saveStatus === 'error' && (
          <span className="flex items-center gap-1 text-red-400 text-xs max-w-xs truncate" title={saveError}>
            <AlertCircle size={12} /> {saveError}
          </span>
        )}
      </div>
    </div>
  )
}

// ── Add marker form ───────────────────────────────────────────────────────────

function parseTimecode(s: string): number | null {
  const m = s.match(/^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d+))?$/)
  if (!m) return null
  return (parseInt(m[1]) * 3600 + parseInt(m[2]) * 60 + parseInt(m[3])) * 1000 + parseInt((m[4] || '0').padEnd(3, '0').slice(0, 3))
}

function AddMarkerForm({
  title,
  onCreated,
}: {
  title: TitleRow
  onCreated: (m: PlexMarker) => void
}) {
  const [open, setOpen] = useState(false)
  const [markerType, setMarkerType] = useState<'intro' | 'credits'>('intro')
  const [startVal, setStartVal] = useState('00:00:00.00')
  const [endVal, setEndVal] = useState('00:01:00.00')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const handleCreate = async () => {
    const start_ms = parseTimecode(startVal)
    const end_ms = parseTimecode(endVal)
    if (start_ms === null || end_ms === null) { setErr('Invalid timecode format — use HH:MM:SS.ff'); return }
    if (start_ms >= end_ms) { setErr('Start must be before end'); return }
    setSaving(true); setErr('')
    try {
      const res = await api.post<{ ok: boolean; marker: PlexMarker; plex_error: string | null }>(
        `/api/markers/titles/${title.rating_key}/create`,
        { plex_guid: title.plex_guid, marker_type: markerType, start_ms, end_ms },
      )
      onCreated(res.marker)
      setOpen(false)
      if (res.plex_error) setErr(`Saved locally. Plex write failed: ${res.plex_error}`)
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-gray-400 border border-dashed border-plex-border hover:border-plex-orange/40 hover:text-plex-orange transition-colors"
      >
        <Plus size={12} /> Add Marker
      </button>
    )
  }

  return (
    <div className="bg-black/20 border border-plex-border rounded-xl p-4 space-y-3">
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">New Marker</p>
      <div className="flex flex-wrap gap-2 items-end">
        <div className="space-y-1">
          <label className="text-xs text-gray-500">Type</label>
          <select
            value={markerType}
            onChange={e => setMarkerType(e.target.value as 'intro' | 'credits')}
            className="bg-plex-dark border border-plex-border rounded px-2 py-1 text-sm text-gray-200 focus:outline-none focus:border-plex-orange"
          >
            <option value="intro">Intro</option>
            <option value="credits">Credits</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-xs text-gray-500">Start (HH:MM:SS.ff)</label>
          <input
            value={startVal}
            onChange={e => setStartVal(e.target.value)}
            className="bg-plex-dark border border-plex-border rounded px-2 py-1 text-sm font-mono text-gray-200 w-36 focus:outline-none focus:border-plex-orange"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-gray-500">End (HH:MM:SS.ff)</label>
          <input
            value={endVal}
            onChange={e => setEndVal(e.target.value)}
            className="bg-plex-dark border border-plex-border rounded px-2 py-1 text-sm font-mono text-gray-200 w-36 focus:outline-none focus:border-plex-orange"
          />
        </div>
        <button
          onClick={handleCreate}
          disabled={saving}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-white border border-plex-orange/40 bg-plex-orange/20 hover:bg-plex-orange/30 transition-colors disabled:opacity-50"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
          Create
        </button>
        <button onClick={() => { setOpen(false); setErr('') }} className="text-xs text-gray-500 hover:text-gray-300 px-2">Cancel</button>
      </div>
      {err && <p className="text-xs text-amber-400 flex items-center gap-1"><AlertCircle size={11} />{err}</p>}
    </div>
  )
}

// ── Title marker panel (expand row) ──────────────────────────────────────────

function TitleMarkerPanel({
  title,
  episodeLabel,
  thumbSrc,
  onMarkerCountChange,
}: {
  title: TitleRow
  episodeLabel?: string
  thumbSrc?: string
  onMarkerCountChange: (guid: string, count: number) => void
}) {
  const [open, setOpen] = useState(false)
  const [markers, setMarkers] = useState<PlexMarker[]>([])
  const [loaded, setLoaded] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncError, setSyncError] = useState('')

  const load = useCallback(async () => {
    const data = await api.get<{ markers: PlexMarker[] }>(
      `/api/markers/titles/${title.rating_key}`,
    )
    if (data.markers.length === 0) {
      // Nothing in DB — auto-sync from Plex on first expand so markers persist for future loads.
      setSyncing(true)
      try {
        await api.post(`/api/markers/titles/${title.rating_key}/sync`, { plex_guid: title.plex_guid })
        const fresh = await api.get<{ markers: PlexMarker[] }>(
          `/api/markers/titles/${title.rating_key}`,
        )
        setMarkers(fresh.markers)
        onMarkerCountChange(title.rating_key, fresh.markers.length)
      } catch {
        setMarkers([])
        onMarkerCountChange(title.rating_key, 0)
      } finally {
        setSyncing(false)
      }
    } else {
      setMarkers(data.markers)
      onMarkerCountChange(title.rating_key, data.markers.length)
    }
    setLoaded(true)
  }, [title.rating_key, title.plex_guid, onMarkerCountChange])

  const handleToggle = async () => {
    if (!open && !loaded) await load()
    setOpen(v => !v)
  }

  const handleSync = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setSyncing(true); setSyncError('')
    try {
      await api.post(`/api/markers/titles/${title.rating_key}/sync`, { plex_guid: title.plex_guid })
      await load()
      setOpen(true)
    } catch (err: unknown) {
      setSyncError(err instanceof Error ? err.message : String(err))
    } finally {
      setSyncing(false)
    }
  }

  const updateMarker = (updated: PlexMarker) => {
    setMarkers(prev => prev.map(m => m.id === updated.id ? updated : m))
  }

  const deleteMarker = (id: number) => {
    setMarkers(prev => prev.filter(m => m.id !== id))
    onMarkerCountChange(title.plex_guid, markers.length - 1)
  }

  const displayLabel = episodeLabel || title.title

  return (
    <div className={`border rounded-xl overflow-hidden transition-colors ${open ? 'border-plex-orange/30' : 'border-plex-border'}`}>
      <button
        onClick={handleToggle}
        className="w-full flex items-center gap-3 px-3 py-2.5 bg-plex-dark hover:bg-white/5 transition-colors text-left"
      >
        {thumbSrc !== undefined && (
          <Poster src={thumbSrc} alt="" className="w-16 h-9 flex-shrink-0" />
        )}
        <span className="flex-1 text-sm text-gray-200 truncate min-w-0">{displayLabel}</span>
        {title.content_rating && <ContentRatingBadge rating={title.content_rating} />}
        <MarkerCountBadge count={title.marker_count} />
        <button
          onClick={handleSync}
          disabled={syncing}
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 border border-plex-border transition-colors disabled:opacity-50 flex-shrink-0"
          title="Pull markers from Plex"
        >
          <RefreshCw size={11} className={syncing ? 'animate-spin' : ''} />
          Sync
        </button>
        {open
          ? <ChevronDown size={15} className="text-gray-500 flex-shrink-0" />
          : <ChevronRight size={15} className="text-gray-500 flex-shrink-0" />
        }
      </button>

      {syncError && (
        <div className="px-4 py-2 bg-red-500/10 border-t border-red-500/20 text-red-400 text-xs flex items-center gap-1">
          <AlertCircle size={11} /> {syncError}
        </div>
      )}

      {open && (
        <div className="border-t border-plex-border p-4 space-y-4 bg-plex-darker">
          {markers.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-6">
              No markers stored.{' '}
              <button onClick={e => handleSync(e as unknown as React.MouseEvent)} className="text-plex-orange hover:underline">Sync from Plex</button>
            </p>
          ) : (
            markers.map(m => <MarkerEditor key={m.id} marker={m} onUpdate={updateMarker} onDelete={deleteMarker} />)
          )}
          <AddMarkerForm
            title={title}
            onCreated={m => {
              setMarkers(prev => [...prev, m])
              onMarkerCountChange(title.rating_key, markers.length + 1)
            }}
          />
        </div>
      )}
    </div>
  )
}

// ── Season group (inside a show) ──────────────────────────────────────────────

function SeasonGroupSection({
  season,
  search,
  onMarkerCountChange,
}: {
  season: SeasonGroup
  search: string
  onMarkerCountChange: (guid: string, count: number) => void
}) {
  const [open, setOpen] = useState(false)
  const filtered = season.episodes.filter(e =>
    e.title.toLowerCase().includes(search.toLowerCase()) ||
    parseEpisodeTitle(e.title).episode.toLowerCase().includes(search.toLowerCase()),
  )
  if (filtered.length === 0) return null

  return (
    <div className="border border-plex-border rounded-lg">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/5 transition-colors rounded-lg"
      >
        <ChevronRight size={13} className={`text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span className="text-xs font-medium text-gray-300">{season.season}</span>
        <span className="text-xs text-gray-600 ml-auto flex items-center gap-2">
          {season.episodes.length} episode{season.episodes.length !== 1 ? 's' : ''}
          {season.total_markers > 0 && <MarkerCountBadge count={season.total_markers} />}
        </span>
      </button>
      {open && (
        <div className="border-t border-plex-border p-2 space-y-2">
          {filtered.map(ep => {
            const parsed = parseEpisodeTitle(ep.title)
            return (
              <TitleMarkerPanel
                key={ep.plex_guid}
                title={ep}
                episodeLabel={parsed.episode}
                thumbSrc={ep.thumb_url}
                onMarkerCountChange={onMarkerCountChange}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Show group card ───────────────────────────────────────────────────────────

function ShowGroupCard({
  group,
  search,
  onMarkerCountChange,
}: {
  group: ShowGroup
  search: string
  onMarkerCountChange: (guid: string, count: number) => void
}) {
  const [open, setOpen] = useState(false)

  const hasMatch = group.episodes.some(e =>
    e.title.toLowerCase().includes(search.toLowerCase()) ||
    group.show_title.toLowerCase().includes(search.toLowerCase()),
  )
  if (!hasMatch) return null

  return (
    <div className="bg-plex-card border border-plex-border rounded-xl p-3">
      {/* Show header */}
      <div className="flex items-center gap-3">
        <Poster src={group.poster_url} alt={`${group.show_title} poster`} className="w-14 h-20" />
        <button
          onClick={() => setOpen(v => !v)}
          className="p-1 text-gray-400 hover:text-white flex-shrink-0"
        >
          <ChevronRight size={14} className={open ? 'rotate-90 transition-transform' : 'transition-transform'} />
        </button>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-100 truncate">{group.show_title}</p>
          <p className="text-xs text-gray-500 mt-0.5">
            {group.seasons.length} season{group.seasons.length !== 1 ? 's' : ''} • {group.episodes.length} episode{group.episodes.length !== 1 ? 's' : ''}
            {group.total_markers > 0 && (
              <span className="ml-2 inline-flex items-center gap-1">
                <MarkerCountBadge count={group.total_markers} />
              </span>
            )}
          </p>
        </div>
      </div>

      {/* Seasons → episodes */}
      {open && (
        <div className="mt-3 space-y-2">
          {group.seasons.map(season => (
            <SeasonGroupSection
              key={season.season}
              season={season}
              search={search}
              onMarkerCountChange={onMarkerCountChange}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ── Sync-all progress banner ──────────────────────────────────────────────────

interface SyncProgress {
  done: number
  total: number
  failed: number
  totalMarkers: number
  finished: boolean
}

function SyncAllBanner({ progress, onDismiss }: { progress: SyncProgress; onDismiss: () => void }) {
  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0
  return (
    <div className="bg-plex-card border border-plex-border rounded-xl p-4 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        {progress.finished
          ? <CheckCircle size={15} className="text-green-400 flex-shrink-0" />
          : <Loader2 size={15} className="animate-spin text-plex-orange flex-shrink-0" />
        }
        <span className="text-sm text-gray-200 font-medium">
          {progress.finished ? 'Sync complete — ' : 'Syncing… '}
          {progress.done}/{progress.total} titles
          {progress.totalMarkers > 0 && ` — ${progress.totalMarkers} markers found`}
        </span>
        {progress.failed > 0 && (
          <span className="text-xs text-red-400 flex items-center gap-1">
            <AlertCircle size={11} /> {progress.failed} failed
          </span>
        )}
        {progress.finished && (
          <button onClick={onDismiss} className="ml-auto text-xs text-gray-500 hover:text-gray-300">Dismiss</button>
        )}
      </div>
      {/* Progress bar */}
      {!progress.finished && (
        <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-plex-orange rounded-full transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Markers() {
  const [libraries, setLibraries] = useState<Library[]>([])
  const [selectedLibrary, setSelectedLibrary] = useState<string>('')
  const [titles, setTitles] = useState<TitleRow[]>([])
  const [loadingLibraries, setLoadingLibraries] = useState(true)
  const [loadingTitles, setLoadingTitles] = useState(false)
  const [notCached, setNotCached] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [search, setSearch] = useState('')
  const [syncingAll, setSyncingAll] = useState(false)
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null)

  const selectedLib = libraries.find(l => l.id === selectedLibrary)
  const isTv = selectedLib?.type === 'show'

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/markers/libraries').then(data => {
      setLibraries(data.libraries || [])
      if (data.libraries?.length) setSelectedLibrary(data.libraries[0].id)
    }).finally(() => setLoadingLibraries(false))
  }, [])

  useEffect(() => {
    if (!selectedLibrary) return
    setLoadingTitles(true)
    setNotCached(false)
    setSyncProgress(null)
    api.get<{ titles: TitleRow[]; cached: boolean }>(`/api/markers/libraries/${selectedLibrary}/titles`)
      .then(data => {
        setTitles(data.titles || [])
        setNotCached(data.cached === false)
      })
      .finally(() => setLoadingTitles(false))
  }, [selectedLibrary])

  // Live (#89): marker edits and syncs (here or in another tab) reload the list
  // quietly, without resetting the loading or sync-progress state.
  const selectedLibraryRef = useRef(selectedLibrary)
  selectedLibraryRef.current = selectedLibrary
  useLiveRefresh(['markers_changed'], () => {
    const lib = selectedLibraryRef.current
    if (!lib) return
    api.get<{ titles: TitleRow[]; cached: boolean }>(`/api/markers/libraries/${lib}/titles`)
      .then(data => { if (selectedLibraryRef.current === lib) setTitles(data.titles || []) })
      .catch(() => {})
  }, { debounceMs: 1000 })

  const handleRefreshLibrary = async () => {
    if (!selectedLibrary) return
    setRefreshing(true)
    try {
      await api.post(`/api/markers/libraries/${selectedLibrary}/refresh`)
      const data = await api.get<{ titles: TitleRow[]; cached: boolean }>(`/api/markers/libraries/${selectedLibrary}/titles`)
      setTitles(data.titles || [])
      setNotCached(false)
    } finally {
      setRefreshing(false)
    }
  }

  const handleSyncAll = async () => {
    if (!selectedLibrary || titles.length === 0) return
    setSyncingAll(true)
    const total = titles.length
    let done = 0, failed = 0, totalMarkers = 0
    setSyncProgress({ done: 0, total, failed: 0, totalMarkers: 0, finished: false })

    // Fan out in batches of 5 — gives live progress and avoids one giant timed-out request.
    const BATCH = 5
    for (let i = 0; i < titles.length; i += BATCH) {
      const batch = titles.slice(i, i + BATCH)
      await Promise.all(batch.map(async t => {
        try {
          const res = await api.post<{ count: number }>(`/api/markers/titles/${t.rating_key}/sync`, { plex_guid: t.plex_guid })
          totalMarkers += res.count ?? 0
        } catch {
          failed++
        } finally {
          done++
          setSyncProgress({ done, total, failed, totalMarkers, finished: false })
        }
      }))
    }

    // Refresh title list to pick up updated marker counts
    try {
      const data = await api.get<{ titles: TitleRow[]; cached: boolean }>(`/api/markers/libraries/${selectedLibrary}/titles`)
      setTitles(data.titles || [])
    } catch {}

    setSyncProgress({ done, total, failed, totalMarkers, finished: true })
    setSyncingAll(false)
  }

  const handleMarkerCountChange = useCallback((ratingKey: string, count: number) => {
    setTitles(prev => prev.map(t => t.rating_key === ratingKey ? { ...t, marker_count: count } : t))
  }, [])

  const filteredMovies = titles.filter(
    t => t.media_type !== 'episode' && t.title.toLowerCase().includes(search.toLowerCase()),
  )
  const filteredEpisodes = titles.filter(t => t.media_type === 'episode')
  const showGroups = groupByShow(filteredEpisodes).filter(g =>
    g.episodes.some(e => e.title.toLowerCase().includes(search.toLowerCase())) ||
    g.show_title.toLowerCase().includes(search.toLowerCase()),
  )

  const totalTitles = isTv ? showGroups.length : filteredMovies.length

  return (
    <div className="space-y-4 max-w-4xl mx-auto">
      {/* Page header */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
            <Bookmark size={22} className="text-plex-orange" />
            Markers
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Adjust Plex intro &amp; credits markers. Drag handles on the timeline, then save to Plex.
          </p>
        </div>
      </div>

      {/* Controls bar */}
      <div className="flex flex-wrap gap-2 items-center">
        {loadingLibraries ? (
          <span className="text-sm text-gray-500 flex items-center gap-1"><Loader2 size={14} className="animate-spin" /> Loading…</span>
        ) : (
          <select
            value={selectedLibrary}
            onChange={e => setSelectedLibrary(e.target.value)}
            className="bg-plex-dark border border-plex-border rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-plex-orange"
          >
            {libraries.map(lib => (
              <option key={lib.id} value={lib.id}>{lib.title}</option>
            ))}
          </select>
        )}

        <input
          type="text"
          placeholder="Filter titles…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="bg-plex-dark border border-plex-border rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-plex-orange"
        />

        <button
          onClick={handleRefreshLibrary}
          disabled={refreshing || !selectedLibrary || loadingTitles}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-gray-300 border border-plex-border bg-plex-dark hover:bg-white/5 transition-colors disabled:opacity-50 ml-auto"
          title="Reload title list from Plex and update cache"
        >
          {refreshing
            ? <><Loader2 size={14} className="animate-spin" /> Refreshing…</>
            : <><RefreshCw size={14} /> Refresh Library</>
          }
        </button>

        <button
          onClick={handleSyncAll}
          disabled={syncingAll || !selectedLibrary || loadingTitles || titles.length === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-white border border-plex-orange/40 bg-plex-orange/20 hover:bg-plex-orange/30 transition-colors disabled:opacity-50"
          title="Pull markers for every title in this library from Plex"
        >
          {syncingAll
            ? <><Loader2 size={14} className="animate-spin" /> Syncing all…</>
            : <><RefreshCw size={14} /> Sync All</>
          }
        </button>

        {!loadingTitles && (
          <span className="text-xs text-gray-600">
            {totalTitles} {isTv ? 'show' : 'title'}{totalTitles !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Sync-all progress banner */}
      {syncProgress && (
        <SyncAllBanner progress={syncProgress} onDismiss={() => setSyncProgress(null)} />
      )}

      {/* Title list */}
      {loadingTitles ? (
        <div className="text-center py-16 text-gray-500 text-sm flex flex-col items-center gap-2">
          <Loader2 size={24} className="animate-spin" />
          Loading titles…
        </div>
      ) : notCached ? (
        <div className="text-center py-16 text-gray-500 text-sm flex flex-col items-center gap-3">
          <p>Library not cached yet. Click <strong className="text-gray-300">Refresh Library</strong> to pull titles from Plex.</p>
          <button
            onClick={handleRefreshLibrary}
            disabled={refreshing}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium text-white border border-plex-orange/40 bg-plex-orange/20 hover:bg-plex-orange/30 transition-colors disabled:opacity-50"
          >
            {refreshing ? <><Loader2 size={14} className="animate-spin" /> Refreshing…</> : <><RefreshCw size={14} /> Refresh Library</>}
          </button>
        </div>
      ) : isTv ? (
        /* TV: grouped by show */
        showGroups.length === 0 ? (
          <div className="text-center py-16 text-gray-600 text-sm">No shows found.</div>
        ) : (
          <div className="space-y-3">
            {showGroups.map(group => (
              <ShowGroupCard
                key={group.show_key}
                group={group}
                search={search}
                onMarkerCountChange={handleMarkerCountChange}
              />
            ))}
          </div>
        )
      ) : (
        /* Movies: flat list with poster */
        filteredMovies.length === 0 ? (
          <div className="text-center py-16 text-gray-600 text-sm">No titles found.</div>
        ) : (
          <div className="space-y-2">
            {filteredMovies.map(title => (
              <div key={title.plex_guid} className="bg-plex-card border border-plex-border rounded-xl p-3">
                <div className="flex gap-3">
                  <Poster src={title.poster_url} alt={title.title} className="w-14 h-20" />
                  <div className="flex-1 min-w-0 space-y-2">
                    <div className="flex items-start gap-2 flex-wrap">
                      <span className="text-sm font-semibold text-gray-100 truncate flex-1">{title.title}</span>
                      {title.year && <span className="text-xs text-gray-500">{title.year}</span>}
                      <ContentRatingBadge rating={title.content_rating} />
                      <MarkerCountBadge count={title.marker_count} />
                    </div>
                    <TitleMarkerPanel
                      title={title}
                      onMarkerCountChange={handleMarkerCountChange}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  )
}
