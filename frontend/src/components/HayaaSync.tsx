import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import { toast } from './Toast'
import { useT, type TranslateFn, type TranslationKey } from '../i18n'

interface SyncResult {
  pushed: number
  retracted: number
  described: number
  pulled: number
  changed: number
  applied?: number
  removed?: number
  hidden?: number
  held?: number
  skipped_quota: boolean
  proved?: number
  deferred?: Record<string, number>
  error: string
  pull_pending?: number
}

// Why Hayaa held titles back (docs/ABUSE_RESISTANCE.md in the Hayaa repo).
const DEFERRED_KEYS: Record<string, TranslationKey> = {
  account_too_new: 'hayaa_def_account_too_new',
  contribute_more: 'hayaa_def_contribute_more',
  not_contributed: 'hayaa_def_not_contributed',
  scan_time: 'hayaa_def_scan_time',
  prove_possession: 'hayaa_def_prove_possession',
  proof_failed: 'hayaa_def_proof_failed',
  budget: 'hayaa_def_budget',
  lifetime_cap: 'hayaa_def_lifetime_cap',
}

function deferredText(d: Record<string, number> | undefined, t: TranslateFn): string {
  const parts = Object.entries(d ?? {}).map(([k, n]) => `${n} ${DEFERRED_KEYS[k] ? t(DEFERRED_KEYS[k]) : k}`)
  return parts.length ? t('hayaa_held_back', { parts: parts.join(', ') }) : ''
}

interface SyncStatus {
  running: boolean
  phase: string
  auto_sync: boolean
  interval_h: number
  preset: 'balanced' | 'cautious' | 'verify_first'
  preset_auto_switched: boolean
  last_sync: number | null
  last_result: SyncResult | null
  paused_until: number | null
  quota: { pushes: number; pulls: number; pushes_limit: number; pulls_limit: number }
  pending_titles?: number
  held: number
  verify_pending: number
}

interface HeldClaim {
  title_key: string
  claim_id: string
  title: string
  start_ms: number
  end_ms: number
  status: string
  basis: string
  labels: string
  severity: string
  supporters: number
  rejecters: number
  verify_state: string
}

interface CommunityFp {
  id: number
  title: string
  start_ms: number
  end_ms: number
  labels: string
  severity: string
}

const PRESETS = [
  { value: 'balanced', labelKey: 'hayaa_preset_balanced' as const },
  { value: 'cautious', labelKey: 'hayaa_preset_cautious' as const },
  { value: 'verify_first', labelKey: 'hayaa_preset_verify_first' as const },
]

const POLL_MS = 2000
const MAX_POLLS = 900 // 30 minutes; a first sync identifies every file

function clock(ms: number) {
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

/**
 * Sync with Hayaa (#222): a manual sync, opt-in automatic sync, the apply preset, and the two
 * lists a user acts on: pulled claims held for review, and local segments the community rejected.
 */
export default function HayaaSync() {
  const { t, formatDateTime, formatTime } = useT()
  const [st, setSt] = useState<SyncStatus | null>(null)
  const [held, setHeld] = useState<HeldClaim[]>([])
  const [fps, setFps] = useState<CommunityFp[]>([])

  const load = useCallback(async (signal?: AbortSignal) => {
    const [s, r] = await Promise.all([
      api.get<SyncStatus>('/api/hayaa/sync', { signal }),
      api.get<{ held: HeldClaim[]; community_false_positives: CommunityFp[] }>('/api/hayaa/review', { signal }),
    ])
    setSt(s)
    setHeld(r.held)
    setFps(r.community_false_positives)
    return s
  }, [])

  useEffect(() => { load().catch(() => toast.error(t('hayaa_sync_load_error'))) }, [load, t])

  // While a sync runs, poll until it stops; MAX_POLLS bounds it if the server never reports done.
  useEffect(() => {
    if (!st?.running) return
    let ctrl: AbortController | null = null
    let polls = 0
    const id = setInterval(() => {
      if (++polls > MAX_POLLS) { clearInterval(id); return }
      ctrl?.abort()
      ctrl = new AbortController()
      load(ctrl.signal).then(s => {
        if (!s.running) {
          const r = s.last_result
          if (r?.error) toast.error(t('hayaa_sync_toast_error', { error: r.error }))
          else toast.success(t('hayaa_sync_toast_done', { pushed: r?.pushed ?? 0, changed: r?.changed ?? 0 }))
        }
      }).catch(() => {})
    }, POLL_MS)
    return () => { clearInterval(id); ctrl?.abort() }
  }, [st?.running, load, t])

  const save = async (body: Partial<{ auto_sync: boolean; interval_h: number; preset: string }>) => {
    try {
      setSt(await api.put<SyncStatus>('/api/hayaa/sync', body))
    } catch (e) {
      toast.error(t('hayaa_save_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }

  const syncNow = async (force = false) => {
    try {
      setSt(await api.post<SyncStatus>(`/api/hayaa/sync${force ? '?force=true' : ''}`))
    } catch (e) {
      toast.error(t('hayaa_start_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }

  const decide = async (c: HeldClaim, keep: boolean) => {
    try {
      await api.post('/api/hayaa/review', { title_key: c.title_key, claim_id: c.claim_id, keep })
      setHeld(h => h.filter(x => !(x.title_key === c.title_key && x.claim_id === c.claim_id)))
    } catch (e) {
      toast.error(t('hayaa_save_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }

  const restore = async (f: CommunityFp) => {
    try {
      await api.post(`/api/segments/${f.id}/false-positive`, { false_positive: false })
      setFps(list => list.filter(x => x.id !== f.id))
    } catch (e) {
      toast.error(t('hayaa_restore_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }

  if (!st) return <div className="text-muted text-sm">{t('common_loading_dots')}</div>

  const r = st.last_result
  const paused = st.paused_until && st.paused_until * 1000 > Date.now()
  const selectCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100"

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary inline-flex items-center gap-2" onClick={() => syncNow()} disabled={st.running || !!paused}>
          {st.running ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
          {st.running ? t('hayaa_syncing_phase', { phase: st.phase || t('hayaa_phase_starting') }) : t('hayaa_sync_btn')}
        </button>
        <button className="btn-outline" onClick={() => syncNow(true)} disabled={st.running || !!paused}
                title={t('hayaa_force_sync_title')}>
          {t('hayaa_force_sync')}
        </button>
        <span className="text-xs text-muted">
          {st.last_sync ? t('hayaa_last_sync', { time: formatDateTime(st.last_sync * 1000) }) : t('hayaa_never_synced')}
          {st.pending_titles ? ` ${t('hayaa_pending_titles', { titles: t('unit_title', { count: st.pending_titles }) })}` : ` ${t('hayaa_everything_sent')}`}
        </span>
      </div>
      {paused && (
        <p className="text-sm text-amber-300" role="status">
          {t('hayaa_paused_until', { time: formatTime((st.paused_until ?? 0) * 1000) })}
        </p>
      )}
      {r && !st.running && (
        <p className="text-sm text-gray-300">
          {r.error ? <span className="text-red-400">{r.error}. </span> : null}
          {t('hayaa_res_uploaded', { observations: t('unit_observation', { count: r.pushed }) })}
          {r.retracted ? t('hayaa_res_withdrew', { count: r.retracted }) : ''}
          {t('hayaa_res_bg_checked', { titles: t('unit_title', { count: r.pulled }), changed: r.changed })}
          {r.applied ? t('hayaa_res_applied', { segments: t('unit_segment', { count: r.applied }) }) : ''}
          {r.hidden ? t('hayaa_res_hid', { count: r.hidden }) : ''}
          {r.skipped_quota ? t('hayaa_res_quota') : ''}
          {r.pull_pending ? t('hayaa_res_preparing', { titles: t('unit_title', { count: r.pull_pending }) }) : ''}
          {deferredText(r.deferred, t)}.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <label className="block text-sm">
          <span className="text-gray-300">{t('hayaa_preset_label')}</span>
          <select className={`${selectCls} mt-1`} value={st.preset} onChange={e => save({ preset: e.target.value })}>
            {PRESETS.map(p => <option key={p.value} value={p.value}>{t(p.labelKey)}</option>)}
          </select>
          {st.preset_auto_switched && (
            <span className="block mt-1 text-xs text-amber-300">
              {t('hayaa_preset_auto_switched')}
            </span>
          )}
        </label>
        <div className="text-sm space-y-2">
          <label className="flex items-center gap-2 text-gray-300">
            <input type="checkbox" checked={st.auto_sync} onChange={e => save({ auto_sync: e.target.checked })} className="w-4 h-4 accent-plex-orange" />
            {t('hayaa_auto_sync')}
          </label>
          <label className="flex items-center gap-2 text-gray-300">
            {t('hayaa_every')}
            <select className="px-2 py-1 bg-plex-darker border border-plex-border rounded text-sm text-gray-100"
                    value={st.interval_h} disabled={!st.auto_sync}
                    onChange={e => save({ interval_h: Number(e.target.value) })}>
              {[3, 6, 12, 24, 48].map(h => <option key={h} value={h}>{t('hayaa_hours', { hours: h })}</option>)}
            </select>
          </label>
          <p className="text-xs text-muted">{t('hayaa_auto_sync_hint')}</p>
        </div>
      </div>

      {st.verify_pending > 0 && (
        <p className="text-xs text-muted">{t('hayaa_verify_pending', { segments: t('unit_segment', { count: st.verify_pending }) })}</p>
      )}

      {held.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-300 mb-2">{t('hayaa_review_heading', { count: held.length })}</h3>
          <p className="text-xs text-muted mb-2">
            {t('hayaa_review_hint')}
          </p>
          <ul className="divide-y divide-plex-border">
            {held.map(c => (
              <li key={`${c.title_key}:${c.claim_id}`} className="py-2 flex flex-wrap items-center gap-3 text-sm">
                <span className="text-gray-100 min-w-0 flex-1 truncate">{c.title || t('hayaa_unknown_title')}</span>
                <span className="text-muted tabular-nums" dir="ltr">{clock(c.start_ms)}–{clock(c.end_ms)}</span>
                <span className="text-xs text-muted">{c.labels.replace(/,/g, ', ') || c.severity} · {c.status}
                  {c.verify_state === 'miss' ? t('hayaa_miss') : c.verify_state === 'pending' ? t('hayaa_recheck_pending') : ''}</span>
                <button className="btn-outline" onClick={() => decide(c, true)}>{t('hayaa_keep')}</button>
                <button className="btn-outline" onClick={() => decide(c, false)}>{t('common_dismiss')}</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {fps.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-300 mb-2">{t('hayaa_fps_heading', { count: fps.length })}</h3>
          <p className="text-xs text-muted mb-2">{t('hayaa_fps_hint')}</p>
          <ul className="divide-y divide-plex-border">
            {fps.map(f => (
              <li key={f.id} className="py-2 flex flex-wrap items-center gap-3 text-sm">
                <span className="text-gray-100 min-w-0 flex-1 truncate">{f.title}</span>
                <span className="text-muted tabular-nums" dir="ltr">{clock(f.start_ms)}–{clock(f.end_ms)}</span>
                <span className="text-xs text-muted">{f.labels.replace(/,/g, ', ')}</span>
                <button className="btn-outline" onClick={() => restore(f)}>{t('hayaa_restore')}</button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
