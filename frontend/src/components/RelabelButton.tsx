import { useEffect, useState } from 'react'
import { Tag } from 'lucide-react'
import { api } from '../api/client'
import Modal from './Modal'

interface Props {
  seg: { id: number; labels?: string }
  /** Called with the saved labels once the server accepted them, so the list can update in place. */
  onChange: (id: number, labels: string) => void
}

// The vocabulary does not change while the app runs, so it is fetched once per page load and
// shared by every card's button. A failed fetch is not remembered, so the next open retries.
let labelCache: string[] | null = null
let labelRequest: Promise<string[]> | null = null

function loadLabels(): Promise<string[]> {
  if (labelCache) return Promise.resolve(labelCache)
  if (!labelRequest) {
    labelRequest = api.get<{ labels: string[] }>('/api/settings/detector-labels')
      .then(d => { labelCache = d.labels; return d.labels })
      .finally(() => { labelRequest = null })
  }
  return labelRequest
}

const pretty = (label: string) => {
  const s = label.toLowerCase().replace(/_/g, ' ').replace(/^face (female|male)$/, '$1 face')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// The detector tiers, highest first, in step with LABEL_SEVERITY in importers/_common.py
// (#230). A label not listed is minimal; minimal labels keep this order, male chest last.
const TIERS = [
  { name: 'High', note: 'Exposed genitalia, female breast', heading: 'text-red-300', bar: 'border-red-400/70',
    labels: ['FEMALE_GENITALIA_EXPOSED', 'MALE_GENITALIA_EXPOSED', 'FEMALE_BREAST_EXPOSED'] },
  { name: 'Medium', note: 'Exposed buttocks, anus, belly', heading: 'text-amber-300', bar: 'border-amber-400/70',
    labels: ['BUTTOCKS_EXPOSED', 'ANUS_EXPOSED', 'BELLY_EXPOSED'] },
  { name: 'Low', note: 'Covered genitalia, female breast', heading: 'text-sky-300', bar: 'border-sky-400/70',
    labels: ['FEMALE_GENITALIA_COVERED', 'FEMALE_BREAST_COVERED'] },
  { name: 'Minimal', note: 'Everything else', heading: 'text-gray-300', bar: 'border-plex-border',
    labels: ['BUTTOCKS_COVERED', 'ANUS_COVERED', 'BELLY_COVERED', 'FEET_EXPOSED', 'FEET_COVERED',
             'ARMPITS_EXPOSED', 'ARMPITS_COVERED', 'FACE_FEMALE', 'FACE_MALE',
             'MALE_BREAST_EXPOSED', 'MALE_BREAST_COVERED'] },
]
const TIER_OF: Record<string, number> = Object.fromEntries(
  TIERS.flatMap((t, i) => t.labels.map(l => [l, i])))

/** Index into TIERS of the highest tier among the picked labels (the segment's severity). */
function severityTier(picked: string[]) {
  return Math.min(...picked.map(l => TIER_OF[l] ?? TIERS.length - 1))
}

/** The labels of the vocabulary grouped by tier; any label the table does not know is minimal. */
function grouped(all: string[]) {
  return TIERS.map((t, i) => ({
    ...t,
    labels: [...t.labels.filter(l => all.includes(l)),
             ...(i === TIERS.length - 1 ? all.filter(l => !(l in TIER_OF)) : [])],
  })).filter(t => t.labels.length > 0)
}

/**
 * Correct a segment's labels (#125). The detector sometimes names the wrong thing, and
 * a segment's severity, and so what each viewer's level skips, follows its labels. The
 * correction is stored as feedback next to the detector's original labels.
 */
export default function RelabelButton({ seg, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [all, setAll] = useState<string[]>([])
  const [attempt, setAttempt] = useState(0)
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Runs on every open, not once at mount: a button mounted before another card filled the
  // shared cache must still pick it up, or its dialog stays empty.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    loadLabels()
      .then(labels => { if (!cancelled) { setAll(labels); setError('') } })
      .catch(() => { if (!cancelled) setError('Could not load the label list.') })
    return () => { cancelled = true }
  }, [open, attempt])

  const show = () => {
    setPicked((seg.labels || '').split(',').map(l => l.trim()).filter(Boolean))
    setError('')
    setOpen(true)
  }

  const toggle = (label: string) =>
    setPicked(p => (p.includes(label) ? p.filter(l => l !== label) : [...p, label]))

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await api.put<{ labels: string }>(`/api/segments/${seg.id}/labels`, { labels: picked })
      onChange(seg.id, res.labels)
      setOpen(false)
    } catch (err: any) {
      setError(err.message || 'Could not save the labels.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        onClick={show}
        title="Wrong label? Correct what this segment shows"
        aria-label="Edit labels"
        className="p-2 rounded-lg text-gray-400 hover:text-sky-300 hover:bg-sky-400/10 transition-colors"
      >
        <Tag size={16} />
      </button>
      {open && (
        <Modal
          title="Edit labels"
          onClose={() => setOpen(false)}
          dismissible={!busy}
          footer={
            <>
              <button onClick={() => setOpen(false)} disabled={busy} className="btn-outline">Cancel</button>
              <button onClick={save} disabled={busy || picked.length === 0} className="btn-primary">Save</button>
            </>
          }
        >
          <p className="text-xs text-gray-400 mb-4">
            Tick what this segment shows. Its highest label sets its severity; each viewer's level decides what
            gets skipped.
          </p>
          <div className="space-y-3">
            {grouped(all).map(tier => (
              <section key={tier.name} className={`border-l-2 ${tier.bar} pl-3`}>
                <h3 className="text-sm mb-1.5">
                  <span className={`font-medium ${tier.heading}`}>{tier.name}</span>
                  <span className="text-xs text-muted ml-2">{tier.note}</span>
                </h3>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5">
                  {tier.labels.map(label => (
                    <label key={label} className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={picked.includes(label)}
                        onChange={() => toggle(label)}
                        className="w-4 h-4 accent-plex-orange"
                      />
                      {pretty(label)}
                    </label>
                  ))}
                </div>
              </section>
            ))}
          </div>
          {picked.length > 0 && all.length > 0 && (
            <p className="text-xs text-muted mt-3">
              Severity: <span className={`font-medium ${TIERS[severityTier(picked)].heading}`}>{TIERS[severityTier(picked)].name}</span>
            </p>
          )}
          {all.length === 0 && !error && <p className="text-xs text-muted">Loading labels…</p>}
          {error && (
            <p className="text-xs text-red-400 mt-3" role="alert">
              {error}{' '}
              {all.length === 0 && (
                <button onClick={() => { setError(''); setAttempt(n => n + 1) }} className="underline hover:text-red-300">Retry</button>
              )}
            </p>
          )}
        </Modal>
      )}
    </>
  )
}
