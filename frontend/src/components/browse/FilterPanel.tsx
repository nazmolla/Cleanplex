import { X } from 'lucide-react'
import { ActorPicker } from './ActorPicker'
import { SegmentFilters, SegmentState, SEGMENT_STATE_LABEL_KEYS, shortLabel } from './model'
import { useT } from '../../i18n'
import { originDisplayName } from '../../utils/segmentOrigin'

export const STATUS_TABS = ['all', 'pending', 'scanning', 'done', 'failed'] as const
export type StatusTab = typeof STATUS_TABS[number]

const STATUS_TAB_KEYS: Record<StatusTab, 'filter_status_all' | 'filter_status_pending' | 'filter_status_scanning' | 'filter_status_done' | 'filter_status_failed'> = {
  all: 'filter_status_all',
  pending: 'filter_status_pending',
  scanning: 'filter_status_scanning',
  done: 'filter_status_done',
  failed: 'filter_status_failed',
}

/** Title-level filters. The name search sits in the toolbar; the rest open in the panel (#208). */
export interface TitleFilters {
  name: string
  /** Exact actor names (#227); a title matches any of them, or all with actorMatch 'all'. */
  actors: string[]
  actorMatch: 'any' | 'all'
  /** 'all' or a content rating. */
  rating: string
  status: StatusTab
  yearFrom: string
  yearTo: string
  /** Inclusive YYYY-MM-DD bounds on the scan date. */
  scanFrom: string
  scanTo: string
  showIgnored: boolean
}

export const NO_TITLE_FILTERS: TitleFilters = {
  name: '', actors: [], actorMatch: 'any', rating: 'all', status: 'all', yearFrom: '', yearTo: '', scanFrom: '', scanTo: '', showIgnored: false,
}

/** Filters that narrow which titles match. The ignored toggle widens instead, so it is not one. */
export function titleFiltersActive(f: TitleFilters): boolean {
  return !!(f.name || f.actors.length || f.rating !== 'all' || f.status !== 'all' || f.yearFrom || f.yearTo || f.scanFrom || f.scanTo)
}

const inputCls = 'w-full min-w-0 px-2 py-1.5 bg-hayaa-dark border border-hayaa-border rounded-lg text-sm text-gray-200 placeholder-muted focus:border-hayaa-gold/50'
const fieldLabel = 'block text-[11px] uppercase tracking-wide text-muted mb-1'

/** How the actor filter's suggestions are ordered (#227). */
export type CastSort = 'titles' | 'name' | 'segments'
export interface CastSuggestion { name: string; titles: number; segments: number }

/** Every actor of the given titles with their title count and segment total, ordered by title
 *  count, name, or segment total (actors whose titles have no segments drop out of that order). */
export function castSuggestionsFor(titles: { cast?: string[]; segment_count: number }[], sort: CastSort): CastSuggestion[] {
  const n = new Map<string, CastSuggestion>()
  for (const t of titles) for (const a of t.cast ?? []) {
    const c = n.get(a) ?? { name: a, titles: 0, segments: 0 }
    c.titles += 1
    c.segments += t.segment_count
    n.set(a, c)
  }
  const all = [...n.values()]
  const byName = (a: CastSuggestion, b: CastSuggestion) => a.name.localeCompare(b.name)
  return sort === 'segments'
    ? all.filter(c => c.segments > 0).sort((a, b) => b.segments - a.segments || byName(a, b))
    : all.sort(sort === 'name' ? byName : (a, b) => b.titles - a.titles || byName(a, b))
}

interface PanelProps {
  titles: TitleFilters
  onTitles: (patch: Partial<TitleFilters>) => void
  /** Matching titles per status at this level, for the status choice. */
  statusCounts: Record<string, number>
  ratings: string[]
  castSuggestions: CastSuggestion[]
  castSort: CastSort
  onCastSort: (sort: CastSort) => void
  actorPhotos: Record<string, string>
  /** False on a single title's page: title filters have nothing to narrow there. */
  showTitleSection: boolean
  segments: SegmentFilters
  onSegments: (next: SegmentFilters) => void
  segmentLabels: string[]
  segmentOrigins: string[]
  /** Only in the Segments view. */
  showSegmentSection: boolean
}

/** The filters behind the toolbar's Filters button, inline below the toolbar (not an overlay). */
export function FilterPanel(p: PanelProps) {
  const { t: tr } = useT()
  const t = p.titles
  const toggleLabel = (label: string) => {
    const next = new Set(p.segments.labels)
    next.has(label) ? next.delete(label) : next.add(label)
    p.onSegments({ ...p.segments, labels: next })
  }
  return (
    <div className="mb-3 rounded-xl border border-hayaa-border bg-hayaa-card/60 p-3 space-y-3" id="browse-filters">
      {p.showTitleSection && (
        <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1.6fr]">
          <div className="sm:col-span-2 xl:col-span-4">
            <span className={fieldLabel}>{tr('filter_scan_status')}</span>
            <div className="flex flex-wrap p-0.5 gap-0.5 rounded-lg bg-black/25 border border-hayaa-border w-fit max-w-full">
              {STATUS_TABS.map(s => (
                <button
                  key={s}
                  onClick={() => p.onTitles({ status: s })}
                  aria-pressed={t.status === s}
                  className={`px-3 py-1 text-xs rounded-md transition-colors whitespace-nowrap ${t.status === s ? 'bg-hayaa-gold text-black font-semibold' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'}`}
                >
                  {tr(STATUS_TAB_KEYS[s])}
                  <span className={`ms-1.5 tabular-nums ${t.status === s ? 'opacity-70' : 'text-muted'}`}>{p.statusCounts[s] ?? 0}</span>
                </button>
              ))}
            </div>
          </div>
          <div>
            <span className={fieldLabel}>{tr('filter_actor')}</span>
            <ActorPicker suggestions={p.castSuggestions} sort={p.castSort} onSort={p.onCastSort}
              selected={t.actors} onChange={actors => p.onTitles({ actors })}
              match={t.actorMatch} onMatch={actorMatch => p.onTitles({ actorMatch })} photos={p.actorPhotos} />
          </div>
          <label>
            <span className={fieldLabel}>{tr('filter_rating')}</span>
            <select value={t.rating} aria-label={tr('filter_rating_aria')} onChange={e => p.onTitles({ rating: e.target.value })} className={inputCls}>
              <option value="all">{tr('filter_all_ratings')}</option>
              {p.ratings.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <div>
            <span className={fieldLabel}>{tr('filter_released')}</span>
            <div className="flex items-center gap-1.5">
              <input type="number" placeholder={tr('filter_from')} aria-label={tr('filter_released_from_aria')} value={t.yearFrom} onChange={e => p.onTitles({ yearFrom: e.target.value })} className={inputCls} />
              <span className="text-muted">–</span>
              <input type="number" placeholder={tr('filter_to')} aria-label={tr('filter_released_to_aria')} value={t.yearTo} onChange={e => p.onTitles({ yearTo: e.target.value })} className={inputCls} />
            </div>
          </div>
          <div>
            <span className={fieldLabel}>{tr('filter_scanned')}</span>
            <div className="flex items-center gap-1.5">
              <input type="date" aria-label={tr('filter_scanned_from_aria')} value={t.scanFrom} onChange={e => p.onTitles({ scanFrom: e.target.value })} className={inputCls} />
              <span className="text-muted">–</span>
              <input type="date" aria-label={tr('filter_scanned_to_aria')} value={t.scanTo} onChange={e => p.onTitles({ scanTo: e.target.value })} className={inputCls} />
              {/* Titles not scanned yet have no scan date, so any range hides pending and scanning ones (#217). */}
              {(t.scanFrom || t.scanTo) && (
                <button type="button" onClick={() => p.onTitles({ scanFrom: '', scanTo: '' })} className="btn-outline text-xs px-2 py-1 flex-shrink-0">{tr('common_clear')}</button>
              )}
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-300 sm:col-span-2 xl:col-span-4">
            <input type="checkbox" checked={t.showIgnored} onChange={e => p.onTitles({ showIgnored: e.target.checked })} className="w-4 h-4 accent-hayaa-gold" />
            {tr('filter_show_ignored')}
          </label>
          {titleFiltersActive(t) || t.showIgnored ? (
            <div className="sm:col-span-2 xl:col-span-4">
              <button type="button" onClick={() => p.onTitles({ ...NO_TITLE_FILTERS, name: t.name })} className="btn-outline text-xs">{tr('filter_reset')}</button>
            </div>
          ) : null}
        </div>
      )}

      {p.showSegmentSection && (
        <div className={`grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 ${p.showTitleSection ? 'pt-3 border-t border-hayaa-border' : ''}`}>
          <label>
            <span className={fieldLabel}>{tr('filter_segment_state')}</span>
            <select value={p.segments.state} aria-label={tr('filter_segment_state')} onChange={e => p.onSegments({ ...p.segments, state: e.target.value as SegmentState })} className={inputCls}>
              {(Object.keys(SEGMENT_STATE_LABEL_KEYS) as SegmentState[]).map(s => <option key={s} value={s}>{tr(SEGMENT_STATE_LABEL_KEYS[s])}</option>)}
            </select>
          </label>
          <label>
            <span className={fieldLabel}>{tr('filter_found_by')}</span>
            <select value={p.segments.origin} aria-label={tr('filter_found_by')} onChange={e => p.onSegments({ ...p.segments, origin: e.target.value })} className={inputCls}>
              <option value="">{tr('filter_anything')}</option>
              {p.segmentOrigins.map(o => <option key={o} value={o}>{originDisplayName(o, tr)}</option>)}
            </select>
          </label>
          {p.segmentLabels.length > 0 && (
            <div className="sm:col-span-2">
              <span className={fieldLabel}>{tr('filter_labels')}</span>
              <div className="flex flex-wrap gap-1.5">
                {p.segmentLabels.map(label => {
                  const on = p.segments.labels.has(label)
                  return (
                    <button
                      key={label}
                      onClick={() => toggleLabel(label)}
                      aria-pressed={on}
                      title={label}
                      className={`text-xs px-2 py-1 rounded-full border transition-colors ${on ? 'bg-hayaa-gold/20 text-hayaa-gold border-hayaa-gold/50' : 'text-gray-400 border-hayaa-border hover:text-gray-200'}`}
                    >
                      {shortLabel(label, tr)}
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface ChipsProps {
  titles: TitleFilters
  onTitles: (patch: Partial<TitleFilters>) => void
  segments: SegmentFilters
  onSegments: (next: SegmentFilters) => void
  showSegments: boolean
  onClearAll: () => void
}

/** The active filters as removable chips, so a closed panel still says what is narrowing the list. */
export function FilterChips({ titles: t, onTitles, segments: s, onSegments, showSegments, onClearAll }: ChipsProps) {
  const { t: tr } = useT()
  const chips: { key: string; text: string; clear: () => void }[] = []
  if (t.status !== 'all') chips.push({ key: 'status', text: tr('filter_chip_status', { status: tr(STATUS_TAB_KEYS[t.status]) }), clear: () => onTitles({ status: 'all' }) })
  for (const a of t.actors) {
    chips.push({ key: `a:${a}`, text: tr('filter_chip_actor', { name: a }), clear: () => onTitles({ actors: t.actors.filter(x => x !== a) }) })
  }
  if (t.rating !== 'all') chips.push({ key: 'rating', text: tr('filter_chip_rating', { rating: t.rating }), clear: () => onTitles({ rating: 'all' }) })
  if (t.yearFrom || t.yearTo) chips.push({ key: 'year', text: tr('filter_chip_released', { from: t.yearFrom || '…', to: t.yearTo || '…' }), clear: () => onTitles({ yearFrom: '', yearTo: '' }) })
  if (t.scanFrom || t.scanTo) chips.push({ key: 'scan', text: tr('filter_chip_scanned', { from: t.scanFrom || '…', to: t.scanTo || '…' }), clear: () => onTitles({ scanFrom: '', scanTo: '' }) })
  if (t.showIgnored) chips.push({ key: 'ignored', text: tr('filter_chip_ignored'), clear: () => onTitles({ showIgnored: false }) })
  if (showSegments) {
    if (s.state !== 'all') chips.push({ key: 'state', text: tr(SEGMENT_STATE_LABEL_KEYS[s.state]), clear: () => onSegments({ ...s, state: 'all' }) })
    if (s.origin) chips.push({ key: 'origin', text: tr('filter_chip_origin', { origin: originDisplayName(s.origin, tr) }), clear: () => onSegments({ ...s, origin: '' }) })
    for (const label of s.labels) {
      chips.push({ key: `l:${label}`, text: shortLabel(label, tr), clear: () => { const n = new Set(s.labels); n.delete(label); onSegments({ ...s, labels: n }) } })
    }
  }
  if (chips.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5 mb-3">
      {chips.map(c => (
        <span key={c.key} className="flex items-center gap-1 text-xs ps-2 pe-1 py-0.5 rounded-full bg-hayaa-gold/15 text-hayaa-gold border border-hayaa-gold/40">
          {c.text}
          <button onClick={c.clear} aria-label={tr('filter_remove_aria', { text: c.text })} className="p-0.5 rounded-full hover:bg-hayaa-gold/20"><X size={11} /></button>
        </span>
      ))}
      <button onClick={onClearAll} className="text-xs text-gray-400 hover:text-white px-1">{tr('common_clear_all')}</button>
    </div>
  )
}
