import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CheckCircle2, XCircle, Loader2, Eye, EyeOff, RotateCcw } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { UsersPanel } from './Users'
import BulkRescan from '../components/BulkRescan'
import VerifierPanel from '../components/VerifierPanel'
import EnforcementPanel from '../components/EnforcementPanel'
import HayaaPanel from '../components/HayaaPanel'
import { normalizeRating } from '../utils/ratings'
import { useT } from '../i18n'

// A content rating in the libraries as Scan ratings compares it (#219); sources are Plex's spellings.
interface LibraryRating { rating: string; count: number; sources: string[] }

interface Settings {
  plex_url: string
  plex_token: string
  // The server never returns the token itself, only whether one is stored (#155).
  plex_token_set?: boolean
  poll_interval: string
  confidence_threshold: string
  skip_buffer_ms: string
  scan_step_ms: string
  scan_workers: string
  nudenet_model: string
  nudenet_model_path: string
  segment_gap_ms: string
  segment_min_hits: string
  refine_step_ms: string
  scan_window_start: string
  scan_window_end: string
  log_level: string
  excluded_library_ids: string
  scan_ratings: string
  scan_labels: string
  siglip_enabled: string
  siglip_threshold: string
  siglip_min_hits: string
  ignore_credits: string
  verifier_loss_pct: string
  label_thresholds: string
  siglip_label_thresholds: string
  hw_decode: string
  hover_previews_enabled: string
  hover_when_idle: string
  hover_window_start: string
  hover_window_end: string
}

type SectionId = 'connection' | 'playback' | 'filtering' | 'schedule' | 'nudenet' | 'siglip' | 'segments' | 'hayaa' | 'users'
const SECTION_IDS: SectionId[] = ['connection', 'users', 'playback', 'filtering', 'schedule', 'nudenet', 'siglip', 'segments', 'hayaa']

interface Library {
  id: string
  title: string
  type: string
}

const DEFAULT: Settings = {
  plex_url: '',
  plex_token: '',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  nudenet_model: '320n',
  nudenet_model_path: '',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  refine_step_ms: '500',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED","FEMALE_GENITALIA_EXPOSED","MALE_GENITALIA_EXPOSED","ANUS_EXPOSED","BUTTOCKS_EXPOSED"]',
  siglip_enabled: '0',
  siglip_threshold: '0.3',
  siglip_min_hits: '2',
  ignore_credits: '1',
  verifier_loss_pct: '0',
  label_thresholds: '{}',
  siglip_label_thresholds: '{}',
  hw_decode: '1',
  hover_previews_enabled: '1',
  hover_when_idle: '1',
  hover_window_start: '00:00',
  hover_window_end: '07:00',
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-300 mb-1">{label}</label>
      {children}
      {hint && <p className="text-xs text-muted mt-1">{hint}</p>}
    </div>
  )
}

// Per-label threshold overrides stored as a JSON object in one setting. A blank box
// means "inherit the global threshold", so only labels the user typed into are saved.
function LabelThresholds({ labels, value, globalValue, disabled, onChange }: {
  labels: string[]
  value: string
  globalValue: string
  disabled?: boolean
  onChange: (next: string) => void
}) {
  const { t } = useT()
  const map: Record<string, number> = (() => { try { return JSON.parse(value) } catch { return {} } })()
  const update = (label: string, raw: string) => {
    const next = { ...map }
    if (raw === '' || Number.isNaN(Number(raw))) delete next[label]
    else next[label] = Number(raw)
    onChange(JSON.stringify(next))
  }
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
      {labels.map(label => (
        <label key={label} className="flex items-center justify-between gap-3 text-sm text-gray-300">
          <span className="truncate">{label}</span>
          <input
            type="number" min="0" max="1" step="0.05"
            value={map[label] ?? ''}
            placeholder={globalValue}
            disabled={disabled}
            onChange={e => update(label, e.target.value)}
            aria-label={t('settings_label_threshold_aria', { label })}
            className="w-24 bg-plex-card border border-plex-border rounded px-2 py-1 text-sm text-gray-100 disabled:opacity-40"
          />
        </label>
      ))}
    </div>
  )
}

export default function SettingsPage() {
  const { t, formatNumber } = useT()
  const [form, setForm] = useState<Settings>(DEFAULT)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [validatingModel, setValidatingModel] = useState(false)
  const [modelValidationResult, setModelValidationResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [showToken, setShowToken] = useState(false)
  const [libraries, setLibraries] = useState<Library[]>([])
  // Segments stored before dense re-sampling that the background job has not reached yet (#141).
  const [refineRemaining, setRefineRemaining] = useState(0)
  const [detectorLabels, setDetectorLabels] = useState<string[]>([])
  const [libraryRatings, setLibraryRatings] = useState<LibraryRating[]>([])
  const [restarting, setRestarting] = useState(false)
  const [restarted, setRestarted] = useState(false)
  // The open section lives in ?section= so it can be linked (e.g. /users redirects here).
  const [params, setParams] = useSearchParams()
  const section: SectionId = SECTION_IDS.includes(params.get('section') as SectionId)
    ? (params.get('section') as SectionId)
    : 'connection'
  const setSection = (id: SectionId) => setParams(id === 'connection' ? {} : { section: id }, { replace: true })

  useEffect(() => {
    Promise.all([
      api.get<Settings>('/api/settings'),
      api.get<{ libraries: Library[] }>('/api/libraries').catch(() => ({ libraries: [] })),
      api.get<{ labels: string[] }>('/api/settings/detector-labels').catch(() => ({ labels: [] })),
      api.get<{ remaining: number; enabled: boolean }>('/api/segments/refine-status').catch(() => null),
      api.get<LibraryRating[]>('/api/settings/ratings').catch(() => []),
    ]).then(([settings, libs, labels, refine, ratings]) => {
      setLibraryRatings(Array.isArray(ratings) ? ratings : [])
      setRefineRemaining(refine?.enabled ? refine.remaining : 0)
      setForm({ ...DEFAULT, ...settings })
      setLibraries(libs.libraries)
      setDetectorLabels(labels.labels)
      setLoading(false)
    })
  }, [])

  const excludedIds: string[] = (() => {
    try { return JSON.parse(form.excluded_library_ids) } catch { return [] }
  })()

  const toggleExcluded = (id: string) => {
    const next = excludedIds.includes(id)
      ? excludedIds.filter(x => x !== id)
      : [...excludedIds, id]
    setForm(f => ({ ...f, excluded_library_ids: JSON.stringify(next) }))
  }

  const enabledLabels: string[] = (() => { try { return JSON.parse(form.scan_labels) } catch { return [] } })()

  const set = (k: keyof Settings) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm(f => ({ ...f, [k]: e.target.value }))

  const save = async () => {
    setSaving(true)
    setSaved(false)
    try {
      await api.put('/api/settings', form)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } finally {
      setSaving(false)
    }
  }

  const testConnection = async () => {
    setTesting(true)
    setTestResult(null)
    // Save first so the server uses current values
    await api.put('/api/settings', { plex_url: form.plex_url, plex_token: form.plex_token })
    try {
      const r = await api.post<{ ok: boolean; message: string }>('/api/settings/test-connection')
      setTestResult(r)
    } finally {
      setTesting(false)
    }
  }

  const validateModelPath = async () => {
    setValidatingModel(true)
    setModelValidationResult(null)
    try {
      const r = await api.post<{ ok: boolean; message: string }>('/api/settings/validate-model-path', {
        nudenet_model: form.nudenet_model,
        nudenet_model_path: form.nudenet_model_path,
      })
      setModelValidationResult(r)
    } catch {
      setModelValidationResult({ ok: false, message: t('settings_validation_failed') })
    } finally {
      setValidatingModel(false)
    }
  }

  const restartScanner = async () => {
    setRestarting(true)
    setRestarted(false)
    try {
      await api.post('/api/scan/restart-scanner')
      setRestarted(true)
      setTimeout(() => setRestarted(false), 5000)
    } finally {
      setRestarting(false)
    }
  }

  // Same small uppercase label as CollapsibleSection and Browse (#211).
  const h2Cls = "text-sm font-semibold text-gray-400 uppercase tracking-wide mb-4"
  const inputCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100 placeholder-muted focus:border-plex-orange/60 transition-colors"

  if (loading) return <div className="text-muted text-sm">{t('common_loading_dots')}</div>

  const sections: { id: SectionId; label: string }[] = [
    { id: 'connection', label: t('settings_sec_connection') },
    { id: 'users', label: t('settings_sec_users') },
    { id: 'playback', label: t('settings_sec_playback') },
    { id: 'filtering', label: t('settings_sec_filtering') },
    { id: 'schedule', label: t('settings_sec_schedule') },
    { id: 'nudenet', label: t('settings_sec_nudenet') },
    { id: 'siglip', label: t('settings_sec_siglip') },
    { id: 'segments', label: t('settings_sec_segments') },
    { id: 'hayaa', label: t('settings_sec_hayaa') },
  ]

  return (
    <div className="w-full">
      <h1 className="text-2xl font-bold text-gray-100 mb-4">{t('settings_title')}</h1>
      <div className="flex flex-col md:flex-row gap-6">
        {/* Section nav: a column on desktop, a scrolling row on phones. */}
        <nav aria-label={t('settings_nav_aria')} className="flex md:flex-col gap-1 md:w-48 flex-shrink-0 overflow-x-auto">
          {sections.map(sec => (
            <button
              key={sec.id}
              onClick={() => setSection(sec.id)}
              aria-current={section === sec.id ? 'page' : undefined}
              className={`text-start whitespace-nowrap px-3 py-2 rounded-lg text-sm transition-colors ${
                section === sec.id ? 'bg-plex-orange/20 text-plex-orange' : 'text-gray-300 hover:text-gray-100 hover:bg-white/5'
              }`}
            >
              {sec.label}
            </button>
          ))}
        </nav>

        {/* Each section's blocks are cards on a two-column grid on wide screens (#118);
            a section with a single block spans both columns. */}
        <div className="flex-1 min-w-0 grid gap-6 xl:grid-cols-2 items-start content-start pb-6 [&>section]:bg-plex-card [&>section]:border [&>section]:border-plex-border [&>section]:rounded-xl [&>section]:p-5 xl:[&>section:only-child]:col-span-2">
          {section === 'users' && (
            <section>
              <UsersPanel />
            </section>
          )}

          {section === 'connection' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_connection')}</h2>
              <div className="grid gap-4 lg:grid-cols-2 items-start">
                <Field label={t('settings_plex_url')} hint={t('settings_plex_url_hint')}>
                  <input
                    type="url"
                    dir="ltr"
                    value={form.plex_url}
                    onChange={set('plex_url')}
                    placeholder="http://localhost:32400"
                    className={inputCls}
                  />
                </Field>

                <Field label={t('settings_plex_token')} hint={t('settings_plex_token_hint')}>
                  <div className="relative">
                    <input
                      type={showToken ? 'text' : 'password'}
                      dir="ltr"
                      value={form.plex_token}
                      onChange={set('plex_token')}
                      placeholder={form.plex_token_set ? t('settings_plex_token_saved_placeholder') : 'xxxxxxxxxxxxxxxxxxxx'}
                      className={inputCls + ' pe-10'}
                    />
                    <button
                      type="button"
                      onClick={() => setShowToken(v => !v)}
                      className="absolute end-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-300"
                    >
                      {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                    </button>
                  </div>
                </Field>

                <div className="flex items-center gap-3">
                  <button
                    onClick={testConnection}
                    disabled={testing || !form.plex_url || !(form.plex_token || form.plex_token_set)}
                    className="btn-outline text-sm"
                  >
                    {testing && <Loader2 size={14} className="animate-spin" />}
                    {t('settings_test_connection')}
                  </button>
                  {testResult && (
                    <span className={`flex items-center gap-1.5 text-sm ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                      {testResult.ok ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
                      {testResult.message}
                    </span>
                  )}
                </div>
              </div>
      </section>
          )}

          {section === 'playback' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_playback')}</h2>
              <p className="text-xs text-muted mb-4">{t('settings_playback_desc')}</p>
              <div className="grid gap-4 lg:grid-cols-2 items-start">
                <Field label={t('settings_poll_interval')} hint={t('settings_poll_interval_hint')}>
                            <input type="number" min="2" max="30" value={form.poll_interval} onChange={set('poll_interval')} className={inputCls} />
                          </Field>
                <Field label={t('settings_skip_buffer')} hint={t('settings_skip_buffer_hint')}>
                            <input type="number" min="0" step="500" value={form.skip_buffer_ms} onChange={set('skip_buffer_ms')} className={inputCls} />
                          </Field>
              </div>
            </section>
          )}

          {section === 'filtering' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_filtering')}</h2>
              <p className="text-xs text-muted mb-4">
                {t('settings_filtering_desc')}
              </p>
              <EnforcementPanel />
            </section>
          )}

          {section === 'schedule' && (
            <>
              <section>
                <h2 className={h2Cls}>{t('settings_scan_schedule')}</h2>
                <p className="text-xs text-muted mb-4">{t('settings_scan_schedule_desc')}</p>
                <div className="flex gap-4">
                  <Field label={t('settings_start_time')}>
                    <input type="time" value={form.scan_window_start} onChange={set('scan_window_start')} className={inputCls} />
                  </Field>
                  <Field label={t('settings_end_time')}>
                    <input type="time" value={form.scan_window_end} onChange={set('scan_window_end')} className={inputCls} />
                  </Field>
                </div>
      </section>
              <section>
                <h2 className={h2Cls}>{t('settings_rescan_titles')}</h2>
                <p className="text-xs text-muted mb-4">
                  {t('settings_rescan_titles_desc')}
                </p>
                <BulkRescan libraries={libraries} />
              </section>
              <section>
                <h2 className={h2Cls}>{t('settings_hover_previews')}</h2>
                <p className="text-xs text-muted mb-4">
                  {t('settings_hover_previews_desc')}
                </p>
                <label className="flex items-start gap-3 cursor-pointer mb-4">
                  <input
                    type="checkbox"
                    checked={form.hover_previews_enabled !== '0'}
                    onChange={e => setForm(f => ({ ...f, hover_previews_enabled: e.target.checked ? '1' : '0' }))}
                    className="w-4 h-4 mt-0.5 accent-plex-orange"
                  />
                  <span className="text-sm text-gray-300">{t('settings_hover_enable')}</span>
                </label>
                <label className="flex items-start gap-3 cursor-pointer mb-4">
                  <input
                    type="checkbox"
                    checked={form.hover_when_idle !== '0'}
                    onChange={e => setForm(f => ({ ...f, hover_when_idle: e.target.checked ? '1' : '0' }))}
                    disabled={form.hover_previews_enabled === '0'}
                    className="w-4 h-4 mt-0.5 accent-plex-orange disabled:opacity-40"
                  />
                  <span className="text-sm text-gray-300">
                    {t('settings_hover_when_idle')}
                    <span className="block text-xs text-muted">{t('settings_hover_when_idle_hint')}</span>
                  </span>
                </label>
                <div className="flex gap-4">
                  <Field label={t('settings_start_time')}>
                    <input type="time" value={form.hover_window_start} onChange={set('hover_window_start')} className={inputCls} />
                  </Field>
                  <Field label={t('settings_end_time')}>
                    <input type="time" value={form.hover_window_end} onChange={set('hover_window_end')} className={inputCls} />
                  </Field>
                </div>
              </section>
              <section>
                <h2 className={h2Cls}>{t('settings_workers')}</h2>
                <div className="space-y-4">
                  <Field label={t('settings_scan_workers')} hint={t('settings_scan_workers_hint')}>
                              <input type="number" min="1" max="12" step="1" value={form.scan_workers} onChange={set('scan_workers')} className={inputCls} />
                            </Field>
                  <label className="flex items-start gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.hw_decode !== '0'}
                      onChange={e => setForm(f => ({ ...f, hw_decode: e.target.checked ? '1' : '0' }))}
                      className="w-4 h-4 mt-0.5 accent-plex-orange"
                    />
                    <span className="text-sm text-gray-300">
                      {t('settings_hw_decode')}
                      <span className="block text-xs text-muted">
                        {t('settings_hw_decode_hint')}
                      </span>
                    </span>
                  </label>
                </div>
              </section>
              <section>
                <h2 className={h2Cls}>{t('settings_scan_ratings')}</h2>
                <p className="text-xs text-muted mb-3">{t('settings_scan_ratings_desc')}</p>
                <div className="grid grid-cols-2 gap-2">
                  {/* The ratings the libraries hold (#219), normalized the way the scanner compares them, so
                      "eg/16" is under R. A checked rating no title has any more stays listed so it can be unchecked.
                      "" is titles Plex left unrated. */}
                  {(() => {
                    const selected: string[] = (() => { try { return JSON.parse(form.scan_ratings) } catch { return [] } })()
                    const selectedNorm = new Set(selected.map(normalizeRating))
                    const rows = [...libraryRatings]
                    for (const r of selectedNorm) if (!rows.some(x => x.rating === r)) rows.push({ rating: r, count: 0, sources: [] })
                    rows.sort((x, y) => (x.rating === '' ? 1 : y.rating === '' ? -1 : x.rating.localeCompare(y.rating)))
                    if (rows.length === 0) return <p className="text-sm text-muted col-span-2">{t('settings_no_titles_yet')}</p>
                    return rows.map(({ rating, count, sources }) => {
                      const checked = selectedNorm.has(rating)
                      const toggle = () => {
                        // Saved values are stored normalized; older spellings of the same rating are dropped with it.
                        const rest = selected.filter(r => normalizeRating(r) !== rating)
                        setForm(f => ({ ...f, scan_ratings: JSON.stringify(checked ? rest : [...rest, rating]) }))
                      }
                      const also = sources.filter(s => s !== rating)
                      return (
                        <label key={rating === '' ? '__unrated__' : rating} className="flex items-start gap-2 cursor-pointer">
                          <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 mt-0.5 accent-plex-orange" />
                          <span className={`text-sm ${checked ? 'text-gray-100' : 'text-muted'}`}>
                            {rating === '' ? t('settings_unrated_plex') : rating}
                            <span className="text-xs text-muted"> · {count}</span>
                            {also.length > 0 && <span className="block text-xs text-muted">{t('settings_includes_sources', { sources: also.join(', ') })}</span>}
                          </span>
                        </label>
                      )
                    })
                  })()}
                </div>
      </section>
              {libraries.length > 0 && (
                <section>
                  <h2 className={h2Cls}>{t('settings_library_exclusions')}</h2>
                    <p className="text-xs text-muted mb-3">{t('settings_library_exclusions_desc')}</p>
                    <div className="space-y-2">
                      {libraries.map(lib => (
                        <label key={lib.id} className="flex items-center gap-3 cursor-pointer group">
                          <input
                            type="checkbox"
                            checked={excludedIds.includes(lib.id)}
                            onChange={() => toggleExcluded(lib.id)}
                            className="w-4 h-4 accent-plex-orange"
                          />
                          <span className={`text-sm ${excludedIds.includes(lib.id) ? 'text-muted line-through' : 'text-gray-200'}`}>
                            {lib.title}
                          </span>
                          <span className="text-xs text-muted">
                            {lib.type === 'movie' ? t('settings_lib_type_movie') : lib.type === 'show' ? t('settings_lib_type_show') : lib.type}
                          </span>
                        </label>
                      ))}
                    </div>
        </section>
              )}
            </>
          )}

          {section === 'nudenet' && (
            <>
              <section>
                <h2 className={h2Cls}>{t('settings_nudenet_model')}</h2>
                <div className="space-y-4">
                  <Field label={t('settings_nudenet_model')} hint={t('settings_nudenet_model_hint')}>
                              <select value={form.nudenet_model} onChange={set('nudenet_model')} className={inputCls}>
                                <option value="320n">{t('settings_nudenet_320n')}</option>
                                <option value="640m">{t('settings_nudenet_640m')}</option>
                              </select>
                            </Field>
                  <Field label={t('settings_prepare_640m')} hint={t('settings_prepare_640m_hint')}>
                              <div className="mt-2 flex items-center gap-3">
                                <button
                                  type="button"
                                  onClick={validateModelPath}
                                  disabled={validatingModel}
                                  className="btn-outline text-xs"
                                >
                                  {validatingModel && <Loader2 size={13} className="animate-spin" />}
                                  {t('settings_download_check_640m')}
                                </button>
                                {modelValidationResult && (
                                  <span className={`flex items-center gap-1.5 text-xs ${modelValidationResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                                    {modelValidationResult.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                                    {modelValidationResult.message}
                                  </span>
                                )}
                              </div>
                            </Field>
                  <Field label={t('settings_confidence_threshold')} hint={t('settings_confidence_threshold_hint')}>
                              <input type="number" min="0.1" max="1" step="0.05" value={form.confidence_threshold} onChange={set('confidence_threshold')} className={inputCls} />
                            </Field>
                  <Field label={t('settings_scan_step')} hint={t('settings_scan_step_hint')}>
                              <input type="number" min="1000" max="20000" step="500" value={form.scan_step_ms} onChange={set('scan_step_ms')} className={inputCls} />
                            </Field>
                </div>
              </section>
              <section>
                <h2 className={h2Cls}>{t('settings_detector_labels')}</h2>
                <p className="text-xs text-muted mb-3">{t('settings_detector_labels_desc')}</p>
                <div className="grid grid-cols-2 gap-2">
                  {detectorLabels.map(label => {
                    const selected = (() => { try { return JSON.parse(form.scan_labels) } catch { return [] } })()
                    const checked = selected.includes(label)
                    const toggle = () => {
                      const next = checked ? selected.filter((r: string) => r !== label) : [...selected, label]
                      setForm(f => ({ ...f, scan_labels: JSON.stringify(next) }))
                    }
                    return (
                      <label key={label} className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-plex-orange" />
                        <span className={`text-sm ${checked ? 'text-gray-100' : 'text-muted'}`}>{label}</span>
                      </label>
                    )
                  })}
                </div>
      </section>
              <section>
                <h2 className={h2Cls}>{t('settings_per_label_thresholds')}</h2>
                <p className="text-xs text-muted mb-3">
                  {t('settings_per_label_thresholds_desc')}
                </p>
                <LabelThresholds
                  labels={enabledLabels}
                  value={form.label_thresholds}
                  globalValue={form.confidence_threshold}
                  onChange={next => setForm(f => ({ ...f, label_thresholds: next }))}
                />
              </section>
            </>
          )}

          {section === 'siglip' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_siglip')}</h2>
              <p className="text-xs text-muted mb-4">
                {t('settings_siglip_desc_before')} <span className="text-gray-300">siglip</span>{t('settings_siglip_desc_after')}
              </p>
              <div className="grid gap-4 lg:grid-cols-2 items-start">
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.siglip_enabled === '1'}
                    onChange={e => setForm(f => ({ ...f, siglip_enabled: e.target.checked ? '1' : '0' }))}
                    className="w-4 h-4 accent-plex-orange"
                  />
                  <span className="text-sm text-gray-300">{t('settings_siglip_enable')}</span>
                </label>
                <Field label={t('settings_siglip_threshold')} hint={t('settings_siglip_threshold_hint')}>
                  <input type="number" min="0.05" max="0.95" step="0.05" value={form.siglip_threshold} onChange={set('siglip_threshold')} disabled={form.siglip_enabled !== '1'} className={inputCls + ' disabled:opacity-40'} />
                </Field>
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.ignore_credits === '1'}
                    onChange={e => setForm(f => ({ ...f, ignore_credits: e.target.checked ? '1' : '0' }))}
                    disabled={form.siglip_enabled !== '1'}
                    className="w-4 h-4 accent-plex-orange"
                  />
                  <span className="text-sm text-gray-300">{t('settings_ignore_credits')}</span>
                </label>
                <div className="lg:col-span-2">
                  <p className="text-sm font-medium text-gray-300 mb-1">{t('settings_siglip_per_label')}</p>
                  <p className="text-xs text-muted mb-2">{t('settings_siglip_per_label_desc')}</p>
                  <LabelThresholds
                    labels={enabledLabels}
                    value={form.siglip_label_thresholds}
                    globalValue={form.siglip_threshold}
                    disabled={form.siglip_enabled !== '1'}
                    onChange={next => setForm(f => ({ ...f, siglip_label_thresholds: next }))}
                  />
                </div>
                <Field label={t('settings_siglip_min_hits')} hint={t('settings_siglip_min_hits_hint')}>
                  <input type="number" min="1" max="6" step="1" value={form.siglip_min_hits} onChange={set('siglip_min_hits')} disabled={form.siglip_enabled !== '1'} className={inputCls + ' disabled:opacity-40'} />
                </Field>
                <VerifierPanel value={form.verifier_loss_pct} onChange={v => setForm(f => ({ ...f, verifier_loss_pct: v }))} />
              </div>
            </section>
          )}

          {section === 'segments' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_segments')}</h2>
              <p className="text-xs text-muted mb-4">{t('settings_segments_desc')}</p>
              <div className="grid gap-4 lg:grid-cols-2 items-start">
                <Field label={t('settings_segment_gap')} hint={t('settings_segment_gap_hint')}>
                            <input type="number" min="1000" max="30000" step="500" value={form.segment_gap_ms} onChange={set('segment_gap_ms')} className={inputCls} />
                          </Field>
                <Field label={t('settings_refine_step')} hint={t('settings_refine_step_hint')}>
                            <input type="number" min="0" max="5000" step="100" value={form.refine_step_ms} onChange={set('refine_step_ms')} className={inputCls} />
                          </Field>
                {refineRemaining > 0 && (
                  <p className="text-xs text-gray-400 lg:col-span-2" role="status">
                    {t('settings_refine_remaining', { count: refineRemaining, formatted: formatNumber(refineRemaining) })}
                  </p>
                )}
                <Field label={t('settings_min_hits')} hint={t('settings_min_hits_hint')}>
                            <input type="number" min="1" max="6" step="1" value={form.segment_min_hits} onChange={set('segment_min_hits')} className={inputCls} />
                          </Field>
              </div>
            </section>
          )}

          {section === 'hayaa' && (
            <section>
              <h2 className={h2Cls}>{t('settings_sec_hayaa')}</h2>
              <HayaaPanel />
            </section>
          )}

        </div>
      </div>

      {/* Sticky so Save is reachable from every section; it saves all of them at once.
          Hidden on Users, which saves each change as it is made. */}
      {section !== 'users' && (
      <div className="sticky bottom-0 py-3 bg-plex-darker/95 border-t border-plex-border backdrop-blur">
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={save}
            disabled={saving}
            className="btn-primary px-5 py-2"
          >
            {saving && <Loader2 size={14} className="animate-spin" />}
            {t('settings_save')}
          </button>
          {saved && (
            <span className="flex items-center gap-1.5 text-sm text-green-400">
              <CheckCircle2 size={15} /> {t('common_saved')}
            </span>
          )}
          <button
            onClick={restartScanner}
            disabled={restarting}
            className="btn-outline py-2"
            title={t('settings_restart_scanner_title')}
          >
            <RotateCcw size={14} className={restarting ? 'animate-spin' : ''} />
            {t('settings_restart_scanner')}
          </button>
          {restarted && (
            <span className="flex items-center gap-1.5 text-sm text-green-400">
              <CheckCircle2 size={15} /> {t('settings_restarting_msg')}
            </span>
          )}
        </div>
      </div>
      )}
    </div>
  )
}
