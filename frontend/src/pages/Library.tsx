import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Film, Tv, ChevronRight, RotateCcw, Zap, Moon } from 'lucide-react'

interface Library {
  id: string
  title: string
  type: string
}

interface Title {
  plex_guid: string
  rating_key: string
  title: string
  status: string
  progress: number
  thumb_url: string
  segment_count: number
}

function StatusBadge({ status, progress }: { status: string; progress: number }) {
  switch (status) {
    case 'done':
      return <span className="text-xs bg-green-500/15 text-green-400 px-2 py-0.5 rounded-full">Done</span>
    case 'failed':
      return <span className="text-xs bg-red-500/15 text-red-400 px-2 py-0.5 rounded-full">Failed</span>
    case 'scanning':
      return (
        <span className="text-xs bg-plex-orange/15 text-plex-orange px-2 py-0.5 rounded-full flex items-center gap-1">
          <span className="animate-pulse">●</span> {Math.round(progress * 100)}%
        </span>
      )
    default:
      return <span className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full">Pending</span>
  }
}

export default function Library() {
  const [libraries, setLibraries] = useState<Library[]>([])
  const [selected, setSelected] = useState<Library | null>(null)
  const [titles, setTitles] = useState<Title[]>([])
  const [loadingTitles, setLoadingTitles] = useState(false)
  const [scanning, setScanning] = useState<Record<string, boolean>>({})
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/libraries').then(d => setLibraries(d.libraries))
  }, [])

  const selectLibrary = async (lib: Library) => {
    setSelected(lib)
    setFilter('')
    setLoadingTitles(true)
    try {
      const d = await api.get<{ titles: Title[] }>(`/api/libraries/${lib.id}/titles`)
      setTitles(d.titles)
    } finally {
      setLoadingTitles(false)
    }
  }

  const scanTitle = async (guid: string, now: boolean) => {
    setScanning(s => ({ ...s, [guid]: true }))
    try {
      await api.post(`/api/scan/title/${encodeURIComponent(guid)}?now=${now}`)
      // Refresh titles
      if (selected) {
        const d = await api.get<{ titles: Title[] }>(`/api/libraries/${selected.id}/titles`)
        setTitles(d.titles)
      }
    } finally {
      setScanning(s => ({ ...s, [guid]: false }))
    }
  }

  const scanLibrary = async (libId: string, now: boolean) => {
    await api.post(`/api/scan/library/${libId}?now=${now}`)
    if (selected) {
      const d = await api.get<{ titles: Title[] }>(`/api/libraries/${selected.id}/titles`)
      setTitles(d.titles)
    }
  }

  const filtered = titles.filter(t => t.title.toLowerCase().includes(filter.toLowerCase()))

  return (
    <div className="flex gap-6 h-full">
      {/* Library list */}
      <div className="w-52 flex-shrink-0">
        <h1 className="text-2xl font-bold text-gray-100 mb-4">Library</h1>
        <div className="space-y-1">
          {libraries.map(lib => (
            <button
              key={lib.id}
              onClick={() => selectLibrary(lib)}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-left transition-colors ${
                selected?.id === lib.id
                  ? 'bg-plex-orange/20 text-plex-orange'
                  : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
              }`}
            >
              {lib.type === 'movie' ? <Film size={16} /> : <Tv size={16} />}
              <span className="truncate">{lib.title}</span>
              <ChevronRight size={14} className="ml-auto opacity-50" />
            </button>
          ))}
        </div>
      </div>

      {/* Titles panel */}
      <div className="flex-1 min-w-0">
        {!selected ? (
          <div className="flex items-center justify-center h-64 text-gray-600">
            Select a library to browse titles
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between mb-4 gap-3">
              <h2 className="text-xl font-semibold text-gray-100 truncate">{selected.title}</h2>
              <div className="flex gap-2 flex-shrink-0">
                <button
                  onClick={() => scanLibrary(selected.id, false)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-plex-orange/50 transition-colors"
                >
                  <Moon size={13} /> Scan Tonight
                </button>
                <button
                  onClick={() => scanLibrary(selected.id, true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-plex-orange/20 border border-plex-orange/30 rounded-lg text-plex-orange hover:bg-plex-orange/30 transition-colors"
                >
                  <Zap size={13} /> Scan Now
                </button>
              </div>
            </div>

            <input
              type="text"
              placeholder="Filter titles..."
              value={filter}
              onChange={e => setFilter(e.target.value)}
              className="w-full mb-4 px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-plex-orange/50"
            />

            {loadingTitles ? (
              <div className="text-gray-500 text-sm">Loading...</div>
            ) : filtered.length === 0 ? (
              <div className="text-gray-600 text-sm">No titles found</div>
            ) : (
              <div className="grid gap-2">
                {filtered.map(title => (
                  <div key={title.plex_guid} className="bg-plex-card border border-plex-border rounded-xl p-3 flex items-center gap-3">
                    {title.thumb_url ? (
                      <img
                        src={title.thumb_url}
                        alt=""
                        className="w-10 h-14 object-cover rounded bg-plex-border flex-shrink-0"
                        onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                      />
                    ) : (
                      <div className="w-10 h-14 bg-plex-border rounded flex-shrink-0" />
                    )}
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-gray-100 truncate">{title.title}</p>
                      <div className="flex items-center gap-2 mt-1">
                        <StatusBadge status={title.status} progress={title.progress} />
                        {title.segment_count > 0 && (
                          <span className="text-xs text-gray-500">{title.segment_count} segment{title.segment_count !== 1 ? 's' : ''}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 flex-shrink-0">
                      <button
                        onClick={() => scanTitle(title.plex_guid, false)}
                        disabled={scanning[title.plex_guid]}
                        title="Scan Tonight"
                        className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-white/5 rounded transition-colors disabled:opacity-40"
                      >
                        <Moon size={14} />
                      </button>
                      <button
                        onClick={() => scanTitle(title.plex_guid, true)}
                        disabled={scanning[title.plex_guid]}
                        title="Scan Now"
                        className="p-1.5 text-gray-500 hover:text-plex-orange hover:bg-plex-orange/10 rounded transition-colors disabled:opacity-40"
                      >
                        <Zap size={14} />
                      </button>
                      <button
                        onClick={() => scanTitle(title.plex_guid, false)}
                        disabled={scanning[title.plex_guid]}
                        title="Re-scan"
                        className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-white/5 rounded transition-colors disabled:opacity-40"
                      >
                        <RotateCcw size={14} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
