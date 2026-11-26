import { useEffect, useState } from 'react'
import { Tag } from 'lucide-react'
import { api } from '../api/client'
import Modal from './Modal'
import { useT } from '../i18n'
import { prettyLabel } from '../utils/labels'

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

// The detector tiers, highest first, in step with LABEL_SEVERITY in importers/_common.py
// (#230). A label not listed is minimal; minimal labels keep this order, male chest last.
const TIERS = [
  { nameKey: 'relabel_tier_high' as const, noteKey: 'relabel_tier_high_note' as const, heading: 'text-red-300', bar: 'border-red-400/70',
    labels: ['FEMALE_GENITALIA_EXPOSED', 'MALE_GENITALIA_EXPOSED', 'FEMALE_BREAST_EXPOSED'] },
  { nameKey: 'relabel_tier_medium' as const, noteKey: 'relabel_tier_medium_note' as const, heading: 'text-amber-300', bar: 'border-amber-400/70',
    labels: ['BUTTOCKS_EXPOSED', 'ANUS_EXPOSED', 'BELLY_EXPOSED'] },
  { nameKey: 'relabel_tier_low' as const, noteKey: 'relabel_tier_low_note' as const, heading: 'text-sky-300', bar: 'border-sky-400/70',
    labels: ['FEMALE_GENITALIA_COVERED', 'FEMALE_BREAST_COVERED'] },
  { nameKey: 'relabel_tier_minimal' as const, noteKey: 'relabel_tier_minimal_note' as const, heading: 'text-gray-300', bar: 'border-plex-border',
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
  const { t } = useT()
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
      .catch(() => { if (!cancelled) setError(t('relabel_load_error')) })
    return () => { cancelled = true }
  }, [open, attempt, t])

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
      setError(err.message || t('relabel_save_error'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        onClick={show}
        title={t('relabel_btn_title')}
        aria-label={t('relabel_btn_aria')}
        className="p-2 rounded-lg text-gray-400 hover:text-sky-300 hover:bg-sky-400/10 transition-colors"
      >
        <Tag size={16} />
      </button>
      {open && (
        <Modal
          title={t('relabel_modal_title')}
          onClose={() => setOpen(false)}
          dismissible={!busy}
          footer={
            <>
              <button onClick={() => setOpen(false)} disabled={busy} className="btn-outline">{t('common_cancel')}</button>
              <button onClick={save} disabled={busy || picked.length === 0} className="btn-primary">{t('common_save')}</button>
            </>
          }
        >
          <p className="text-xs text-gray-400 mb-4">
            {t('relabel_desc')}
          </p>
          <div className="space-y-3">
            {grouped(all).map(tier => (
              <section key={tier.nameKey} className={`border-s-2 ${tier.bar} ps-3`}>
                <h3 className="text-sm mb-1.5">
                  <span className={`font-medium ${tier.heading}`}>{t(tier.nameKey)}</span>
                  <span className="text-xs text-muted ms-2">{t(tier.noteKey)}</span>
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
                      {prettyLabel(label, t)}
                    </label>
                  ))}
                </div>
              </section>
            ))}
          </div>
          {picked.length > 0 && all.length > 0 && (
            <p className="text-xs text-muted mt-3">
              {t('relabel_severity')} <span className={`font-medium ${TIERS[severityTier(picked)].heading}`}>{t(TIERS[severityTier(picked)].nameKey)}</span>
            </p>
          )}
          {all.length === 0 && !error && <p className="text-xs text-muted">{t('relabel_loading')}</p>}
          {error && (
            <p className="text-xs text-red-400 mt-3" role="alert">
              {error}{' '}
              {all.length === 0 && (
                <button onClick={() => { setError(''); setAttempt(n => n + 1) }} className="underline hover:text-red-300">{t('common_retry')}</button>
              )}
            </p>
          )}
        </Modal>
      )}
    </>
  )
}
