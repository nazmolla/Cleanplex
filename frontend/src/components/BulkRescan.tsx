import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useT } from '../i18n'

type Scope = 'outdated' | 'with_segments' | 'all_scanned' | 'scanned_before' | 'failed'

interface Library {
  id: string
  title: string
}

interface Counts {
  matched: number
  queued: number
  skipped_ineligible: number
  skipped_missing_file?: number
  ignored_cleared?: number
}

const SCOPES: { id: Scope; labelKey: 'rescan_scope_outdated' | 'rescan_scope_with_segments' | 'rescan_scope_all_scanned' | 'rescan_scope_scanned_before' | 'rescan_scope_failed'; hintKey: 'rescan_scope_outdated_hint' | 'rescan_scope_with_segments_hint' | 'rescan_scope_all_scanned_hint' | 'rescan_scope_scanned_before_hint' | 'rescan_scope_failed_hint' }[] = [
  { id: 'outdated', labelKey: 'rescan_scope_outdated', hintKey: 'rescan_scope_outdated_hint' },
  { id: 'with_segments', labelKey: 'rescan_scope_with_segments', hintKey: 'rescan_scope_with_segments_hint' },
  { id: 'all_scanned', labelKey: 'rescan_scope_all_scanned', hintKey: 'rescan_scope_all_scanned_hint' },
  { id: 'scanned_before', labelKey: 'rescan_scope_scanned_before', hintKey: 'rescan_scope_scanned_before_hint' },
  { id: 'failed', labelKey: 'rescan_scope_failed', hintKey: 'rescan_scope_failed_hint' },
]

/**
 * Schedule many titles for rescan at once (#138). They go through the normal scan queue, so
 * the scan window, rating filters and ordering apply and nothing jumps ahead of a title you
 * asked to scan now. A live count shows exactly what will be queued before anything is.
 */
export default function BulkRescan({ libraries }: { libraries: Library[] }) {
  const { t } = useT()
  const [scope, setScope] = useState<Scope>('outdated')
  const [libraryId, setLibraryId] = useState('')
  const [before, setBefore] = useState('')
  const [counts, setCounts] = useState<Counts | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const seq = useRef(0)

  const body = (dryRun: boolean) => ({
    scope,
    library_id: libraryId || null,
    before: scope === 'scanned_before' ? before : null,
    dry_run: dryRun,
  })
  const ready = scope !== 'scanned_before' || /^\d{4}-\d{2}-\d{2}$/.test(before)

  // Recounts whenever the choice changes. A response that arrives after a newer request
  // started is dropped, so a slow count cannot overwrite the current one.
  useEffect(() => {
    setConfirming(false)
    setCounts(null)
    if (!ready) return
    const mine = ++seq.current
    api.post<Counts>('/api/scan/rescan', body(true))
      .then(c => { if (mine === seq.current) setCounts(c) })
      .catch(() => { if (mine === seq.current) setMessage(t('rescan_count_error')) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, libraryId, before])

  const queue = async () => {
    setBusy(true)
    setMessage('')
    try {
      const r = await api.post<Counts>('/api/scan/rescan', body(false))
      setMessage(t('rescan_queued_done', { titles: t('unit_title', { count: r.queued }) }))
      setCounts({ matched: 0, queued: 0, skipped_ineligible: 0 })
    } catch (err: any) {
      setMessage(t('rescan_queue_error', { error: err.message || t('common_unknown_error_lower') }))
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div>
      <fieldset className="space-y-2 mb-4">
        <legend className="sr-only">{t('rescan_legend')}</legend>
        {SCOPES.map(s => (
          <label key={s.id} className="flex items-start gap-3 cursor-pointer">
            <input type="radio" name="rescan-scope" checked={scope === s.id} onChange={() => setScope(s.id)}
              className="mt-1 accent-hayaa-gold" />
            <span>
              <span className="text-sm text-gray-300">{t(s.labelKey)}</span>
              <span className="block text-xs text-muted">{t(s.hintKey)}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-end gap-4 mb-4">
        {scope === 'scanned_before' && (
          <label className="text-xs text-gray-400">
            {t('rescan_before')}
            <input type="date" value={before} onChange={e => setBefore(e.target.value)}
              className="block mt-1 bg-hayaa-card border border-hayaa-border rounded px-2 py-1 text-sm text-gray-100" />
          </label>
        )}
        <label className="text-xs text-gray-400">
          {t('rescan_library')}
          <select value={libraryId} onChange={e => setLibraryId(e.target.value)} aria-label={t('rescan_library')}
            className="block mt-1 bg-hayaa-card border border-hayaa-border rounded px-2 py-1 text-sm text-gray-100">
            <option value="">{t('rescan_all_libraries')}</option>
            {libraries.map(l => <option key={l.id} value={l.id}>{l.title}</option>)}
          </select>
        </label>
      </div>
      <p className="text-sm text-gray-300 mb-3" aria-live="polite">
        {!ready ? t('rescan_pick_date') : counts === null ? t('rescan_counting') : (
          <>
            {t('rescan_will_queue', { titles: t('unit_title', { count: counts.queued }) })}
            {counts.skipped_ineligible > 0 && (
              <span className="text-muted"> {t('rescan_skipped_ineligible', { count: counts.skipped_ineligible })}</span>
            )}
            {!!counts.skipped_missing_file && (
              <span className="text-muted"> {t('rescan_skipped_missing', { count: counts.skipped_missing_file })}</span>
            )}
            {!!counts.ignored_cleared && (
              <span className="text-muted"> {t('rescan_ignored_cleared', { titles: t('unit_ignored_title', { count: counts.ignored_cleared }) })}</span>
            )}
          </>
        )}
      </p>
      {confirming ? (
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">{t('rescan_confirm_prompt', { titles: t('unit_title', { count: counts?.queued ?? 0 }) })}</span>
          <button onClick={queue} disabled={busy} className="btn-primary text-sm">{t('common_confirm')}</button>
          <button onClick={() => setConfirming(false)} disabled={busy} className="btn-outline text-sm">{t('common_cancel')}</button>
        </div>
      ) : (
        <button
          onClick={() => setConfirming(true)}
          disabled={!ready || !counts || counts.queued === 0}
          className="btn-primary text-sm"
        >
          {t('rescan_queue_btn')}
        </button>
      )}
      {message && <p className="text-xs text-gray-400 mt-3">{message}</p>}
    </div>
  )
}
