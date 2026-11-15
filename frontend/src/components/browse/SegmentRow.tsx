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
import { Segment, msToTimecode, segmentLabels, shortLabel } from './model'

interface Props {
  seg: Segment
  onPreview: (seg: Segment) => void
  /** The segment changed in place (relabelled, or marked / unmarked as a false positive). */
  onChange: (seg: Segment) => void
  onDeleted: (seg: Segment) => void
}

/** One segment with every action it supports. The only segment row Browse renders (#207). */
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

  const timeCls = `font-mono text-xs ${seg.false_positive ? 'text-gray-400 line-through' : 'text-plex-orange'}`
  const labels = segmentLabels(seg)

  return (
    <div className={`flex flex-col sm:flex-row bg-plex-darker border border-plex-border rounded-lg overflow-hidden ${seg.false_positive ? '[&_img]:opacity-40 [&_video]:opacity-40' : ''}`}>
      <HoverPreview src={seg.hover_url} className="w-full sm:w-40 sm:flex-shrink-0 bg-black">
        {seg.has_thumbnail ? (
          <img src={seg.thumbnail_url} alt="Flagged frame" loading="lazy" className="w-full object-cover" style={{ height: '100px' }} />
        ) : (
          <div className="w-full flex items-center justify-center text-faint" style={{ height: '100px' }}><AlertTriangle size={20} /></div>
        )}
        <div className="absolute bottom-0.5 left-0.5 bg-black/70 text-gray-300 px-1 py-0.5 rounded text-[10px]">
          {Math.round(seg.confidence * 100)}%
        </div>
        <ThumbnailTime ms={seg.thumbnail_ms} />
      </HoverPreview>
      <div className="flex-1 p-2 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 min-w-0">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            {/* A false positive recedes without losing legibility: the thumbnail dims, the times are struck through. */}
            <span className={timeCls}>{msToTimecode(seg.start_ms)}</span>
            <span className="text-muted text-xs">→</span>
            <span className={timeCls}>{msToTimecode(seg.end_ms)}</span>
            <span className="text-xs text-muted">({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)</span>
            <SegmentOriginBadge seg={seg} />
            {seg.false_positive && <FalsePositiveTag autoRejected={seg.auto_rejected} />}
          </div>
          <p className="text-xs text-muted mt-0.5">Detected {parseDbTimestamp(seg.created_at).toLocaleDateString()}</p>
          {labels.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {labels.map(label => (
                <span key={label} className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-300 border border-red-500/30" title={label}>
                  {shortLabel(label)}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1 sm:flex-shrink-0">
          <button onClick={() => onPreview(seg)} title="Preview segment video" aria-label="Preview segment video" className="p-2 text-gray-400 hover:text-green-400 hover:bg-green-400/10 rounded transition-colors">
            <Play size={15} />
          </button>
          <a href={`/api/segments/${seg.id}/vlc`} title="Open in VLC" aria-label="Open in VLC" className="p-2 text-gray-400 hover:text-blue-400 hover:bg-blue-400/10 rounded transition-colors">
            <Tv2 size={15} />
          </a>
          <button onClick={jump} disabled={jumping} title="Jump active Plex playback to this segment" aria-label="Jump active Plex playback to this segment" className="p-2 text-gray-400 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40">
            <SkipForward size={15} />
          </button>
          <RelabelButton seg={seg} onChange={(_, labels) => onChange({ ...seg, labels })} />
          <FalsePositiveButton seg={seg} onChange={(_, fp) => onChange({ ...seg, false_positive: fp, auto_rejected: fp ? seg.auto_rejected : false })} />
          <button onClick={remove} disabled={deleting} title="Delete this segment" aria-label="Delete this segment" className="p-2 text-gray-400 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors disabled:opacity-40">
            <Trash2 size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}
