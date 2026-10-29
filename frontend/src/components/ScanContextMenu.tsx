import { useEffect, useRef } from 'react'
import { Zap, Moon } from 'lucide-react'

export interface ScanTarget {
  x: number
  y: number
  label: string
  /** The titles the menu was opened on; for a show in the Segments page, only those with segments. */
  guids: string[]
  /** Set on a show: lets the menu also offer every episode, not just those listed. */
  wholeShow?: { showGuid: string; showTitle: string; libraryId?: string }
}

interface Props {
  target: ScanTarget
  /** whole=true asks for every episode of the show (#137). */
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
  const top = Math.min(target.y, window.innerHeight - (target.wholeShow ? 190 : 110))
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
      {target.wholeShow && <div className="px-3 pt-1 text-[10px] uppercase tracking-wide text-gray-600">Listed episodes ({count})</div>}
      {item(true, <Zap size={12} className="text-plex-orange" />, 'Rescan now')}
      {item(false, <Moon size={12} className="text-blue-400" />, 'Rescan tonight')}
      {target.wholeShow && (
        <>
          <div className="px-3 pt-2 text-[10px] uppercase tracking-wide text-gray-600">Whole show, every episode</div>
          {item(true, <Zap size={12} className="text-plex-orange" />, 'Rescan whole show now', true)}
          {item(false, <Moon size={12} className="text-blue-400" />, 'Rescan whole show tonight', true)}
        </>
      )}
    </div>
  )
}
