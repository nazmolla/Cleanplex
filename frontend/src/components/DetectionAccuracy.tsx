import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import { segmentOrigin } from '../utils/segmentOrigin'
import { useT } from '../i18n'
import { prettyLabel } from '../utils/labels'

interface Row {
  source: string
  detector: string
  label: string
  total: number
  false_positives: number
  false_positive_rate: number
  avg_score_kept: number | null
  avg_score_false: number | null
}

const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`)

/**
 * How often each detector and label was marked as a false positive (#101). The
 * mean scores of kept vs. marked segments show where a threshold would separate
 * them; this is the sample threshold tuning, and later retraining, is based on.
 */
export default function DetectionAccuracy() {
  const { t } = useT()
  const [rows, setRows] = useState<Row[]>([])
  const [marked, setMarked] = useState(0)
  const [loading, setLoading] = useState(true)

  const ctl = useRef<AbortController | null>(null)
  const load = useCallback(() => {
    ctl.current?.abort()
    const controller = (ctl.current = new AbortController())
    api.get<{ rows: Row[]; marked: number }>('/api/analytics/detection-accuracy', { signal: controller.signal })
      .then(d => {
        setRows(d.rows)
        setMarked(d.marked)
        setLoading(false)
      })
      .catch(err => {
        if (err.name !== 'AbortError') setLoading(false)
      })
  }, [])

  useEffect(() => {
    load()
    return () => ctl.current?.abort()
  }, [load])
  // Marks and rescans both change these numbers.
  useLiveRefresh(['segments_changed'], load, { debounceMs: 2000 })

  if (loading) return <div className="text-muted text-sm">{t('acc_loading')}</div>
  if (rows.length === 0) return <div className="text-muted text-sm">{t('acc_empty')}</div>

  return (
    <div className="space-y-2">
      {marked === 0 && (
        <p className="text-xs text-muted">
          {t('acc_no_fps_hint')}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-start text-muted border-b border-plex-border">
              <th className="py-1.5 pe-3 font-medium">{t('acc_col_detector')}</th>
              <th className="py-1.5 pe-3 font-medium">{t('acc_col_label')}</th>
              <th className="py-1.5 pe-3 font-medium text-end">{t('acc_col_found')}</th>
              <th className="py-1.5 pe-3 font-medium text-end">{t('acc_col_false')}</th>
              <th className="py-1.5 pe-3 font-medium text-end">{t('acc_col_false_rate')}</th>
              <th className="py-1.5 pe-3 font-medium text-end">{t('acc_col_avg_kept')}</th>
              <th className="py-1.5 font-medium text-end">{t('acc_col_avg_false')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={`${r.source}|${r.detector}|${r.label}`} className="border-b border-plex-border/50 text-gray-300">
                <td className="py-1.5 pe-3 whitespace-nowrap">{segmentOrigin(r, t)}</td>
                <td className="py-1.5 pe-3 whitespace-nowrap" title={r.label}>{prettyLabel(r.label, t)}</td>
                <td className="py-1.5 pe-3 text-end">{r.total}</td>
                <td className="py-1.5 pe-3 text-end">{r.false_positives}</td>
                <td className={`py-1.5 pe-3 text-end ${r.false_positive_rate >= 0.3 ? 'text-amber-300' : ''}`}>
                  {pct(r.false_positive_rate)}
                </td>
                <td className="py-1.5 pe-3 text-end">{pct(r.avg_score_kept)}</td>
                <td className="py-1.5 text-end">{pct(r.avg_score_false)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
