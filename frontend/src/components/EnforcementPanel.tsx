import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { useT } from '../i18n'

interface Enforcement {
  enabled: boolean
  mute_mode: 'cut' | 'leave'
  backstop: boolean
  proxied_last_day: number
  plex: { relay?: string | null; dlna?: string | null; transcode_limit?: string | null }
  recommendations: { relay: boolean; dlna: boolean }
}

// Plex reports booleans as 1/0 or True/False depending on the setting.
const isOn = (v?: string | null) => v != null && ['1', 'true'].includes(String(v).toLowerCase())

/**
 * Stream filtering through the front door (#184): every client reaches Plex through Cleanplex,
 * which removes filtered ranges from the stream itself. Settings here apply immediately.
 */
export default function EnforcementPanel() {
  const { t } = useT()
  const [data, setData] = useState<Enforcement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [limit, setLimit] = useState('')

  const onOff = (v?: string | null) => (v == null ? t('common_unknown') : isOn(v) ? t('common_on') : t('common_off'))

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const d = await api.get<Enforcement>('/api/enforcement', { signal })
      setData(d)
      setLimit(d.plex.transcode_limit ?? '')
    } catch {
      // keep what is on screen
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  const post = async (path: string, body: object) => {
    setBusy(true)
    setError('')
    try {
      const d = await api.post<Enforcement>(path, body)
      setData(d)
      setLimit(d.plex.transcode_limit ?? '')
    } catch (e: any) {
      setError(e?.message || t('enf_request_failed'))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return <p className="text-sm text-muted">{t('common_loading')}</p>

  return (
    <div className="space-y-5">
      <div className="text-sm">
        <p className={data.enabled ? 'text-green-400' : 'text-gray-400'}>
          {data.enabled
            ? t('enf_frontdoor_active')
            : t('enf_frontdoor_inactive')}
        </p>
        <p className="text-xs text-muted mt-1">
          {t('enf_install_hint_1')} <code dir="ltr">sudo bash scripts/frontdoor/install.sh</code>
          {' '}(<code dir="ltr">--uninstall</code> {t('enf_install_hint_2', { count: data.proxied_last_day })}
        </p>
      </div>

      <label className="block">
        <span className="text-sm font-medium text-gray-300">{t('enf_mute_label')}</span>
        <select
          value={data.mute_mode}
          disabled={busy}
          onChange={e => post('/api/enforcement', { mute_mode: e.target.value })}
          className="mt-1 block w-full px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-200"
        >
          <option value="leave">{t('enf_mute_leave')}</option>
          <option value="cut">{t('enf_mute_cut')}</option>
        </select>
        <span className="text-xs text-muted">{t('enf_mute_hint')}</span>
      </label>

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={data.backstop}
          disabled={busy}
          onChange={e => post('/api/enforcement', { backstop: e.target.checked })}
          className="w-4 h-4 accent-plex-orange"
        />
        <span className="text-sm text-gray-300">
          {t('enf_backstop')}
        </span>
      </label>

      <div>
        <p className="text-sm font-medium text-gray-300 mb-2">{t('enf_plex_settings_title')}</p>
        <div className="space-y-2 text-sm">
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              {t('enf_relay_status', { state: onOff(data.plex.relay) })}
              <span className="block text-xs text-muted">{t('enf_relay_hint')}</span>
            </span>
            <button disabled={busy} onClick={() => post('/api/enforcement/plex-settings', { relay: !isOn(data.plex.relay) })}
              className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
              {isOn(data.plex.relay) ? t('enf_turn_off') : t('enf_turn_on')}
            </button>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              {t('enf_dlna_status', { state: onOff(data.plex.dlna) })}
              <span className="block text-xs text-muted">{t('enf_dlna_hint')}</span>
            </span>
            <button disabled={busy} onClick={() => post('/api/enforcement/plex-settings', { dlna: !isOn(data.plex.dlna) })}
              className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
              {isOn(data.plex.dlna) ? t('enf_turn_off') : t('enf_turn_on')}
            </button>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              {t('enf_transcodes')}
              <span className="block text-xs text-muted">{t('enf_transcodes_hint')}</span>
            </span>
            <span className="flex items-center gap-2">
              <input type="number" min="0" max="50" value={limit} onChange={e => setLimit(e.target.value)}
                className="w-16 px-2 py-1 bg-plex-card border border-plex-border rounded text-sm text-gray-200" aria-label={t('enf_transcodes')} />
              <button disabled={busy || limit === ''} onClick={() => post('/api/enforcement/plex-settings', { transcode_limit: Number(limit) })}
                className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
                {t('common_apply')}
              </button>
            </span>
          </div>
        </div>
      </div>
      {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
    </div>
  )
}
