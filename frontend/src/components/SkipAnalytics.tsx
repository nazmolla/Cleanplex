import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import { Activity, MonitorSmartphone, Film } from 'lucide-react'

interface CategoryCount {
  category: string
  count: number
}

interface ClientStat {
  client_title: string
  client_identifier: string
  count: number
  failures: number
  avg_latency_ms: number
  failure_rate: number
}

interface TitleCount {
  plex_guid: string
  title: string
  count: number
}

export default function SkipAnalytics() {
  const [categories, setCategories] = useState<CategoryCount[]>([])
  const [clients, setClients] = useState<ClientStat[]>([])
  const [titles, setTitles] = useState<TitleCount[]>([])
  const [loading, setLoading] = useState(true)

  // Refetched when a skip or mute is recorded (#89); `loading` is only set by the
  // first fetch, so live updates do not flash the loading text.
  const [liveTick, setLiveTick] = useState(0)
  useLiveRefresh(['skip_recorded'], () => setLiveTick(n => n + 1), { debounceMs: 1000 })
  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      api.get<{ categories: CategoryCount[] }>('/api/analytics/skips/by-category', {
        signal: controller.signal,
      }),
      api.get<{ clients: ClientStat[] }>('/api/analytics/skips/by-client', {
        signal: controller.signal,
      }),
      api.get<{ titles: TitleCount[] }>('/api/analytics/skips/top-titles?limit=10', {
        signal: controller.signal,
      }),
    ])
      .then(([c, cl, t]) => {
        setCategories(c.categories)
        setClients(cl.clients)
        setTitles(t.titles)
        setLoading(false)
      })
      .catch(err => {
        if (err.name !== 'AbortError') setLoading(false)
      })
    return () => controller.abort()
  }, [liveTick])

  if (loading) return <div className="text-gray-500 text-sm">Loading skip history...</div>

  if (categories.length === 0 && clients.length === 0 && titles.length === 0) {
    return (
      <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500 text-sm">
        No skips recorded yet. History appears here once filtering fires during playback.
      </div>
    )
  }

  const maxCategory = Math.max(...categories.map(c => c.count), 1)

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <section className="bg-plex-card border border-plex-border rounded-xl p-4">
        <h3 className="text-sm font-medium text-gray-200 flex items-center gap-1.5 mb-3">
          <Activity size={14} /> Skips by category
        </h3>
        <div className="space-y-2">
          {categories.map(c => (
            <div key={c.category} className="flex items-center gap-2">
              <span className="text-xs text-gray-400 w-28 truncate">{c.category}</span>
              <div className="flex-1 h-2 bg-plex-border rounded overflow-hidden">
                <div
                  className="h-full bg-plex-orange"
                  style={{ width: `${(c.count / maxCategory) * 100}%` }}
                />
              </div>
              <span className="text-xs text-gray-500 w-10 text-right">{c.count}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="bg-plex-card border border-plex-border rounded-xl p-4">
        <h3 className="text-sm font-medium text-gray-200 flex items-center gap-1.5 mb-3">
          <MonitorSmartphone size={14} /> Clients
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-gray-600">
              <tr>
                <th className="text-left font-normal pb-1.5">Client</th>
                <th className="text-right font-normal pb-1.5">Skips</th>
                <th className="text-right font-normal pb-1.5">Failed</th>
                <th className="text-right font-normal pb-1.5">Latency</th>
              </tr>
            </thead>
            <tbody className="text-gray-300">
              {clients.map(c => (
                <tr key={c.client_identifier || c.client_title} className="border-t border-plex-border">
                  <td className="py-1.5 truncate max-w-[10rem]">{c.client_title}</td>
                  <td className="text-right">{c.count}</td>
                  <td
                    className={`text-right ${c.failure_rate > 0.1 ? 'text-red-400' : 'text-gray-500'}`}
                  >
                    {Math.round(c.failure_rate * 100)}%
                  </td>
                  <td className="text-right text-gray-500">{c.avg_latency_ms}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="bg-plex-card border border-plex-border rounded-xl p-4 md:col-span-2">
        <h3 className="text-sm font-medium text-gray-200 flex items-center gap-1.5 mb-3">
          <Film size={14} /> Most-skipped titles
        </h3>
        <div className="divide-y divide-plex-border">
          {titles.map(t => (
            <div key={t.plex_guid} className="flex items-center justify-between py-1.5">
              <span className="text-xs text-gray-300 truncate">{t.title}</span>
              <span className="text-xs text-gray-500 ml-3">{t.count}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
