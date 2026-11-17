import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import Modal from '../Modal'
import SegmentPlayer from '../SegmentPlayer'
import SegmentRow from './SegmentRow'
import { Segment, Title, msToTimecode } from './model'

interface Props {
  title: Title
  /** A segment changed or was deleted: the title's counts in Browse are out of date. */
  onChanged: (t: Title) => void
}

/**
 * A title's segments shown inline under its Browse row when the row is expanded (#209).
 * Refetched when the title's counts change, so a scan or delete elsewhere shows up here.
 */
export default function TitleSegments({ title, onChanged }: Props) {
  const [segs, setSegs] = useState<Segment[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const countKey = `${title.segment_count}:${title.false_positive_count ?? 0}`

  // Reload when the counts move; a reply for an older key is dropped so it cannot overwrite a newer one.
  useEffect(() => {
    let cancelled = false
    setFailed(false)
    api.post<{ segments: Segment[] }>('/api/titles/segments/batch', { guids: [title.plex_guid] })
      .then(d => { if (!cancelled) setSegs(d.segments) })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => { cancelled = true }
  }, [title.plex_guid, countKey])

  const patch = (seg: Segment) => { setSegs(prev => prev?.map(x => (x.id === seg.id ? seg : x)) ?? null); onChanged(title) }
  const drop = (seg: Segment) => { setSegs(prev => prev?.filter(x => x.id !== seg.id) ?? null); onChanged(title) }

  return (
    <div className="px-3 py-3 pl-10 border-b border-plex-border bg-black/20 space-y-2">
      {failed && <p className="text-sm text-red-400">Could not load segments. Collapse and expand the row to retry.</p>}
      {!failed && segs === null && <p className="text-xs text-muted" role="status">Loading segments…</p>}
      {segs?.length === 0 && <p className="text-xs text-muted">No segments for this title.</p>}
      {segs?.map(seg => (
        <SegmentRow key={seg.id} seg={seg} onPreview={setPreviewSeg} onChange={patch} onDeleted={drop} />
      ))}
      {previewSeg && (
        <Modal
          title="Segment preview"
          subtitle={<>{msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)} · clip includes 5s either side</>}
          size="xl"
          onClose={() => setPreviewSeg(null)}
        >
          <SegmentPlayer segmentId={previewSeg.id} autoPlay className="max-h-[70vh]" />
        </Modal>
      )}
    </div>
  )
}
