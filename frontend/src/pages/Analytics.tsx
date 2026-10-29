import { useEffect, useState, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import SegmentOriginBadge from '../components/SegmentOriginBadge'
import FalsePositiveButton from '../components/FalsePositiveButton'
import RelabelButton from '../components/RelabelButton'
import SkipAnalytics from '../components/SkipAnalytics'
import DetectionAccuracy from '../components/DetectionAccuracy'
import { BarChart2, X, AlertTriangle, Trash2, Play, SkipForward, ChevronLeft, ChevronRight } from 'lucide-react'
import HoverPreview from '../components/HoverPreview'
import CollapsibleSection from '../components/CollapsibleSection'

interface LabelCount {
  label: string
  count: number
}

interface RatingCount {
  content_rating: string
  count: number
}

interface Segment {
  id: number
  plex_guid: string
  title: string
  start_ms: number
  end_ms: number
  confidence: number
  labels: string
  has_thumbnail: boolean
  thumbnail_url: string
  /** Short silent loop played on hover; empty until rendered (#114). */
  hover_url?: string
  poster_url: string
  content_rating: string
  media_type: string
  rating_key: string
  source?: string
  detector?: string
  false_positive?: boolean
  /** When it was marked (UTC, SQLite format); False positives tab only. */
  marked_at?: string | null
}

const PAGE_SIZE = 50

// Tabs, selected by ?tab= so each one can be linked like a sub-page (#117).
const TABS = [
  { id: 'detections', label: 'Detections' },
  { id: 'skips', label: 'Skips' },
  { id: 'accuracy', label: 'Accuracy' },
  { id: 'false-positives', label: 'False positives' },
] as const
type TabId = typeof TABS[number]['id']

function formatMarkedAt(v: string): string {
  const d = new Date(v.replace(' ', 'T') + 'Z')
  return isNaN(d.getTime()) ? v : d.toLocaleDateString()
}

function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

function shortLabel(label: string): string {
  return label
    .replace('_EXPOSED', '')
    .replace('FEMALE_', 'F ')
    .replace('MALE_', 'M ')
    .replace('GENITALIA', 'Gen.')
    .replace('BREAST', 'Breast')
    .replace(/_/g, ' ')
    .trim()
}

// Pure-CSS horizontal bar chart
function BarChart({
  data,
  maxVal,
  selectedBar,
  onBarClick,
  colorClass = 'bg-plex-orange',
}: {
  data: { key: string; count: number; label?: string }[]
  maxVal: number
  selectedBar?: string | null
  onBarClick?: (key: string) => void
  colorClass?: string
}) {
  if (!data.length) return <p className="text-gray-600 text-sm">No data</p>

  return (
    <div className="space-y-2">
      {data.map(({ key, count, label }) => {
        const pct = maxVal > 0 ? (count / maxVal) * 100 : 0
        const isSelected = selectedBar === key
        return (
          <div
            key={key}
            className={`group flex items-center gap-3 rounded-lg px-3 py-2 transition-colors ${
              onBarClick ? 'cursor-pointer hover:bg-white/5' : ''
            } ${isSelected ? 'bg-plex-orange/10 ring-1 ring-plex-orange/30' : ''}`}
            onClick={() => onBarClick?.(key)}
          >
            <div className="w-44 flex-shrink-0 text-xs text-gray-300 truncate" title={label ?? key}>
              {label ?? shortLabel(key)}
            </div>
            <div className="flex-1 h-5 bg-plex-border rounded-full overflow-hidden">
              <div
                className={`h-full ${colorClass} rounded-full transition-all duration-300`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="w-10 text-right text-xs text-gray-400 flex-shrink-0 tabular-nums">{count}</div>
          </div>
        )
      })}
    </div>
  )
}

function Pager({ page, totalPages, onPage, compact = false }: {
  page: number
  totalPages: number
  onPage: (p: number) => void
  compact?: boolean
}) {
  if (totalPages <= 1) return null
  const btn = compact
    ? 'p-1 rounded hover:bg-white/5 disabled:opacity-40'
    : 'flex items-center gap-1 px-3 py-1.5 bg-plex-card border border-plex-border rounded-lg hover:text-white hover:border-gray-500 disabled:opacity-40 transition-colors'
  return (
    <div className={`flex items-center text-xs text-gray-400 ${compact ? "gap-2" : "gap-3 justify-center mt-4"}`}>
      <button onClick={() => onPage(Math.max(0, page - 1))} disabled={page === 0} className={btn} aria-label="Previous page">
        <ChevronLeft size={compact ? 14 : 13} />{!compact && ' Prev'}
      </button>
      <span>{page + 1} / {totalPages}</span>
      <button onClick={() => onPage(Math.min(totalPages - 1, page + 1))} disabled={page >= totalPages - 1} className={btn} aria-label="Next page">
        {!compact && 'Next '}<ChevronRight size={compact ? 14 : 13} />
      </button>
    </div>
  )
}

export default function Analytics() {
  const [params, setParams] = useSearchParams()
  const tab: TabId = TABS.some(t => t.id === params.get('tab')) ? (params.get('tab') as TabId) : 'detections'
  const selectTab = (id: TabId) => setParams(id === 'detections' ? {} : { tab: id }, { replace: true })

  const [labelCounts, setLabelCounts] = useState<LabelCount[]>([])
  const [loadingLabels, setLoadingLabels] = useState(true)

  // Drill-down: rating breakdown for a selected label
  const [drillLabel, setDrillLabel] = useState<string | null>(null)
  const [ratingCounts, setRatingCounts] = useState<RatingCount[]>([])
  const [loadingRatings, setLoadingRatings] = useState(false)

  // Multi-select filter
  const [selectedLabels, setSelectedLabels] = useState<string[]>([])

  // Segment list
  const [segments, setSegments] = useState<Segment[]>([])
  const [totalSegments, setTotalSegments] = useState(0)
  const [loadingSegments, setLoadingSegments] = useState(false)
  const [page, setPage] = useState(0)

  // Per-segment state
  const [deletingSegs, setDeletingSegs] = useState<Record<number, boolean>>({})
  const [jumpingSegs, setJumpingSegs] = useState<Record<number, boolean>>({})
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)

  const loadLabels = useCallback(() => {
    api.get<{ labels: LabelCount[] }>('/api/analytics/labels')
      .then(d => setLabelCounts(d.labels))
      .finally(() => setLoadingLabels(false))
  }, [])

  // Load label counts on mount
  useEffect(() => { loadLabels() }, [loadLabels])

  // Load drill-down when a bar is clicked
  useEffect(() => {
    if (!drillLabel) { setRatingCounts([]); return }
    setLoadingRatings(true)
    api.get<{ ratings: RatingCount[] }>(`/api/analytics/labels/${encodeURIComponent(drillLabel)}/ratings`)
      .then(d => setRatingCounts(d.ratings))
      .finally(() => setLoadingRatings(false))
  }, [drillLabel])

  // `silent` is for live refetches (#116): the current list stays on screen and is
  // swapped when the data arrives, instead of blanking to "Loading…" (which reads
  // as a page reload, e.g. two seconds after marking a false positive).
  const segmentsCtl = useRef<AbortController | null>(null)
  const fetchSegments = useCallback(async (labels: string[], p: number, silent = false) => {
    segmentsCtl.current?.abort()
    if (!labels.length) { setSegments([]); setTotalSegments(0); return }
    const controller = (segmentsCtl.current = new AbortController())
    if (!silent) setLoadingSegments(true)
    try {
      const d = await api.get<{ segments: Segment[]; total: number }>(
        `/api/analytics/segments?labels=${encodeURIComponent(labels.join(','))}&limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}`,
        { signal: controller.signal },
      )
      setSegments(d.segments)
      setTotalSegments(d.total)
    } catch (err: any) {
      if (err?.name !== 'AbortError') console.warn('Could not load segments:', err?.message)
    } finally {
      if (segmentsCtl.current === controller) setLoadingSegments(false)
    }
  }, [])

  // False positives tab (#117): every marked segment, newest mark first.
  const [fpSegments, setFpSegments] = useState<Segment[]>([])
  const [fpTotal, setFpTotal] = useState(0)
  const [fpPage, setFpPage] = useState(0)
  const [loadingFp, setLoadingFp] = useState(false)
  const fpCtl = useRef<AbortController | null>(null)
  const fetchFalsePositives = useCallback(async (p: number, silent = false) => {
    fpCtl.current?.abort()
    const controller = (fpCtl.current = new AbortController())
    if (!silent) setLoadingFp(true)
    try {
      const d = await api.get<{ segments: Segment[]; total: number }>(
        `/api/analytics/false-positives?limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}`,
        { signal: controller.signal },
      )
      setFpSegments(d.segments)
      setFpTotal(d.total)
    } catch (err: any) {
      if (err?.name !== 'AbortError') console.warn('Could not load false positives:', err?.message)
    } finally {
      if (fpCtl.current === controller) setLoadingFp(false)
    }
  }, [])
  // Loads when the tab opens and on page changes; nothing is fetched for other tabs.
  useEffect(() => {
    if (tab === 'false-positives') fetchFalsePositives(fpPage)
  }, [tab, fpPage, fetchFalsePositives])
  useEffect(() => () => { segmentsCtl.current?.abort(); fpCtl.current?.abort() }, [])

  // Single effect handles both label changes (reset to page 0) and page navigation.
  // A ref tracks the previous labels so we can distinguish the two cases and avoid
  // the race condition that occurs when two separate effects both depend on selectedLabels:
  // the page effect would fire with the stale page number while the labels effect resets
  // to 0, producing two concurrent fetches whose results arrive in arbitrary order.
  const prevLabelsRef = useRef<string>('')
  useEffect(() => {
    const labelsKey = selectedLabels.join(',')
    if (labelsKey !== prevLabelsRef.current) {
      // Labels changed — always go back to page 0.
      prevLabelsRef.current = labelsKey
      setPage(0)
      fetchSegments(selectedLabels, 0)
    } else {
      // Only the page changed — fetch the requested page.
      fetchSegments(selectedLabels, page)
    }
  }, [page, selectedLabels, fetchSegments])

  // Live (#89): scans, imports and deletes change label counts and the segment
  // list. Debounced generously: a scan writes segments in bursts.
  const liveView = useRef({ selectedLabels, page, tab, fpPage })
  liveView.current = { selectedLabels, page, tab, fpPage }
  useLiveRefresh(['segments_changed'], () => {
    const v = liveView.current
    loadLabels()
    fetchSegments(v.selectedLabels, v.page, true)
    if (v.tab === 'false-positives') fetchFalsePositives(v.fpPage, true)
  }, { debounceMs: 2000 })

  const toggleLabel = (label: string) => {
    setSelectedLabels(prev =>
      prev.includes(label) ? prev.filter(l => l !== label) : [...prev, label]
    )
  }

  const handleBarClick = (key: string) => {
    setDrillLabel(prev => (prev === key ? null : key))
    // Also toggle in filter
    toggleLabel(key)
  }

  // This list shows active detections only, so a marked segment leaves it, as on
  // delete; the mark can be undone from the Segments or Library page.
  // Swap in the corrected labels without a refetch; the live refresh that follows
  // drops the row if it no longer matches the selected labels.
  const relabelInPlace = (id: number, labels: string) => {
    const apply = (list: Segment[]) => list.map(s => (s.id === id ? { ...s, labels } : s))
    setSegments(apply)
    setFpSegments(apply)
  }

  const dropFromList = (seg: Segment) => {
    setSegments(prev => prev.filter(s => s.id !== seg.id))
    setTotalSegments(t => Math.max(0, t - 1))
    setLabelCounts(prev => {
      const tokens = (seg.labels || '').split(',').map(l => l.trim()).filter(Boolean)
      return prev.map(lc =>
        tokens.includes(lc.label) ? { ...lc, count: Math.max(0, lc.count - 1) } : lc
      ).filter(lc => lc.count > 0)
    })
  }

  // Undo on the False positives tab: the segment is active again, so it leaves this list.
  const dropFromFalsePositives = (seg: Segment) => {
    setFpSegments(prev => prev.filter(s => s.id !== seg.id))
    setFpTotal(t => Math.max(0, t - 1))
  }

  const deleteSeg = async (seg: Segment) => {
    setDeletingSegs(d => ({ ...d, [seg.id]: true }))
    try {
      await api.delete(`/api/segments/${seg.id}`)
      if (seg.false_positive) dropFromFalsePositives(seg)
      else dropFromList(seg)
    } finally {
      setDeletingSegs(d => ({ ...d, [seg.id]: false }))
    }
  }

  const jumpSeg = async (seg: Segment) => {
    setJumpingSegs(j => ({ ...j, [seg.id]: true }))
    try {
      const d = await api.post<{ ok: boolean; client: string; user: string; seek_to_ms: number }>(
        `/api/segments/${seg.id}/jump`
      )
      if (d.ok) {
        alert(`Jumped playback to ${msToTimecode(d.seek_to_ms)} on ${d.client} (${d.user})`)
      }
    } catch (err: any) {
      alert(err?.message || 'Could not jump. Start this title in Plex first.')
    } finally {
      setJumpingSegs(j => ({ ...j, [seg.id]: false }))
    }
  }

  const renderRow = (seg: Segment) => (
                <div
                  key={seg.id}
                  className="bg-plex-card border border-plex-border rounded-xl overflow-hidden flex flex-col sm:flex-row"
                >
                  {/* Poster + thumbnail side-by-side on mobile */}
                  <div className="flex sm:flex-col">
                    {/* Poster */}
                    {seg.poster_url ? (
                      <img
                        src={seg.poster_url}
                        alt=""
                        loading="lazy"
                        className="w-12 sm:w-20 object-cover bg-plex-border flex-shrink-0"
                        style={{ minHeight: '48px' }}
                        onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                      />
                    ) : (
                      <div className="w-12 sm:w-20 bg-plex-border/50 flex-shrink-0" />
                    )}
                  </div>
                  {/* Thumbnail */}
                  <HoverPreview src={seg.hover_url} className="w-full sm:w-36 flex-shrink-0 bg-black">
                    {seg.has_thumbnail ? (
                      <img
                        src={seg.thumbnail_url}
                        alt="Flagged frame"
                        loading="lazy"
                        className="w-full object-cover"
                        style={{ height: '100px' }}
                      />
                    ) : (
                      <div className="w-full flex items-center justify-center text-gray-700" style={{ height: '100px' }}>
                        <AlertTriangle size={18} />
                      </div>
                    )}
                    <div className="absolute bottom-0.5 left-0.5 bg-black/70 text-[10px] text-gray-300 px-1 py-0.5 rounded">
                      {Math.round(seg.confidence * 100)}%
                    </div>
                  </HoverPreview>

                  {/* Info + actions */}
                  <div className="flex-1 p-3 flex flex-col justify-between min-w-0">
                    <div>
                      <p className="text-sm text-gray-100 truncate font-medium" title={seg.title}>
                        {seg.title}
                      </p>
                      <div className="flex flex-wrap items-center gap-2 mt-1">
                        <span className="font-mono text-xs text-plex-orange">
                          {msToTimecode(seg.start_ms)} → {msToTimecode(seg.end_ms)}
                        </span>
                        <span className="text-xs text-gray-600">
                          ({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)
                        </span>
                        {seg.content_rating && (
                          <span className="text-xs text-gray-600 bg-white/5 px-1.5 py-0.5 rounded">
                            {seg.content_rating}
                          </span>
                        )}
                        <SegmentOriginBadge seg={seg} />
                        {seg.marked_at && (
                          <span className="text-xs text-amber-300/80">Marked {formatMarkedAt(seg.marked_at)}</span>
                        )}
                      </div>
                      {/* Label chips */}
                      {seg.labels && (
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {seg.labels.split(',').filter(l => l.trim()).map(l => {
                            const t = l.trim()
                            const isActive = selectedLabels.includes(t)
                            return (
                              <span
                                key={t}
                                className={`text-[10px] px-1.5 py-0.5 rounded border ${
                                  isActive
                                    ? 'bg-plex-orange/20 border-plex-orange/40 text-plex-orange'
                                    : 'bg-red-500/10 border-red-500/20 text-red-300'
                                }`}
                              >
                                {shortLabel(t)}
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-1 mt-2">
                      <button
                        onClick={() => setPreviewSeg(seg)}
                        title="Preview segment"
                        className="p-1.5 text-gray-600 hover:text-green-400 hover:bg-green-400/10 rounded transition-colors"
                      >
                        <Play size={14} />
                      </button>
                      <button
                        onClick={() => jumpSeg(seg)}
                        disabled={jumpingSegs[seg.id]}
                        title="Jump active Plex playback to this segment"
                        className="p-1.5 text-gray-600 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40"
                      >
                        <SkipForward size={14} />
                      </button>
                      <RelabelButton seg={seg} onChange={relabelInPlace} />
                      <FalsePositiveButton
                        seg={seg}
                        onChange={(_, fp) => { if (seg.false_positive ? !fp : fp) (seg.false_positive ? dropFromFalsePositives : dropFromList)(seg) }}
                      />
                      <button
                        onClick={() => deleteSeg(seg)}
                        disabled={deletingSegs[seg.id]}
                        title="Delete this segment"
                        className="p-1.5 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors disabled:opacity-40"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                </div>
  )

  const maxLabelCount = labelCounts[0]?.count ?? 0
  const maxRatingCount = ratingCounts[0]?.count ?? 0
  const totalPages = Math.ceil(totalSegments / PAGE_SIZE)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
        <BarChart2 size={22} className="text-plex-orange" />
        Analytics
      </h1>

      {/* Tabs (#117) */}
      <div role="tablist" aria-label="Analytics sections" className="flex gap-1 border-b border-plex-border overflow-x-auto">
        {TABS.map(t => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => selectTab(t.id)}
            className={`px-4 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${
              tab === t.id
                ? 'border-plex-orange text-plex-orange font-medium'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'detections' && (
        <>
          <CollapsibleSection id="analytics.labels" title="Segments by label" hint="click a bar to drill down or add to filter">
            {loadingLabels ? (
              <p className="text-gray-500 text-sm">Loading…</p>
            ) : (
              <BarChart
                data={labelCounts.map(lc => ({ key: lc.label, count: lc.count }))}
                maxVal={maxLabelCount}
                selectedBar={drillLabel}
                onBarClick={handleBarClick}
              />
            )}
          </CollapsibleSection>

          {/* ── Drill-down: segments per rating for selected label ────────────── */}
          {drillLabel && (
            <section className="bg-plex-card border border-plex-orange/30 rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-gray-300">
                  <span className="text-plex-orange">{shortLabel(drillLabel)}</span>
                  <span className="text-gray-500 font-normal ml-2">— by content rating</span>
                </h2>
                <button
                  onClick={() => setDrillLabel(null)}
                  className="p-1 text-gray-500 hover:text-gray-300 rounded transition-colors"
                >
                  <X size={14} />
                </button>
              </div>
              {loadingRatings ? (
                <p className="text-gray-500 text-sm">Loading…</p>
              ) : (
                <BarChart
                  data={ratingCounts.map(rc => ({ key: rc.content_rating, count: rc.count, label: rc.content_rating || 'Unrated' }))}
                  maxVal={maxRatingCount}
                  colorClass="bg-blue-500"
                />
              )}
            </section>
          )}


          <CollapsibleSection id="analytics.filter" title="Filter by label">
            <div className="flex flex-wrap gap-2">
              {labelCounts.map(lc => {
                const active = selectedLabels.includes(lc.label)
                return (
                  <button
                    key={lc.label}
                    onClick={() => toggleLabel(lc.label)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors border ${
                      active
                        ? 'bg-plex-orange/20 border-plex-orange/50 text-plex-orange'
                        : 'bg-plex-darker border-plex-border text-gray-400 hover:text-gray-200 hover:border-gray-500'
                    }`}
                  >
                    {shortLabel(lc.label)}
                    <span className={`tabular-nums ${active ? 'text-plex-orange/70' : 'text-gray-600'}`}>
                      {lc.count}
                    </span>
                    {active && <X size={11} />}
                  </button>
                )
              })}
            </div>
            {selectedLabels.length > 0 && (
              <button
                onClick={() => setSelectedLabels([])}
                className="mt-2 text-xs text-gray-500 hover:text-gray-300 transition-colors"
              >
                Clear all
              </button>
            )}
          </CollapsibleSection>

          {/* ── Segment list for selected labels ─────────────────────────────── */}
          {selectedLabels.length > 0 && (
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
                  Segments
                  {!loadingSegments && (
                    <span className="ml-2 text-gray-600 normal-case font-normal">
                      {totalSegments} total
                    </span>
                  )}
                </h2>
                <Pager page={page} totalPages={totalPages} onPage={setPage} compact />
              </div>

              {loadingSegments ? (
                <p className="text-gray-500 text-sm">Loading…</p>
              ) : segments.length === 0 ? (
                <p className="text-gray-600 text-sm">No segments found for the selected labels.</p>
              ) : (
                <div className="space-y-2">
                  {segments.map(seg => renderRow(seg))}
                </div>
              )}

              <Pager page={page} totalPages={totalPages} onPage={setPage} />
            </section>
          )}

        </>
      )}

      {tab === 'skips' && <SkipAnalytics />}

      {tab === 'accuracy' && (
        <CollapsibleSection id="analytics.accuracy" title="Detection accuracy" hint="false-positive rate per detector and label">
          <DetectionAccuracy />
        </CollapsibleSection>
      )}

      {tab === 'false-positives' && (
          <section>
            <div className="flex items-center justify-between mb-3">
              <p className="text-xs text-gray-500">
                Segments marked as not containing what they claim. They no longer skip or mute and are left out
                of counts and sync; undo a mark to make the segment act again.
                {!loadingFp && <span className="ml-2 text-gray-600">{fpTotal} total</span>}
              </p>
              <Pager page={fpPage} totalPages={Math.ceil(fpTotal / PAGE_SIZE)} onPage={setFpPage} compact />
            </div>
            {loadingFp ? (
              <p className="text-gray-500 text-sm">Loading…</p>
            ) : fpSegments.length === 0 ? (
              <p className="text-gray-600 text-sm">No false positives marked yet.</p>
            ) : (
              <div className="space-y-2">
                {fpSegments.map(seg => renderRow(seg))}
              </div>
            )}
            <Pager page={fpPage} totalPages={Math.ceil(fpTotal / PAGE_SIZE)} onPage={setFpPage} />
          </section>
      )}

      {/* ── Segment preview modal ─────────────────────────────────────────── */}
      {previewSeg && (
        <div
          className="fixed inset-0 z-50 bg-black/90 flex items-end sm:items-center justify-center sm:p-4"
          onClick={() => setPreviewSeg(null)}
        >
          <div
            className="w-full sm:max-w-4xl bg-plex-card sm:border border-t border-plex-border sm:rounded-xl overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-plex-border">
              <div>
                <h3 className="text-sm font-semibold text-gray-100 truncate">{previewSeg.title}</h3>
                <p className="text-xs text-gray-500">
                  {msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}
                </p>
              </div>
              <button
                onClick={() => setPreviewSeg(null)}
                className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
              >
                Close
              </button>
            </div>
            <div className="p-3 sm:p-4">
              <video
                key={previewSeg.id}
                controls
                playsInline
                className="w-full rounded-lg bg-black max-h-[50vh] sm:max-h-[70vh]"
                src={`/api/segments/${previewSeg.id}/preview`}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
