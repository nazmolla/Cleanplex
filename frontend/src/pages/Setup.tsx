import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import HayaaPanel from '../components/HayaaPanel'
import { useT, type TranslationKey } from '../i18n'
import hayaaIcon from '../assets/hayaa-icon-64.png'

/**
 * First-run setup (#245): connect to Plex on this computer, pick libraries, pick who is
 * filtered and how much, optionally join Hayaa community sync. Shown until it finishes.
 */

type Step = 'welcome' | 'plex' | 'libraries' | 'people' | 'community' | 'done'
const STEPS: Step[] = ['welcome', 'plex', 'libraries', 'people', 'community', 'done']

interface Library { id: string; title: string; type: string }
interface PlexUser { username: string; thumb: string; enabled: boolean }

// Presets map onto the per-category levels (0 off .. 4 everything); "everything" is the
// default (no saved levels), so it is applied by clearing them.
export type Preset = 'everything' | 'nudity_language' | 'nudity'
const PRESET_LEVELS: Record<Exclude<Preset, 'everything'>, Record<string, number>> = {
  nudity_language: { nudity: 4, sex: 4, language: 4 },
  nudity: { nudity: 4, sex: 4 },
}
const CATEGORIES = ['commercial', 'discrimination', 'dispensable', 'drugs', 'fear', 'language',
  'nudity', 'sex', 'violence', 'other']

export async function applyPreset(username: string, preset: Preset) {
  const name = encodeURIComponent(username)
  await api.delete(`/api/users/${name}/categories`)
  if (preset === 'everything') return
  const levels = PRESET_LEVELS[preset]
  for (const category of CATEGORIES) {
    await api.put(`/api/users/${name}/categories/${category}`, { level: levels[category] ?? 0, action: '' })
  }
}

type PlexState =
  | { kind: 'checking' }
  | { kind: 'connected'; name: string }
  | { kind: 'error'; reason: string }
  | { kind: 'signing_in' }

const REASONS: Record<string, TranslationKey> = {
  not_found: 'setup_err_not_found',
  unreachable: 'setup_err_unreachable',
  sign_in_needed: 'setup_err_sign_in_needed',
  not_on_this_machine: 'setup_err_not_on_this_machine',
  no_server: 'setup_err_no_server',
  expired: 'setup_err_expired',
  plex_tv: 'setup_err_plex_tv',
}

export default function Setup() {
  const { t } = useT()
  const navigate = useNavigate()
  const [step, setStep] = useState<Step>('welcome')
  const [plex, setPlex] = useState<PlexState>({ kind: 'checking' })
  const [libraries, setLibraries] = useState<Library[]>([])
  const [excluded, setExcluded] = useState<string[]>([])
  const [people, setPeople] = useState<PlexUser[]>([])
  const [presets, setPresets] = useState<Record<string, Preset>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [scanWindow, setScanWindow] = useState('23:00')
  const pollRef = useRef<number | null>(null)

  useEffect(() => { api.post('/api/setup/start').catch(() => {}) }, [])
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  const detect = useCallback(async () => {
    setPlex({ kind: 'checking' })
    try {
      const status = await api.get<{ plex_connected: boolean; server_name: string }>('/api/setup/status')
      if (status.plex_connected) { setPlex({ kind: 'connected', name: status.server_name }); return }
      const r = await api.post<{ ok: boolean; server_name?: string; reason?: string }>('/api/setup/detect')
      setPlex(r.ok ? { kind: 'connected', name: r.server_name ?? '' } : { kind: 'error', reason: r.reason ?? 'not_found' })
    } catch {
      setPlex({ kind: 'error', reason: 'unreachable' })
    }
  }, [])

  useEffect(() => { if (step === 'plex') detect() }, [step, detect])

  const signInWithPlex = async () => {
    setPlex({ kind: 'signing_in' })
    // Open the window first, inside the click, so pop-up blockers allow it.
    const win = window.open('about:blank', '_blank')
    try {
      const pin = await api.post<{ id: number; auth_url: string }>('/api/setup/plex/pin')
      if (win) win.location.href = pin.auth_url
      else window.open(pin.auth_url, '_blank')
      const started = Date.now()
      pollRef.current = window.setInterval(async () => {
        if (Date.now() - started > 10 * 60_000) {
          window.clearInterval(pollRef.current!)
          setPlex({ kind: 'error', reason: 'expired' })
          return
        }
        try {
          const r = await api.get<{ status: string; server_name?: string; reason?: string }>(`/api/setup/plex/pin/${pin.id}`)
          if (r.status === 'pending') return
          window.clearInterval(pollRef.current!)
          if (r.status === 'linked') setPlex({ kind: 'connected', name: r.server_name ?? '' })
          else setPlex({ kind: 'error', reason: r.reason ?? r.status })
        } catch { /* keep polling through a blip */ }
      }, 2000)
    } catch {
      win?.close()
      setPlex({ kind: 'error', reason: 'plex_tv' })
    }
  }

  const loadLibraries = async () => {
    const [libs, settings] = await Promise.all([
      api.get<{ libraries: Library[] }>('/api/libraries').catch(() => ({ libraries: [] })),
      api.get<Record<string, string>>('/api/settings').catch(() => ({} as Record<string, string>)),
    ])
    setLibraries(libs.libraries)
    try { setExcluded(JSON.parse(settings.excluded_library_ids || '[]')) } catch { setExcluded([]) }
    if (settings.scan_window_start) setScanWindow(settings.scan_window_start)
  }

  const loadPeople = async () => {
    const r = await api.get<{ users: PlexUser[] }>('/api/users').catch(() => ({ users: [] }))
    setPeople(r.users)
    setPresets(Object.fromEntries(r.users.map(u => [u.username, 'everything' as Preset])))
  }

  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError('')
    try { await fn() } catch { setError(t('setup_save_error')) } finally { setBusy(false) }
  }

  const next = () => run(async () => {
    if (step === 'plex') await loadLibraries()
    if (step === 'libraries') {
      await api.put('/api/settings', { excluded_library_ids: JSON.stringify(excluded) })
      await loadPeople()
    }
    if (step === 'people') {
      for (const p of people) {
        await api.put(`/api/users/${encodeURIComponent(p.username)}`, { enabled: p.enabled })
        if (p.enabled) await applyPreset(p.username, presets[p.username] ?? 'everything')
      }
    }
    setStep(STEPS[STEPS.indexOf(step) + 1])
  })

  const back = () => setStep(STEPS[Math.max(0, STEPS.indexOf(step) - 1)])

  const finish = () => run(async () => {
    await api.post('/api/setup/finish')
    navigate('/', { replace: true })
  })

  const canContinue = step !== 'plex' || plex.kind === 'connected'
  const stepIndex = STEPS.indexOf(step)

  return (
    <div className="min-h-screen bg-hayaa-darker text-gray-100 flex items-start sm:items-center justify-center p-4">
      <div className="w-full max-w-xl bg-hayaa-card border border-hayaa-border rounded-2xl p-6 sm:p-8 shadow-xl">
        <div className="flex items-center gap-3 mb-6">
          <img src={hayaaIcon} alt="" className="h-10 w-10 rounded-xl" />
          <div>
            <div className="text-hayaa-gold font-bold text-lg">{t('brand_name')}</div>
            <div className="text-xs text-muted">{t('setup_step_of', { n: stepIndex + 1, total: STEPS.length })}</div>
          </div>
        </div>
        <div className="flex gap-1.5 mb-6" aria-hidden="true">
          {STEPS.map((s, i) => (
            <div key={s} className={`h-1 flex-1 rounded ${i <= stepIndex ? 'bg-hayaa-gold' : 'bg-hayaa-border'}`} />
          ))}
        </div>

        {step === 'welcome' && (
          <section>
            <h1 className="text-2xl font-semibold mb-3">{t('setup_welcome_title')}</h1>
            <p className="text-gray-300 mb-3">{t('setup_welcome_body')}</p>
            <p className="text-sm text-muted">{t('setup_welcome_time')}</p>
          </section>
        )}

        {step === 'plex' && (
          <section>
            <h1 className="text-2xl font-semibold mb-3">{t('setup_plex_title')}</h1>
            {plex.kind === 'checking' && (
              <p className="flex items-center gap-2 text-gray-300"><Loader2 size={16} className="animate-spin" /> {t('setup_plex_checking')}</p>
            )}
            {plex.kind === 'signing_in' && (
              <p className="flex items-center gap-2 text-gray-300"><Loader2 size={16} className="animate-spin" /> {t('setup_plex_waiting')}</p>
            )}
            {plex.kind === 'connected' && (
              <p className="flex items-center gap-2 text-green-400"><CheckCircle2 size={18} /> {t('setup_plex_connected', { name: plex.name })}</p>
            )}
            {plex.kind === 'error' && (
              <div className="space-y-4">
                <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200">
                  <AlertTriangle size={18} className="flex-shrink-0 mt-0.5" />
                  <span>{t(REASONS[plex.reason] ?? 'setup_err_unreachable')}</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn-outline text-sm" onClick={detect}>{t('setup_try_again')}</button>
                  {plex.reason !== 'not_on_this_machine' && plex.reason !== 'not_found' && (
                    <button type="button" className="btn-primary text-sm" onClick={signInWithPlex}>
                      <ExternalLink size={14} /> {t('setup_sign_in_plex')}
                    </button>
                  )}
                </div>
                <p className="text-xs text-muted">{t('setup_plex_same_computer')}</p>
              </div>
            )}
          </section>
        )}

        {step === 'libraries' && (
          <section>
            <h1 className="text-2xl font-semibold mb-2">{t('setup_libraries_title')}</h1>
            <p className="text-sm text-gray-300 mb-4">{t('setup_libraries_body')}</p>
            {libraries.length === 0 && <p className="text-sm text-muted">{t('setup_libraries_none')}</p>}
            <div className="space-y-2">
              {libraries.map(lib => {
                const included = !excluded.includes(lib.id)
                return (
                  <label key={lib.id} className="flex items-center gap-3 cursor-pointer rounded-lg border border-hayaa-border px-3 py-2">
                    <input type="checkbox" checked={included} className="w-4 h-4 accent-hayaa-gold"
                      onChange={() => setExcluded(x => included ? [...x, lib.id] : x.filter(id => id !== lib.id))} />
                    <span className="text-sm">{lib.title}</span>
                  </label>
                )
              })}
            </div>
          </section>
        )}

        {step === 'people' && (
          <section>
            <h1 className="text-2xl font-semibold mb-2">{t('setup_people_title')}</h1>
            <p className="text-sm text-gray-300 mb-4">{t('setup_people_body')}</p>
            {people.length === 0 && <p className="text-sm text-muted">{t('setup_people_none')}</p>}
            <div className="space-y-2">
              {people.map(p => (
                <div key={p.username} className="flex flex-wrap items-center gap-3 rounded-lg border border-hayaa-border px-3 py-2">
                  <label className="flex flex-1 min-w-[10rem] items-center gap-3 cursor-pointer">
                    <input type="checkbox" checked={p.enabled} className="w-4 h-4 accent-hayaa-gold"
                      onChange={() => setPeople(ps => ps.map(x => x.username === p.username ? { ...x, enabled: !x.enabled } : x))} />
                    <span className="text-sm">{p.username}</span>
                  </label>
                  {p.enabled && (
                    <select aria-label={t('setup_people_what', { name: p.username })}
                      value={presets[p.username] ?? 'everything'}
                      onChange={e => setPresets(ps => ({ ...ps, [p.username]: e.target.value as Preset }))}
                      className="bg-hayaa-dark border border-hayaa-border rounded-lg px-2 py-1 text-sm">
                      <option value="everything">{t('setup_preset_everything')}</option>
                      <option value="nudity_language">{t('setup_preset_nudity_language')}</option>
                      <option value="nudity">{t('setup_preset_nudity')}</option>
                    </select>
                  )}
                </div>
              ))}
            </div>
            <p className="text-xs text-muted mt-3">{t('setup_people_later')}</p>
          </section>
        )}

        {step === 'community' && (
          <section>
            <h1 className="text-2xl font-semibold mb-2">{t('setup_community_title')}</h1>
            <p className="text-sm text-gray-300 mb-4">{t('setup_community_body')}</p>
            <HayaaPanel />
            <p className="text-xs text-muted mt-3">{t('setup_community_optional')}</p>
          </section>
        )}

        {step === 'done' && (
          <section>
            <h1 className="text-2xl font-semibold mb-3">{t('setup_done_title')}</h1>
            <p className="text-gray-300 mb-3">{t('setup_done_body', { time: scanWindow })}</p>
            <p className="text-sm text-muted">{t('setup_done_settings')}</p>
          </section>
        )}

        {error && <p role="alert" className="mt-4 text-sm text-red-300">{error}</p>}

        <div className="mt-8 flex items-center justify-between gap-3">
          {stepIndex > 0 && step !== 'done'
            ? <button type="button" className="btn-outline text-sm" onClick={back} disabled={busy}>{t('setup_back')}</button>
            : <span />}
          {step === 'done' ? (
            <button type="button" className="btn-primary" onClick={finish} disabled={busy}>{t('setup_finish')}</button>
          ) : (
            <button type="button" className="btn-primary" onClick={next} disabled={busy || !canContinue}>
              {busy && <Loader2 size={14} className="animate-spin" />}
              {step === 'welcome' ? t('setup_start') : step === 'community' ? t('setup_continue_or_skip') : t('setup_next')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
