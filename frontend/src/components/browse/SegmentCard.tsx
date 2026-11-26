import { useState, type ReactNode } from 'react'
import { AlertTriangle, Play, Tv2, SkipForward, Trash2 } from 'lucide-react'
import { api } from '../../api/client'
import HoverPreview from '../HoverPreview'
import ThumbnailTime from '../ThumbnailTime'
import SegmentOriginBadge from '../SegmentOriginBadge'
import FalsePositiveButton, { FalsePositiveTag } from '../FalsePositiveButton'
import RelabelButton from '../RelabelButton'
import { toast } from '../Toast'
import { parseDbTimestamp } from '../../utils/dbTime'
import SafeImg from './SafeImg'
import { Segment, msToTimecode, segmentLabels, shortLabel } from './model'
import { useT } from '../../i18n'

interface Props {
  seg: Segment
  onPreview: (seg: Segment) => void
  /** The segment changed in place (relabelled, or marked / unmarked as a false positive). */
  onChange: (seg: Segment) => void
  onDeleted: (seg: Segment) => void
  /** Which title the segment is from, when cards of many titles are shown together. */
  caption?: ReactNode
}

/**
 * One segment as a card with a full-width frame (#208), for the Analytics gallery: the frame is
 * what a segment is judged by, so it gets the space. Hovering plays the preview loop at the same
 * size; clicking the frame opens the player. Browse lists segments as rows (SegmentRow).
 */
export default function SegmentCard({ seg, onPreview, onChange, onDeleted, caption }: Props) {
  const { t, formatDate } = useT()
  const [jumping, setJumping] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const jump = async () => {
    setJumping(true)
    try {
      const d = await api.post<{ ok: boolean; client: string; user: string; seek_to_ms: number }>(`/api/segments/${seg.id}/jump`)
      if (d.ok) toast.success(t('analytics_jump_success', { time: msToTimecode(d.seek_to_ms), client: d.client, user: d.user }))
    } catch (err: any) {
      toast.error(err?.message || t('analytics_jump_error'))
    } finally {
      setJumping(false)
    }
  }

  const remove = async () => {
    setDeleting(true)
    try {
      await api.delete(`/api/segments/${seg.id}`)
      onDeleted(seg)
    } catch (err: any) {
      toast.error(t('seg_delete_error', { error: err?.message || t('common_unknown_error') }))
      setDeleting(false)
    }
  }

  const timeCls = `font-mono text-sm ${seg.false_positive ? 'text-gray-400 line-through' : 'text-plex-orange'}`
  const labels = segmentLabels(seg)

  return (
    <div className={`flex flex-col bg-plex-card border rounded-xl overflow-hidden ${seg.false_positive ? 'border-amber-400/30' : 'border-plex-border'}`}>
      <button
        onClick={() => onPreview(seg)}
        aria-label={t('seg_play_aria', { time: msToTimecode(seg.start_ms) })}
        title={t('seg_play_title')}
        className="group block w-full text-start"
      >
        <HoverPreview src={seg.hover_url} fit="contain" className={`aspect-video bg-black ${seg.false_positive ? '[&_img]:brightness-50 [&_video]:brightness-50' : ''}`}>
          {seg.has_thumbnail ? (
            <SafeImg src={seg.thumbnail_url} alt={t('analytics_flagged_frame')} fit="contain" iconSize={28} className="w-full h-full bg-black" />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-faint"><AlertTriangle size={28} /></div>
          )}
          <div className="absolute bottom-1 start-1 bg-black/70 text-xs text-gray-200 px-1.5 py-0.5 rounded">
            {Math.round(seg.confidence * 100)}%
          </div>
          <ThumbnailTime ms={seg.thumbnail_ms} />
          {/* Play hint only on hover, so the frame stays clean while scanning a grid. */}
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity pointer-events-none">
            <span className="rounded-full bg-black/60 p-3 text-white"><Play size={22} /></span>
          </div>
        </HoverPreview>
      </button>

      <div className="flex-1 p-3 space-y-1.5 min-w-0">
        {caption && <div className="text-sm text-gray-100 truncate">{caption}</div>}
        <div className="flex flex-wrap items-center gap-1.5">
          {/* A false positive recedes without losing legibility: the frame dims, the times are struck through. */}
          <span dir="ltr" className="inline-flex items-center gap-1.5">
            <span className={timeCls}>{msToTimecode(seg.start_ms)}</span>
            <span className="text-muted text-xs">→</span>
            <span className={timeCls}>{msToTimecode(seg.end_ms)}</span>
          </span>
          <span className="text-xs text-muted">{t('common_seconds_short', { count: Math.round((seg.end_ms - seg.start_ms) / 1000) })}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <SegmentOriginBadge seg={seg} />
          {seg.false_positive && <FalsePositiveTag autoRejected={seg.auto_rejected} />}
          {labels.map(label => (
            <span key={label} className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 border border-red-500/30" title={label}>
              {shortLabel(label, t)}
            </span>
          ))}
        </div>
        {seg.created_at && <p className="text-xs text-muted">{t('seg_detected_at', { date: formatDate(parseDbTimestamp(seg.created_at)) })}</p>}
      </div>

      <div className="flex items-center justify-between gap-1 px-2 py-1.5 border-t border-plex-border">
        <a href={`/api/segments/${seg.id}/vlc`} title={t('seg_open_vlc')} aria-label={t('seg_open_vlc')} className="p-2 text-gray-400 hover:text-blue-400 hover:bg-blue-400/10 rounded-lg transition-colors">
          <Tv2 size={16} />
        </a>
        <button onClick={jump} disabled={jumping} title={t('analytics_jump_seg')} aria-label={t('analytics_jump_seg')} className="p-2 text-gray-400 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors disabled:opacity-40">
          <SkipForward size={16} />
        </button>
        <RelabelButton seg={seg} onChange={(_, l) => onChange({ ...seg, labels: l })} />
        <FalsePositiveButton seg={seg} onChange={(_, fp) => onChange({ ...seg, false_positive: fp, auto_rejected: fp ? seg.auto_rejected : false })} />
        <button onClick={remove} disabled={deleting} title={t('analytics_delete_seg')} aria-label={t('analytics_delete_seg')} className="p-2 text-gray-400 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-40">
          <Trash2 size={16} />
        </button>
      </div>
    </div>
  )
}
