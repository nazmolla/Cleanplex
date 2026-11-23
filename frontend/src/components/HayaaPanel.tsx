import { useCallback, useEffect, useState } from 'react'
import { ExternalLink, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { toast } from './Toast'

interface LinkState {
  status: 'pending' | 'linked' | 'error'
  user_code: string
  verification_uri: string
  expires_in_s: number
  error: string
}

interface HayaaStatus {
  api_url: string
  auth_url: string
  configured: boolean
  linked: boolean
  device_label: string
  key_id: string
  link: LinkState | null
}

const POLL_MS = 3000

/**
 * Settings → Hayaa (#221): the service URLs and signing this instance in with a short code
 * the user confirms on any phone or computer (OAuth device flow). Syncing comes later.
 */
export default function HayaaPanel() {
  const [st, setSt] = useState<HayaaStatus | null>(null)
  const [urls, setUrls] = useState({ api_url: '', auth_url: '' })
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    const s = await api.get<HayaaStatus>('/api/hayaa/status', { signal })
    setSt(s)
    return s
  }, [])

  useEffect(() => {
    load().then(s => setUrls({ api_url: s.api_url, auth_url: s.auth_url })).catch(() => toast.error('Could not load Hayaa status'))
  }, [load])

  const pending = st?.link?.status === 'pending'

  // While a sign-in waits for approval, poll its status. It ends when the link stops being
  // pending, and the server's code expiry (at most 15 minutes) bounds it.
  useEffect(() => {
    if (!pending) return
    let ctrl: AbortController | null = null
    const id = setInterval(() => {
      ctrl?.abort()
      ctrl = new AbortController()
      load(ctrl.signal).then(s => {
        if (s.link?.status === 'linked') toast.success('Signed in to Hayaa')
      }).catch(() => {})
    }, POLL_MS)
    return () => { clearInterval(id); ctrl?.abort() }
  }, [pending, load])

  const run = async (fn: () => Promise<unknown>, fail: string) => {
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      toast.error(`${fail}: ${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const saveUrls = () => run(async () => {
    setSt(await api.put<HayaaStatus>('/api/hayaa/settings', urls))
    toast.success('Hayaa URLs saved')
  }, 'Could not save')

  const signIn = () => run(async () => { await api.post('/api/hayaa/link'); await load() }, 'Could not start signing in')
  const unlink = () => run(async () => setSt(await api.post<HayaaStatus>('/api/hayaa/unlink')), 'Could not unlink')

  if (!st) return <div className="text-muted text-sm">Loading...</div>

  const inputCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100 placeholder-muted focus:border-plex-orange/60 transition-colors"
  const dirty = urls.api_url !== st.api_url || urls.auth_url !== st.auth_url

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted">
        Hayaa shares segments between Cleanplex instances: only time ranges, labels and votes, keyed by file
        fingerprints. Titles, file names, frames and your preferences never leave this server.
      </p>

      <div className="grid gap-4 lg:grid-cols-2">
        <label className="block text-sm">
          <span className="text-gray-300">Service URL</span>
          <input className={`${inputCls} mt-1`} type="url" placeholder="https://…" value={urls.api_url}
                 onChange={e => setUrls({ ...urls, api_url: e.target.value })} />
        </label>
        <label className="block text-sm">
          <span className="text-gray-300">Sign-in URL</span>
          <input className={`${inputCls} mt-1`} type="url" placeholder="https://…" value={urls.auth_url}
                 onChange={e => setUrls({ ...urls, auth_url: e.target.value })} />
        </label>
      </div>
      {dirty && (
        <button className="btn-outline" onClick={saveUrls} disabled={busy}>Save URLs</button>
      )}

      <div className="border-t border-plex-border pt-4 space-y-3">
        {st.linked && !pending ? (
          <>
            <p className="text-sm text-gray-100">Signed in as <span className="font-medium">{st.device_label}</span>.</p>
            <p className="text-xs text-muted break-all">Instance key {st.key_id}</p>
            <button className="btn-danger" onClick={unlink} disabled={busy}>Unlink from Hayaa</button>
          </>
        ) : pending && st.link ? (
          <div className="space-y-2">
            <p className="text-sm text-gray-300">On any phone or computer, open the link and enter this code:</p>
            <p className="text-3xl font-mono font-bold tracking-widest text-gray-100" aria-live="polite">{st.link.user_code}</p>
            <a href={st.link.verification_uri} target="_blank" rel="noreferrer"
               className="inline-flex items-center gap-1 text-sm text-gray-100 underline break-all">
              {st.link.verification_uri} <ExternalLink size={14} aria-hidden="true" />
            </a>
            <p className="flex items-center gap-2 text-xs text-muted">
              <Loader2 size={14} className="animate-spin" aria-hidden="true" />
              Waiting for approval · code expires in {Math.ceil(st.link.expires_in_s / 60)} min
            </p>
          </div>
        ) : (
          <>
            {st.link?.status === 'error' && <p className="text-sm text-red-400" role="alert">{st.link.error}</p>}
            <button className="btn-primary" onClick={signIn} disabled={busy || !st.configured || dirty}
                    title={st.configured ? undefined : 'Save the service and sign-in URLs first'}>
              Sign in to Hayaa
            </button>
          </>
        )}
      </div>
    </div>
  )
}
