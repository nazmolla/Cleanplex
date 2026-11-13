import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'

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
const onOff = (v?: string | null) => (v == null ? 'unknown' : isOn(v) ? 'On' : 'Off')

/**
 * Stream filtering through the front door (#184): every client reaches Plex through Cleanplex,
 * which removes filtered ranges from the stream itself. Settings here apply immediately.
 */
export default function EnforcementPanel() {
  const [data, setData] = useState<Enforcement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [limit, setLimit] = useState('')

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
      setError(e?.message || 'Request failed')
    } finally {
      setBusy(false)
    }
  }

  if (!data) return <p className="text-sm text-muted">Loading…</p>

  return (
    <div className="space-y-5">
      <div className="text-sm">
        <p className={data.enabled ? 'text-green-400' : 'text-gray-400'}>
          {data.enabled
            ? 'Front door active: every client reaches Plex through Cleanplex.'
            : 'Front door not installed: only clients Cleanplex can control are filtered.'}
        </p>
        <p className="text-xs text-muted mt-1">
          Install or remove it on the server with <code>sudo bash scripts/frontdoor/install.sh</code>
          {' '}(<code>--uninstall</code> to remove). Streams edited in the last day: {data.proxied_last_day}.
        </p>
      </div>

      <label className="block">
        <span className="text-sm font-medium text-gray-300">Mute segments in filtered streams</span>
        <select
          value={data.mute_mode}
          disabled={busy}
          onChange={e => post('/api/enforcement', { mute_mode: e.target.value })}
          className="mt-1 block w-full px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-200"
        >
          <option value="leave">Leave them (play unmuted)</option>
          <option value="cut">Cut them out like skips</option>
        </select>
        <span className="text-xs text-muted">An edited stream cannot lower the volume, so profanity is either cut or left in.</span>
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
          Stop a filtered stream that bypassed the front door (relay, DLNA), with a message to the viewer
        </span>
      </label>

      <div>
        <p className="text-sm font-medium text-gray-300 mb-2">Plex settings the front door relies on</p>
        <div className="space-y-2 text-sm">
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              Relay: {onOff(data.plex.relay)}
              <span className="block text-xs text-muted">Recommended off: relayed streams never reach the front door. Remote viewers then need Plex's direct remote access.</span>
            </span>
            <button disabled={busy} onClick={() => post('/api/enforcement/plex-settings', { relay: !isOn(data.plex.relay) })}
              className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
              Turn {isOn(data.plex.relay) ? 'off' : 'on'}
            </button>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              DLNA: {onOff(data.plex.dlna)}
              <span className="block text-xs text-muted">Recommended off: DLNA serves files on its own port.</span>
            </span>
            <button disabled={busy} onClick={() => post('/api/enforcement/plex-settings', { dlna: !isOn(data.plex.dlna) })}
              className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
              Turn {isOn(data.plex.dlna) ? 'off' : 'on'}
            </button>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-gray-300">
              Simultaneous transcodes
              <span className="block text-xs text-muted">Filtered streams of flagged titles are copied or re-encoded by Plex; raise this for several viewers at once (0 = unlimited).</span>
            </span>
            <span className="flex items-center gap-2">
              <input type="number" min="0" max="50" value={limit} onChange={e => setLimit(e.target.value)}
                className="w-16 px-2 py-1 bg-plex-card border border-plex-border rounded text-sm text-gray-200" aria-label="Simultaneous transcodes" />
              <button disabled={busy || limit === ''} onClick={() => post('/api/enforcement/plex-settings', { transcode_limit: Number(limit) })}
                className="px-3 py-1.5 text-xs rounded-lg border border-plex-border text-gray-200 hover:bg-white/5 disabled:opacity-40">
                Apply
              </button>
            </span>
          </div>
        </div>
      </div>
      {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
    </div>
  )
}
