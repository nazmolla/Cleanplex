import { useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'

interface Props {
  /** Key the open/closed state is remembered under, e.g. "analytics.accuracy". */
  id: string
  title: string
  hint?: string
  defaultOpen?: boolean
  /** Controls shown on the right of the header, outside the toggle. */
  actions?: ReactNode
  children: ReactNode
}

const storageKey = (id: string) => `cleanplex.section.${id}`

// Per-viewer convenience only, so storage that is blocked or empty (private
// windows, cleared site data) simply falls back to defaultOpen.
function readOpen(id: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(storageKey(id))
    return v == null ? fallback : v === '1'
  } catch {
    return fallback
  }
}

function writeOpen(id: string, open: boolean) {
  try {
    localStorage.setItem(storageKey(id), open ? '1' : '0')
  } catch { /* not persisted; the section still toggles */ }
}

/** Card with a header that minimises its body (#117). The state survives reloads. */
export default function CollapsibleSection({ id, title, hint, defaultOpen = true, actions, children }: Props) {
  const [open, setOpen] = useState(() => readOpen(id, defaultOpen))
  const toggle = () => setOpen(o => {
    writeOpen(id, !o)
    return !o
  })

  return (
    <section className="bg-plex-card border border-plex-border rounded-xl">
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          onClick={toggle}
          aria-expanded={open}
          className="flex items-center gap-2 flex-1 min-w-0 text-left group"
        >
          <ChevronDown
            size={15}
            className={`text-gray-500 group-hover:text-gray-300 transition-transform flex-shrink-0 ${open ? '' : '-rotate-90'}`}
          />
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide whitespace-nowrap">{title}</h2>
          {hint && <span className="text-xs text-gray-600 truncate">— {hint}</span>}
        </button>
        {actions}
      </div>
      {open && <div className="px-4 pb-4">{children}</div>}
    </section>
  )
}
