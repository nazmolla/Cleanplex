import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CheckCircle2, XCircle, Loader2, Eye, EyeOff } from 'lucide-react'

interface Settings {
  plex_url: string
  plex_token: string
  poll_interval: string
  confidence_threshold: string
  skip_buffer_ms: string
  scan_step_ms: string
  scan_workers: string
  nudenet_model: string
  nudenet_model_path: string
  segment_gap_ms: string
  segment_min_hits: string
  scan_window_start: string
  scan_window_end: string
  log_level: string
  excluded_library_ids: string
  scan_ratings: string
  scan_labels: string
}

interface Library {
  id: string
  title: string
  type: string
}

interface SyncStatus {
  sync_enabled: boolean
  instance_name: string | null
  github_repo: string | null
  conflict_resolution: string
  verified_threshold: number
  timing_tolerance_ms: number
  last_sync_time: string | null
}

const DEFAULT: Settings = {
  plex_url: '',
  plex_token: '',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  nudenet_model: '320n',
  nudenet_model_path: '',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED","FEMALE_GENITALIA_EXPOSED","MALE_GENITALIA_EXPOSED","ANUS_EXPOSED","BUTTOCKS_EXPOSED"]',
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
  const [validatingModel, setValidatingModel] = useState(false)
  const [modelValidationResult, setModelValidationResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [showToken, setShowToken] = useState(false)
  const [libraries, setLibraries] = useState<Library[]>([])
  const [detectorLabels, setDetectorLabels] = useState<string[]>([])
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null)
  const [syncForm, setSyncForm] = useState({ sync_enabled: false, instance_name: '' })
  const [savingSync, setSavingSync] = useState(false)
  const [savedSync, setSavedSync] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadResult, setDownloadResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    Promise.all([
      api.get<Settings>('/api/settings'),
      api.get<{ libraries: Library[] }>('/api/libraries').catch(() => ({ libraries: [] })),
      api.get<{ labels: string[] }>('/api/settings/detector-labels').catch(() => ({ labels: [] })),
      api.get<SyncStatus>('/api/sync/status').catch(() => null),
    ]).then(([settings, libs, labels, sync]) => {
      setForm({ ...DEFAULT, ...settings })
      setLibraries(libs.libraries)
      setDetectorLabels(labels.labels)
      if (sync) {
        setSyncStatus(sync)
        setSyncForm(f => ({
          ...f,
          sync_enabled: sync.sync_enabled,
          instance_name: sync.instance_name ?? '',
        }))
      }
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

  const saveSync = async () => {
    setSavingSync(true)
    setSavedSync(false)
    try {
      await api.post('/api/sync/settings', {
        instance_name: syncForm.instance_name || 'default',
        sync_enabled: syncForm.sync_enabled,
      })
      setSavedSync(true)
      const updated = await api.get<SyncStatus>('/api/sync/status').catch(() => null)
      if (updated) setSyncStatus(updated)
      setTimeout(() => setSavedSync(false), 3000)
    } catch (e: any) {
      setUploadResult({ ok: false, message: e?.message ?? 'Failed to save sync settings' })
    } finally {
      setSavingSync(false)
    }
  }

  const doUpload = async () => {
    setUploading(true)
    setUploadResult(null)
    try {
      // Step 1: Enqueue the upload job
      const r = await api.post<{ status: string; job_id: number; message: string }>('/api/sync/upload-segment-library')
      const jobId = r.job_id
      setUploadResult({ ok: true, message: `Upload job ${jobId} queued. Starting...` })
      
      // Step 2: Poll for job status — max 120 attempts (≈2 min) before timing out.
      const MAX_POLLS = 120
      let completed = false
      let isSuccess = false
      let finalMessage = ''

      for (let attempt = 0; attempt < MAX_POLLS && !completed; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 1000))

        try {
          const status = await api.get<any>(`/api/sync/job-status/${jobId}`)

          if (status.status === 'completed') {
            completed = true
            isSuccess = true
            const result = status.result || {}
            finalMessage = result.message || 'Upload completed'
          } else if (status.status === 'failed') {
            completed = true
            isSuccess = false
            finalMessage = status.error || 'Upload failed'
          } else {
            const progress = status.progress || 0
            finalMessage = `Uploading... ${progress}%`
          }

          setUploadResult({ ok: isSuccess, message: finalMessage })
        } catch {
          // Transient poll error — keep trying until max attempts
        }
      }

      if (!completed) {
        isSuccess = false
        finalMessage = 'Upload timed out — check server logs'
        setUploadResult({ ok: false, message: finalMessage })
      }
      
      const updated = await api.get<SyncStatus>('/api/sync/status').catch(() => null)
      if (updated) setSyncStatus(updated)
    } catch (e: any) {
      setUploadResult({ ok: false, message: 'Upload failed to start' })
    } finally {
      setUploading(false)
    }
  }

  const doDownload = async () => {
    setDownloading(true)
    setDownloadResult(null)
    try {
      const r = await api.get<{ status: string; results: Record<string, any[]>; merge_results: Record<string, any> }>('/api/sync/download-local-library')
      const fileCount = Object.keys(r.results || {}).length
      const ok = r.status === 'success' || r.status === 'no_data'
      setDownloadResult({ ok, message: ok ? `Downloaded/merged ${fileCount} file(s) from GitHub library` : 'Download failed' })
      const updated = await api.get<SyncStatus>('/api/sync/status').catch(() => null)
      if (updated) setSyncStatus(updated)
    } catch (e: any) {
      setDownloadResult({ ok: false, message: e?.message ?? 'Download failed' })
    } finally {
      setDownloading(false)
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
    } catch (e) {
      setModelValidationResult({ ok: false, message: 'Validation request failed' })
    } finally {
      setValidatingModel(false)
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
          <Field label="Scan Frame Interval (ms)" hint="How often frames are sampled during scanning. Lower catches more scenes but takes longer.">
            <input type="number" min="1000" max="20000" step="500" value={form.scan_step_ms} onChange={set('scan_step_ms')} className={inputCls} />
          </Field>
          <Field label="Scanner Workers" hint="How many titles can be scanned in parallel. Higher values use more CPU, disk, and memory.">
            <input type="number" min="1" max="12" step="1" value={form.scan_workers} onChange={set('scan_workers')} className={inputCls} />
          </Field>
          <Field label="NudeNet Model" hint="320n is bundled and fastest. 640m is downloaded by Cleanplex automatically and then cached locally.">
            <select value={form.nudenet_model} onChange={set('nudenet_model')} className={inputCls}>
              <option value="320n">320n (default, fast)</option>
              <option value="640m">640m (higher accuracy, slower)</option>
            </select>
          </Field>
          <Field label="Prepare 640m Model" hint="Optional: click once to pre-download 640m now. Otherwise it will auto-download on first 640m scan.">
            <div className="mt-2 flex items-center gap-3">
              <button
                type="button"
                onClick={validateModelPath}
                disabled={validatingModel}
                className="px-3 py-2 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
              >
                {validatingModel && <Loader2 size={13} className="animate-spin" />}
                Download/Check 640m model
              </button>
              {modelValidationResult && (
                <span className={`flex items-center gap-1.5 text-xs ${modelValidationResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                  {modelValidationResult.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                  {modelValidationResult.message}
                </span>
              )}
            </div>
          </Field>
          <Field label="Segment Merge Gap (ms)" hint="Flagged frames closer than this are merged into one segment.">
            <input type="number" min="1000" max="30000" step="500" value={form.segment_gap_ms} onChange={set('segment_gap_ms')} className={inputCls} />
          </Field>
          <Field label="Minimum Hits Per Segment" hint="Require at least this many flagged frames in a cluster to keep a segment. Increase to reduce false positives.">
            <input type="number" min="1" max="6" step="1" value={form.segment_min_hits} onChange={set('segment_min_hits')} className={inputCls} />
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

      {/* Scan ratings */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Scan Ratings</h2>
        <p className="text-xs text-gray-500 mb-3">Only scan titles with these content ratings. Leave all unchecked to scan everything.</p>
        <div className="grid grid-cols-2 gap-2">
          {/* VALUE is the exact content_rating string stored in Plex / the DB.
              Empty string ("") represents titles Plex left unrated. */}
          {[
            { label: 'G',            value: 'G' },
            { label: 'PG',           value: 'PG' },
            { label: 'PG-13',        value: 'PG-13' },
            { label: 'R',            value: 'R' },
            { label: 'NC-17',        value: 'NC-17' },
            { label: 'TV-G',         value: 'TV-G' },
            { label: 'TV-PG',        value: 'TV-PG' },
            { label: 'TV-14',        value: 'TV-14' },
            { label: 'TV-MA',        value: 'TV-MA' },
            { label: 'NR',           value: 'NR' },
            { label: 'X',            value: 'X' },
            { label: 'Unrated (no rating set in Plex)', value: '' },
          ].map(({ label, value }) => {
            const selected: string[] = (() => { try { return JSON.parse(form.scan_ratings) } catch { return [] } })()
            const checked = selected.includes(value)
            const toggle = () => {
              const next = checked ? selected.filter((r: string) => r !== value) : [...selected, value]
              setForm(f => ({ ...f, scan_ratings: JSON.stringify(next) }))
            }
            return (
              <label key={value === '' ? '__unrated__' : value} className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-plex-orange" />
                <span className={`text-sm ${checked ? 'text-gray-100' : 'text-gray-500'}`}>{label}</span>
              </label>
            )
          })}
        </div>
      </section>

      {/* Detector labels */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Detector Labels</h2>
        <p className="text-xs text-gray-500 mb-3">Choose which NudeNet labels should count toward detection. Exposed-only labels are usually less noisy.</p>
        <div className="grid grid-cols-2 gap-2">
          {detectorLabels.map(label => {
            const selected = (() => { try { return JSON.parse(form.scan_labels) } catch { return [] } })()
            const checked = selected.includes(label)
            const toggle = () => {
              const next = checked ? selected.filter((r: string) => r !== label) : [...selected, label]
              setForm(f => ({ ...f, scan_labels: JSON.stringify(next) }))
            }
            return (
              <label key={label} className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-plex-orange" />
                <span className={`text-sm ${checked ? 'text-gray-100' : 'text-gray-500'}`}>{label}</span>
              </label>
            )
          })}
        </div>
      </section>

      {/* Library exclusions */}
      {libraries.length > 0 && (
        <section>
          <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Library Exclusions</h2>
          <p className="text-xs text-gray-500 mb-3">Excluded libraries will not be scanned or synced.</p>
          <div className="space-y-2">
            {libraries.map(lib => (
              <label key={lib.id} className="flex items-center gap-3 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={excludedIds.includes(lib.id)}
                  onChange={() => toggleExcluded(lib.id)}
                  className="w-4 h-4 accent-plex-orange"
                />
                <span className={`text-sm ${excludedIds.includes(lib.id) ? 'text-gray-500 line-through' : 'text-gray-200'}`}>
                  {lib.title}
                </span>
                <span className="text-xs text-gray-600">{lib.type}</span>
              </label>
            ))}
          </div>
        </section>
      )}

      {/* Segment Library Sync */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Segment Library Sync</h2>
        <p className="text-xs text-gray-500 mb-4">Crowdsource segment detections through one shared GitHub repository. Upload and download are always manual.</p>
        <div className="space-y-4">
          <Field label="Repository" hint="Fixed shared repository used by all users.">
            <input
              type="text"
              value={syncStatus?.github_repo || 'nazmolla/cleanplex-segments'}
              readOnly
              className={inputCls + ' opacity-80'}
            />
          </Field>
          <Field label="Instance Name" hint="Name attached to your submissions in the crowdsourced dataset.">
            <input
              type="text"
              value={syncForm.instance_name}
              onChange={e => setSyncForm(f => ({ ...f, instance_name: e.target.value }))}
              placeholder="e.g. home-server"
              className={inputCls}
            />
          </Field>
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={syncForm.sync_enabled}
              onChange={e => setSyncForm(f => ({ ...f, sync_enabled: e.target.checked }))}
              className="w-4 h-4 accent-plex-orange"
            />
            <span className="text-sm text-gray-300">Enable sync</span>
          </label>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={saveSync}
              disabled={savingSync}
              className="px-4 py-2 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
            >
              {savingSync && <Loader2 size={13} className="animate-spin" />}
              Save Sync Settings
            </button>
            {savedSync && (
              <span className="flex items-center gap-1.5 text-xs text-green-400">
                <CheckCircle2 size={13} /> Saved
              </span>
            )}
          </div>
          <div className="pt-2 border-t border-plex-border/50 space-y-3">
            <p className="text-xs text-gray-500">Upload and download crowdsourced segment detections from the shared library.</p>
            <div className="flex items-center gap-3 flex-wrap">
              <button
                type="button"
                onClick={doUpload}
                disabled={uploading || !syncForm.sync_enabled}
                className="px-4 py-2 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
              >
                {uploading && <Loader2 size={13} className="animate-spin" />}
                Upload My Segments
              </button>
              <button
                type="button"
                onClick={doDownload}
                disabled={downloading || !syncForm.sync_enabled}
                className="px-4 py-2 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
              >
                {downloading && <Loader2 size={13} className="animate-spin" />}
                Download Crowdsourced Segments
              </button>
              {uploadResult && (
                <span className={`flex items-center gap-1.5 text-xs ${uploadResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                  {uploadResult.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
                  {uploadResult.message}
                </span>
              )}
              {downloadResult && (
                <span className={`flex items-center gap-1.5 text-xs ${downloadResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                  {downloadResult.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
                  {downloadResult.message}
                </span>
              )}
            </div>
            {syncStatus?.last_sync_time && (
              <p className="text-xs text-gray-600">Last sync: {syncStatus.last_sync_time}</p>
            )}
          </div>
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
