import { useState } from 'react'
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

interface Props {
  seg: Segment
  onPreview: (seg: Segment) => void
  /** The segment changed in place (relabelled, or marked / unmarked as a false positive). */
  onChange: (seg: Segment) => void
  onDeleted: (seg: Segment) => void
}

/**
 * One segment as a line of the Browse list (#207, #208). The frame is 240 px wide, 16:9 and not
 * cropped, so a detection can be judged from the list; hovering plays the preview loop at that size
 * and clicking the frame opens the player. The large-card gallery lives in Analytics (SegmentCard).
 */
export default function SegmentRow({ seg, onPreview, onChange, onDeleted }: Props) {
  const [jumping, setJumping] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const jump = async () => {
    setJumping(true)
    try {
      const d = await api.post<{ ok: boolean; client: string; user: string; seek_to_ms: number }>(`/api/segments/${seg.id}/jump`)
      if (d.ok) toast.success(`Jumped to ${msToTimecode(d.seek_to_ms)} on ${d.client} (${d.user}).`)
    } catch (err: any) {
      toast.error(err?.message || 'Could not jump to this segment. Start the title in Plex first.')
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
      toast.error(`Could not delete this segment: ${err?.message || 'Unknown error'}`)
      setDeleting(false)
    }
  }

  const timeCls = `font-mono text-sm ${seg.false_positive ? 'text-gray-400 line-through' : 'text-plex-orange'}`
  const labels = segmentLabels(seg)

  return (
    <div className={`flex flex-col sm:flex-row bg-plex-card border rounded-xl overflow-hidden ${seg.false_positive ? 'border-amber-400/30' : 'border-plex-border'}`}>
      <button onClick={() => onPreview(seg)} aria-label={`Play segment ${msToTimecode(seg.start_ms)}`} title="Play this segment" className="group block w-full sm:w-60 flex-shrink-0">
        <HoverPreview src={seg.hover_url} fit="contain" className={`aspect-video bg-black ${seg.false_positive ? '[&_img]:brightness-50 [&_video]:brightness-50' : ''}`}>
          {seg.has_thumbnail ? (
            <SafeImg src={seg.thumbnail_url} alt="Flagged frame" fit="contain" iconSize={24} className="w-full h-full bg-black" />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-faint"><AlertTriangle size={24} /></div>
          )}
          <div className="absolute bottom-1 left-1 bg-black/70 text-xs text-gray-200 px-1.5 py-0.5 rounded">
            {Math.round(seg.confidence * 100)}%
          </div>
          <ThumbnailTime ms={seg.thumbnail_ms} />
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity pointer-events-none">
            <span className="rounded-full bg-black/60 p-2.5 text-white"><Play size={18} /></span>
          </div>
        </HoverPreview>
      </button>
      <div className="flex-1 min-w-0 p-3 flex flex-col sm:flex-row sm:items-center gap-3">
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            {/* A false positive recedes without losing legibility: the frame dims, the times are struck through. */}
            <span className={timeCls}>{msToTimecode(seg.start_ms)}</span>
            <span className="text-muted text-xs">→</span>
            <span className={timeCls}>{msToTimecode(seg.end_ms)}</span>
            <span className="text-xs text-muted">{Math.round((seg.end_ms - seg.start_ms) / 1000)}s</span>
            <SegmentOriginBadge seg={seg} />
            {seg.false_positive && <FalsePositiveTag autoRejected={seg.auto_rejected} />}
          </div>
          {labels.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {labels.map(label => (
                <span key={label} className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 border border-red-500/30" title={label}>
                  {shortLabel(label)}
                </span>
              ))}
            </div>
          )}
          {seg.created_at && <p className="text-xs text-muted">Detected {parseDbTimestamp(seg.created_at).toLocaleDateString()}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-1 sm:flex-shrink-0">
          <a href={`/api/segments/${seg.id}/vlc`} title="Open in VLC" aria-label="Open in VLC" className="p-2 text-gray-400 hover:text-blue-400 hover:bg-blue-400/10 rounded-lg transition-colors">
            <Tv2 size={16} />
          </a>
          <button onClick={jump} disabled={jumping} title="Jump active Plex playback to this segment" aria-label="Jump active Plex playback to this segment" className="p-2 text-gray-400 hover:text-plex-orange hover:bg-plex-orange/10 rounded-lg transition-colors disabled:opacity-40">
            <SkipForward size={16} />
          </button>
          <RelabelButton seg={seg} onChange={(_, l) => onChange({ ...seg, labels: l })} />
          <FalsePositiveButton seg={seg} onChange={(_, fp) => onChange({ ...seg, false_positive: fp, auto_rejected: fp ? seg.auto_rejected : false })} />
          <button onClick={remove} disabled={deleting} title="Delete this segment" aria-label="Delete this segment" className="p-2 text-gray-400 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-40">
            <Trash2 size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}
