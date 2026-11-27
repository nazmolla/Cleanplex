import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import Modal from '../Modal'
import SegmentPlayer from '../SegmentPlayer'
import SegmentRow from './SegmentRow'
import { MediaVersion, Segment, Title, msToTimecode, versionLabel } from './model'
import { useT } from '../../i18n'

interface Props {
  title: Title
  /** A segment changed or was deleted: the title's counts in Browse are out of date. */
  onChanged: (t: Title) => void
  selectedVersionId?: number
  onSelectVersion?: (versionId: number) => void
  onScanVersion?: (t: Title, versionId: number, now: boolean) => void
  onDeleteVersionSegments?: (t: Title, versionId: number) => void
}

/**
 * A title's segments shown inline under its Browse row when the row is expanded (#209).
 * When a title has multiple versions (#233), lists each version and scopes segments & actions
 * to the selected version.
 */
export default function TitleSegments({
  title, onChanged, selectedVersionId, onSelectVersion, onScanVersion, onDeleteVersionSegments,
}: Props) {
  const { t } = useT()
  const [segs, setSegs] = useState<Segment[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const versions = title.versions ?? []
  const multi = versions.length > 1
  const [localVersionId, setLocalVersionId] = useState<number>(() => versions[0]?.id ?? 0)
  const activeVersionId = multi ? (selectedVersionId || localVersionId || versions[0]?.id || 0) : 0
  const selectedVer: MediaVersion | undefined = multi ? (versions.find(v => v.id === activeVersionId) ?? versions[0]) : undefined
  const dupRes = multi && new Set(versions.map(v => v.resolution || '')).size < versions.length

  const pickVersion = (vid: number) => {
    setLocalVersionId(vid)
    onSelectVersion?.(vid)
  }

  const versionCountKey = versions.map(v => `${v.id}:${v.segment_count}:${v.false_positive_count ?? 0}`).join(',')
  const countKey = `${title.segment_count}:${title.false_positive_count ?? 0}:${versionCountKey}`

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

  const visibleSegs = segs && multi && activeVersionId
    ? segs.filter(s => !s.version_id || s.version_id === activeVersionId)
    : segs

  return (
    <div className="px-3 py-3 ps-10 border-b border-hayaa-border bg-black/20 space-y-2">
      {multi && selectedVer && (
        <div className="flex flex-wrap items-center gap-2 pb-1">
          <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label={t('versions_tablist_aria')}>
            {versions.map(v => {
              const active = v.id === selectedVer.id
              return (
                <button
                  key={v.id}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => pickVersion(v.id)}
                  title={v.file_path}
                  className={`px-2.5 py-1 rounded text-xs flex items-center gap-1.5 border ${
                    active
                      ? 'bg-hayaa-gold/15 border-hayaa-gold text-hayaa-gold'
                      : 'border-hayaa-border text-gray-300 hover:text-white'
                  }`}
                >
                  <span className="font-medium">{versionLabel(v, dupRes, t)}</span>
                  <span className="text-[11px] text-muted">{v.status}</span>
                  <span className="text-[11px] tabular-nums text-muted">({v.segment_count})</span>
                </button>
              )
            })}
          </div>
          <div className="ms-auto flex items-center gap-1.5">
            {onScanVersion && (
              <button
                type="button"
                onClick={() => onScanVersion(title, selectedVer.id, true)}
                className="btn-outline text-xs py-1 px-2"
              >
                {t('versions_scan')}
              </button>
            )}
            {onDeleteVersionSegments && (selectedVer.segment_count > 0 || (selectedVer.false_positive_count ?? 0) > 0) && (
              <button
                type="button"
                onClick={() => onDeleteVersionSegments(title, selectedVer.id)}
                className="btn-danger text-xs py-1 px-2"
              >
                {t('versions_delete_segments')}
              </button>
            )}
          </div>
        </div>
      )}
      {failed && <p className="text-sm text-red-400">{t('title_segs_load_error')}</p>}
      {!failed && segs === null && <p className="text-xs text-muted" role="status">{t('segs_loading')}</p>}
      {visibleSegs?.length === 0 && <p className="text-xs text-muted">{multi ? t('versions_segs_empty') : t('title_segs_empty')}</p>}
      {visibleSegs?.map(seg => (
        <SegmentRow key={seg.id} seg={seg} onPreview={setPreviewSeg} onChange={patch} onDeleted={drop} />
      ))}
      {previewSeg && (
        <Modal
          title={t('segs_preview_title')}
          subtitle={<><span dir="ltr">{msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}</span> · {t('segs_preview_subtitle')}</>}
          size="xl"
          onClose={() => setPreviewSeg(null)}
        >
          <SegmentPlayer segmentId={previewSeg.id} autoPlay className="max-h-[70vh]" />
        </Modal>
      )}
    </div>
  )
}
