import { useState } from 'react'
import { ThumbsDown, Undo2 } from 'lucide-react'
import { api } from '../api/client'
import { toast } from './Toast'
import { useT } from '../i18n'

interface Props {
  seg: { id: number; false_positive?: boolean; auto_rejected?: boolean; community_rejected?: boolean }
  /** Called after the server accepted the change, so the list can update in place. */
  onChange: (id: number, falsePositive: boolean) => void
}

/**
 * Mark a segment as a false positive, or undo that (#101). A marked segment stops
 * acting during playback and is left out of counts, exports and sync, but stays in
 * the list, dimmed, so the mark can be undone. The verdict and the frame are kept
 * for tuning detection later.
 */
export default function FalsePositiveButton({ seg, onChange }: Props) {
  const { t } = useT()
  const [busy, setBusy] = useState(false)
  const marked = !!seg.false_positive

  const toggle = async () => {
    setBusy(true)
    try {
      await api.post(`/api/segments/${seg.id}/false-positive`, { false_positive: !marked })
      onChange(seg.id, !marked)
    } catch (err: any) {
      toast.error(t('fp_update_error', { error: err.message || t('common_unknown_error') }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      onClick={toggle}
      disabled={busy}
      title={
        seg.auto_rejected ? t('fp_title_auto_rejected')
          : seg.community_rejected ? t('fp_title_community')
          : marked ? t('fp_title_undo')
          // Distinct from a preference: with Hayaa this is a vote that nothing is there (#222).
          : t('fp_title_mark')
      }
      aria-label={seg.auto_rejected || seg.community_rejected ? t('fp_aria_restore') : marked ? t('fp_aria_undo') : t('fp_aria_mark')}
      aria-pressed={marked}
      className={`p-2 rounded-lg transition-colors disabled:opacity-40 ${
        marked
          ? 'text-amber-300 bg-amber-400/10 hover:bg-amber-400/20'
          : 'text-gray-400 hover:text-amber-300 hover:bg-amber-400/10'
      }`}
    >
      {marked ? <Undo2 size={16} /> : <ThumbsDown size={16} />}
    </button>
  )
}

/** Shown next to a marked segment's time range. */
export function FalsePositiveTag({ autoRejected = false }: { autoRejected?: boolean }) {
  const { t } = useT()
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded border whitespace-nowrap ${
        autoRejected
          ? 'bg-sky-400/10 border-sky-400/30 text-sky-300'
          : 'bg-amber-400/10 border-amber-400/30 text-amber-300'
      }`}
    >
      {autoRejected ? t('fp_tag_verifier') : t('fp_tag_fp')}
    </span>
  )
}
