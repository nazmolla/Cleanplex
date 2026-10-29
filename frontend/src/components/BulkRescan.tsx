import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

type Scope = 'outdated' | 'with_segments' | 'all_scanned' | 'scanned_before'

interface Library {
  id: string
  title: string
}

interface Counts {
  matched: number
  queued: number
  skipped_ineligible: number
}

const SCOPES: { id: Scope; label: string; hint: string }[] = [
  { id: 'outdated', label: 'Scanned by an older scanner', hint: 'Best after an update: only titles whose scan predates the current detector.' },
  { id: 'with_segments', label: 'Titles that have segments', hint: 'Re-checks what was found; titles with nothing found are left alone.' },
  { id: 'all_scanned', label: 'Every scanned title', hint: 'The whole library again.' },
  { id: 'scanned_before', label: 'Scanned before a date', hint: 'Titles whose last scan finished before this day (UTC).' },
]

/**
 * Schedule many titles for rescan at once (#138). They go through the normal scan queue, so
 * the scan window, rating filters and ordering apply and nothing jumps ahead of a title you
 * asked to scan now. A live count shows exactly what will be queued before anything is.
 */
export default function BulkRescan({ libraries }: { libraries: Library[] }) {
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
      .catch(() => { if (mine === seq.current) setMessage('Could not count the titles.') })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, libraryId, before])

  const queue = async () => {
    setBusy(true)
    setMessage('')
    try {
      const r = await api.post<Counts>('/api/scan/rescan', body(false))
      setMessage(`${r.queued} title${r.queued === 1 ? '' : 's'} queued for the scan window.`)
      setCounts({ matched: 0, queued: 0, skipped_ineligible: 0 })
    } catch (err: any) {
      setMessage(`Could not queue the rescan: ${err.message || 'unknown error'}`)
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div>
      <fieldset className="space-y-2 mb-4">
        <legend className="sr-only">Which titles</legend>
        {SCOPES.map(s => (
          <label key={s.id} className="flex items-start gap-3 cursor-pointer">
            <input type="radio" name="rescan-scope" checked={scope === s.id} onChange={() => setScope(s.id)}
              className="mt-1 accent-plex-orange" />
            <span>
              <span className="text-sm text-gray-300">{s.label}</span>
              <span className="block text-xs text-gray-600">{s.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-end gap-4 mb-4">
        {scope === 'scanned_before' && (
          <label className="text-xs text-gray-400">
            Before
            <input type="date" value={before} onChange={e => setBefore(e.target.value)}
              className="block mt-1 bg-plex-card border border-plex-border rounded px-2 py-1 text-sm text-gray-100" />
          </label>
        )}
        <label className="text-xs text-gray-400">
          Library
          <select value={libraryId} onChange={e => setLibraryId(e.target.value)} aria-label="Library"
            className="block mt-1 bg-plex-card border border-plex-border rounded px-2 py-1 text-sm text-gray-100">
            <option value="">All libraries</option>
            {libraries.map(l => <option key={l.id} value={l.id}>{l.title}</option>)}
          </select>
        </label>
      </div>
      <p className="text-sm text-gray-300 mb-3" aria-live="polite">
        {!ready ? 'Pick a date.' : counts === null ? 'Counting…' : (
          <>
            {counts.queued} title{counts.queued === 1 ? '' : 's'} will be queued
            {counts.skipped_ineligible > 0 && (
              <span className="text-gray-500"> · {counts.skipped_ineligible} skipped by your rating filters or excluded libraries</span>
            )}
          </>
        )}
      </p>
      {confirming ? (
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">Reset and queue {counts?.queued} title{counts?.queued === 1 ? '' : 's'}?</span>
          <button onClick={queue} disabled={busy} className="px-3 py-1.5 text-sm rounded bg-plex-orange text-white disabled:opacity-40">Confirm</button>
          <button onClick={() => setConfirming(false)} disabled={busy} className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">Cancel</button>
        </div>
      ) : (
        <button
          onClick={() => setConfirming(true)}
          disabled={!ready || !counts || counts.queued === 0}
          className="px-3 py-1.5 text-sm rounded bg-plex-orange text-white disabled:opacity-40"
        >
          Queue rescan
        </button>
      )}
      {message && <p className="text-xs text-gray-400 mt-3">{message}</p>}
    </div>
  )
}
