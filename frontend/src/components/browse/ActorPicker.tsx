import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search, X } from 'lucide-react'
import type { CastSort, CastSuggestion } from './FilterPanel'

// Rows rendered at once: a large library has thousands of actors, and searching narrows them.
const MAX_ROWS = 200

interface Props {
  suggestions: CastSuggestion[]
  sort: CastSort
  onSort: (sort: CastSort) => void
  selected: string[]
  onChange: (actors: string[]) => void
  match: 'any' | 'all'
  onMatch: (match: 'any' | 'all') => void
  /** Actor -> /api/plex-image path of their photo (#227). */
  photos: Record<string, string>
}

const initials = (name: string) => name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase()

function Photo({ name, path }: { name: string; path?: string }) {
  const [failed, setFailed] = useState(false)
  if (!path || failed) {
    return <span aria-hidden="true" className="w-7 h-7 rounded-full bg-plex-border text-[10px] text-gray-400 flex items-center justify-center flex-shrink-0">{initials(name)}</span>
  }
  return <img src={`/api/plex-image?path=${encodeURIComponent(path)}`} alt="" loading="lazy" onError={() => setFailed(true)} className="w-7 h-7 rounded-full object-cover bg-plex-border flex-shrink-0" />
}

/** The actor filter (#227): search, three orders, photos, and several actors at once. */
export function ActorPicker(p: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const root = useRef<HTMLDivElement>(null)
  const search = useRef<HTMLInputElement>(null)

  // While open: a click outside or Escape closes the list and returns focus to the button.
  useEffect(() => {
    if (!open) return
    search.current?.focus()
    const onDown = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) } }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey, true) }
  }, [open])

  const chosen = useMemo(() => new Set(p.selected), [p.selected])
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const hits = q ? p.suggestions.filter(c => c.name.toLowerCase().includes(q)) : p.suggestions
    return hits.slice(0, MAX_ROWS)
  }, [p.suggestions, query])
  const toggle = (name: string) => p.onChange(chosen.has(name) ? p.selected.filter(a => a !== name) : [...p.selected, name])

  const summary = p.selected.length === 0 ? 'Any actor'
    : p.selected.length === 1 ? p.selected[0] : `${p.selected[0]} +${p.selected.length - 1}`

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Filter by actor: ${summary}`}
        className={`w-full min-w-0 px-2 py-1.5 bg-plex-dark border rounded-lg text-sm text-left flex items-center gap-2 ${p.selected.length ? 'border-plex-orange/50 text-gray-100' : 'border-plex-border text-muted'}`}
      >
        <span className="flex-1 truncate">{summary}</span>
        <ChevronDown size={14} className="text-gray-400 flex-shrink-0" aria-hidden="true" />
      </button>
      {open && (
        <div className="absolute z-30 mt-1 w-80 max-w-[calc(100vw-2rem)] bg-plex-card border border-plex-border rounded-lg shadow-xl">
          <div className="p-2 space-y-2 border-b border-plex-border">
            <div className="flex items-center gap-2 px-2 bg-plex-dark border border-plex-border rounded-lg">
              <Search size={13} className="text-gray-400" aria-hidden="true" />
              <input ref={search} value={query} onChange={e => setQuery(e.target.value)} placeholder="Search actors" aria-label="Search actors"
                className="flex-1 min-w-0 py-1.5 bg-transparent text-sm text-gray-200 placeholder-muted" />
            </div>
            <div className="flex items-center justify-between gap-2 text-xs">
              <label className="flex items-center gap-1 text-muted">
                Sort
                <select value={p.sort} onChange={e => p.onSort(e.target.value as CastSort)} aria-label="Sort actors"
                  className="bg-plex-dark border border-plex-border rounded px-1 py-0.5 text-gray-200">
                  <option value="titles">Most titles</option>
                  <option value="name">Name</option>
                  <option value="segments">Most segments</option>
                </select>
              </label>
              {p.selected.length > 1 && (
                <div role="group" aria-label="Match" className="flex rounded border border-plex-border overflow-hidden">
                  {(['any', 'all'] as const).map(m => (
                    <button key={m} type="button" onClick={() => p.onMatch(m)} aria-pressed={p.match === m}
                      className={`px-2 py-0.5 ${p.match === m ? 'bg-plex-orange text-black font-semibold' : 'text-gray-400 hover:text-gray-100'}`}>
                      {m === 'any' ? 'Any of them' : 'All of them'}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
          <ul role="listbox" aria-multiselectable="true" aria-label="Actors" className="max-h-80 overflow-y-auto py-1">
            {rows.length === 0 && <li className="px-3 py-2 text-sm text-muted">No actor matches</li>}
            {rows.map(c => {
              const on = chosen.has(c.name)
              return (
                <li key={c.name} role="option" aria-selected={on}>
                  <button type="button" onClick={() => toggle(c.name)}
                    className={`w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-white/5 ${on ? 'text-gray-100' : 'text-gray-300'}`}>
                    <Photo name={c.name} path={p.photos[c.name]} />
                    <span className="flex-1 min-w-0 truncate">{c.name}</span>
                    <span className="text-[11px] text-muted tabular-nums flex-shrink-0">
                      {p.sort === 'segments' ? `${c.segments} seg` : `${c.titles} title${c.titles === 1 ? '' : 's'}`}
                    </span>
                    <span className={`w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 ${on ? 'bg-plex-orange border-plex-orange text-black' : 'border-plex-border'}`}>
                      {on && <Check size={11} aria-hidden="true" />}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
          {p.selected.length > 0 && (
            <div className="flex items-center justify-between px-3 py-2 border-t border-plex-border text-xs">
              <span className="text-muted">{p.selected.length} selected</span>
              <button type="button" onClick={() => p.onChange([])} className="flex items-center gap-1 text-gray-400 hover:text-gray-100">
                <X size={11} aria-hidden="true" /> Clear
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
