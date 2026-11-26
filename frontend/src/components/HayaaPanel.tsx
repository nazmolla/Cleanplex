import { useCallback, useEffect, useState } from 'react'
import { ExternalLink, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { toast } from './Toast'
import HayaaSync from './HayaaSync'
import { useT } from '../i18n'

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
 * Settings → Hayaa (#221): signing this instance in with a short code
 * the user confirms on any phone or computer (OAuth device flow); once linked, syncing.
 */
export default function HayaaPanel() {
  const { t } = useT()
  const [st, setSt] = useState<HayaaStatus | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    const s = await api.get<HayaaStatus>('/api/hayaa/status', { signal })
    setSt(s)
    return s
  }, [])

  useEffect(() => {
    load().catch(() => toast.error(t('hayaa_load_error')))
  }, [load, t])

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
        if (s.link?.status === 'linked') toast.success(t('hayaa_signed_in_toast'))
      }).catch(() => {})
    }, POLL_MS)
    return () => { clearInterval(id); ctrl?.abort() }
  }, [pending, load, t])

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

  const signIn = () => run(async () => { await api.post('/api/hayaa/link'); await load() }, t('hayaa_signin_error'))
  const unlink = () => run(async () => setSt(await api.post<HayaaStatus>('/api/hayaa/unlink')), t('hayaa_unlink_error'))

  if (!st) return <div className="text-muted text-sm">{t('common_loading_dots')}</div>


  return (
    <div className="space-y-5">
      <p className="text-sm text-muted">
        {t('hayaa_desc')}
      </p>

      <div className="border-t border-plex-border pt-4 space-y-3">
        {st.linked && !pending ? (
          <>
            <p className="text-sm text-gray-100">{t('hayaa_signed_in_as')} <span className="font-medium">{st.device_label}</span>.</p>
            <p className="text-xs text-muted break-all">{t('hayaa_instance_key', { keyId: st.key_id })}</p>
            <button className="btn-danger" onClick={unlink} disabled={busy}>{t('hayaa_unlink')}</button>
            <div className="border-t border-plex-border pt-4">
              <HayaaSync />
            </div>
          </>
        ) : pending && st.link ? (
          <div className="space-y-2">
            <p className="text-sm text-gray-300">{t('hayaa_link_prompt')}</p>
            <p className="text-3xl font-mono font-bold tracking-widest text-gray-100" aria-live="polite" dir="ltr">{st.link.user_code}</p>
            <a href={st.link.verification_uri} target="_blank" rel="noreferrer"
               className="inline-flex items-center gap-1 text-sm text-gray-100 underline break-all" dir="ltr">
              {st.link.verification_uri} <ExternalLink size={14} aria-hidden="true" />
            </a>
            <p className="flex items-center gap-2 text-xs text-muted">
              <Loader2 size={14} className="animate-spin" aria-hidden="true" />
              {t('hayaa_waiting_approval', { minutes: Math.ceil(st.link.expires_in_s / 60) })}
            </p>
          </div>
        ) : (
          <>
            {st.link?.status === 'error' && <p className="text-sm text-red-400" role="alert">{st.link.error}</p>}
            <button className="btn-primary" onClick={signIn} disabled={busy}>
              {t('hayaa_sign_in')}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
