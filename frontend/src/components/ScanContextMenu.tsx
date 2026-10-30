import { useEffect, useRef } from 'react'
import { Zap, Moon } from 'lucide-react'

export interface ScanTarget {
  x: number
  y: number
  label: string
  /** The titles the menu was opened on; for a show in the Segments page, only those with segments. */
  guids: string[]
  /**
   * Set on a show, season or library: lets the menu also offer the whole thing, not just the
   * listed titles (which in the Segments page are only those that have segments).
   */
  whole?: {
    kind: 'show' | 'season' | 'library'
    showGuid?: string
    showTitle?: string
    /** As it appears in episode titles, e.g. "Season 1" (kind 'season'). */
    season?: string
    libraryId?: string
  }
}

interface Props {
  target: ScanTarget
  /** whole=true asks for the whole show, season or library rather than the listed titles (#137). */
  onScan: (target: ScanTarget, now: boolean, whole: boolean) => void
  onClose: () => void
}

export default function ScanContextMenu({ target, onScan, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  // Any click outside, Escape, scroll or resize dismisses the menu, like a native one.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onClose, true)
    window.addEventListener('resize', onClose)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onClose, true)
      window.removeEventListener('resize', onClose)
    }
  }, [onClose])

  // Keep the menu on screen when opened near the right or bottom edge.
  const left = Math.min(target.x, window.innerWidth - 200)
  const top = Math.min(target.y, window.innerHeight - (target.whole ? 190 : 110))
  const count = target.guids.length

  const item = (now: boolean, icon: JSX.Element, text: string, whole = false) => (
    <button
      role="menuitem"
      onClick={() => {
        onScan(target, now, whole)
        onClose()
      }}
      className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-gray-300 hover:bg-white/10 hover:text-gray-100"
    >
      {icon}
      {text}
    </button>
  )

  return (
    <div
      ref={ref}
      role="menu"
      aria-label={`Rescan ${target.label}`}
      style={{ left, top }}
      className="fixed z-50 w-48 bg-plex-card border border-plex-border rounded-lg shadow-xl py-1 text-xs"
    >
      <div className="px-3 py-1 text-gray-500 truncate">
        {target.label}
        {count > 1 ? ` · ${count} titles` : ''}
      </div>
      {count > 0 && (
        <>
          {target.whole && <div className="px-3 pt-1 text-[10px] uppercase tracking-wide text-gray-600">Listed {target.whole.kind === 'library' ? 'titles' : 'episodes'} ({count})</div>}
          {item(true, <Zap size={12} className="text-plex-orange" />, 'Rescan now')}
          {item(false, <Moon size={12} className="text-blue-400" />, 'Rescan tonight')}
        </>
      )}
      {target.whole && (
        <>
          <div className="px-3 pt-2 text-[10px] uppercase tracking-wide text-gray-600">
            {target.whole.kind === 'library' ? 'Whole library, every title' : `Whole ${target.whole.kind}, every episode`}
          </div>
          {/* A library is thousands of titles: only ever scheduled, never jumped to the front. */}
          {target.whole.kind !== 'library' && item(true, <Zap size={12} className="text-plex-orange" />, `Rescan whole ${target.whole.kind} now`, true)}
          {item(false, <Moon size={12} className="text-blue-400" />, `Rescan whole ${target.whole.kind} tonight`, true)}
        </>
      )}
    </div>
  )
}
