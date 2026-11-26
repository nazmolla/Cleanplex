import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import { toast } from './Toast'

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
const DEFERRED_TEXT: Record<string, string> = {
  account_too_new: 'your Hayaa account is still new',
  contribute_more: 'Hayaa wants more of your own scans first',
  not_contributed: 'not scanned here yet',
  scan_time: 'waiting out a scan-length delay',
  prove_possession: 'file check sent, unlocks next sync',
  proof_failed: 'file check did not match',
  budget: "today's viewing allowance is used up",
  lifetime_cap: 'account title limit reached',
}

function deferredText(d: Record<string, number> | undefined): string {
  const parts = Object.entries(d ?? {}).map(([k, n]) => `${n} ${DEFERRED_TEXT[k] ?? k}`)
  return parts.length ? `; held back: ${parts.join(', ')}` : ''
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
  { value: 'balanced', label: 'Balanced: apply accepted claims, re-check the rest locally' },
  { value: 'cautious', label: 'Cautious: also apply unconfirmed claims, ignore community rejections' },
  { value: 'verify_first', label: 'Verify first: apply nothing until this server re-checked it' },
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

  useEffect(() => { load().catch(() => toast.error('Could not load Hayaa sync status')) }, [load])

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
          if (r?.error) toast.error(`Sync: ${r.error}`)
          else toast.success(`Synced: ${r?.pushed ?? 0} sent, ${r?.changed ?? 0} titles updated`)
        }
      }).catch(() => {})
    }, POLL_MS)
    return () => { clearInterval(id); ctrl?.abort() }
  }, [st?.running, load])

  const save = async (body: Partial<{ auto_sync: boolean; interval_h: number; preset: string }>) => {
    try {
      setSt(await api.put<SyncStatus>('/api/hayaa/sync', body))
    } catch (e) {
      toast.error(`Could not save: ${e instanceof Error ? e.message : e}`)
    }
  }

  const syncNow = async (force = false) => {
    try {
      setSt(await api.post<SyncStatus>(`/api/hayaa/sync${force ? '?force=true' : ''}`))
    } catch (e) {
      toast.error(`Could not start a sync: ${e instanceof Error ? e.message : e}`)
    }
  }

  const decide = async (c: HeldClaim, keep: boolean) => {
    try {
      await api.post('/api/hayaa/review', { title_key: c.title_key, claim_id: c.claim_id, keep })
      setHeld(h => h.filter(x => !(x.title_key === c.title_key && x.claim_id === c.claim_id)))
    } catch (e) {
      toast.error(`Could not save: ${e instanceof Error ? e.message : e}`)
    }
  }

  const restore = async (f: CommunityFp) => {
    try {
      await api.post(`/api/segments/${f.id}/false-positive`, { false_positive: false })
      setFps(list => list.filter(x => x.id !== f.id))
    } catch (e) {
      toast.error(`Could not restore: ${e instanceof Error ? e.message : e}`)
    }
  }

  if (!st) return <div className="text-muted text-sm">Loading...</div>

  const r = st.last_result
  const paused = st.paused_until && st.paused_until * 1000 > Date.now()
  const selectCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100"

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary inline-flex items-center gap-2" onClick={() => syncNow()} disabled={st.running || !!paused}>
          {st.running ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
          {st.running ? `Syncing: ${st.phase || 'starting'}` : 'Sync with Hayaa'}
        </button>
        <button className="btn-outline" onClick={() => syncNow(true)} disabled={st.running || !!paused}
                title="Send and fetch every title again, not only what changed">
          Force sync
        </button>
        <span className="text-xs text-muted">
          {st.last_sync ? `Last sync ${new Date(st.last_sync * 1000).toLocaleString()}` : 'Never synced'}
          {st.pending_titles ? ` · ${st.pending_titles} title(s) with changes to send` : ' · everything sent'}
        </span>
      </div>
      {paused && (
        <p className="text-sm text-amber-300" role="status">
          Hayaa asked to wait; syncing resumes after {new Date((st.paused_until ?? 0) * 1000).toLocaleTimeString()}.
        </p>
      )}
      {r && !st.running && (
        <p className="text-sm text-gray-300">
          {r.error ? <span className="text-red-400">{r.error}. </span> : null}
          Uploaded {r.pushed} observation(s){r.retracted ? `, withdrew ${r.retracted}` : ''} (Hayaa adds them in the background); checked {r.pulled} title(s), {r.changed} updated
          {r.applied ? `; applied ${r.applied} segment(s)` : ''}{r.hidden ? `, hid ${r.hidden}` : ''}
          {r.skipped_quota ? '; stopped at today\'s quota, the rest goes next time' : ''}
          {r.pull_pending ? `; Hayaa is still preparing ${r.pull_pending} title(s), they arrive on the next sync` : ''}
          {deferredText(r.deferred)}.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <label className="block text-sm">
          <span className="text-gray-300">How pulled segments are applied</span>
          <select className={`${selectCls} mt-1`} value={st.preset} onChange={e => save({ preset: e.target.value })}>
            {PRESETS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
          {st.preset_auto_switched && (
            <span className="block mt-1 text-xs text-amber-300">
              Switched automatically: this server's re-checks kept disagreeing with shared segments.
            </span>
          )}
        </label>
        <div className="text-sm space-y-2">
          <label className="flex items-center gap-2 text-gray-300">
            <input type="checkbox" checked={st.auto_sync} onChange={e => save({ auto_sync: e.target.checked })} />
            Sync automatically
          </label>
          <label className="flex items-center gap-2 text-gray-300">
            every
            <select className="px-2 py-1 bg-plex-darker border border-plex-border rounded text-sm text-gray-100"
                    value={st.interval_h} disabled={!st.auto_sync}
                    onChange={e => save({ interval_h: Number(e.target.value) })}>
              {[3, 6, 12, 24, 48].map(h => <option key={h} value={h}>{h} hours</option>)}
            </select>
          </label>
          <p className="text-xs text-muted">Automatic syncs leave a fifth of the daily quota for syncing by hand.</p>
        </div>
      </div>

      {st.verify_pending > 0 && (
        <p className="text-xs text-muted">{st.verify_pending} pulled segment(s) wait to be re-checked on this server during the scan window.</p>
      )}

      {held.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-300 mb-2">Waiting for review ({held.length})</h3>
          <p className="text-xs text-muted mb-2">
            Shared segments this server could not confirm. Keep applies one and vouches for it; dismiss hides it here for good.
          </p>
          <ul className="divide-y divide-plex-border">
            {held.map(c => (
              <li key={`${c.title_key}:${c.claim_id}`} className="py-2 flex flex-wrap items-center gap-3 text-sm">
                <span className="text-gray-100 min-w-0 flex-1 truncate">{c.title || 'Unknown title'}</span>
                <span className="text-muted tabular-nums">{clock(c.start_ms)}–{clock(c.end_ms)}</span>
                <span className="text-xs text-muted">{c.labels.replace(/,/g, ', ') || c.severity} · {c.status}
                  {c.verify_state === 'miss' ? ' · not found here' : c.verify_state === 'pending' ? ' · re-check pending' : ''}</span>
                <button className="btn-outline" onClick={() => decide(c, true)}>Keep</button>
                <button className="btn-outline" onClick={() => decide(c, false)}>Dismiss</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {fps.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-300 mb-2">Community false positives ({fps.length})</h3>
          <p className="text-xs text-muted mb-2">Your detections that Hayaa users rejected. They no longer act; restore one if it is real.</p>
          <ul className="divide-y divide-plex-border">
            {fps.map(f => (
              <li key={f.id} className="py-2 flex flex-wrap items-center gap-3 text-sm">
                <span className="text-gray-100 min-w-0 flex-1 truncate">{f.title}</span>
                <span className="text-muted tabular-nums">{clock(f.start_ms)}–{clock(f.end_ms)}</span>
                <span className="text-xs text-muted">{f.labels.replace(/,/g, ', ')}</span>
                <button className="btn-outline" onClick={() => restore(f)}>Restore</button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
