import { ChevronRight, Film, Tv, ThumbsDown } from 'lucide-react'
import { Title } from './model'
import { parseDbTimestamp } from '../../utils/dbTime'
import SafeImg from './SafeImg'

export function StatusBadge({ status, progress }: { status: string; progress: number }) {
  switch (status) {
    case 'done':
      return <span className="text-xs bg-green-500/15 text-green-400 px-2 py-0.5 rounded-full">Done</span>
    case 'failed':
      return <span className="text-xs bg-red-500/15 text-red-400 px-2 py-0.5 rounded-full">Failed</span>
    case 'scanning':
      return (
        <span className="text-xs bg-plex-orange/15 text-plex-orange px-2 py-0.5 rounded-full flex items-center gap-1 w-fit">
          <span className="animate-pulse">●</span> {Math.round(progress * 100)}%
        </span>
      )
    default:
      return <span className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full">Pending</span>
  }
}

/** A show, season or library's scan state in the same badges as a title's: what still needs attention, else Done. */
export function GroupStatus({ titles }: { titles: Title[] }) {
  const n = { scanning: 0, failed: 0, pending: 0 }
  for (const t of titles) if (t.status in n) n[t.status as keyof typeof n]++
  if (titles.length === 0) return <span className="text-xs text-muted">–</span>
  if (!n.scanning && !n.failed && !n.pending) return <span className="text-xs bg-green-500/15 text-green-400 px-2 py-0.5 rounded-full">Done</span>
  return (
    <span className="flex flex-wrap gap-1">
      {n.scanning > 0 && <span className="text-xs bg-plex-orange/15 text-plex-orange px-2 py-0.5 rounded-full">{n.scanning} scanning</span>}
      {n.failed > 0 && <span className="text-xs bg-red-500/15 text-red-400 px-2 py-0.5 rounded-full">{n.failed} failed</span>}
      {n.pending > 0 && <span className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full">{n.pending} pending</span>}
    </span>
  )
}

export function IgnoredBadge({ titles }: { titles: Title[] }) {
  if (titles.length === 0 || !titles.some(t => t.ignored)) return null
  const all = titles.every(t => t.ignored)
  return <span className={`text-xs text-yellow-300 px-2 py-0.5 rounded-full ${all ? 'bg-yellow-500/20' : 'bg-yellow-500/10'}`}>{all ? 'Ignored' : 'Partly ignored'}</span>
}

/** Newest scan date among the titles, as a short date; the full time is in the tooltip. */
export function scannedCell(titles: Title[]): { text: string; full: string } {
  let latest = ''
  for (const t of titles) if (t.finished_at && t.finished_at > latest) latest = t.finished_at
  if (!latest) return { text: 'Never', full: 'Not scanned yet' }
  const d = parseDbTimestamp(latest)
  return Number.isNaN(d.getTime()) ? { text: '', full: '' } : { text: d.toLocaleDateString(), full: d.toLocaleString() }
}

/** Segment count with the false-positive count beside it; or a marker count in the Markers view. */
export function CountCell({ count, falsePositives = 0, unit }: { count: number; falsePositives?: number; unit: 'segments' | 'markers' }) {
  if (count === 0 && falsePositives === 0) return <span className="text-xs text-muted">0</span>
  const tone = unit === 'markers' ? 'text-plex-orange bg-plex-orange/10 border-plex-orange/30' : 'text-red-400 bg-red-500/10 border-red-500/20'
  return (
    <span className="flex items-center justify-end gap-1.5 text-xs">
      <span className={`px-1.5 py-0.5 rounded border font-medium tabular-nums ${count > 0 ? tone : 'text-gray-400 bg-white/5 border-plex-border'}`}>{count}</span>
      {falsePositives > 0 && <span className="flex items-center gap-0.5 text-amber-300 tabular-nums" title={`${falsePositives} marked false positive`}><ThumbsDown size={10} /> {falsePositives}</span>}
    </span>
  )
}

export type RowImage =
  | { kind: 'poster'; src: string }
  | { kind: 'still'; src: string }
  | { kind: 'icon'; icon: 'movie' | 'show' }

function Thumb({ image }: { image: RowImage }) {
  if (image.kind === 'icon') {
    const Icon = image.icon === 'movie' ? Film : Tv
    return <div className="w-10 h-[3.75rem] rounded bg-plex-border/50 flex items-center justify-center text-gray-400"><Icon size={18} /></div>
  }
  const cls = image.kind === 'still' ? 'w-24 h-[3.375rem]' : 'w-10 h-[3.75rem]'
  return <SafeImg src={image.src} className={`${cls} rounded bg-plex-border`} />
}

// Column widths shared by the header and every row, so the columns line up at every level.
export const COL = {
  check: 'w-4 flex-shrink-0',
  image: 'w-24 flex-shrink-0 flex items-center',
  name: 'flex-1 min-w-0',
  status: 'hidden md:flex w-32 flex-shrink-0',
  scanned: 'hidden lg:block w-24 flex-shrink-0 text-xs text-muted',
  count: 'w-16 flex-shrink-0 text-right',
  // Fixed width (room for six icons) so the status, scanned and count columns line up across rows.
  actions: 'w-48 flex-shrink-0 flex items-center justify-end gap-0.5',
}

interface RowProps {
  image: RowImage
  name: string
  context: string
  /** Shown under the context line, e.g. why a scan failed. */
  note?: React.ReactNode
  badges?: React.ReactNode
  status: React.ReactNode
  scanned: { text: string; full: string }
  count: React.ReactNode
  actions: React.ReactNode
  checked: boolean
  onCheck: () => void
  onOpen: () => void
  onContextMenu?: (e: React.MouseEvent) => void
  ignored?: boolean
  /** Set for a row that expands in place (a title, #209) rather than opening a level. */
  expanded?: boolean
}

/**
 * One line of the Browse list (#208). Libraries, shows, seasons, movies and episodes all use it:
 * same columns, same place for status and counts, and clicking anywhere on the row opens it.
 */
export default function BrowseRow(p: RowProps) {
  return (
    <div
      onClick={p.onOpen}
      onContextMenu={p.onContextMenu}
      className={`group flex items-center gap-3 px-3 py-2 border-b border-plex-border last:border-b-0 cursor-pointer transition-colors ${p.ignored ? 'bg-yellow-500/5 hover:bg-yellow-500/10' : 'hover:bg-white/[0.04]'}`}
    >
      <input
        type="checkbox"
        checked={p.checked}
        onChange={p.onCheck}
        onClick={e => e.stopPropagation()}
        aria-label={`Select ${p.name}`}
        className={`${COL.check} h-4 accent-plex-orange`}
      />
      <div className={COL.image}><Thumb image={p.image} /></div>
      <div className={COL.name}>
        <div className="flex items-center gap-2 min-w-0">
          <button onClick={e => { e.stopPropagation(); p.onOpen() }} className="text-sm font-medium text-gray-100 truncate text-left hover:text-plex-orange" title={p.expanded === undefined ? `Open ${p.name}` : `Show segments of ${p.name}`} aria-expanded={p.expanded}>
            {p.name}
          </button>
          {p.badges}
        </div>
        {p.context && <p className="text-xs text-muted truncate">{p.context}</p>}
        {p.note}
        {/* Status moves under the name where its column is hidden (narrow screens). */}
        <div className="md:hidden mt-1">{p.status}</div>
      </div>
      <div className={COL.status}>{p.status}</div>
      <div className={COL.scanned} title={p.scanned.full}>{p.scanned.text}</div>
      <div className={COL.count}>{p.count}</div>
      <div className={`${COL.actions} opacity-70 group-hover:opacity-100 transition-opacity`} onClick={e => e.stopPropagation()}>
        {p.actions}
      </div>
      <ChevronRight size={16} className={`flex-shrink-0 text-gray-400 transition-transform ${p.expanded ? 'rotate-90' : ''}`} aria-hidden="true" />
    </div>
  )
}
