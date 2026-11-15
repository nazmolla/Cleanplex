import { useState } from 'react'
import { ChevronRight, Film, Tv, Layers } from 'lucide-react'
import { Library, Scope } from './model'

export interface TreeSeason { season: string; count: number }
export interface TreeShow { key: string; name: string; count: number; seasons: TreeSeason[] }
export interface TreeLibrary { lib: Library; count: number; shows: TreeShow[] }

interface Props {
  tree: TreeLibrary[]
  total: number
  scope: Scope
  onScope: (scope: Scope) => void
  /** Right-click on a library, show or season: the rescan menu. */
  onMenu: (e: React.MouseEvent, scope: Scope) => void
}

function Node({ depth, label, count, active, icon, open, onToggle, onClick, onContextMenu }: {
  depth: number
  label: string
  count: number
  active: boolean
  icon?: React.ReactNode
  /** undefined = no children to expand. */
  open?: boolean
  onToggle?: () => void
  onClick: () => void
  onContextMenu?: (e: React.MouseEvent) => void
}) {
  return (
    <div
      className={`flex items-center gap-1 rounded-lg text-sm transition-colors ${
        active ? 'bg-plex-orange/20 text-plex-orange' : count ? 'text-gray-300 hover:bg-white/5' : 'text-muted hover:bg-white/5'
      }`}
      style={{ paddingLeft: 4 + depth * 14 }}
      onContextMenu={onContextMenu}
    >
      {onToggle ? (
        <button onClick={onToggle} aria-label={open ? `Collapse ${label}` : `Expand ${label}`} aria-expanded={open} className="p-1 text-gray-400 hover:text-white flex-shrink-0">
          <ChevronRight size={13} className={`transition-transform ${open ? 'rotate-90' : ''}`} />
        </button>
      ) : (
        <span className="w-[21px] flex-shrink-0" />
      )}
      <button onClick={onClick} aria-current={active ? 'true' : undefined} className="flex-1 min-w-0 flex items-center gap-2 py-1.5 pr-2 text-left">
        {icon}
        <span className="truncate">{label}</span>
        <span className="ml-auto text-xs tabular-nums text-muted flex-shrink-0">{count}</span>
      </button>
    </div>
  )
}

/**
 * The level picker of Browse (#207): all libraries, a library, a show, a season. Movies and
 * episodes are not listed here (a library has thousands); they are opened from the list.
 * Counts are the titles under each node that pass the current filters.
 */
export default function ScopeTree({ tree, total, scope, onScope, onMenu }: Props) {
  // Nodes the user opened or closed by hand; the node in scope is always open.
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
  const isOpen = (key: string, inScopePath: boolean) => toggled[key] ?? inScopePath
  const toggle = (key: string, inScopePath: boolean) =>
    setToggled(prev => ({ ...prev, [key]: !(prev[key] ?? inScopePath) }))

  return (
    <nav aria-label="Level" className="space-y-0.5">
      <Node depth={0} label="All libraries" count={total} active={!scope.lib && !scope.guid} icon={<Layers size={15} className="flex-shrink-0" />} onClick={() => onScope({})} />
      {tree.map(({ lib, count, shows }) => {
        const libKey = `l:${lib.id}`
        const libInPath = scope.lib === lib.id
        const libOpen = shows.length > 0 && isOpen(libKey, libInPath)
        return (
          <div key={lib.id}>
            <Node
              depth={0}
              label={lib.title}
              count={count}
              active={libInPath && !scope.show && !scope.guid}
              icon={lib.type === 'movie' ? <Film size={15} className="flex-shrink-0" /> : <Tv size={15} className="flex-shrink-0" />}
              open={shows.length > 0 ? libOpen : undefined}
              onToggle={shows.length > 0 ? () => toggle(libKey, libInPath) : undefined}
              onClick={() => onScope({ lib: lib.id })}
              onContextMenu={e => onMenu(e, { lib: lib.id })}
            />
            {libOpen && shows.map(show => {
              const showKey = `s:${lib.id}:${show.key}`
              const showInPath = libInPath && scope.show === show.key
              // A show with nothing that passes the filters is left out, unless it is the one being looked at.
              if (show.count === 0 && !showInPath) return null
              const showOpen = isOpen(showKey, showInPath)
              return (
                <div key={show.key}>
                  <Node
                    depth={1}
                    label={show.name}
                    count={show.count}
                    active={showInPath && !scope.season && !scope.guid}
                    open={showOpen}
                    onToggle={() => toggle(showKey, showInPath)}
                    onClick={() => onScope({ lib: lib.id, show: show.key })}
                    onContextMenu={e => onMenu(e, { lib: lib.id, show: show.key })}
                  />
                  {showOpen && show.seasons.filter(season => season.count > 0 || (showInPath && scope.season === season.season)).map(season => (
                    <Node
                      key={season.season}
                      depth={2}
                      label={season.season}
                      count={season.count}
                      active={showInPath && scope.season === season.season && !scope.guid}
                      onClick={() => onScope({ lib: lib.id, show: show.key, season: season.season })}
                      onContextMenu={e => onMenu(e, { lib: lib.id, show: show.key, season: season.season })}
                    />
                  ))}
                </div>
              )
            })}
          </div>
        )
      })}
    </nav>
  )
}
