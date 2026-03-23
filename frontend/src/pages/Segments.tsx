import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Film, Tv, ChevronRight, Trash2, AlertTriangle } from 'lucide-react'

interface Library {
  id: string
  title: string
  type: string
}

interface Title {
  plex_guid: string
  title: string
  status: string
  thumb_url: string
  segment_count: number
}

interface Segment {
  id: number
  plex_guid: string
  title: string
  start_ms: number
  end_ms: number
  confidence: number
  has_thumbnail: boolean
  thumbnail_url: string
  created_at: string
}

function msToTimecode(ms: number): string {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

export default function Segments() {
  const [libraries, setLibraries] = useState<Library[]>([])
  const [selectedLib, setSelectedLib] = useState<Library | null>(null)
  const [titles, setTitles] = useState<Title[]>([])
  const [selectedTitle, setSelectedTitle] = useState<Title | null>(null)
  const [segments, setSegments] = useState<Segment[]>([])
  const [loadingTitles, setLoadingTitles] = useState(false)
  const [loadingSegs, setLoadingSegs] = useState(false)
  const [deleting, setDeleting] = useState<Record<number, boolean>>({})

  useEffect(() => {
    api.get<{ libraries: Library[] }>('/api/libraries').then(d => setLibraries(d.libraries))
  }, [])

  const selectLib = async (lib: Library) => {
    setSelectedLib(lib)
    setSelectedTitle(null)
    setSegments([])
    setLoadingTitles(true)
    try {
      const d = await api.get<{ titles: Title[] }>(`/api/libraries/${lib.id}/titles`)
      // Only show titles that have segments or are done
      setTitles(d.titles.filter(t => t.segment_count > 0 || t.status === 'done'))
    } finally {
      setLoadingTitles(false)
    }
  }

  const selectTitle = async (title: Title) => {
    setSelectedTitle(title)
    setLoadingSegs(true)
    try {
      const d = await api.get<{ segments: Segment[] }>(`/api/titles/${encodeURIComponent(title.plex_guid)}/segments`)
      setSegments(d.segments)
    } finally {
      setLoadingSegs(false)
    }
  }

  const deleteSegment = async (id: number) => {
    setDeleting(d => ({ ...d, [id]: true }))
    try {
      await api.delete(`/api/segments/${id}`)
      setSegments(s => s.filter(seg => seg.id !== id))
      if (selectedTitle) {
        setTitles(ts => ts.map(t =>
          t.plex_guid === selectedTitle.plex_guid
            ? { ...t, segment_count: t.segment_count - 1 }
            : t
        ))
      }
    } finally {
      setDeleting(d => ({ ...d, [id]: false }))
    }
  }

  return (
    <div className="flex gap-4 h-full">
      {/* Library tree */}
      <div className="w-44 flex-shrink-0">
        <h1 className="text-xl font-bold text-gray-100 mb-3">Segments</h1>
        <div className="space-y-0.5">
          {libraries.map(lib => (
            <div key={lib.id}>
              <button
                onClick={() => selectLib(lib)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left transition-colors ${
                  selectedLib?.id === lib.id ? 'bg-plex-orange/20 text-plex-orange' : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
                }`}
              >
                {lib.type === 'movie' ? <Film size={13} /> : <Tv size={13} />}
                <span className="truncate">{lib.title}</span>
                <ChevronRight size={11} className="ml-auto opacity-40" />
              </button>

              {selectedLib?.id === lib.id && (
                <div className="ml-4 mt-0.5 space-y-0.5">
                  {loadingTitles ? (
                    <div className="text-xs text-gray-600 px-2 py-1">Loading...</div>
                  ) : titles.length === 0 ? (
                    <div className="text-xs text-gray-600 px-2 py-1">No scanned titles</div>
                  ) : (
                    titles.map(t => (
                      <button
                        key={t.plex_guid}
                        onClick={() => selectTitle(t)}
                        className={`w-full flex items-center gap-1.5 px-2 py-1.5 rounded text-xs text-left transition-colors ${
                          selectedTitle?.plex_guid === t.plex_guid
                            ? 'bg-white/10 text-gray-100'
                            : 'text-gray-500 hover:text-gray-200 hover:bg-white/5'
                        }`}
                      >
                        <span className="truncate flex-1">{t.title.split(' – ')[0]}</span>
                        <span className="flex-shrink-0 text-gray-600">{t.segment_count}</span>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Segments detail panel */}
      <div className="flex-1 min-w-0">
        {!selectedTitle ? (
          <div className="flex items-center justify-center h-64 text-gray-600 text-sm">
            Select a title to review its segments
          </div>
        ) : (
          <>
            <div className="flex items-center gap-3 mb-4">
              {selectedTitle.thumb_url && (
                <img
                  src={selectedTitle.thumb_url}
                  alt=""
                  className="w-10 h-14 object-cover rounded bg-plex-border"
                  onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                />
              )}
              <div>
                <h2 className="text-lg font-semibold text-gray-100">{selectedTitle.title}</h2>
                <p className="text-sm text-gray-500">{segments.length} segment{segments.length !== 1 ? 's' : ''} detected</p>
              </div>
            </div>

            {loadingSegs ? (
              <div className="text-gray-500 text-sm">Loading segments...</div>
            ) : segments.length === 0 ? (
              <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500 text-sm">
                No segments found for this title
              </div>
            ) : (
              <div className="grid gap-3">
                {segments.map(seg => (
                  <div key={seg.id} className="bg-plex-card border border-plex-border rounded-xl overflow-hidden flex gap-0">
                    {/* Thumbnail */}
                    <div className="w-40 flex-shrink-0 bg-black relative">
                      {seg.has_thumbnail ? (
                        <img
                          src={seg.thumbnail_url}
                          alt="Flagged frame"
                          className="w-full h-full object-cover"
                          style={{ minHeight: '90px' }}
                        />
                      ) : (
                        <div className="w-full h-24 flex items-center justify-center text-gray-700">
                          <AlertTriangle size={20} />
                        </div>
                      )}
                      <div className="absolute bottom-1 left-1 bg-black/70 text-xs text-gray-300 px-1.5 py-0.5 rounded">
                        {Math.round(seg.confidence * 100)}%
                      </div>
                    </div>

                    {/* Info */}
                    <div className="flex-1 p-4 flex items-center justify-between gap-4">
                      <div>
                        <div className="flex items-center gap-3 mb-1">
                          <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.start_ms)}</span>
                          <span className="text-gray-600">→</span>
                          <span className="font-mono text-sm text-plex-orange">{msToTimecode(seg.end_ms)}</span>
                          <span className="text-xs text-gray-600">
                            ({Math.round((seg.end_ms - seg.start_ms) / 1000)}s)
                          </span>
                        </div>
                        <p className="text-xs text-gray-500">
                          Detected {new Date(seg.created_at).toLocaleDateString()}
                        </p>
                      </div>
                      <button
                        onClick={() => deleteSegment(seg.id)}
                        disabled={deleting[seg.id]}
                        title="Remove this segment (false positive)"
                        className="p-2 text-gray-600 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors disabled:opacity-40 flex-shrink-0"
                      >
                        <Trash2 size={16} />
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
