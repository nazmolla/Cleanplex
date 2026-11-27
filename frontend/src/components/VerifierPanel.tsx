import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { useT } from '../i18n'

interface GroupStatus {
  auc: number
  n: number
  false_positives: number
}

interface Status {
  running: boolean
  progress: number
  error: string
  trained_at: number | null
  groups: Record<string, GroupStatus>
}

const LEVELS = [
  { value: '0', labelKey: 'ver_level_0' as const },
  { value: '5', labelKey: 'ver_level_5' as const },
  { value: '10', labelKey: 'ver_level_10' as const },
  { value: '20', labelKey: 'ver_level_20' as const },
]

const POLL_MS = 3000
const MAX_POLLS = 600 // 30 minutes; training a few thousand frames takes minutes

/** Status of the false-positive filter, polled while it trains (stops when done or after MAX_POLLS). */
function useVerifierStatus() {
  const [status, setStatus] = useState<Status | null>(null)
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await api.get<Status>('/api/scan/verifier', { signal }))
    } catch {
      // a failed poll leaves the last status on screen
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  // While training runs, poll its progress; stops when it finishes or after MAX_POLLS.
  useEffect(() => {
    if (!status?.running) return
    let polls = 0
    const controller = new AbortController()
    const id = setInterval(() => {
      if (++polls > MAX_POLLS) return clearInterval(id)
      load(controller.signal)
    }, POLL_MS)
    return () => {
      clearInterval(id)
      controller.abort()
    }
  }, [status?.running, load])

  return { status, setStatus }
}

/**
 * Feedback-trained false-positive filter (#165): how strictly it drops new detections. A
 * setting, so it lives with detection; training it is an action, in VerifierTraining (#236).
 */
export default function VerifierPanel({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { t } = useT()
  const { status } = useVerifierStatus()
  const trained = !!status?.trained_at
  return (
    <div>
      <label htmlFor="verifier-level" className="block text-sm font-medium text-gray-300 mb-1">{t('ver_title')}</label>
      <select
        id="verifier-level"
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={!trained}
        className="w-full max-w-xs px-3 py-2 bg-hayaa-darker border border-hayaa-border rounded-lg text-sm text-gray-100 disabled:opacity-40"
      >
        {LEVELS.map(l => (
          <option key={l.value} value={l.value}>{t(l.labelKey)}</option>
        ))}
      </select>
      <p className="text-xs text-muted mt-1">{trained ? t('ver_desc') : t('ver_untrained_hint')}</p>
    </div>
  )
}

/** Train or retrain the false-positive filter and show how well each label's model separates. */
export function VerifierTraining() {
  const { t, formatDateTime } = useT()
  const { status, setStatus } = useVerifierStatus()
  const [error, setError] = useState('')

  const train = async () => {
    setError('')
    try {
      await api.post('/api/scan/verifier/train')
      setStatus(s => (s ? { ...s, running: true, progress: 0, error: '' } : s))
    } catch (e) {
      setError(e instanceof Error ? e.message : t('ver_train_error'))
    }
  }

  const trained = !!status?.trained_at
  const groups = Object.entries(status?.groups ?? {})
  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={train} disabled={status?.running} className="btn-outline text-sm">
          {status?.running
            ? t('ver_training_pct', { pct: Math.round((status.progress || 0) * 100) })
            : trained
              ? t('ver_retrain')
              : t('ver_train')}
        </button>
        {trained && (
          <span className="text-xs text-muted">{t('ver_trained_at', { time: formatDateTime((status!.trained_at as number) * 1000) })}</span>
        )}
      </div>
      {(error || status?.error) && <p className="text-xs text-red-400 mt-2">{error || status?.error}</p>}
      {groups.length > 0 && (
        <table className="mt-3 w-full max-w-xl text-sm">
          <thead>
            <tr className="text-xs text-muted border-b border-hayaa-border">
              <th className="text-start font-medium py-1.5 pe-3">{t('ver_col_label')}</th>
              <th className="text-end font-medium py-1.5 pe-3">{t('ver_col_false')}</th>
              <th className="text-end font-medium py-1.5 pe-3">{t('ver_col_total')}</th>
              <th className="text-end font-medium py-1.5">{t('ver_col_auc')}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map(([g, s]) => (
              <tr key={g} className="border-b border-hayaa-border/50 text-gray-300">
                <td className="py-1.5 pe-3" dir="ltr">{g}</td>
                <td className="py-1.5 pe-3 text-end tabular-nums">{s.false_positives}</td>
                <td className="py-1.5 pe-3 text-end tabular-nums">{s.n}</td>
                <td className="py-1.5 text-end tabular-nums">{s.auc.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
