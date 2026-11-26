import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import Modal from '../Modal'
import SegmentPlayer from '../SegmentPlayer'
import SkipFileImport from '../SkipFileImport'
import SegmentRow from './SegmentRow'
import { Segment, SegmentFilters, Title, msToTimecode, segmentLabels, segmentOrigin, segmentPasses, versionLabel } from './model'
import { useT } from '../../i18n'

// Guids per batch request: keeps one request's SQL parameter list and reply size bounded.
const BATCH_GUIDS = 300
// Segment rows rendered at first and added per "Show more": each row holds a frame.
const PAGE = 80

const countKey = (t: Title) =>
  `${t.segment_count}:${t.false_positive_count ?? 0}:${(t.versions ?? []).map(v => `${v.id}:${v.segment_count}`).join(',')}`

interface Props {
  /** The titles in scope that pass Browse's filters, in display order. */
  titles: Title[]
  /** One title is in scope: show its import / export box and no per-title headings. */
  single: boolean
  /** How a title is named in a heading, relative to the current level. */
  nameOf: (t: Title) => { name: string; context: string }
  onOpenTitle: (t: Title) => void
  /** Segments were imported for a title: its counts in Browse are out of date. */
  onImported: (t: Title) => void
  filters: SegmentFilters
  /** The labels and detectors present in scope, for the filter panel's choices. */
  onOptions: (labels: string[], origins: string[]) => void
  selectedVersionId?: number
  onSelectVersion?: (versionId: number) => void
}

/**
 * Every segment of the titles in scope (#207), one row each, grouped by title (#208).
 * Lists are fetched through the batch endpoint and cached per title against the counts they were
 * fetched at, so a scan, import or delete elsewhere refetches only the titles it touched.
 */
export default function SegmentsView({
  titles, single, nameOf, onOpenTitle, onImported, filters, onOptions, selectedVersionId, onSelectVersion,
}: Props) {
  const { t: tr } = useT()
  const [segs, setSegs] = useState<Record<string, Segment[]>>({})
  // The title's counts when its list was fetched. Compared with later counts rather than
  // the list length, because the list hides disabled labels and the count does not.
  const fetchedAt = useRef<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const [limit, setLimit] = useState(PAGE)
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const [showImport, setShowImport] = useState(false)
  const singleVersions = single ? (titles[0]?.versions ?? []) : []
  const multiSingle = singleVersions.length > 1
  const [localVersionId, setLocalVersionId] = useState<number>(() => singleVersions[0]?.id ?? 0)
  const activeVersionId = multiSingle ? (selectedVersionId || localVersionId || singleVersions[0]?.id || 0) : undefined
  const dupRes = multiSingle && new Set(singleVersions.map(v => v.resolution || '')).size < singleVersions.length

  const withSegments = useMemo(
    () => titles.filter(t => t.segment_count > 0 || (t.false_positive_count ?? 0) > 0),
    [titles],
  )
  // Changes only when a title enters the scope or its counts move, not on every re-render.
  const wanted = withSegments.map(t => `${t.plex_guid}=${countKey(t)}`).join('|')

  // Fetch the lists that are missing or stale. An earlier run still in flight is dropped by
  // flag: its replies are ignored so they cannot overwrite a newer scope's state.
  useEffect(() => {
    const need = withSegments.filter(t => fetchedAt.current[t.plex_guid] !== countKey(t))
    if (need.length === 0) return
    let cancelled = false
    setLoading(true)
    setFailed(false)
    ;(async () => {
      try {
        for (let i = 0; i < need.length && !cancelled; i += BATCH_GUIDS) {
          const chunk = need.slice(i, i + BATCH_GUIDS)
          const d = await api.post<{ segments: Segment[] }>('/api/titles/segments/batch', { guids: chunk.map(t => t.plex_guid) })
          if (cancelled) return
          const byGuid: Record<string, Segment[]> = Object.fromEntries(chunk.map(t => [t.plex_guid, []]))
          for (const seg of d.segments) byGuid[seg.plex_guid]?.push(seg)
          for (const t of chunk) fetchedAt.current[t.plex_guid] = countKey(t)
          setSegs(prev => ({ ...prev, ...byGuid }))
        }
      } catch {
        if (!cancelled) setFailed(true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [wanted])

  // A new scope or new filters start from the first page again.
  const scopeKey = withSegments.map(t => t.plex_guid).join('|')
  useEffect(() => { setLimit(PAGE) }, [scopeKey, filters])

  const inScope = useMemo(() => {
    const raw = withSegments.flatMap(t => segs[t.plex_guid] ?? [])
    return multiSingle && activeVersionId
      ? raw.filter(s => !s.version_id || s.version_id === activeVersionId)
      : raw
  }, [withSegments, segs, multiSingle, activeVersionId])

  // Report the choices for the filter panel when they change.
  const labelKey = useMemo(() => Array.from(new Set(inScope.flatMap(segmentLabels))).sort().join('\n'), [inScope])
  const originKey = useMemo(() => Array.from(new Set(inScope.map(segmentOrigin))).sort().join('\n'), [inScope])
  useEffect(() => {
    onOptions(labelKey ? labelKey.split('\n') : [], originKey ? originKey.split('\n') : [])
  }, [labelKey, originKey])

  const groups = withSegments
    .map(t => ({
      title: t,
      segments: (segs[t.plex_guid] ?? []).filter(s =>
        (!multiSingle || !activeVersionId || !s.version_id || s.version_id === activeVersionId)
        && segmentPasses(s, filters),
      ),
    }))
    .filter(g => g.segments.length > 0)
  const total = groups.reduce((n, g) => n + g.segments.length, 0)

  // Cut the groups off at the page limit, mid-title if needed.
  let budget = limit
  const shown = groups
    .map(g => {
      const take = g.segments.slice(0, Math.max(0, budget))
      budget -= take.length
      return { title: g.title, count: g.segments.length, segments: take }
    })
    .filter(g => g.segments.length > 0)

  const patch = (seg: Segment) =>
    setSegs(prev => ({ ...prev, [seg.plex_guid]: (prev[seg.plex_guid] ?? []).map(x => (x.id === seg.id ? seg : x)) }))
  const drop = (seg: Segment) =>
    setSegs(prev => ({ ...prev, [seg.plex_guid]: (prev[seg.plex_guid] ?? []).filter(x => x.id !== seg.id) }))

  const filtered = total !== inScope.length

  return (
    <div className="space-y-5">
      {multiSingle && (
        <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label={tr('versions_tablist_aria')}>
          {singleVersions.map(v => {
            const active = v.id === activeVersionId
            return (
              <button
                key={v.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => { setLocalVersionId(v.id); onSelectVersion?.(v.id) }}
                title={v.file_path}
                className={`px-2.5 py-1 rounded text-xs flex items-center gap-1.5 border ${
                  active
                    ? 'bg-plex-orange/15 border-plex-orange text-plex-orange'
                    : 'border-plex-border text-gray-300 hover:text-white'
                }`}
              >
                <span className="font-medium">{versionLabel(v, dupRes, tr)}</span>
                <span className="text-[11px] text-muted">{v.status}</span>
                <span className="text-[11px] tabular-nums text-muted">({v.segment_count})</span>
              </button>
            )
          })}
        </div>
      )}

      {single && titles[0] && showImport && (
        <SkipFileImport
          plexGuid={titles[0].plex_guid}
          versionId={activeVersionId}
          onImported={() => {
            // Drop the cached list: the counts Browse reloads will differ and trigger a refetch,
            // and an import that changes no count is still picked up.
            delete fetchedAt.current[titles[0].plex_guid]
            onImported(titles[0])
          }}
        />
      )}

      <div className="flex items-center gap-3">
        <p className="text-xs text-muted tabular-nums" role="status">
          {loading ? tr('segs_loading')
            : filtered ? tr('segs_match_count', { matched: total, total: inScope.length })
            : single ? tr('segs_count_single', { segments: tr('unit_segment', { count: total }) })
            : tr('segs_count_multi', { segments: tr('unit_segment', { count: total }), titles: tr('unit_title', { count: groups.length }) })}
        </p>
        {/* Import and export are occasional, so they stay folded away until asked for. */}
        {single && (
          <button onClick={() => setShowImport(v => !v)} aria-expanded={showImport} className="ms-auto btn-outline text-xs">
            {showImport ? tr('segs_close_import') : tr('segs_import_files')}
          </button>
        )}
      </div>

      {failed && <p className="text-sm text-red-400">{tr('segs_load_error')}</p>}
      {!loading && !failed && total === 0 && (
        <p className="text-sm text-muted py-8 text-center">
          {inScope.length > 0 ? tr('segs_none_matching') : tr('segs_none_here')}
        </p>
      )}

      {shown.map(g => {
        const { name, context } = nameOf(g.title)
        return (
          <section key={g.title.plex_guid} className="space-y-2">
            {!single && (
              <h3 className="flex items-baseline gap-2 min-w-0">
                <button onClick={() => onOpenTitle(g.title)} className="text-sm font-semibold text-gray-100 hover:text-plex-orange truncate text-start">
                  {name}
                </button>
                {context && <span className="text-xs text-muted truncate">{context}</span>}
                <span className="ms-auto text-xs text-muted flex-shrink-0 tabular-nums">{g.count}</span>
              </h3>
            )}
            <div className="space-y-2">
              {g.segments.map(seg => (
                <SegmentRow key={seg.id} seg={seg} onPreview={setPreviewSeg} onChange={patch} onDeleted={drop} />
              ))}
            </div>
          </section>
        )
      })}

      {total > limit && (
        <div className="text-center">
          <button onClick={() => setLimit(l => l + PAGE)} className="btn-outline">
            {tr('browse_show_more', { count: total - limit })}
          </button>
        </div>
      )}

      {previewSeg && (
        <Modal
          title={tr('segs_preview_title')}
          subtitle={<><span dir="ltr">{msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}</span> · {tr('segs_preview_subtitle')}</>}
          size="xl"
          onClose={() => setPreviewSeg(null)}
        >
          <SegmentPlayer segmentId={previewSeg.id} autoPlay className="max-h-[70vh]" />
        </Modal>
      )}
    </div>
  )
}
