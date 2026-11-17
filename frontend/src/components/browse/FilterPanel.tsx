import { X } from 'lucide-react'
import { SegmentFilters, SegmentState, SEGMENT_STATE_LABELS, shortLabel } from './model'

export const STATUS_TABS = ['all', 'pending', 'scanning', 'done', 'failed'] as const
export type StatusTab = typeof STATUS_TABS[number]

/** Title-level filters. The name search sits in the toolbar; the rest open in the panel (#208). */
export interface TitleFilters {
  name: string
  actor: string
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
  name: '', actor: '', rating: 'all', status: 'all', yearFrom: '', yearTo: '', scanFrom: '', scanTo: '', showIgnored: false,
}

/** Filters that narrow which titles match. The ignored toggle widens instead, so it is not one. */
export function titleFiltersActive(f: TitleFilters): boolean {
  return !!(f.name || f.actor || f.rating !== 'all' || f.status !== 'all' || f.yearFrom || f.yearTo || f.scanFrom || f.scanTo)
}

const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const inputCls = 'w-full min-w-0 px-2 py-1.5 bg-plex-dark border border-plex-border rounded-lg text-sm text-gray-200 placeholder-muted focus:border-plex-orange/50'
const fieldLabel = 'block text-[11px] uppercase tracking-wide text-muted mb-1'

interface PanelProps {
  titles: TitleFilters
  onTitles: (patch: Partial<TitleFilters>) => void
  /** Matching titles per status at this level, for the status choice. */
  statusCounts: Record<string, number>
  ratings: string[]
  castSuggestions: string[]
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
  const t = p.titles
  const toggleLabel = (label: string) => {
    const next = new Set(p.segments.labels)
    next.has(label) ? next.delete(label) : next.add(label)
    p.onSegments({ ...p.segments, labels: next })
  }
  return (
    <div className="mb-3 rounded-xl border border-plex-border bg-plex-card/60 p-3 space-y-3" id="browse-filters">
      {p.showTitleSection && (
        <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1.6fr]">
          <div className="sm:col-span-2 xl:col-span-4">
            <span className={fieldLabel}>Scan status</span>
            <div className="flex flex-wrap p-0.5 gap-0.5 rounded-lg bg-black/25 border border-plex-border w-fit max-w-full">
              {STATUS_TABS.map(s => (
                <button
                  key={s}
                  onClick={() => p.onTitles({ status: s })}
                  aria-pressed={t.status === s}
                  className={`px-3 py-1 text-xs rounded-md transition-colors whitespace-nowrap ${t.status === s ? 'bg-plex-orange text-black font-semibold' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'}`}
                >
                  {s === 'all' ? 'All' : capital(s)}
                  <span className={`ml-1.5 tabular-nums ${t.status === s ? 'opacity-70' : 'text-muted'}`}>{p.statusCounts[s] ?? 0}</span>
                </button>
              ))}
            </div>
          </div>
          <label>
            <span className={fieldLabel}>Actor</span>
            <input type="text" list="cast-suggestions" placeholder="Any actor" aria-label="Filter by actor" value={t.actor} onChange={e => p.onTitles({ actor: e.target.value })} className={inputCls} />
            <datalist id="cast-suggestions">{p.castSuggestions.map(a => <option key={a} value={a} />)}</datalist>
          </label>
          <label>
            <span className={fieldLabel}>Rating</span>
            <select value={t.rating} aria-label="Filter by rating" onChange={e => p.onTitles({ rating: e.target.value })} className={inputCls}>
              <option value="all">All ratings</option>
              {p.ratings.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <div>
            <span className={fieldLabel}>Released</span>
            <div className="flex items-center gap-1.5">
              <input type="number" placeholder="From" aria-label="Released from year" value={t.yearFrom} onChange={e => p.onTitles({ yearFrom: e.target.value })} className={inputCls} />
              <span className="text-muted">–</span>
              <input type="number" placeholder="To" aria-label="Released to year" value={t.yearTo} onChange={e => p.onTitles({ yearTo: e.target.value })} className={inputCls} />
            </div>
          </div>
          <div>
            <span className={fieldLabel}>Scanned</span>
            <div className="flex items-center gap-1.5">
              <input type="date" aria-label="Scanned from" value={t.scanFrom} onChange={e => p.onTitles({ scanFrom: e.target.value })} className={inputCls} />
              <span className="text-muted">–</span>
              <input type="date" aria-label="Scanned to" value={t.scanTo} onChange={e => p.onTitles({ scanTo: e.target.value })} className={inputCls} />
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-300 sm:col-span-2 xl:col-span-4">
            <input type="checkbox" checked={t.showIgnored} onChange={e => p.onTitles({ showIgnored: e.target.checked })} className="w-4 h-4 accent-plex-orange" />
            Show ignored and out-of-scope titles
          </label>
        </div>
      )}

      {p.showSegmentSection && (
        <div className={`grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 ${p.showTitleSection ? 'pt-3 border-t border-plex-border' : ''}`}>
          <label>
            <span className={fieldLabel}>Segment state</span>
            <select value={p.segments.state} aria-label="Segment state" onChange={e => p.onSegments({ ...p.segments, state: e.target.value as SegmentState })} className={inputCls}>
              {(Object.keys(SEGMENT_STATE_LABELS) as SegmentState[]).map(s => <option key={s} value={s}>{SEGMENT_STATE_LABELS[s]}</option>)}
            </select>
          </label>
          <label>
            <span className={fieldLabel}>Found by</span>
            <select value={p.segments.origin} aria-label="Found by" onChange={e => p.onSegments({ ...p.segments, origin: e.target.value })} className={inputCls}>
              <option value="">Anything</option>
              {p.segmentOrigins.map(o => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
          {p.segmentLabels.length > 0 && (
            <div className="sm:col-span-2">
              <span className={fieldLabel}>Labels</span>
              <div className="flex flex-wrap gap-1.5">
                {p.segmentLabels.map(label => {
                  const on = p.segments.labels.has(label)
                  return (
                    <button
                      key={label}
                      onClick={() => toggleLabel(label)}
                      aria-pressed={on}
                      title={label}
                      className={`text-xs px-2 py-1 rounded-full border transition-colors ${on ? 'bg-plex-orange/20 text-plex-orange border-plex-orange/50' : 'text-gray-400 border-plex-border hover:text-gray-200'}`}
                    >
                      {shortLabel(label)}
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
  const chips: { key: string; text: string; clear: () => void }[] = []
  if (t.status !== 'all') chips.push({ key: 'status', text: `Status: ${capital(t.status)}`, clear: () => onTitles({ status: 'all' }) })
  if (t.actor) chips.push({ key: 'actor', text: `Actor: ${t.actor}`, clear: () => onTitles({ actor: '' }) })
  if (t.rating !== 'all') chips.push({ key: 'rating', text: `Rating: ${t.rating}`, clear: () => onTitles({ rating: 'all' }) })
  if (t.yearFrom || t.yearTo) chips.push({ key: 'year', text: `Released ${t.yearFrom || '…'} – ${t.yearTo || '…'}`, clear: () => onTitles({ yearFrom: '', yearTo: '' }) })
  if (t.scanFrom || t.scanTo) chips.push({ key: 'scan', text: `Scanned ${t.scanFrom || '…'} – ${t.scanTo || '…'}`, clear: () => onTitles({ scanFrom: '', scanTo: '' }) })
  if (t.showIgnored) chips.push({ key: 'ignored', text: 'Showing ignored', clear: () => onTitles({ showIgnored: false }) })
  if (showSegments) {
    if (s.state !== 'all') chips.push({ key: 'state', text: SEGMENT_STATE_LABELS[s.state], clear: () => onSegments({ ...s, state: 'all' }) })
    if (s.origin) chips.push({ key: 'origin', text: `Found by ${s.origin}`, clear: () => onSegments({ ...s, origin: '' }) })
    for (const label of s.labels) {
      chips.push({ key: `l:${label}`, text: shortLabel(label), clear: () => { const n = new Set(s.labels); n.delete(label); onSegments({ ...s, labels: n }) } })
    }
  }
  if (chips.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5 mb-3">
      {chips.map(c => (
        <span key={c.key} className="flex items-center gap-1 text-xs pl-2 pr-1 py-0.5 rounded-full bg-plex-orange/15 text-plex-orange border border-plex-orange/40">
          {c.text}
          <button onClick={c.clear} aria-label={`Remove filter ${c.text}`} className="p-0.5 rounded-full hover:bg-plex-orange/20"><X size={11} /></button>
        </span>
      ))}
      <button onClick={onClearAll} className="text-xs text-gray-400 hover:text-white px-1">Clear all</button>
    </div>
  )
}
