import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Users, X } from 'lucide-react'
import { api } from '../api/client'
import { useT } from '../i18n'

export const HAYAA_BANNER_KEY = 'cleanplex.hayaaBanner.dismissed'

function dismissed(): boolean {
  try { return localStorage.getItem(HAYAA_BANNER_KEY) === '1' } catch { return false }
}

/**
 * One-time invitation to Hayaa for instances that are not signed in. "Not now" hides it for good
 * (per browser); signing in hides it too. Not shown on the Settings page, where the panel is.
 */
export default function HayaaBanner() {
  const { t } = useT()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [show, setShow] = useState(false)

  useEffect(() => {
    if (dismissed()) return
    const ctl = new AbortController()
    api.get<{ linked: boolean }>('/api/hayaa/status', { signal: ctl.signal })
      .then(s => setShow(!s.linked))
      .catch(() => {})
    return () => ctl.abort()
  }, [])

  if (!show || pathname === '/settings') return null

  const hide = () => {
    try { localStorage.setItem(HAYAA_BANNER_KEY, '1') } catch { /* private mode */ }
    setShow(false)
  }

  return (
    <div role="region" aria-label={t('hayaa_banner_title')}
      className="mb-4 flex items-start gap-3 rounded-xl border border-plex-orange/40 bg-plex-orange/10 p-4">
      <Users size={20} className="mt-0.5 flex-shrink-0 text-plex-orange" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-gray-100">{t('hayaa_banner_title')}</p>
        <p className="mt-1 text-sm text-gray-300">{t('hayaa_banner_body')}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={() => navigate('/settings?section=hayaa')}
            className="rounded-lg bg-plex-orange px-3 py-1.5 text-sm font-medium text-black hover:bg-plex-orange/90">
            {t('hayaa_banner_on')}
          </button>
          <button type="button" onClick={hide}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-gray-300 hover:bg-white/5">
            {t('hayaa_banner_later')}
          </button>
        </div>
      </div>
      <button type="button" onClick={hide} aria-label={t('hayaa_banner_later')}
        className="rounded p-1 text-gray-400 hover:bg-white/5 hover:text-gray-100">
        <X size={16} />
      </button>
    </div>
  )
}
