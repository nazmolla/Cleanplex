import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import { RotateCcw, Volume2, SkipForward } from 'lucide-react'
import { useT, type TranslationKey } from '../i18n'

interface CategoryPref {
  category: string
  level: number
  action: string
  has_segments: boolean
}

interface CategoriesResponse {
  username: string
  uses_defaults: boolean
  categories: CategoryPref[]
}

// Levels follow the threshold model the filter engine uses: a segment fires when
// the viewer's level plus its severity rank exceeds 4 (#230). Labelling them by what
// they let through is far clearer than showing the raw number.
const LEVEL_KEYS = ['cat_level_0', 'cat_level_1', 'cat_level_2', 'cat_level_3', 'cat_level_4'] as const
const MAX_LEVEL = LEVEL_KEYS.length - 1

// What each level skips for detector (scanner) segments, whose severity follows the
// detected label (#126). Imported segments carry their author's own severities.
const NUDITY_HINT_KEYS = [
  'cat_nudity_hint_0',
  'cat_nudity_hint_1',
  'cat_nudity_hint_2',
  'cat_nudity_hint_3',
  'cat_nudity_hint_4',
] as const

// Detector severities and the labels behind them (kept in step with LABEL_SEVERITY in
// importers/_common.py). Rank is what the filter adds to the viewer's level.
const SEVERITY_COLUMNS = [
  { rank: 4, nameKey: 'cat_sev_high' as const, examplesKey: 'cat_sev_high_examples' as const },
  { rank: 3, nameKey: 'cat_sev_medium' as const, examplesKey: 'cat_sev_medium_examples' as const },
  { rank: 2, nameKey: 'cat_sev_low' as const, examplesKey: 'cat_sev_low_examples' as const },
  { rank: 1, nameKey: 'cat_sev_minimal' as const, examplesKey: 'cat_sev_minimal_examples' as const },
]

const CATEGORY_KEYS: Record<string, TranslationKey> = {
  commercial: 'cat_label_commercial',
  discrimination: 'cat_label_discrimination',
  dispensable: 'cat_label_dispensable',
  drugs: 'cat_label_drugs',
  fear: 'cat_label_fear',
  language: 'cat_label_language',
  nudity: 'cat_label_nudity',
  sex: 'cat_label_sex',
  violence: 'cat_label_violence',
  other: 'cat_label_other',
}

interface Props {
  username: string
  /** Master switch: when off, the matrix is shown but visibly inert. */
  enabled: boolean
}

export default function CategoryMatrix({ username, enabled }: Props) {
  const { t } = useT()
  const [data, setData] = useState<CategoriesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  // Reloaded when this user's levels change elsewhere, e.g. another tab (#89).
  const [liveTick, setLiveTick] = useState(0)
  useLiveRefresh(['users_changed'], () => setLiveTick(n => n + 1), {
    relevant: (_, d) => !d.username || d.username === username,
  })

  // Refetch when the selected user changes; an in-flight request for the previous
  // user would otherwise overwrite this one's state. Live reloads keep the current
  // matrix on screen instead of flashing the loading state.
  const shownUser = useRef<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    if (shownUser.current !== username) setLoading(true)
    shownUser.current = username
    api
      .get<CategoriesResponse>(`/api/users/${encodeURIComponent(username)}/categories`, {
        signal: controller.signal,
      })
      .then(d => {
        setData(d)
        setLoading(false)
      })
      .catch(err => {
        if (err.name !== 'AbortError') {
          setError(t('cat_load_error'))
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [username, liveTick, t])

  const update = async (category: string, level: number, action: string) => {
    setSaving(s => ({ ...s, [category]: true }))
    setError('')
    try {
      await api.put(`/api/users/${encodeURIComponent(username)}/categories/${category}`, {
        level,
        action,
      })
      setData(d =>
        d
          ? {
              ...d,
              uses_defaults: false,
              categories: d.categories.map(c =>
                c.category === category ? { ...c, level, action } : c,
              ),
            }
          : d,
      )
    } catch {
      setError(t('cat_save_error'))
    } finally {
      setSaving(s => ({ ...s, [category]: false }))
    }
  }

  const reset = async () => {
    await api.delete(`/api/users/${encodeURIComponent(username)}/categories`)
    const fresh = await api.get<CategoriesResponse>(
      `/api/users/${encodeURIComponent(username)}/categories`,
    )
    setData(fresh)
  }

  if (loading) return <div className="text-muted text-sm px-5 py-4">{t('cat_loading')}</div>
  if (!data) return null

  return (
    <div className={enabled ? '' : 'opacity-40 pointer-events-none'}>
      <div className="flex items-center justify-between px-5 py-3 border-b border-hayaa-border">
        <div>
          <p className="text-sm font-medium text-gray-200">{t('cat_title')}</p>
          <p className="text-xs text-muted mt-0.5">
            {data.uses_defaults
              ? t('cat_using_defaults')
              : t('cat_customised')}
            {!enabled && ` ${t('cat_filtering_off_note')}`}
          </p>
        </div>
        {!data.uses_defaults && (
          <button
            onClick={reset}
            className="text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1"
          >
            <RotateCcw size={12} /> {t('common_reset')}
          </button>
        )}
      </div>

      {/* The levels, spelled out, so nobody has to work out level + severity > 4. */}
      <details className="px-5 py-3 border-b border-hayaa-border text-xs text-gray-400">
        <summary className="cursor-pointer text-gray-300">{t('cat_details_summary')}</summary>
        <p className="mt-2 mb-2 text-muted">
          {t('cat_details_desc')}
        </p>
        <table className="w-full text-start">
          <thead>
            <tr className="text-muted">
              <th className="font-normal py-1 pe-3">{t('cat_table_level')}</th>
              {SEVERITY_COLUMNS.map(col => (
                <th key={col.nameKey} className="font-normal py-1 pe-3">
                  {t(col.nameKey)}
                  <span className="block text-[10px] text-muted">{t(col.examplesKey)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {LEVEL_KEYS.map((key, level) => (
              <tr key={level} className="border-t border-hayaa-border/60">
                <td className="py-1 pe-3 text-gray-300">{level} · {t(key)}</td>
                {SEVERITY_COLUMNS.map(col => (
                  <td key={col.nameKey} className={`py-1 pe-3 ${level + col.rank > MAX_LEVEL ? 'text-red-300' : 'text-muted'}`}>
                    {level + col.rank > MAX_LEVEL ? t('cat_cell_skipped') : t('cat_cell_allowed')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </details>

      {error && <p className="text-xs text-red-400 px-5 pt-3">{error}</p>}

      <div className="divide-y divide-hayaa-border">
        {data.categories.map(cat => (
          <div key={cat.category} className="px-5 py-3 flex items-center gap-4">
            <div className="w-44 flex-shrink-0">
              <p className="text-sm text-gray-200">
                {CATEGORY_KEYS[cat.category] ? t(CATEGORY_KEYS[cat.category]) : cat.category}
              </p>
              {!cat.has_segments && (
                <p className="text-[11px] text-muted mt-0.5">{t('cat_no_segments_yet')}</p>
              )}
              {cat.category === 'nudity' && (
                <p className="text-[11px] text-muted mt-0.5">{t(NUDITY_HINT_KEYS[cat.level] ?? NUDITY_HINT_KEYS[0])}</p>
              )}
            </div>

            <div className="flex-1 flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={MAX_LEVEL}
                step={1}
                value={cat.level}
                disabled={saving[cat.category]}
                onChange={e => update(cat.category, Number(e.target.value), cat.action)}
                className="flex-1 accent-hayaa-gold"
              />
              <span className="text-xs text-muted w-32" title={cat.category === 'nudity' ? t(NUDITY_HINT_KEYS[cat.level] ?? NUDITY_HINT_KEYS[0]) : undefined}>
                {t(LEVEL_KEYS[cat.level] ?? LEVEL_KEYS[0])}
              </span>
            </div>

            {/* Action override. Only skip and mute are possible through the Plex
                playback API, so those are the only choices offered. */}
            <div className="flex gap-1">
              {(['', 'skip', 'mute'] as const).map(act => (
                <button
                  key={act || 'auto'}
                  onClick={() => update(cat.category, cat.level, act)}
                  disabled={saving[cat.category]}
                  title={act === '' ? t('cat_action_auto_title') : act === 'skip' ? t('cat_action_always_skip') : t('cat_action_always_mute')}
                  className={`px-2 py-1 rounded text-[11px] border transition-colors ${
                    cat.action === act
                      ? 'bg-hayaa-gold border-hayaa-gold text-black'
                      : 'border-hayaa-border text-gray-400 hover:text-gray-300'
                  }`}
                >
                  {act === '' ? t('cat_action_auto') : act === 'skip' ? <SkipForward size={11} /> : <Volume2 size={11} />}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
