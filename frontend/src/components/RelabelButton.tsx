import { useEffect, useState } from 'react'
import { Tag } from 'lucide-react'
import { api } from '../api/client'

interface Props {
  seg: { id: number; labels?: string }
  /** Called with the saved labels once the server accepted them, so the list can update in place. */
  onChange: (id: number, labels: string) => void
}

// Fetched once per page load: the vocabulary does not change while the app runs.
let labelCache: string[] | null = null

const pretty = (label: string) => label.toLowerCase().replace(/_/g, ' ')

/**
 * Correct a segment's labels (#125). The detector sometimes names the wrong thing, and
 * a segment's severity, and so what each viewer's level skips, follows its labels. The
 * correction is stored as feedback next to the detector's original labels.
 */
export default function RelabelButton({ seg, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [all, setAll] = useState<string[]>(labelCache ?? [])
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || labelCache) return
    api.get<{ labels: string[] }>('/api/settings/detector-labels')
      .then(d => { labelCache = d.labels; setAll(d.labels) })
      .catch(() => setError('Could not load the label list.'))
  }, [open])

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
        className="p-2 rounded-lg text-gray-600 hover:text-sky-300 hover:bg-sky-400/10 transition-colors"
      >
        <Tag size={16} />
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Edit labels"
          onClick={() => !busy && setOpen(false)}
        >
          <div
            className="bg-plex-card border border-plex-border rounded-xl p-5 w-full max-w-lg max-h-[85vh] overflow-y-auto"
            onClick={e => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-gray-100 mb-1">Edit labels</h3>
            <p className="text-xs text-gray-500 mb-4">
              Pick what this segment actually shows. Viewers' levels decide what to skip from these labels:
              exposed genitalia and anus are the most severe, exposed breast and buttocks next, covered parts the least.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5">
              {all.map(label => (
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
            {error && <p className="text-xs text-red-400 mt-3">{error}</p>}
            <div className="flex justify-end gap-2 mt-5">
              <button onClick={() => setOpen(false)} disabled={busy} className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">
                Cancel
              </button>
              <button
                onClick={save}
                disabled={busy || picked.length === 0}
                className="px-3 py-1.5 text-sm rounded bg-plex-orange text-white disabled:opacity-40"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
