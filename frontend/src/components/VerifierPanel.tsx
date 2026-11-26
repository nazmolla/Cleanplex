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

/**
 * Feedback-trained false-positive filter (#165). Learns from the segments marked as false
 * positives and, when switched on, drops new detections whose frame looks like them.
 */
export default function VerifierPanel({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { t, formatDateTime } = useT()
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState('')

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
    <div className="lg:col-span-2 border border-gray-800 rounded-lg p-4">
      <p className="text-sm font-medium text-gray-300 mb-1">{t('ver_title')}</p>
      <p className="text-xs text-muted mb-3">
        {t('ver_desc')}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={value}
          onChange={e => onChange(e.target.value)}
          disabled={!trained}
          className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 disabled:opacity-40"
        >
          {LEVELS.map(l => (
            <option key={l.value} value={l.value}>{t(l.labelKey)}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={train}
          disabled={status?.running}
          className="px-3 py-2 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-200 disabled:opacity-40"
        >
          {status?.running
            ? t('ver_training_pct', { pct: Math.round((status.progress || 0) * 100) })
            : trained
              ? t('ver_retrain')
              : t('ver_train')}
        </button>
      </div>
      {(error || status?.error) && <p className="text-xs text-red-400 mt-2">{error || status?.error}</p>}
      {trained && (
        <p className="text-xs text-muted mt-2">
          {t('ver_trained_at', { time: formatDateTime((status!.trained_at as number) * 1000) })}{' '}
          {groups.map(([g, s]) => t('ver_group_stat', { group: g, fp: s.false_positives, n: s.n, auc: s.auc.toFixed(2) })).join(' · ')}
        </p>
      )}
    </div>
  )
}
