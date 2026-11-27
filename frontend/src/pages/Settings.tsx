import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CheckCircle2, XCircle, Loader2, Eye, EyeOff, RotateCcw } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { UsersPanel } from './Users'
import BulkRescan from '../components/BulkRescan'
import VerifierPanel, { VerifierTraining } from '../components/VerifierPanel'
import EnforcementPanel from '../components/EnforcementPanel'
import HayaaPanel from '../components/HayaaPanel'
import { normalizeRating } from '../utils/ratings'
import { useT } from '../i18n'
import { prettyLabel } from '../utils/labels'

// A content rating in the libraries as Scan ratings compares it (#219); sources are Plex's spellings.
interface LibraryRating { rating: string; count: number; sources: string[] }

interface Settings {
  plex_url: string
  plex_token: string
  // The server never returns the token itself, only whether one is stored (#155).
  plex_token_set?: boolean
  poll_interval: string
  confidence_threshold: string
  pre_buffer_ms: string
  post_buffer_ms: string
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

// Seven sections grouped by what the user is doing (#236). Users, Hayaa and Maintenance save or act
// on their own; the others edit the buffered form and share one Save bar.
type SectionId = 'general' | 'users' | 'playback' | 'schedule' | 'detection' | 'hayaa' | 'maintenance'
const SECTION_IDS: SectionId[] = ['general', 'users', 'playback', 'schedule', 'detection', 'hayaa', 'maintenance']
const BUFFERED: SectionId[] = ['general', 'playback', 'schedule', 'detection']
// Section ids from before #236, so old links (and /users) still open the right place.
const LEGACY_SECTIONS: Record<string, SectionId> = {
  connection: 'general', language: 'general', filtering: 'playback',
  nudenet: 'detection', siglip: 'detection', segments: 'detection',
}

/** The section a ?section= value names, old ids included; anything else opens General. */
export function resolveSection(raw: string | null): SectionId {
  if (raw && (SECTION_IDS as string[]).includes(raw)) return raw as SectionId
  return (raw && LEGACY_SECTIONS[raw]) || 'general'
}

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
  pre_buffer_ms: '3000',
  post_buffer_ms: '3000',
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

// A <label> around the control, so each box is named by its label for screen readers (#238).
function Field({ label, hint, unit, children }: { label: string; hint?: string; unit?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-sm font-medium text-gray-300 mb-1">{label}</span>
      {unit ? (
        <span className="flex items-center gap-2 max-w-xs">{children}<span className="text-xs text-muted flex-shrink-0">{unit}</span></span>
      ) : children}
      {hint && <span className="block text-xs text-muted mt-1">{hint}</span>}
    </label>
  )
}

/** A titled card; every Settings block uses it so spacing and headings match (#236). */
function Card({ title, desc, children }: { title: string; desc?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="bg-hayaa-card border border-hayaa-border rounded-xl p-5">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-1">{title}</h2>
      {desc && <p className="text-xs text-muted mb-4">{desc}</p>}
      <div className={desc ? '' : 'mt-3'}>{children}</div>
    </section>
  )
}

// Per-label threshold overrides stored as a JSON object in one setting. A blank box
// means "inherit the global threshold", so only labels the user typed into are saved.
function LabelThresholds({ detector, labels, value, globalValue, disabled, onChange }: {
  /** Named in each box's label: NudeNet and SigLIP grids share a section (#236). */
  detector: string
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
          <span className="truncate" title={label}>{prettyLabel(label, t)}</span>
          <input
            type="number" min="0" max="1" step="0.05"
            value={map[label] ?? ''}
            placeholder={globalValue}
            disabled={disabled}
            onChange={e => update(label, e.target.value)}
            aria-label={t('settings_label_threshold_aria', { label: prettyLabel(label, t), detector })}
            className="w-24 bg-hayaa-card border border-hayaa-border rounded px-2 py-1 text-sm text-gray-100 disabled:opacity-40"
          />
        </label>
      ))}
    </div>
  )
}

export default function SettingsPage() {
  const { t, formatNumber, lang, setLang } = useT()
  const [form, setForm] = useState<Settings>(DEFAULT)
  // What the server last confirmed; the Save bar shows while form differs from it.
  const [savedForm, setSavedForm] = useState<Settings>(DEFAULT)
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
  const section = resolveSection(params.get('section'))
  const setSection = (id: SectionId) => setParams(id === 'general' ? {} : { section: id }, { replace: true })

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
      setSavedForm({ ...DEFAULT, ...settings })
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
      // The token field is write-only: once saved, it is stored and the box goes back to blank.
      const next = { ...form, plex_token: '', plex_token_set: form.plex_token_set || !!form.plex_token }
      setForm(next)
      setSavedForm(next)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } finally {
      setSaving(false)
    }
  }

  const testConnection = async () => {
    setTesting(true)
    setTestResult(null)
    // Tests what is typed without saving it (#236); a blank token means the stored one.
    try {
      const r = await api.post<{ ok: boolean; message: string }>('/api/settings/test-connection', {
        plex_url: form.plex_url,
        plex_token: form.plex_token,
      })
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

  const inputCls = "w-full px-3 py-2 bg-hayaa-darker border border-hayaa-border rounded-lg text-sm text-gray-100 placeholder-muted focus:border-hayaa-gold/60 transition-colors"
  // Numbers and times are short; a full-width box makes them look like free text.
  const numCls = inputCls + ' max-w-xs disabled:opacity-40'
  const timeCls = inputCls + ' w-36'

  if (loading) return <div className="text-muted text-sm">{t('common_loading_dots')}</div>

  const dirty = (Object.keys(form) as (keyof Settings)[]).some(k => k !== 'plex_token_set' && form[k] !== savedForm[k])
  const discard = () => { setForm(savedForm); setTestResult(null) }

  const sections: { id: SectionId; label: string }[] = [
    { id: 'general', label: t('settings_sec_general') },
    { id: 'users', label: t('settings_sec_users') },
    { id: 'playback', label: t('settings_sec_playback_enforcement') },
    { id: 'schedule', label: t('settings_sec_library_schedule') },
    { id: 'detection', label: t('settings_sec_detection') },
    { id: 'hayaa', label: t('settings_sec_hayaa_sync') },
    { id: 'maintenance', label: t('settings_sec_maintenance') },
  ]

  const checkbox = (key: keyof Settings, label: string, hint?: string, disabled?: boolean) => (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={key === 'siglip_enabled' || key === 'ignore_credits' ? form[key] === '1' : form[key] !== '0'}
        onChange={e => setForm(f => ({ ...f, [key]: e.target.checked ? '1' : '0' }))}
        disabled={disabled}
        className="w-4 h-4 mt-0.5 accent-hayaa-gold disabled:opacity-40"
      />
      <span className="text-sm text-gray-300">
        {label}
        {hint && <span className="block text-xs text-muted">{hint}</span>}
      </span>
    </label>
  )

  const timeRange = (startKey: keyof Settings, endKey: keyof Settings, disabled?: boolean) => (
    <div className="flex items-end gap-3">
      <Field label={t('settings_start_time')}>
        <input type="time" value={form[startKey] as string} onChange={set(startKey)} disabled={disabled} className={timeCls + ' disabled:opacity-40'} />
      </Field>
      <span className="pb-2 text-muted" aria-hidden="true">–</span>
      <Field label={t('settings_end_time')}>
        <input type="time" value={form[endKey] as string} onChange={set(endKey)} disabled={disabled} className={timeCls + ' disabled:opacity-40'} />
      </Field>
    </div>
  )

  const siglipOff = form.siglip_enabled !== '1'

  return (
    <div className="w-full">
      <h1 className="text-2xl font-bold text-gray-100 mb-4">{t('settings_title')}</h1>
      <div className="flex flex-col md:flex-row gap-6">
        {/* Section nav: a column on desktop, a scrolling row on phones. */}
        <nav aria-label={t('settings_nav_aria')} className="flex md:flex-col gap-1 md:w-52 flex-shrink-0 overflow-x-auto">
          {sections.map(sec => (
            <button
              key={sec.id}
              onClick={() => setSection(sec.id)}
              aria-current={section === sec.id ? 'page' : undefined}
              className={`text-start whitespace-nowrap px-3 py-2 rounded-lg text-sm transition-colors ${
                section === sec.id ? 'bg-hayaa-gold/20 text-hayaa-gold' : 'text-gray-300 hover:text-gray-100 hover:bg-white/5'
              }`}
            >
              {sec.label}
            </button>
          ))}
        </nav>

        {/* One column of cards (#236): settings read top to bottom, no ragged two-column gaps. */}
        <div className="flex-1 min-w-0 max-w-4xl space-y-6 pb-6">
          {section === 'general' && (
            <>
              <Card title={t('settings_sec_connection')}>
                <div className="space-y-4">
                  <Field label={t('settings_plex_url')} hint={t('settings_plex_url_hint')}>
                    <input type="url" dir="ltr" value={form.plex_url} onChange={set('plex_url')} placeholder="http://localhost:32400" className={inputCls + ' max-w-lg'} />
                  </Field>
                  <Field label={t('settings_plex_token')} hint={t('settings_plex_token_hint')}>
                    <div className="relative max-w-lg">
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
                        aria-label={showToken ? t('settings_hide_token') : t('settings_show_token')}
                        className="absolute end-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-300"
                      >
                        {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                  </Field>
                  <div className="flex flex-wrap items-center gap-3">
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
                  <p className="text-xs text-muted">{t('settings_test_connection_hint')}</p>
                </div>
              </Card>
              <Card title={t('settings_sec_language')} desc={t('settings_language_hint')}>
                <div role="group" aria-label={t('lang_toggle_label')} className="inline-flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-hayaa-border">
                  {(['en', 'ar'] as const).map(code => (
                    <button
                      key={code}
                      type="button"
                      onClick={() => setLang(code)}
                      aria-pressed={lang === code}
                      className={`py-1.5 px-4 rounded-md text-sm font-medium transition-colors ${lang === code ? 'bg-hayaa-gold text-black' : 'text-gray-400 hover:text-gray-100'}`}
                    >
                      {code === 'en' ? t('lang_english') : t('lang_arabic')}
                    </button>
                  ))}
                </div>
              </Card>
            </>
          )}

          {section === 'users' && (
            <section className="bg-hayaa-card border border-hayaa-border rounded-xl p-5">
              <UsersPanel />
            </section>
          )}

          {section === 'playback' && (
            <>
              <Card title={t('settings_sec_filtering')} desc={t('settings_filtering_desc')}>
                <EnforcementPanel />
              </Card>
              <Card title={t('settings_stream_check')} desc={t('settings_stream_check_desc')}>
                <Field label={t('settings_stream_check_interval')} hint={t('settings_stream_check_interval_hint')} unit={t('unit_seconds_short')}>
                  <input type="number" min="2" max="30" value={form.poll_interval} onChange={set('poll_interval')} className={numCls} />
                </Field>
              </Card>
              {/* Seek-based skipping is the fallback for streams the front door did not edit;
                  edited streams are cut at the exact segment edges and use no buffer (#238). */}
              <Card title={t('settings_fallback_skip')} desc={t('settings_fallback_skip_desc')}>
                <div className="space-y-4">
                  <Field label={t('settings_seek_before')} hint={t('settings_seek_before_hint')} unit={t('unit_ms_short')}>
                    <input type="number" min="0" max="30000" step="500" value={form.pre_buffer_ms} onChange={set('pre_buffer_ms')} className={numCls} />
                  </Field>
                  <Field label={t('settings_seek_after')} hint={t('settings_seek_after_hint')} unit={t('unit_ms_short')}>
                    <input type="number" min="0" max="30000" step="500" value={form.post_buffer_ms} onChange={set('post_buffer_ms')} className={numCls} />
                  </Field>
                </div>
              </Card>
            </>
          )}

          {section === 'schedule' && (
            <>
              <Card title={t('settings_schedule_windows')} desc={t('settings_schedule_windows_desc')}>
                <div className="space-y-5">
                  <div>
                    <p className="text-sm font-medium text-gray-200 mb-2">{t('settings_scan_schedule')}</p>
                    {timeRange('scan_window_start', 'scan_window_end')}
                  </div>
                  <div className="border-t border-hayaa-border pt-5 space-y-3">
                    {checkbox('hover_previews_enabled', t('settings_hover_enable'), t('settings_hover_previews_short'))}
                    <div className="ps-7 space-y-3">
                      {timeRange('hover_window_start', 'hover_window_end', form.hover_previews_enabled === '0')}
                      {checkbox('hover_when_idle', t('settings_hover_when_idle'), t('settings_hover_when_idle_hint'), form.hover_previews_enabled === '0')}
                    </div>
                  </div>
                </div>
              </Card>

              {libraries.length > 0 && (
                <Card title={t('settings_libraries_to_scan')} desc={t('settings_libraries_to_scan_desc')}>
                  <div className="grid sm:grid-cols-2 gap-2">
                    {/* Stored as excluded_library_ids; shown inverted because ticking what is included reads plainer. */}
                    {libraries.map(lib => {
                      const included = !excludedIds.includes(lib.id)
                      return (
                        <label key={lib.id} className="flex items-center gap-3 cursor-pointer">
                          <input type="checkbox" checked={included} onChange={() => toggleExcluded(lib.id)} className="w-4 h-4 accent-hayaa-gold" />
                          <span className={`text-sm ${included ? 'text-gray-200' : 'text-muted'}`}>{lib.title}</span>
                          <span className="text-xs text-muted">
                            {lib.type === 'movie' ? t('settings_lib_type_movie') : lib.type === 'show' ? t('settings_lib_type_show') : lib.type}
                          </span>
                        </label>
                      )
                    })}
                  </div>
                </Card>
              )}

              <Card title={t('settings_scan_ratings')} desc={t('settings_scan_ratings_desc')}>
                {/* The ratings the libraries hold (#219), normalized the way the scanner compares them, so
                    "eg/16" is under R. A checked rating no title has any more stays listed so it can be unchecked.
                    "" is titles Plex left unrated. */}
                {(() => {
                  const selected: string[] = (() => { try { return JSON.parse(form.scan_ratings) } catch { return [] } })()
                  const selectedNorm = new Set(selected.map(normalizeRating))
                  const rows = [...libraryRatings]
                  for (const r of selectedNorm) if (!rows.some(x => x.rating === r)) rows.push({ rating: r, count: 0, sources: [] })
                  rows.sort((x, y) => (x.rating === '' ? 1 : y.rating === '' ? -1 : x.rating.localeCompare(y.rating)))
                  if (rows.length === 0) return <p className="text-sm text-muted">{t('settings_no_titles_yet')}</p>
                  const aliases = rows.filter(r => r.sources.some(s => s !== r.rating))
                  return (
                    <>
                      <div className="flex flex-wrap gap-2">
                        {rows.map(({ rating, count }) => {
                          const checked = selectedNorm.has(rating)
                          const toggle = () => {
                            // Saved values are stored normalized; older spellings of the same rating are dropped with it.
                            const rest = selected.filter(r => normalizeRating(r) !== rating)
                            setForm(f => ({ ...f, scan_ratings: JSON.stringify(checked ? rest : [...rest, rating]) }))
                          }
                          return (
                            <button
                              key={rating === '' ? '__unrated__' : rating}
                              type="button"
                              onClick={toggle}
                              aria-pressed={checked}
                              className={`px-3 py-1 rounded-full border text-sm transition-colors ${
                                checked ? 'border-hayaa-gold/60 bg-hayaa-gold/15 text-gray-100' : 'border-hayaa-border text-gray-400 hover:text-gray-200'
                              }`}
                            >
                              {rating === '' ? t('settings_unrated_plex') : rating}
                              <span className="ms-1.5 text-xs text-muted tabular-nums">{count}</span>
                            </button>
                          )
                        })}
                      </div>
                      {aliases.length > 0 && (
                        <details className="mt-4 text-xs text-muted">
                          <summary className="cursor-pointer hover:text-gray-300">{t('settings_rating_aliases')}</summary>
                          <ul className="mt-2 space-y-1">
                            {aliases.map(r => (
                              <li key={r.rating || '__unrated__'}>
                                <span className="text-gray-300">{r.rating === '' ? t('settings_unrated_plex') : r.rating}</span>
                                {' ← '}<span dir="ltr">{r.sources.filter(s => s !== r.rating).join(', ')}</span>
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </>
                  )
                })()}
              </Card>
            </>
          )}

          {section === 'detection' && (
            <>
              <Card title={t('settings_performance')}>
                <div className="space-y-4">
                  <Field label={t('settings_scan_workers')} hint={t('settings_scan_workers_hint')}>
                    <input type="number" min="1" max="12" step="1" value={form.scan_workers} onChange={set('scan_workers')} className={numCls} />
                  </Field>
                  {checkbox('hw_decode', t('settings_hw_decode'), t('settings_hw_decode_hint'))}
                </div>
              </Card>

              <Card title={t('settings_nudenet_model')}>
                <div className="space-y-4">
                  <Field label={t('settings_nudenet_model')} hint={t('settings_nudenet_model_hint')}>
                    <select value={form.nudenet_model} onChange={set('nudenet_model')} className={inputCls + ' max-w-sm'}>
                      <option value="320n">{t('settings_nudenet_320n')}</option>
                      <option value="640m">{t('settings_nudenet_640m')}</option>
                    </select>
                  </Field>
                  <Field label={t('settings_confidence_threshold')} hint={t('settings_confidence_threshold_hint')}>
                    <input type="number" min="0.1" max="1" step="0.05" value={form.confidence_threshold} onChange={set('confidence_threshold')} className={numCls} />
                  </Field>
                  <Field label={t('settings_scan_step')} hint={t('settings_scan_step_hint')} unit={t('unit_ms_short')}>
                    <input type="number" min="1000" max="20000" step="500" value={form.scan_step_ms} onChange={set('scan_step_ms')} className={numCls} />
                  </Field>
                </div>
              </Card>

              <Card title={t('settings_detector_labels')} desc={t('settings_detector_labels_desc')}>
                <div className="grid sm:grid-cols-2 gap-2">
                  {detectorLabels.map(label => {
                    const checked = enabledLabels.includes(label)
                    const toggle = () => {
                      const next = checked ? enabledLabels.filter(r => r !== label) : [...enabledLabels, label]
                      setForm(f => ({ ...f, scan_labels: JSON.stringify(next) }))
                    }
                    return (
                      <label key={label} className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-hayaa-gold" />
                        <span className={`text-sm ${checked ? 'text-gray-100' : 'text-muted'}`} title={label}>{prettyLabel(label, t)}</span>
                      </label>
                    )
                  })}
                </div>
              </Card>

              <Card title={t('settings_per_label_thresholds')} desc={t('settings_per_label_thresholds_desc')}>
                <LabelThresholds
                  detector="NudeNet"
                  labels={enabledLabels}
                  value={form.label_thresholds}
                  globalValue={form.confidence_threshold}
                  onChange={next => setForm(f => ({ ...f, label_thresholds: next }))}
                />
              </Card>

              <Card
                title={t('settings_sec_siglip')}
                desc={<>{t('settings_siglip_desc_before')} <span className="text-gray-300">siglip</span>{t('settings_siglip_desc_after')}</>}
              >
                <div className="space-y-4">
                  {checkbox('siglip_enabled', t('settings_siglip_enable'))}
                  <Field label={t('settings_siglip_threshold')} hint={t('settings_siglip_threshold_hint')}>
                    <input type="number" min="0.05" max="0.95" step="0.05" value={form.siglip_threshold} onChange={set('siglip_threshold')} disabled={siglipOff} className={numCls} />
                  </Field>
                  <Field label={t('settings_siglip_min_hits')} hint={t('settings_siglip_min_hits_hint')}>
                    <input type="number" min="1" max="6" step="1" value={form.siglip_min_hits} onChange={set('siglip_min_hits')} disabled={siglipOff} className={numCls} />
                  </Field>
                  {checkbox('ignore_credits', t('settings_ignore_credits'), undefined, siglipOff)}
                  <div>
                    <p className="text-sm font-medium text-gray-300 mb-1">{t('settings_siglip_per_label')}</p>
                    <p className="text-xs text-muted mb-2">{t('settings_siglip_per_label_desc')}</p>
                    <LabelThresholds
                      detector="SigLIP"
                      labels={enabledLabels}
                      value={form.siglip_label_thresholds}
                      globalValue={form.siglip_threshold}
                      disabled={siglipOff}
                      onChange={next => setForm(f => ({ ...f, siglip_label_thresholds: next }))}
                    />
                  </div>
                </div>
              </Card>

              <Card title={t('settings_sec_segments')} desc={t('settings_segments_desc')}>
                <div className="space-y-4">
                  <Field label={t('settings_segment_gap')} hint={t('settings_segment_gap_hint')} unit={t('unit_ms_short')}>
                    <input type="number" min="1000" max="30000" step="500" value={form.segment_gap_ms} onChange={set('segment_gap_ms')} className={numCls} />
                  </Field>
                  <Field label={t('settings_min_hits')} hint={t('settings_min_hits_hint')}>
                    <input type="number" min="1" max="6" step="1" value={form.segment_min_hits} onChange={set('segment_min_hits')} className={numCls} />
                  </Field>
                  <Field label={t('settings_refine_step')} hint={t('settings_refine_step_hint')} unit={t('unit_ms_short')}>
                    <input type="number" min="0" max="5000" step="100" value={form.refine_step_ms} onChange={set('refine_step_ms')} className={numCls} />
                  </Field>
                  {refineRemaining > 0 && (
                    <p className="text-xs text-gray-400" role="status">
                      {t('settings_refine_remaining', { count: refineRemaining, formatted: formatNumber(refineRemaining) })}
                    </p>
                  )}
                  <VerifierPanel value={form.verifier_loss_pct} onChange={v => setForm(f => ({ ...f, verifier_loss_pct: v }))} />
                </div>
              </Card>
            </>
          )}

          {section === 'hayaa' && (
            <Card title={t('settings_sec_hayaa')}>
              <HayaaPanel />
            </Card>
          )}

          {section === 'maintenance' && (
            <>
              <Card title={t('settings_rescan_titles')} desc={t('settings_rescan_titles_desc')}>
                <BulkRescan libraries={libraries} />
              </Card>
              <Card title={t('settings_restart_scanner')} desc={t('settings_restart_scanner_title')}>
                <div className="flex flex-wrap items-center gap-3">
                  <button onClick={restartScanner} disabled={restarting} className="btn-outline text-sm">
                    <RotateCcw size={14} className={restarting ? 'animate-spin' : ''} />
                    {t('settings_restart_scanner')}
                  </button>
                  {restarted && (
                    <span className="flex items-center gap-1.5 text-sm text-green-400">
                      <CheckCircle2 size={15} /> {t('settings_restarting_msg')}
                    </span>
                  )}
                </div>
              </Card>
              <Card title={t('settings_prepare_640m')} desc={t('settings_prepare_640m_hint')}>
                <div className="flex flex-wrap items-center gap-3">
                  <button type="button" onClick={validateModelPath} disabled={validatingModel} className="btn-outline text-sm">
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
              </Card>
              <Card title={t('ver_training_title')} desc={t('ver_training_desc')}>
                <VerifierTraining />
              </Card>
            </>
          )}
        </div>
      </div>

      {/* Shown only while a buffered section has unsaved edits (#236); Users, Hayaa and
          Maintenance save or act on their own. */}
      {(dirty || saved) && BUFFERED.includes(section) && (
        <div className="sticky bottom-0 py-3 bg-hayaa-darker/95 border-t border-hayaa-border backdrop-blur">
          <div className="flex flex-wrap items-center gap-3 max-w-4xl md:ms-[14.5rem]">
            {dirty ? (
              <>
                <span className="text-sm text-gray-300 me-auto">{t('settings_unsaved')}</span>
                <button onClick={discard} disabled={saving} className="btn-outline text-sm">{t('settings_discard')}</button>
                <button onClick={save} disabled={saving} className="btn-primary px-5 py-2">
                  {saving && <Loader2 size={14} className="animate-spin" />}
                  {t('settings_save')}
                </button>
              </>
            ) : (
              <span className="flex items-center gap-1.5 text-sm text-green-400" role="status">
                <CheckCircle2 size={15} /> {t('common_saved')}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
