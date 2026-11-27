import { useEffect, useState, useCallback, useRef } from 'react'
import { parseDbTimestamp } from '../utils/dbTime'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import SegmentOriginBadge from '../components/SegmentOriginBadge'
import FalsePositiveButton, { FalsePositiveTag } from '../components/FalsePositiveButton'
import RelabelButton from '../components/RelabelButton'
import ThumbnailTime from '../components/ThumbnailTime'
import SkipAnalytics from '../components/SkipAnalytics'
import DetectionAccuracy from '../components/DetectionAccuracy'
import { X, AlertTriangle, Trash2, Play, SkipForward, ChevronLeft, ChevronRight, LayoutGrid, List } from 'lucide-react'
import HoverPreview from '../components/HoverPreview'
import SegmentPlayer from '../components/SegmentPlayer'
import Modal from '../components/Modal'
import { toast } from '../components/Toast'
import CollapsibleSection from '../components/CollapsibleSection'
import SegmentCard from '../components/browse/SegmentCard'
import type { Segment as CardSegment } from '../components/browse/model'
import { useT, type TranslationKey } from '../i18n'
import { shortLabel } from '../utils/labels'

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
  /** Film time the thumbnail frame was taken at; null for segments scanned before it was recorded. */
  thumbnail_ms?: number | null
  /** Short silent loop played on hover; empty until rendered (#114). */
  hover_url?: string
  poster_url: string
  content_rating: string
  media_type: string
  rating_key: string
  source?: string
  detector?: string
  false_positive?: boolean
  /** Inactive because the verifier rejected it (not a mark by you); restore it if it was real. */
  auto_rejected?: boolean
  /** When it was marked (UTC, SQLite format); False positives tab only. */
  marked_at?: string | null
}

const PAGE_SIZE = 50

// Tabs, selected by ?tab= so each one can be linked like a sub-page (#117).
const TABS = [
  { id: 'detections', labelKey: 'analytics_tab_detections' },
  { id: 'skips', labelKey: 'analytics_tab_skips' },
  { id: 'accuracy', labelKey: 'analytics_tab_accuracy' },
  { id: 'false-positives', labelKey: 'analytics_tab_fps' },
] as const
type TabId = typeof TABS[number]['id']

function formatMarkedAt(v: string, formatDate: (d: Date) => string): string {
  const d = parseDbTimestamp(v)
  return isNaN(d.getTime()) ? v : formatDate(d)
}

function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

// Pure-CSS horizontal bar chart
function BarChart({
  data,
  maxVal,
  selectedBar,
  onBarClick,
  colorClass = 'bg-hayaa-gold',
}: {
  data: { key: string; count: number; label?: string }[]
  maxVal: number
  selectedBar?: string | null
  onBarClick?: (key: string) => void
  colorClass?: string
}) {
  const { t } = useT()
  if (!data.length) return <p className="text-muted text-sm">{t('analytics_no_data')}</p>

  return (
    <div className="space-y-2">
      {data.map(({ key, count, label }) => {
        const pct = maxVal > 0 ? (count / maxVal) * 100 : 0
        const isSelected = selectedBar === key
        const displayLabel = label ?? shortLabel(key, t)
        // A clickable row is a real button so it can be reached and pressed from the keyboard.
        const Row = onBarClick ? 'button' : 'div'
        return (
          <Row
            key={key}
            {...(onBarClick ? {
              type: 'button' as const,
              'aria-pressed': isSelected,
              'aria-label': t('analytics_bar_aria', { label: displayLabel, count }),
              onClick: () => onBarClick(key),
            } : {})}
            className={`w-full text-start flex items-center gap-3 rounded-lg px-3 py-1.5 transition-colors ${
              onBarClick ? 'hover:bg-white/5' : ''
            } ${isSelected ? 'bg-hayaa-gold/10 ring-1 ring-hayaa-gold/30' : ''}`}
          >
            {/* The label column gives way on a narrow screen so the bar keeps some width. */}
            <div className="w-24 sm:w-44 flex-shrink-0 text-xs text-gray-300 truncate" title={label ?? key}>
              {displayLabel}
            </div>
            <div className="flex-1 h-2.5 bg-hayaa-border rounded-full overflow-hidden">
              <div
                className={`h-full ${colorClass} rounded-full transition-all duration-300`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="w-10 text-end text-xs text-gray-400 flex-shrink-0 tabular-nums">{count}</div>
          </Row>
        )
      })}
    </div>
  )
}

type SortKey = 'score' | 'added' | 'marked'
interface SortSpec { sort: SortKey; order: 'asc' | 'desc' }

const SORT_LABELS: Record<SortKey, TranslationKey> = {
  score: 'analytics_sort_score',
  added: 'analytics_sort_added',
  marked: 'analytics_sort_marked',
}

// Sort picker for a segment list: the key, plus a button flipping ascending/descending.
type Layout = 'list' | 'gallery'

/** List or gallery (large frames, #208) for the segment lists; the choice is remembered per browser. */
function LayoutToggle({ value, onChange }: { value: Layout; onChange: (next: Layout) => void }) {
  const { t } = useT()
  const btn = (id: Layout, label: string, Icon: typeof List) => (
    <button
      type="button"
      onClick={() => onChange(id)}
      aria-pressed={value === id}
      title={label}
      aria-label={label}
      className={`p-1.5 rounded-md transition-colors ${value === id ? 'bg-hayaa-gold text-black' : 'text-gray-400 hover:text-gray-100'}`}
    >
      <Icon size={14} />
    </button>
  )
  return (
    <div className="flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-hayaa-border">
      {btn('list', t('analytics_layout_list'), List)}
      {btn('gallery', t('analytics_layout_gallery'), LayoutGrid)}
    </div>
  )
}

function SortControl({ value, options, onChange }: {
  value: SortSpec
  options: SortKey[]
  onChange: (next: SortSpec) => void
}) {
  const { t } = useT()
  return (
    <div className="flex items-center gap-1.5 text-xs text-gray-400">
      <label htmlFor="segment-sort" className="sr-only">{t('analytics_sort_by')}</label>
      <select
        id="segment-sort"
        value={value.sort}
        onChange={e => onChange({ ...value, sort: e.target.value as SortKey })}
        className="bg-hayaa-card border border-hayaa-border rounded px-2 py-1 text-gray-200"
      >
        {options.map(k => <option key={k} value={k}>{t(SORT_LABELS[k])}</option>)}
      </select>
      <button
        type="button"
        onClick={() => onChange({ ...value, order: value.order === 'desc' ? 'asc' : 'desc' })}
        aria-label={value.order === 'desc' ? t('analytics_sort_desc') : t('analytics_sort_asc')}
        title={value.order === 'desc' ? t('analytics_sort_desc_title') : t('analytics_sort_asc_title')}
        className="px-2 py-1 rounded border border-hayaa-border text-gray-200 hover:bg-hayaa-card"
      >
        {value.order === 'desc' ? '↓' : '↑'}
      </button>
    </div>
  )
}

function Pager({ page, totalPages, onPage, compact = false }: {
  page: number
  totalPages: number
  onPage: (p: number) => void
  compact?: boolean
}) {
  const { t } = useT()
  if (totalPages <= 1) return null
  const btn = compact
    ? 'p-1 rounded hover:bg-white/5 disabled:opacity-40'
    : 'flex items-center gap-1 px-3 py-1.5 bg-hayaa-card border border-hayaa-border rounded-lg hover:text-white hover:border-gray-500 disabled:opacity-40 transition-colors'
  return (
    <div className={`flex items-center text-xs text-gray-400 ${compact ? "gap-2" : "gap-3 justify-center mt-4"}`}>
      <button onClick={() => onPage(Math.max(0, page - 1))} disabled={page === 0} className={btn} aria-label={t('analytics_prev_aria')}>
        <ChevronLeft size={compact ? 14 : 13} className="rtl:-scale-x-100" />{!compact && <> {t('analytics_prev')}</>}
      </button>
      <span>{page + 1} / {totalPages}</span>
      <button onClick={() => onPage(Math.min(totalPages - 1, page + 1))} disabled={page >= totalPages - 1} className={btn} aria-label={t('analytics_next_aria')}>
        {!compact && <>{t('analytics_next')} </>}<ChevronRight size={compact ? 14 : 13} className="rtl:-scale-x-100" />
      </button>
    </div>
  )
}

export default function Analytics() {
  const { t, formatDate } = useT()
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
  const [segSort, setSegSort] = useState<SortSpec>({ sort: 'score', order: 'desc' })

  // Per-segment state
  const [deletingSegs, setDeletingSegs] = useState<Record<number, boolean>>({})
  const [jumpingSegs, setJumpingSegs] = useState<Record<number, boolean>>({})
  const [previewSeg, setPreviewSeg] = useState<Segment | null>(null)
  const [layout, setLayoutState] = useState<Layout>(() => {
    // Storage can be unavailable (#117); the list is the default then.
    try { return localStorage.getItem('analytics.segmentLayout') === 'gallery' ? 'gallery' : 'list' } catch { return 'list' }
  })
  const setLayout = (next: Layout) => {
    setLayoutState(next)
    try { localStorage.setItem('analytics.segmentLayout', next) } catch { /* not remembered */ }
  }

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
  const fetchSegments = useCallback(async (labels: string[], p: number, spec: SortSpec, silent = false) => {
    segmentsCtl.current?.abort()
    if (!labels.length) { setSegments([]); setTotalSegments(0); return }
    const controller = (segmentsCtl.current = new AbortController())
    if (!silent) setLoadingSegments(true)
    try {
      const d = await api.get<{ segments: Segment[]; total: number }>(
        `/api/analytics/segments?labels=${encodeURIComponent(labels.join(','))}&limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}&sort=${spec.sort}&order=${spec.order}`,
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
  // Marked by you, or rejected by the verifier (#165); counts are per kind whatever is shown.
  const [fpKind, setFpKind] = useState<'all' | 'marked' | 'verifier'>('all')
  const [fpCounts, setFpCounts] = useState({ marked: 0, verifier: 0 })
  const [fpPage, setFpPage] = useState(0)
  const [fpSort, setFpSort] = useState<SortSpec>({ sort: 'marked', order: 'desc' })
  const [loadingFp, setLoadingFp] = useState(false)
  const fpCtl = useRef<AbortController | null>(null)
  const fetchFalsePositives = useCallback(async (p: number, spec: SortSpec, silent = false, kind = 'all') => {
    fpCtl.current?.abort()
    const controller = (fpCtl.current = new AbortController())
    if (!silent) setLoadingFp(true)
    try {
      const d = await api.get<{ segments: Segment[]; total: number; counts?: { marked: number; verifier: number } }>(
        `/api/analytics/false-positives?limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}&sort=${spec.sort}&order=${spec.order}&kind=${kind}`,
        { signal: controller.signal },
      )
      setFpSegments(d.segments)
      setFpTotal(d.total)
      if (d.counts) setFpCounts(d.counts)
    } catch (err: any) {
      if (err?.name !== 'AbortError') console.warn('Could not load false positives:', err?.message)
    } finally {
      if (fpCtl.current === controller) setLoadingFp(false)
    }
  }, [])
  // Loads when the tab opens and on page or sort changes; nothing is fetched for other tabs.
  useEffect(() => {
    if (tab === 'false-positives') fetchFalsePositives(fpPage, fpSort, false, fpKind)
  }, [tab, fpPage, fpSort, fpKind, fetchFalsePositives])
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
      fetchSegments(selectedLabels, 0, segSort)
    } else {
      // Only the page or sort changed — fetch the requested page.
      fetchSegments(selectedLabels, page, segSort)
    }
  }, [page, selectedLabels, segSort, fetchSegments])

  // Live (#89): scans, imports and deletes change label counts and the segment
  // list. Debounced generously: a scan writes segments in bursts.
  const liveView = useRef({ selectedLabels, page, tab, fpPage, segSort, fpSort, fpKind })
  liveView.current = { selectedLabels, page, tab, fpPage, segSort, fpSort, fpKind }
  useLiveRefresh(['segments_changed'], () => {
    const v = liveView.current
    loadLabels()
    fetchSegments(v.selectedLabels, v.page, v.segSort, true)
    if (v.tab === 'false-positives') fetchFalsePositives(v.fpPage, v.fpSort, true, v.fpKind)
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
  // delete; the mark can be undone from Browse or the False positives tab.
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
        toast.success(t('analytics_jump_success', { time: msToTimecode(d.seek_to_ms), client: d.client, user: d.user }))
      }
    } catch (err: any) {
      toast.error(err?.message || t('analytics_jump_error'))
    } finally {
      setJumpingSegs(j => ({ ...j, [seg.id]: false }))
    }
  }

  const onCardChange = (seg: Segment, next: CardSegment) => {
    if ((next.labels ?? '') !== (seg.labels ?? '')) relabelInPlace(seg.id, next.labels ?? '')
    if (!!next.false_positive !== !!seg.false_positive) {
      // Marking drops it from the detections list; undoing drops it from the false-positives list.
      if (seg.false_positive) dropFromFalsePositives(seg)
      else dropFromList(seg)
    }
  }
  const onCardDeleted = (seg: Segment) => {
    if (seg.false_positive) dropFromFalsePositives(seg)
    else dropFromList(seg)
  }
  const renderGallery = (list: Segment[]) => (
    <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(300px,1fr))]">
      {list.map(seg => (
        <SegmentCard
          key={seg.id}
          seg={{ ...seg, created_at: '' } as CardSegment}
          caption={
            <span title={seg.title}>
              {seg.title}
              {seg.marked_at && <span className="ms-2 text-xs text-amber-300/80">{t('analytics_marked_at', { date: formatMarkedAt(seg.marked_at, formatDate) })}</span>}
            </span>
          }
          onPreview={() => setPreviewSeg(seg)}
          onChange={next => onCardChange(seg, next)}
          onDeleted={() => onCardDeleted(seg)}
        />
      ))}
    </div>
  )

  const renderRow = (seg: Segment) => (
                <div
                  key={seg.id}
                  className="bg-hayaa-card border border-hayaa-border rounded-xl overflow-hidden flex flex-col sm:flex-row"
                >
                  {/* Poster + thumbnail side-by-side on mobile */}
                  <div className="flex sm:flex-col">
                    {/* Poster */}
                    {seg.poster_url ? (
                      <img
                        src={seg.poster_url}
                        alt=""
                        loading="lazy"
                        className="w-12 sm:w-20 object-cover bg-hayaa-border flex-shrink-0"
                        style={{ minHeight: '48px' }}
                        onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                      />
                    ) : (
                      <div className="w-12 sm:w-20 bg-hayaa-border/50 flex-shrink-0" />
                    )}
                  </div>
                  {/* Thumbnail */}
                  <HoverPreview src={seg.hover_url} className="w-full sm:w-36 flex-shrink-0 bg-black">
                    {seg.has_thumbnail ? (
                      <img
                        src={seg.thumbnail_url}
                        alt={t('analytics_flagged_frame')}
                        loading="lazy"
                        className="w-full object-cover"
                        style={{ height: '100px' }}
                      />
                    ) : (
                      <div className="w-full flex items-center justify-center text-muted" style={{ height: '100px' }}>
                        <AlertTriangle size={18} />
                      </div>
                    )}
                    <div className="absolute bottom-0.5 start-0.5 bg-black/70 text-[10px] text-gray-300 px-1 py-0.5 rounded">
                      {Math.round(seg.confidence * 100)}%
                    </div>
                    <ThumbnailTime ms={seg.thumbnail_ms} />
                  </HoverPreview>

                  {/* Info + actions */}
                  <div className="flex-1 p-3 flex flex-col justify-between min-w-0">
                    <div>
                      <p className="text-sm text-gray-100 truncate font-medium" title={seg.title}>
                        {seg.title}
                      </p>
                      <div className="flex flex-wrap items-center gap-2 mt-1">
                        <span className="font-mono text-xs text-hayaa-gold" dir="ltr">
                          {msToTimecode(seg.start_ms)} → {msToTimecode(seg.end_ms)}
                        </span>
                        <span className="text-xs text-muted">
                          ({t('common_seconds_short', { count: Math.round((seg.end_ms - seg.start_ms) / 1000) })})
                        </span>
                        {seg.false_positive && <FalsePositiveTag autoRejected={seg.auto_rejected} />}
                        {seg.content_rating && (
                          <span className="text-xs text-gray-400 bg-white/5 px-1.5 py-0.5 rounded">
                            {seg.content_rating}
                          </span>
                        )}
                        <SegmentOriginBadge seg={seg} />
                        {seg.marked_at && (
                          <span className="text-xs text-amber-300/80">{t('analytics_marked_at', { date: formatMarkedAt(seg.marked_at, formatDate) })}</span>
                        )}
                      </div>
                      {/* Label chips */}
                      {seg.labels && (
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {seg.labels.split(',').filter(l => l.trim()).map(l => {
                            const label = l.trim()
                            const isActive = selectedLabels.includes(label)
                            return (
                              <span
                                key={label}
                                className={`text-[10px] px-1.5 py-0.5 rounded border ${
                                  isActive
                                    ? 'bg-hayaa-gold/20 border-hayaa-gold/40 text-hayaa-gold'
                                    : 'bg-red-500/10 border-red-500/20 text-red-300'
                                }`}
                              >
                                {shortLabel(label, t)}
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-1 mt-2">
                      <button
                        onClick={() => setPreviewSeg(seg)}
                        title={t('analytics_preview_seg')}
                        className="p-1.5 text-gray-400 hover:text-green-400 hover:bg-green-400/10 rounded transition-colors"
                      >
                        <Play size={14} />
                      </button>
                      <button
                        onClick={() => jumpSeg(seg)}
                        disabled={jumpingSegs[seg.id]}
                        title={t('analytics_jump_seg')}
                        className="p-1.5 text-gray-400 hover:text-hayaa-gold hover:bg-hayaa-gold/10 rounded transition-colors disabled:opacity-40"
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
                        title={t('analytics_delete_seg')}
                        className="p-1.5 text-gray-400 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors disabled:opacity-40"
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
      <h1 className="text-2xl font-bold text-gray-100">{t('analytics_title')}</h1>

      {/* Tabs (#117) */}
      <div role="tablist" aria-label={t('analytics_tabs_aria')} className="flex gap-1 border-b border-hayaa-border overflow-x-auto">
        {TABS.map(tabItem => (
          <button
            key={tabItem.id}
            role="tab"
            aria-selected={tab === tabItem.id}
            onClick={() => selectTab(tabItem.id)}
            className={`px-4 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${
              tab === tabItem.id
                ? 'border-hayaa-gold text-hayaa-gold font-medium'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {t(tabItem.labelKey)}
          </button>
        ))}
      </div>

      {tab === 'detections' && (
        <>
          <CollapsibleSection id="analytics.labels" title={t('analytics_sec_labels')} hint={t('analytics_sec_labels_hint')}>
            {loadingLabels ? (
              <p className="text-muted text-sm">{t('common_loading')}</p>
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
            <section className="bg-hayaa-card border border-hayaa-gold/30 rounded-xl p-4">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-gray-300">
                  <span className="text-hayaa-gold">{shortLabel(drillLabel, t)}</span>
                  <span className="text-muted font-normal ms-2">{t('analytics_by_rating')}</span>
                </h2>
                <button
                  onClick={() => setDrillLabel(null)}
                  aria-label={t('common_close')}
                  className="p-1 text-gray-400 hover:text-gray-300 rounded transition-colors"
                >
                  <X size={14} />
                </button>
              </div>
              {loadingRatings ? (
                <p className="text-muted text-sm">{t('common_loading')}</p>
              ) : (
                <BarChart
                  data={ratingCounts.map(rc => ({ key: rc.content_rating, count: rc.count, label: rc.content_rating || t('analytics_unrated') }))}
                  maxVal={maxRatingCount}
                  colorClass="bg-blue-500"
                />
              )}
            </section>
          )}


          <CollapsibleSection id="analytics.filter" title={t('analytics_sec_filter')}>
            <div className="flex flex-wrap gap-2">
              {labelCounts.map(lc => {
                const active = selectedLabels.includes(lc.label)
                return (
                  <button
                    key={lc.label}
                    onClick={() => toggleLabel(lc.label)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors border ${
                      active
                        ? 'bg-hayaa-gold/20 border-hayaa-gold/50 text-hayaa-gold'
                        : 'bg-hayaa-darker border-hayaa-border text-gray-400 hover:text-gray-200 hover:border-gray-500'
                    }`}
                  >
                    {shortLabel(lc.label, t)}
                    <span className={`tabular-nums ${active ? 'text-hayaa-gold/70' : 'text-muted'}`}>
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
                className="mt-2 text-xs text-gray-400 hover:text-gray-300 transition-colors"
              >
                {t('common_clear_all')}
              </button>
            )}
          </CollapsibleSection>

          {/* ── Segment list for selected labels ─────────────────────────────── */}
          {selectedLabels.length > 0 && (
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
                  {t('analytics_segments_heading')}
                  {!loadingSegments && (
                    <span className="ms-2 text-muted normal-case font-normal">
                      {t('analytics_total_count', { count: totalSegments })}
                    </span>
                  )}
                </h2>
                <div className="flex items-center gap-3">
                  <LayoutToggle value={layout} onChange={setLayout} />
                  <SortControl value={segSort} options={['score', 'added']} onChange={next => { setSegSort(next); setPage(0) }} />
                  <Pager page={page} totalPages={totalPages} onPage={setPage} compact />
                </div>
              </div>

              {loadingSegments ? (
                <p className="text-muted text-sm">{t('common_loading')}</p>
              ) : segments.length === 0 ? (
                <p className="text-muted text-sm">{t('analytics_no_segs_for_labels')}</p>
              ) : (
                layout === 'gallery' ? renderGallery(segments) : (
                  <div className="space-y-2">
                    {segments.map(seg => renderRow(seg))}
                  </div>
                )
              )}

              <Pager page={page} totalPages={totalPages} onPage={setPage} />
            </section>
          )}

        </>
      )}

      {tab === 'skips' && <SkipAnalytics />}

      {tab === 'accuracy' && (
        <CollapsibleSection id="analytics.accuracy" title={t('analytics_sec_accuracy')} hint={t('analytics_sec_accuracy_hint')}>
          <DetectionAccuracy />
        </CollapsibleSection>
      )}

      {tab === 'false-positives' && (
          <section>
            <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
              <p className="text-xs text-muted min-w-0 flex-1 basis-64">
                {t('analytics_fp_desc')}
                {!loadingFp && <span className="ms-2 text-muted">{t('analytics_total_count', { count: fpTotal })}</span>}
              </p>
              <div className="flex items-center gap-3 shrink-0">
                <select
                  value={fpKind}
                  onChange={e => { setFpKind(e.target.value as typeof fpKind); setFpPage(0) }}
                  aria-label={t('analytics_fp_show_aria')}
                  className="bg-hayaa-card border border-hayaa-border rounded-lg px-2 py-1.5 text-xs text-gray-300"
                >
                  <option value="all">{t('analytics_fp_all', { count: fpCounts.marked + fpCounts.verifier })}</option>
                  <option value="marked">{t('analytics_fp_marked', { count: fpCounts.marked })}</option>
                  <option value="verifier">{t('analytics_fp_verifier', { count: fpCounts.verifier })}</option>
                </select>
                <LayoutToggle value={layout} onChange={setLayout} />
                <SortControl value={fpSort} options={['marked', 'score', 'added']} onChange={next => { setFpSort(next); setFpPage(0) }} />
                <Pager page={fpPage} totalPages={Math.ceil(fpTotal / PAGE_SIZE)} onPage={setFpPage} compact />
              </div>
            </div>
            {loadingFp ? (
              <p className="text-muted text-sm">{t('common_loading')}</p>
            ) : fpSegments.length === 0 ? (
              <p className="text-muted text-sm">{t('analytics_fp_empty')}</p>
            ) : (
              layout === 'gallery' ? renderGallery(fpSegments) : (
                <div className="space-y-2">
                  {fpSegments.map(seg => renderRow(seg))}
                </div>
              )
            )}
            <Pager page={fpPage} totalPages={Math.ceil(fpTotal / PAGE_SIZE)} onPage={setFpPage} />
          </section>
      )}

      {/* ── Segment preview modal ─────────────────────────────────────────── */}
      {previewSeg && (
        <Modal
          title={previewSeg.title}
          subtitle={<span dir="ltr">{msToTimecode(previewSeg.start_ms)} → {msToTimecode(previewSeg.end_ms)}</span>}
          size="xl"
          onClose={() => setPreviewSeg(null)}
        >
          <SegmentPlayer segmentId={previewSeg.id} className="max-h-[70vh]" />
        </Modal>
      )}
    </div>
  )
}
