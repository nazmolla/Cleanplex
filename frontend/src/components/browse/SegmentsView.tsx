import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import Modal from '../Modal'
import SegmentPlayer from '../SegmentPlayer'
import SkipFileImport from '../SkipFileImport'
import SegmentRow from './SegmentRow'
import { Segment, Title, msToTimecode, segmentLabels, segmentOrigin, shortLabel } from './model'

type StateFilter = 'all' | 'active' | 'fp_user' | 'fp_verifier'

// Guids per batch request: keeps one request's SQL parameter list and reply size bounded.
const BATCH_GUIDS = 300
// Segment rows rendered at first and added per "Show more": each row holds a thumbnail.
const PAGE = 100

const countKey = (t: Title) => `${t.segment_count}:${t.false_positive_count ?? 0}`

interface Props {
  /** The titles in scope that pass Browse's filters, in display order. */
  titles: Title[]
  /** One title is in scope: show its import / export box and no per-title headings. */
  single: boolean
  /** Name shown above a title's segments, relative to the current level. */
  titleLabel: (t: Title) => string
  onOpenTitle: (t: Title) => void
  /** Segments were imported for a title: its counts in Browse are out of date. */
  onImported: (t: Title) => void
}

/**
 * Every segment of the titles in scope (#207), with the segment-level filters. Lists are
 * fetched through the batch endpoint and cached per title against the counts they were
 * fetched at, so a scan, import or delete elsewhere refetches only the titles it touched.
 */
export default function SegmentsView({ titles, single, titleLabel, onOpenTitle, onImported }: Props) {
  const [segs, setSegs] = useState<Record<string, Segment[]>>({})
  // The title's counts when its list was fetched. Compared with later counts rather than
  // the list length, because the list hides disabled labels and the count does not.
  const fetchedAt = useRef<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const [labelFilter, setLabelFilter] = useState<Set<string>>(new Set())
  const [originFilter, setOriginFilter] = useState('')
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [limit, setLimit] = useState(PAGE)
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)

  const withSegments = useMemo(
    () => titles.filter(t => t.segment_count > 0 || (t.false_positive_count ?? 0) > 0),
    [titles],
  )
  // Changes only when a title enters the scope or its counts move, not on every re-render.
  const wanted = withSegments.map(t => `${t.plex_guid}=${countKey(t)}`).join('|')

  // Fetch the lists that are missing or stale. An earlier run still in flight is aborted
  // by flag: its replies are dropped so they cannot overwrite a newer scope's state.
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

  // A new scope starts from the first page again.
  const scopeKey = withSegments.map(t => t.plex_guid).join('|')
  useEffect(() => { setLimit(PAGE) }, [scopeKey])

  const inScope = useMemo(() => withSegments.flatMap(t => segs[t.plex_guid] ?? []), [withSegments, segs])
  const labelOptions = useMemo(() => Array.from(new Set(inScope.flatMap(segmentLabels))).sort(), [inScope])
  const originOptions = useMemo(() => Array.from(new Set(inScope.map(segmentOrigin))).sort(), [inScope])

  const passes = (s: Segment) => {
    if (stateFilter === 'active' && s.false_positive) return false
    if (stateFilter === 'fp_user' && !(s.false_positive && !s.auto_rejected)) return false
    if (stateFilter === 'fp_verifier' && !s.auto_rejected) return false
    if (originFilter && segmentOrigin(s) !== originFilter) return false
    // A label filter keeps segments that carry at least one selected label.
    if (labelFilter.size > 0 && !segmentLabels(s).some(l => labelFilter.has(l))) return false
    return true
  }

  const groups = withSegments
    .map(t => ({ title: t, segments: (segs[t.plex_guid] ?? []).filter(passes) }))
    .filter(g => g.segments.length > 0)
  const total = groups.reduce((n, g) => n + g.segments.length, 0)

  // Cut the groups off at the page limit, mid-title if needed.
  let budget = limit
  const shown = groups
    .map(g => {
      const take = g.segments.slice(0, Math.max(0, budget))
      budget -= take.length
      return { title: g.title, segments: take }
    })
    .filter(g => g.segments.length > 0)

  const patch = (seg: Segment) =>
    setSegs(prev => ({ ...prev, [seg.plex_guid]: (prev[seg.plex_guid] ?? []).map(x => (x.id === seg.id ? seg : x)) }))
  const drop = (seg: Segment) =>
    setSegs(prev => ({ ...prev, [seg.plex_guid]: (prev[seg.plex_guid] ?? []).filter(x => x.id !== seg.id) }))

  const toggleLabel = (label: string) =>
    setLabelFilter(prev => {
      const next = new Set(prev)
      next.has(label) ? next.delete(label) : next.add(label)
      return next
    })

  const filtering = labelFilter.size > 0 || originFilter !== '' || stateFilter !== 'all'
  const selectCls = 'px-2 py-1.5 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-300 focus:border-plex-orange/50'

  return (
    <div className="space-y-3">
      {single && titles[0] && (
        <SkipFileImport
          plexGuid={titles[0].plex_guid}
          onImported={() => {
            // Drop the cached list: the counts Browse reloads will differ and trigger a refetch,
            // and an import that changes no count is still picked up.
            delete fetchedAt.current[titles[0].plex_guid]
            onImported(titles[0])
          }}
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        <select value={stateFilter} onChange={e => setStateFilter(e.target.value as StateFilter)} aria-label="Segment state" className={selectCls}>
          <option value="all">All segments</option>
          <option value="active">Active only</option>
          <option value="fp_user">False positives (marked by you)</option>
          <option value="fp_verifier">Rejected by the verifier</option>
        </select>
        {originOptions.length > 1 && (
          <select value={originFilter} onChange={e => setOriginFilter(e.target.value)} aria-label="Found by" className={selectCls}>
            <option value="">Found by anything</option>
            {originOptions.map(o => <option key={o} value={o}>{o}</option>)}
          </select>
        )}
        {labelOptions.map(label => {
          const active = labelFilter.has(label)
          return (
            <button
              key={label}
              onClick={() => toggleLabel(label)}
              aria-pressed={active}
              title={label}
              className={`text-xs px-2 py-1 rounded-full border transition-colors ${
                active ? 'bg-plex-orange/20 text-plex-orange border-plex-orange/50' : 'text-gray-400 border-plex-border hover:text-gray-200'
              }`}
            >
              {shortLabel(label)}
            </button>
          )
        })}
        {filtering && (
          <button onClick={() => { setLabelFilter(new Set()); setOriginFilter(''); setStateFilter('all') }} className="text-xs text-gray-400 hover:text-white px-1">
            Clear
          </button>
        )}
        <span className="ml-auto text-xs text-muted tabular-nums whitespace-nowrap" role="status">
          {loading ? 'Loading segments…' : filtering ? `${total} of ${inScope.length} segments` : `${total} segment${total === 1 ? '' : 's'} in ${groups.length} title${groups.length === 1 ? '' : 's'}`}
        </span>
      </div>

      {failed && <p className="text-sm text-red-400">Could not load segments. Change the level or reload the page to retry.</p>}
      {!loading && !failed && total === 0 && (
        <p className="text-sm text-muted py-6 text-center">
          {inScope.length > 0 ? 'No segments match the segment filters.' : 'No segments here.'}
        </p>
      )}

      {shown.map(g => (
        <section key={g.title.plex_guid} className="space-y-2">
          {!single && (
            <h3 className="flex items-baseline gap-2 text-sm">
              <button onClick={() => onOpenTitle(g.title)} className="text-gray-200 font-medium hover:text-plex-orange truncate text-left">
                {titleLabel(g.title)}
              </button>
              <span className="text-xs text-muted flex-shrink-0">{(segs[g.title.plex_guid] ?? []).filter(passes).length}</span>
            </h3>
          )}
          {g.segments.map(seg => (
            <SegmentRow key={seg.id} seg={seg} onPreview={setPreviewSeg} onChange={patch} onDeleted={drop} />
          ))}
        </section>
      ))}

      {total > limit && (
        <div className="text-center">
          <button onClick={() => setLimit(l => l + PAGE)} className="btn-outline">
            Show more ({total - limit} left)
          </button>
        </div>
      )}

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
