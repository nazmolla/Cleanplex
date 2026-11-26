// Types and pure helpers shared by the Browse page and its parts (#207).
import { parseDbTimestamp } from '../../utils/dbTime'

export interface Library {
  id: string
  title: string
  type: string
}

export interface MediaVersion {
  id: number
  plex_media_id: string
  resolution: string
  file_path: string
  status: string
  progress: number
  finished_at?: string | null
  error_msg?: string
  duration_ms: number
  segment_count: number
  false_positive_count?: number
}

export interface Title {
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
  /** Segments marked as false positives; not in segment_count (#112). */
  false_positive_count?: number
  content_rating: string
  media_type: string
  year?: number | null
  /** Actors Plex lists for the title (a show's for its episodes), for the cast filter (#180). */
  cast?: string[]
  ignored: boolean
  /** Why the last scan failed; empty unless status is 'failed' (#190). */
  error_msg?: string
  /** Set by Browse: the library the title was loaded from. */
  library_id: string
  /** Per-media-file versions for this title (#233). */
  versions?: MediaVersion[]
}

export function versionLabel(v: MediaVersion, includeFile = false): string {
  const res = (v.resolution || '').trim()
  const base = v.file_path ? v.file_path.split(/[\\/]/).pop() || v.file_path : ''
  const resLabel = !res ? '' : res.toLowerCase() === '4k' ? '4K' : /^\d+$/.test(res) ? `${res}p` : res
  if (resLabel && base && includeFile) return `${resLabel} · ${base}`
  return resLabel || base || `Version ${v.id}`
}

export interface Segment {
  id: number
  plex_guid: string
  version_id?: number
  title: string
  start_ms: number
  end_ms: number
  confidence: number
  has_thumbnail: boolean
  thumbnail_url: string
  /** Film time the thumbnail frame was taken at (#133). */
  thumbnail_ms?: number | null
  /** Short silent loop played on hover; empty until rendered (#114). */
  hover_url?: string
  created_at: string
  labels?: string
  source?: string
  detector?: string
  false_positive?: boolean
  /** Inactive because the verifier rejected it (not a mark by you); restore it if it was real. */
  auto_rejected?: boolean
}

export interface ScannerStatus {
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
  /** The whole queue in order (sessions.py builds it from the database). */
  queue_items?: { guid: string }[]
}

/**
 * The level Browse is looking at. Each field narrows the one before it:
 * no lib = all libraries; lib; lib + show; lib + show + season; guid = one title.
 */
export interface Scope {
  lib?: string
  show?: string
  season?: string
  guid?: string
}

export type ScopeLevel = 'all' | 'library' | 'show' | 'season' | 'title'

export function scopeLevel(s: Scope): ScopeLevel {
  if (s.guid) return 'title'
  if (s.season) return 'season'
  if (s.show) return 'show'
  if (s.lib) return 'library'
  return 'all'
}

/** Episode titles are stored as "<show> – <season> – <episode>". */
export function parseEpisodeTitle(title: string): { show: string; season: string; episode: string } {
  const parts = title.split(' – ')
  if (parts.length >= 3) {
    return { show: parts[0].trim(), season: parts[1].trim(), episode: parts.slice(2).join(' – ').trim() }
  }
  return { show: 'Unknown Show', season: 'Unknown Season', episode: title }
}

/** Groups episodes by show: the Plex show guid, or the parsed name for rows synced before it was stored. */
export function showKeyOf(t: Title): string {
  return t.show_guid || parseEpisodeTitle(t.title).show
}

export function showNameOf(t: Title): string {
  return t.show_title || parseEpisodeTitle(t.title).show
}

export function inScope(t: Title, s: Scope): boolean {
  if (s.guid) return t.plex_guid === s.guid
  if (s.lib && t.library_id !== s.lib) return false
  if (s.show) {
    if (t.media_type !== 'episode' || showKeyOf(t) !== s.show) return false
    if (s.season && parseEpisodeTitle(t.title).season !== s.season) return false
  }
  return true
}

/** "Series 10" sorts after "Series 9"; a season without a number sorts first. */
export function seasonSortKey(season: string): number {
  const m = season.match(/\d+/)
  return m ? parseInt(m[0], 10) : 0
}

export function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

export function formatFinishedAt(value?: string | null): string {
  if (!value) return ''
  const dt = parseDbTimestamp(value)
  if (Number.isNaN(dt.getTime())) return ''
  return dt.toLocaleString()
}

/** Shortens "Could not determine video duration for: /long/path/file.mp4" to the file name. */
export function failureText(message: string): string {
  return message.replace(/(: )(?:\/|[A-Za-z]:\\)\S.*?([^\/\\]+)$/, '$1$2')
}

export function segmentLabels(seg: Segment): string[] {
  return seg.labels ? seg.labels.split(',').map(l => l.trim()).filter(Boolean) : []
}

/** "FEMALE_BREAST_EXPOSED" -> "F Breast"; free-text labels of imported segments pass through. */
export function shortLabel(label: string): string {
  return label.replace('_EXPOSED', '').replace('FEMALE_', 'F ').replace('MALE_', 'M ').replace('GENITALIA', 'Gen.').replace('BREAST', 'Breast').replace('_', ' ')
}

/** What found a segment, for the "Found by" filter: the detector for scanner results, else the source. */
export function segmentOrigin(seg: Segment): string {
  return seg.detector || seg.source || 'scanner'
}

/** Segment-level filters of the Segments view; they live in Browse's filter panel (#208). */
export type SegmentState = 'all' | 'active' | 'fp_user' | 'fp_verifier'
export interface SegmentFilters {
  state: SegmentState
  /** '' = anything; else a value of segmentOrigin(). */
  origin: string
  /** Keep segments carrying at least one of these labels; empty = any label. */
  labels: Set<string>
}
export const NO_SEGMENT_FILTERS: SegmentFilters = { state: 'all', origin: '', labels: new Set() }

export function segmentFiltersActive(f: SegmentFilters): boolean {
  return f.state !== 'all' || f.origin !== '' || f.labels.size > 0
}

export function segmentPasses(s: Segment, f: SegmentFilters): boolean {
  if (f.state === 'active' && s.false_positive) return false
  if (f.state === 'fp_user' && !(s.false_positive && !s.auto_rejected)) return false
  if (f.state === 'fp_verifier' && !s.auto_rejected) return false
  if (f.origin && segmentOrigin(s) !== f.origin) return false
  if (f.labels.size > 0 && !segmentLabels(s).some(l => f.labels.has(l))) return false
  return true
}

export const SEGMENT_STATE_LABELS: Record<SegmentState, string> = {
  all: 'All segments',
  active: 'Active only',
  fp_user: 'False positives (marked by you)',
  fp_verifier: 'Rejected by the verifier',
}

/**
 * A title's name and the line under it, relative to the level being looked at (#208): an
 * episode is named by its own title, with its show and season as context unless the level
 * already says them. Every row and every segment heading names titles this way.
 */
export function titleNameParts(t: Title, level: ScopeLevel, flat: boolean): { name: string; context: string } {
  const extra = [t.year ? String(t.year) : '', t.content_rating].filter(Boolean)
  if (t.media_type !== 'episode') return { name: t.title, context: extra.join(' · ') }
  const p = parseEpisodeTitle(t.title)
  const where = level === 'season' && !flat ? [] : level === 'show' && !flat ? [p.season] : [showNameOf(t), p.season]
  return { name: p.episode, context: [...where, t.content_rating].filter(Boolean).join(' · ') }
}

/** Plex's own picture of a show or season, through the server's image proxy. */
export function plexThumb(ratingKey?: string): string {
  return ratingKey ? `/api/plex-image?path=${encodeURIComponent(`/library/metadata/${ratingKey}/thumb`)}` : ''
}
