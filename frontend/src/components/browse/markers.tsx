import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../../api/client'
import Modal from '../Modal'
import { useLiveRefresh } from '../../api/events'
import {
  Film, Tv, ChevronRight, ChevronDown, RefreshCw, Play, Save,
  AlertCircle, CheckCircle, Loader2, Trash2, Plus, Tv2,
} from 'lucide-react'
import { translate, useT, type TranslateFn } from '../../i18n'

interface Library {
  id: string
  title: string
  type: string
}

export interface TitleRow {
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

const defaultT: TranslateFn = (key, vars) => translate('en', key, vars)

function parseEpisodeTitle(title: string, t: TranslateFn = defaultT) {
  const parts = title.split(' – ')
  return parts.length >= 3
    ? { show: parts[0].trim(), season: parts[1].trim(), episode: parts.slice(2).join(' – ').trim() }
    : { show: t('unknown_show'), season: t('unknown_season'), episode: title }
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
  const { t } = useT()
  if (count === 0) return null
  return (
    <span className="text-xs px-1.5 py-0.5 rounded bg-hayaa-gold/15 text-hayaa-gold border border-hayaa-gold/25 font-medium">
      {t('unit_marker', { count })}
    </span>
  )
}

// ── Marker type badge ─────────────────────────────────────────────────────────

function MarkerTypeBadge({ type }: { type: string }) {
  const { t } = useT()
  const isCredits = type === 'credits'
  return (
    <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
      isCredits
        ? 'bg-purple-500/20 text-purple-300 border border-purple-500/40'
        : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'
    }`}>
      {isCredits ? t('markers_type_credits') : t('markers_type_intro')}
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
  if (!src || err) return <div className={`${className} bg-hayaa-border/60 rounded flex-shrink-0`} />
  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      className={`${className} object-cover rounded flex-shrink-0 bg-hayaa-border`}
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
  const { t } = useT()
  const videoRef = useRef<HTMLVideoElement>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const previewCleanup = useRef<(() => void) | null>(null)

  const [startMs, setStartMs] = useState(marker.start_ms)
  const [endMs, setEndMs] = useState(marker.end_ms)
  const [duration, setDuration] = useState(0)
  const [previewing, setPreviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
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
    setConfirmingDelete(false)
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
    <div className="bg-black/20 border border-hayaa-border rounded-xl p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2 flex-wrap">
        <MarkerTypeBadge type={marker.marker_type} />
        {marker.final === 1 && (
          <span className="text-xs bg-green-500/15 text-green-400 border border-green-500/25 px-1.5 py-0.5 rounded-full">{t('markers_final')}</span>
        )}
        <span className="text-sm font-mono text-gray-300" dir="ltr">
          {msToTimecode(startMs)} <span className="text-muted">→</span> {msToTimecode(endMs)}
        </span>
        <span className="text-xs text-muted ms-auto" dir="ltr">{msToTimecode(endMs - startMs)}</span>
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
        dir="ltr"
        className="relative h-7 bg-gray-800/80 rounded-lg select-none cursor-col-resize"
      >
        {/* Active region */}
        <div
          className="absolute top-0 h-full bg-hayaa-gold/25 border-x border-hayaa-gold/60"
          style={{ left: `${startPct}%`, width: `${Math.max(0, endPct - startPct)}%` }}
        />
        {/* Start handle */}
        <div
          className="absolute top-0 h-full w-2 bg-hayaa-gold rounded-s cursor-ew-resize hover:brightness-125 transition-all"
          style={{ left: `${startPct}%`, transform: 'translateX(-50%)' }}
          onMouseDown={e => { e.preventDefault(); dragging.current = 'start' }}
          title={t('markers_drag_start')}
        />
        {/* End handle */}
        <div
          className="absolute top-0 h-full w-2 bg-hayaa-gold rounded-e cursor-ew-resize hover:brightness-125 transition-all"
          style={{ left: `${endPct}%`, transform: 'translateX(-50%)' }}
          onMouseDown={e => { e.preventDefault(); dragging.current = 'end' }}
          title={t('markers_drag_end')}
        />
        {/* Window labels */}
        {duration > 0 && (
          <>
            <span className="absolute start-1 top-1/2 -translate-y-1/2 text-[10px] text-muted pointer-events-none select-none">{msToTimecode(winStart)}</span>
            <span className="absolute end-1 top-1/2 -translate-y-1/2 text-[10px] text-muted pointer-events-none select-none">{msToTimecode(winEnd)}</span>
          </>
        )}
      </div>
      {duration > 0 && (
        <p className="text-[10px] text-muted -mt-1">
          {t('markers_window_hint', { start: msToTimecode(winStart), end: msToTimecode(winEnd) })}
        </p>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={handlePlay}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700/80 hover:bg-gray-600 text-sm text-gray-200 transition-colors border border-hayaa-border"
        >
          {previewing ? <><span className="w-3 h-3 border-2 border-gray-300 rounded-sm inline-block" /> {t('markers_stop')}</> : <><Play size={13} /> {t('markers_preview')}</>}
        </button>
        <a
          href={`/api/markers/${marker.id}/vlc`}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700/80 hover:bg-gray-600 text-sm text-gray-200 transition-colors border border-hayaa-border"
          title={t('markers_vlc_title')}
        >
          <Tv2 size={13} /> {t('markers_vlc')}
        </a>
        <button
          onClick={handleSave}
          disabled={saving || !dirty}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-white transition-colors disabled:opacity-40 border border-hayaa-gold/40 bg-hayaa-gold/20 hover:bg-hayaa-gold/30"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
          {saving ? t('common_saving') : dirty ? t('markers_save_plex') : t('common_saved')}
        </button>
        <button
          onClick={() => setConfirmingDelete(true)}
          disabled={deleting}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm text-red-400 border border-red-500/30 bg-red-500/10 hover:bg-red-500/20 transition-colors disabled:opacity-40 ms-auto"
          title={t('markers_delete_title')}
          aria-label={t('markers_delete_aria')}
        >
          {deleting ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
        </button>
        {confirmingDelete && (
          <Modal
            title={t('markers_delete_modal_title', { type: marker.marker_type === 'credits' ? t('markers_credits') : t('markers_intro') })}
            size="sm"
            onClose={() => setConfirmingDelete(false)}
            footer={
              <>
                <button data-autofocus onClick={() => setConfirmingDelete(false)} className="btn-outline">{t('common_cancel')}</button>
                <button onClick={handleDelete} className="btn-danger">{t('common_delete')}</button>
              </>
            }
          >
            <p className="text-sm text-gray-300">{t('markers_delete_modal_body')}</p>
          </Modal>
        )}
        {saveStatus === 'ok' && (
          <span className="flex items-center gap-1 text-green-400 text-xs">
            <CheckCircle size={12} /> {t('markers_saved_plex')}
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
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const [markerType, setMarkerType] = useState<'intro' | 'credits'>('intro')
  const [startVal, setStartVal] = useState('00:00:00.00')
  const [endVal, setEndVal] = useState('00:01:00.00')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const handleCreate = async () => {
    const start_ms = parseTimecode(startVal)
    const end_ms = parseTimecode(endVal)
    if (start_ms === null || end_ms === null) { setErr(t('markers_err_invalid_timecode')); return }
    if (start_ms >= end_ms) { setErr(t('markers_err_order')); return }
    setSaving(true); setErr('')
    try {
      const res = await api.post<{ ok: boolean; marker: PlexMarker; plex_error: string | null }>(
        `/api/markers/titles/${title.rating_key}/create`,
        { plex_guid: title.plex_guid, marker_type: markerType, start_ms, end_ms },
      )
      onCreated(res.marker)
      setOpen(false)
      if (res.plex_error) setErr(t('markers_err_plex_write', { error: res.plex_error }))
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
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-gray-400 border border-dashed border-hayaa-border hover:border-hayaa-gold/40 hover:text-hayaa-gold transition-colors"
      >
        <Plus size={12} /> {t('markers_add')}
      </button>
    )
  }

  return (
    <div className="bg-black/20 border border-hayaa-border rounded-xl p-4 space-y-3">
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">{t('markers_new')}</p>
      <div className="flex flex-wrap gap-2 items-end">
        <div className="space-y-1">
          <label className="text-xs text-muted">{t('markers_type')}</label>
          <select
            value={markerType}
            onChange={e => setMarkerType(e.target.value as 'intro' | 'credits')}
            className="bg-hayaa-dark border border-hayaa-border rounded px-2 py-1 text-sm text-gray-200 focus:border-hayaa-gold"
          >
            <option value="intro">{t('markers_intro')}</option>
            <option value="credits">{t('markers_credits')}</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted">{t('markers_start_label')}</label>
          <input
            dir="ltr"
            value={startVal}
            onChange={e => setStartVal(e.target.value)}
            className="bg-hayaa-dark border border-hayaa-border rounded px-2 py-1 text-sm font-mono text-gray-200 w-36 focus:border-hayaa-gold"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted">{t('markers_end_label')}</label>
          <input
            dir="ltr"
            value={endVal}
            onChange={e => setEndVal(e.target.value)}
            className="bg-hayaa-dark border border-hayaa-border rounded px-2 py-1 text-sm font-mono text-gray-200 w-36 focus:border-hayaa-gold"
          />
        </div>
        <button
          onClick={handleCreate}
          disabled={saving}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-white border border-hayaa-gold/40 bg-hayaa-gold/20 hover:bg-hayaa-gold/30 transition-colors disabled:opacity-50"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
          {t('common_create')}
        </button>
        <button onClick={() => { setOpen(false); setErr('') }} className="text-xs text-gray-400 hover:text-gray-300 px-2">{t('common_cancel')}</button>
      </div>
      {err && <p className="text-xs text-amber-400 flex items-center gap-1"><AlertCircle size={11} />{err}</p>}
    </div>
  )
}

// ── Title marker panel (expand row) ──────────────────────────────────────────
// Marker editing for Browse (#207, #208): one title's markers, and the sync-all progress banner.

export function TitleMarkerPanel({
  title,
  episodeLabel,
  thumbSrc,
  defaultOpen = false,
  onMarkerCountChange,
}: {
  title: TitleRow
  episodeLabel?: string
  thumbSrc?: string
  /** Open (and load) at once: Browse shows one title's markers on its own page (#208). */
  defaultOpen?: boolean
  onMarkerCountChange: (guid: string, count: number) => void
}) {
  const { t } = useT()
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

  // Runs once per title shown: a title page opens its editor without a click.
  useEffect(() => {
    if (defaultOpen) handleToggle()
  }, [title.rating_key])

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
    <div className={`border rounded-xl overflow-hidden transition-colors ${open ? 'border-hayaa-gold/30' : 'border-hayaa-border'}`}>
      <button
        onClick={handleToggle}
        className="w-full flex items-center gap-3 px-3 py-2.5 bg-hayaa-dark hover:bg-white/5 transition-colors text-start"
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
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 border border-hayaa-border transition-colors disabled:opacity-50 flex-shrink-0"
          title={t('markers_sync_title')}
        >
          <RefreshCw size={11} className={syncing ? 'animate-spin' : ''} />
          {t('markers_sync')}
        </button>
        {open
          ? <ChevronDown size={15} className="text-muted flex-shrink-0" />
          : <ChevronRight size={15} className="text-muted flex-shrink-0 rtl:-scale-x-100" />
        }
      </button>

      {syncError && (
        <div className="px-4 py-2 bg-red-500/10 border-t border-red-500/20 text-red-400 text-xs flex items-center gap-1">
          <AlertCircle size={11} /> {syncError}
        </div>
      )}

      {open && (
        <div className="border-t border-hayaa-border p-4 space-y-4 bg-hayaa-darker">
          {markers.length === 0 ? (
            <p className="text-sm text-muted text-center py-6">
              {t('markers_none_stored')}{' '}
              <button onClick={e => handleSync(e as unknown as React.MouseEvent)} className="text-hayaa-gold hover:underline">{t('markers_sync_from_plex')}</button>
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

// ── Sync-all progress banner ──────────────────────────────────────────────────

export interface SyncProgress {
  done: number
  total: number
  failed: number
  totalMarkers: number
  finished: boolean
}

export function SyncAllBanner({ progress, onDismiss }: { progress: SyncProgress; onDismiss: () => void }) {
  const { t } = useT()
  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0
  return (
    <div className="bg-hayaa-card border border-hayaa-border rounded-xl p-4 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        {progress.finished
          ? <CheckCircle size={15} className="text-green-400 flex-shrink-0" />
          : <Loader2 size={15} className="animate-spin text-hayaa-gold flex-shrink-0" />
        }
        <span className="text-sm text-gray-200 font-medium">
          {progress.finished ? t('markers_sync_complete') : t('markers_syncing')}
          {t('markers_sync_progress_titles', { done: progress.done, total: progress.total })}
          {progress.totalMarkers > 0 && t('markers_sync_found', { count: progress.totalMarkers })}
        </span>
        {progress.failed > 0 && (
          <span className="text-xs text-red-400 flex items-center gap-1">
            <AlertCircle size={11} /> {t('markers_sync_failed', { count: progress.failed })}
          </span>
        )}
        {progress.finished && (
          <button onClick={onDismiss} className="ms-auto text-xs text-gray-400 hover:text-gray-300">{t('common_dismiss')}</button>
        )}
      </div>
      {/* Progress bar */}
      {!progress.finished && (
        <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-hayaa-gold rounded-full transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  )
}
