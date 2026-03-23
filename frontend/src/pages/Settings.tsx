import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CheckCircle2, XCircle, Loader2, Eye, EyeOff } from 'lucide-react'

interface Settings {
  plex_url: string
  plex_token: string
  poll_interval: string
  confidence_threshold: string
  skip_buffer_ms: string
  scan_window_start: string
  scan_window_end: string
  log_level: string
}

const DEFAULT: Settings = {
  plex_url: '',
  plex_token: '',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-300 mb-1">{label}</label>
      {children}
      {hint && <p className="text-xs text-gray-600 mt-1">{hint}</p>}
    </div>
  )
}

export default function SettingsPage() {
  const [form, setForm] = useState<Settings>(DEFAULT)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [showToken, setShowToken] = useState(false)

  useEffect(() => {
    api.get<Settings>('/api/settings').then(d => {
      setForm({ ...DEFAULT, ...d })
      setLoading(false)
    })
  }, [])

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

  const inputCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-plex-orange/60 transition-colors"

  if (loading) return <div className="text-gray-500 text-sm">Loading...</div>

  return (
    <div className="max-w-xl space-y-8">
      <h1 className="text-2xl font-bold text-gray-100">Settings</h1>

      {/* Plex connection */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Plex Connection</h2>
        <div className="space-y-4">
          <Field label="Plex Server URL" hint="e.g. http://192.168.1.10:32400">
            <input
              type="url"
              value={form.plex_url}
              onChange={set('plex_url')}
              placeholder="http://localhost:32400"
              className={inputCls}
            />
          </Field>

          <Field label="Plex Token" hint="Find yours at plex.tv/devices.xml or in Plex logs">
            <div className="relative">
              <input
                type={showToken ? 'text' : 'password'}
                value={form.plex_token}
                onChange={set('plex_token')}
                placeholder="xxxxxxxxxxxxxxxxxxxx"
                className={inputCls + ' pr-10'}
              />
              <button
                type="button"
                onClick={() => setShowToken(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
              >
                {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </Field>

          <div className="flex items-center gap-3">
            <button
              onClick={testConnection}
              disabled={testing || !form.plex_url || !form.plex_token}
              className="px-4 py-2 text-sm bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
            >
              {testing && <Loader2 size={14} className="animate-spin" />}
              Test Connection
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

      {/* Filter settings */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Filter Settings</h2>
        <div className="space-y-4">
          <Field label="Poll Interval (seconds)" hint="How often to check active streams for scenes to skip">
            <input type="number" min="2" max="30" value={form.poll_interval} onChange={set('poll_interval')} className={inputCls} />
          </Field>
          <Field label="Detection Confidence Threshold" hint="Frames scoring above this value (0–1) are flagged as nudity. Lower = more sensitive.">
            <input type="number" min="0.1" max="1" step="0.05" value={form.confidence_threshold} onChange={set('confidence_threshold')} className={inputCls} />
          </Field>
          <Field label="Skip Buffer (ms)" hint="Extra milliseconds to seek past the end of a detected segment">
            <input type="number" min="0" step="500" value={form.skip_buffer_ms} onChange={set('skip_buffer_ms')} className={inputCls} />
          </Field>
        </div>
      </section>

      {/* Scan schedule */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Scan Schedule</h2>
        <p className="text-xs text-gray-500 mb-4">Background video scanning only runs within this time window to avoid slowing your device during the day.</p>
        <div className="flex gap-4">
          <Field label="Start Time">
            <input type="time" value={form.scan_window_start} onChange={set('scan_window_start')} className={inputCls} />
          </Field>
          <Field label="End Time">
            <input type="time" value={form.scan_window_end} onChange={set('scan_window_end')} className={inputCls} />
          </Field>
        </div>
      </section>

      {/* Save */}
      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={saving}
          className="px-6 py-2.5 bg-plex-orange text-black font-semibold text-sm rounded-lg hover:bg-plex-orange/90 transition-colors disabled:opacity-50 flex items-center gap-2"
        >
          {saving && <Loader2 size={14} className="animate-spin" />}
          Save Settings
        </button>
        {saved && (
          <span className="flex items-center gap-1.5 text-sm text-green-400">
            <CheckCircle2 size={15} /> Saved
          </span>
        )}
      </div>
    </div>
  )
}
